"""Req: F138 — v3 health 应用服务（opencode-agora v3 P3-T10，设计 §4/§5）。

健康探活与升级拍人，语义自 v2 core/health.py 平移（F5 边沿判僵/F70 自检
提示/F72 env-gone/F110 交付终局观测/F124 受控等待解析/F25 推改拉）。
分层（§1）：仓库/oc/gitx/Notifier/recover 钩全 Protocol 构造注入——不
import access/storage，零 SQL（架构守卫在 test_architecture.py）；oc 探测
与 gitx 出版对账（网络/子进程面）恒在 UoW 外（§4）。

tick 顺序：wait_scan（F124 解析先行）→ 快照 → 逐活动任务探 worker 会话
（死→recover 钩 reinject 决策〔可桩化，真实重建归 T13 e2e〕，失败升级
reinject-fail/dispatcher-dead；oc 不可达≠死——该轮整体免判不误杀）→
边沿判僵（20min 阈；busy 回合/waiting_on/in_gate/gate_resource.waiting
免判；离开候选集复位、再僵再拍；重复升级 ×4 退避封顶 24h；env 在册不在
盘改拍一次性 env-gone〔每丢失周期恰一次〕）→ delivery_watch（external
handoff 超龄 external-stale；managed delivered 未出版 unpublished——出版
投影经 gitx Protocol，退避同口径）。

escalate：正文服务权威合成；任务/worker 供给文本拍平进「不可信原文」段
（≤500 字符，防伪造服务行）；沿 dispatcher_sid 上行至根任务，拍根任务
派发方（主会话）；六类 quiet（stalled/env-gone/verify-dead/verify-infra/
external-stale/unpublished）走 notify 通道（DeliveryPolicy NOTIFY_EMAIL）
＋邮件缝（emailer 注入，缺省无）；事件审计行同 UoW 落账。nudge：僵态
拍人后 worker 自检提示注入恰一次（F70），失败不重试不阻断拍人。

驻留龄基线：v3 DeliveryState 无 v2 handoff_at/delivered_at 投影，以任务
行 updated_ms（awaiting_external/delivered 落账时点）为龄——语义等价
（v2 delivered_at 亦回退 t["updated"]）。
"""
import dataclasses
import pathlib
import time
import typing

from agorad.domain import Event, Task, epoch_ms

KIND_STALLED = "stalled"
KIND_ENV_GONE = "env-gone"
KIND_VERIFY_DEAD = "verify-dead"
KIND_VERIFY_INFRA = "verify-infra"
KIND_EXT_STALE = "external-stale"
KIND_UNPUBLISHED = "unpublished"
KIND_REINJECT = "reinject-fail"
KIND_DISPATCHER_DEAD = "dispatcher-dead"

QUIET_ESCALATES = frozenset({KIND_STALLED, KIND_ENV_GONE, KIND_VERIFY_DEAD,
                             KIND_VERIFY_INFRA, KIND_EXT_STALE,
                             KIND_UNPUBLISHED})

KIND_NOTE = {
    KIND_STALLED: "在飞超 20min 无活动（僵态候选），请人工关注",
    KIND_ENV_GONE: "任务环境已不在盘——项目仓仍在则重派可重建 env（F71），"
                   "项目仓已亡则 cancel 归档",
    KIND_VERIFY_DEAD: "verify 命令自身死（exit 126/127：命令不存在/不可执行）"
                      "——非活不达标，worker 无从自愈；请修正验收命令或取消",
    KIND_VERIFY_INFRA: "verify 机械失败（signal 类：SIGKILL/SIGSEGV 等，疑似"
                       " OOM/基础设施）——非质量败不计连败，完整产物在 "
                       "gate_artifact 投影指向目录；请核对基础设施后重派或取消",
    KIND_EXT_STALE: "external handoff 驻留 awaiting_external 超龄（缺省 30min）"
                    "——外部派发方未确认交付，可能派发链路断裂，请人工核对",
    KIND_UNPUBLISHED: "managed 交付已 merged 但 origin 未同步（本地支领先未推）"
                      "——请派发方尽快自推，防 origin 长期缺交付",
    KIND_REINJECT: "下级会话死亡且重注入失败",
    KIND_DISPATCHER_DEAD: "派发方会话死亡，其在飞子链失管",
}

SELF_CHECK_PROMPT = ("核对任务目标、当前动作与程序状态。若正常，简述当前进展后"
                     "继续；若异常或偏离目标，先安全停止当前动作，说明原因，"
                     "再选择安全的恢复方式；不要机械重启。")

_UNTRUSTED = "---- 不可信原文 ----"
_RAW_MAX = 500
STALE_AFTER_MS = 20 * 60 * 1000      # 判僵阈（doctor 同口径 20min）
GAP0_MS = 1_200_000                  # 重复升级退避首档（4×tick 节奏）
CAP_MS = 86_400_000                  # 退避封顶 24h
EXT_STALE_AFTER_MS = 30 * 60 * 1000  # awaiting_external 驻留超龄阈
UNPUBLISHED_AFTER_MS = 60 * 60 * 1000  # managed 未出版超龄阈

_ACTIVE = frozenset({"dispatched", "running", "done", "verify_failed",
                     "reopened", "blocked"})
_IN_FLIGHT = frozenset({"dispatched", "running"})  # 自愈重注入仅限在跑两态
_STALE_STATES = frozenset({"dispatched", "running", "done", "verify_failed",
                           "reopened"})  # blocked 已拍人不重拍（doctor 口径）
_WAIT_DONE = frozenset({"merged", "closed", "cancelled", "blocked"})


class TaskRepo(typing.Protocol):
    """storage 公开仓库接口（结构化；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def get_task(self, tid) -> Task | None: ...
    def tasks_all(self) -> list[Task]: ...
    def upsert_task(self, t: Task): ...
    def append_event(self, ev: Event): ...
    def events_for_task(self, tid) -> list[Event]: ...


class OcProbe(typing.Protocol):
    """oc 注入面：session→dict|None（None＝死）；inject＝prompt 注入。"""

    def session(self, sid): ...
    def inject(self, sid, text): ...


class PublicationGitx(typing.Protocol):
    """gitx 出版对账注入面（Req: F110；调用必在 UoW 外）。"""

    def publication(self, repo_dir, branch=None) -> dict: ...
    def published_on_origin(self, repo_dir, commit, branch) -> bool: ...
    def patch_equivalent_on_origin(self, repo_dir, commit, branch) -> bool: ...


class Notifier(typing.Protocol):
    """messaging.MessagingService 结构化满足（post＋record＋dispatch）。"""

    def post(self, from_sid, to_sid, task_id, kind, body, *,
             dedup_key=None, escalate_kind=None): ...
    def record(self, *, ts_ms, from_sid, to_sid, task_id, kind, body,
               dedup_key=None): ...
    def dispatch(self, mid, *, escalate_kind=None) -> bool: ...


class RecoverHook(typing.Protocol):
    """reinject 决策接口（可桩化）：True＝重建成功；None＝身份易主让位；
    False＝失败（升级 reinject-fail）。真实重建归 T13 e2e 装配。"""

    def reinject(self, tid, expect_wsid=None): ...


class TaskFace(typing.Protocol):
    """tasksvc.TaskService 结构化满足（last_seen 批量窗单源复用）。"""

    def touch_seen(self, tid, worker_sid=None, now_ms=None) -> bool: ...


class HealthService:
    """tick 由装配定时驱动；无锁无线程（单写者假设，tasksvc 同款）。
    边沿记忆为内存态（崩溃回退＝重拍一次）；退避账经事件面重建（miss 时
    按 escalate 审计行续命，重启不提前重拍——v2 _esc_book 同口径）。"""

    def __init__(self, store: TaskRepo, oc: OcProbe, notifier: Notifier, *,
                 clock=None, recover=None, tasks: TaskFace | None = None,
                 emailer=None, gitx=None, stale_after_ms=STALE_AFTER_MS,
                 ext_stale_after_ms=EXT_STALE_AFTER_MS,
                 unpublished_after_ms=UNPUBLISHED_AFTER_MS):
        self._store, self._oc, self._msg = store, oc, notifier
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._recover = recover
        self._tasks = tasks
        self._emailer = emailer  # 邮件缝：(tid, body) -> bool（缺省无）
        self._gitx = gitx
        self._stale_after_ms = stale_after_ms
        self._ext_stale_after_ms = ext_stale_after_ms
        self._unpublished_after_ms = unpublished_after_ms
        self._stale_seen = set()  # 僵态候选边沿记忆（离开候选集即复位）
        self._stale_esc = {}      # tid -> (连拍次数, 末拍时刻)
        self._env_gone_seen = set()  # env 丢失周期边沿记忆（F72）
        self._ext_esc = {}        # external-stale 退避
        self._unpub_esc = {}      # unpublished 退避

    # ---- tick 主循环 ----

    def tick(self):
        """wait_scan 先行（解析后恢复活性口径）→ 探活 → 判僵 → 交付观测。"""
        self._wait_scan()
        tasks = {t.id: t for t in self._store.tasks_all()}
        oc_down = False
        busy = set()  # updated>idle＝回合进行中：本轮免判（doctor 口径减 busy）
        for tid, t in tasks.items():
            if t.state not in _ACTIVE or not t.worker_sid:
                continue
            try:
                data = self._oc.session(t.worker_sid)
            except Exception:  # oc 不可达≠会话死：本轮免判（含僵态），勿误杀
                oc_down = True
                continue
            if data is None:
                manages = any(x.dispatcher_sid == t.worker_sid
                              and x.state in _IN_FLIGHT for x in tasks.values())
                if t.state in _IN_FLIGHT:
                    try:
                        r = (self._recover.reinject(
                                tid, expect_wsid=t.worker_sid)
                             if self._recover is not None else False)
                    except Exception:
                        r = False  # 钩异常＝失败口径（不废整轮 tick）
                    if r is not False:
                        continue  # True＝重建成功；None＝身份易主让位（免误报）
                    self.escalate(
                        KIND_DISPATCHER_DEAD if manages else KIND_REINJECT, tid)
                elif manages:
                    self.escalate(KIND_DISPATCHER_DEAD, tid)
            else:
                tm = data.get("time") or {}  # 缺形状→不 busy→照判（安全回退）
                if (tm.get("updated") or 0) > (tm.get("idle") or 0):
                    busy.add(tid)
        if not oc_down:
            self._stale_scan(tasks, busy)
        self._delivery_watch(tasks)  # 与 oc 活性无关

    # ---- F124 受控等待解析 ----

    def _wait_scan(self):
        """依赖终态即清 waiting_on＋审计＋双通知；等待任务自身终态跳过
        （字段留作历史）。清除带 CAS：并发换登记不被旧清除抹掉。"""
        for t in self._store.tasks_all():
            dep = t.waiting_on
            if not dep or t.state in _WAIT_DONE:
                continue
            d = self._store.get_task(dep)
            if d is None or d.state not in _WAIT_DONE:
                continue  # 依赖不在册（账面不可能）／未终态：免判
            self._resolve_wait(t.id, dep, d.state)

    def _resolve_wait(self, tid, dep, dep_state) -> bool:
        """CAS 解析单点：read-decide-write 同事务（waiting_on==dep 才清）。
        与 tasksvc.clear_wait_if 双源有意分立：本面在清除同 UoW 内产审计＋
        双消息（clear_wait_if 无消息面）——CAS 谓词语义两处保持一致，漂移
        由 test_v3_health_auth CAS 锚互锁。"""
        now, msgs = self._now(), []
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh is None or fresh.waiting_on != dep:
                return False  # 并发换登记：新等待不抹（下轮按新依赖重判）
            task = dataclasses.replace(fresh, waiting_on=None, updated_ms=now)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, "health", "wait_resolved", tid,
                {"dependency": dep, "note": f"dependency_state={dep_state}"}))
            if self._msg is not None:
                verdict = ("成功——可继续执行"
                           if dep_state in ("merged", "closed")
                           else "未成功——需处置（按裁定继续或取消）")
                text = (f"受控等待解除：任务 {tid} 等待的依赖 {dep}"
                        f" 已结束（{dep_state}，{verdict}）")
                if task.worker_sid:
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="health", to_sid=task.worker_sid,
                        task_id=tid, kind="process", body=text)[0])
                if task.dispatcher_sid:
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="health",
                        to_sid=task.dispatcher_sid, task_id=tid,
                        kind="result", body=text)[0])
        for m in msgs:
            self._msg.dispatch(m.id)
        return True

    # ---- 边沿判僵（F5/F72）＋退避（×4 封顶 24h） ----

    def _stale_scan(self, tasks, busy):
        """免判口径：busy 回合/waiting_on（running）/in_gate/
        gate_resource.waiting（F116 排队视同正常态）/gate_busy 期 done
        （ag-b49329：他任务持门＝串行排队，20min 必假拍）。边沿＝在候选集
        而不在记忆集（拍到才记）；退避窗内不拍不记（下轮到期重试，不丢报）。
        env 在册不在盘改拍一次性 env-gone：持续丢失不拍 stalled 不注自检，
        恢复在盘回常规 stalled 边沿。"""
        now = self._now()
        gate_busy = any(x.gate.in_gate
                        or (x.gate.gate_resource or {}).get("waiting")
                        for x in tasks.values())
        cand = set()
        for tid, t in tasks.items():
            if tid in busy or t.state not in _STALE_STATES or t.gate.in_gate:
                continue
            if t.gate.gate_resource and t.gate.gate_resource.get("waiting"):
                continue
            if t.waiting_on and t.state == "running":
                continue  # 受控等待免判（F124 单一过滤点）
            if gate_busy and t.state == "done":
                continue  # 门忙排队＝正常态（v2 stale_candidates 同口径）
            base = t.last_seen_ms or t.created_ms or 0  # v2 口径：last_seen 缺席回退 created
            if now - base > self._stale_after_ms:
                cand.add(tid)
        for tid in cand:
            t = tasks[tid]
            env = t.env_dir
            if env and not pathlib.Path(env).is_dir():
                if tid not in self._env_gone_seen:  # 每丢失周期只拍一次
                    self.escalate(KIND_ENV_GONE, tid)
                    self._env_gone_seen.add(tid)
                continue
            if tid not in self._stale_seen:  # 边沿触发
                n, last = self._esc_book(self._stale_esc, tid, KIND_STALLED)
                if now - last < self._gap(n):
                    continue  # 退避窗内：抑制本轮拍人，不记边沿下轮重试
                self.escalate(KIND_STALLED, tid, streak=n + 1)
                self._stale_esc[tid] = (n + 1, now)
                self._stale_seen.add(tid)  # 拍到才记：失败下轮重试不丢报
                if t.worker_sid:
                    self.nudge_worker(tid)
        self._stale_seen &= cand  # 离开候选集即移除（复位再拍）
        # 终态出账：防长驻无界增长（在飞退避记忆保留）
        self._stale_esc = {tid: v for tid, v in self._stale_esc.items()
                           if tasks.get(tid) is not None
                           and tasks[tid].state in _ACTIVE}
        self._env_gone_seen = {tid for tid in self._env_gone_seen
                               if tasks.get(tid) is not None
                               and tasks[tid].state in _ACTIVE
                               and tasks[tid].env_dir
                               and not pathlib.Path(tasks[tid].env_dir).is_dir()}

    # ---- F110 交付终局观测 ----

    def _delivery_watch(self, tasks):
        """external-stale＝done＋awaiting_external 驻留超龄；unpublished＝
        managed delivered 未出版（sha 与补丁均不在 origin 才拍——祖先对账
        ＋cherry 对账双口径防误报）。确认/出版/除籍即出账，再犯再拍；
        对账不可得（异常）保账不清（unknown≠不适用，R1 B8）——下轮重试。
        驻留龄基线＝任务行 updated_ms：门体 handoff/delivered 落账后驻留期
        无他写（T9b 约定），故等价 v2 handoff_at/delivered_at。"""
        now = self._now()
        for tid, t in tasks.items():
            d = t.delivery
            if (t.state == "done" and d is not None
                    and d.status == "awaiting_external"
                    and now - (t.updated_ms or 0)
                    > self._ext_stale_after_ms):
                n, last = self._esc_book(self._ext_esc, tid, KIND_EXT_STALE)
                if now - last >= self._gap(n):
                    self.escalate(KIND_EXT_STALE, tid, streak=n + 1,
                                  raw=f"commit={d.commit} "
                                      f"age_ms={now - (t.updated_ms or 0)}")
                    self._ext_esc[tid] = (n + 1, now)
            else:
                self._ext_esc.pop(tid, None)
            if (t.state in ("merged", "closed") and d is not None
                    and d.mode == "managed" and d.status == "delivered"
                    and now - (t.updated_ms or 0)
                    > self._unpublished_after_ms):
                try:
                    if self._gitx is None:
                        pub, unpublished = None, False
                    else:
                        pub = self._gitx.publication(t.project_dir,
                                                     t.target_branch)
                        # 分支 ahead 可能来自无关提交；交付 commit 也可能被
                        # rebase 改 sha 后出版——sha 与补丁均不在 origin 才拍
                        unpublished = bool(
                            pub and pub.get("origin_ref") and d.commit
                            and not self._gitx.published_on_origin(
                                t.project_dir, d.commit, pub["branch"])
                            and not self._gitx.patch_equivalent_on_origin(
                                t.project_dir, d.commit, pub["branch"]))
                except Exception:
                    continue  # 对账不可得：保账不清，本轮免判（下轮重试）
                if unpublished:
                    n, last = self._esc_book(self._unpub_esc, tid,
                                             KIND_UNPUBLISHED)
                    if now - last >= self._gap(n):
                        self.escalate(
                            KIND_UNPUBLISHED, tid, streak=n + 1,
                            raw=f"branch={pub.get('branch')} "
                                f"ahead={pub.get('ahead')} commit={d.commit}")
                        self._unpub_esc[tid] = (n + 1, now)
                else:
                    self._unpub_esc.pop(tid, None)  # 已出版/无 origin：出账
            else:
                self._unpub_esc.pop(tid, None)
        for book in (self._ext_esc, self._unpub_esc):
            for tid in list(book):
                if tid not in tasks:  # 除籍出账
                    del book[tid]

    # ---- 升级拍人 / 自检注入 / 活动心跳 ----

    def escalate(self, kind, tid, raw=None, streak=None):
        """失控拍人：正文服务权威合成；raw 为任务/worker 供给文本（不可信，
        拍平防伪造服务行）。quiet 六类走 notify 通道＋邮件缝；沿
        dispatcher_sid 上行至根任务拍根派发方（主会话，R25-A）。"""
        t = self._store.get_task(tid)
        if t is None:
            return
        body = (f"失控[{kind}] 任务{tid}：{KIND_NOTE[kind]}；"
                f"状态 {t.state}，worker={t.worker_sid or '-'}，"
                f"dispatcher={t.dispatcher_sid or '-'}，"
                f"连败 {t.fail_streak}")
        if streak:
            body += f"，第 {streak} 次升级"
        if raw:
            flat = " ".join(str(raw).split())
            body += f"\n{_UNTRUSTED}\n{flat[:_RAW_MAX]}"
        root = self._root(tid)
        to = self._store.get_task(root)
        to_sid = to.dispatcher_sid if to is not None else None
        with self._store.uow():
            self._store.append_event(self._ev(
                self._now(), "health", "escalate", tid, {"kind": kind}))
        # 通知面逐面尽力而为（F54 口径）：单任务通知败不废整轮 tick、
        # 已落审计不回滚（评审环 R1 B2）
        if to_sid:
            try:
                self._msg.post("health", to_sid, tid, "escalate", body,
                               escalate_kind=kind)
            except Exception:
                pass
        if kind in QUIET_ESCALATES and self._emailer is not None:
            try:
                self._emailer(tid, body)
            except Exception:
                pass

    def nudge_worker(self, tid, text=None):
        """F70/F73 单一注入面：缺省＝自检提示；不强制打断；无 worker 或
        注入异常返回 False（同轮不重试、不阻断拍人——调用方各一次）。"""
        t = self._store.get_task(tid)
        wsid = t.worker_sid if t is not None else None
        if not wsid:
            return False
        body = (f"[agora] 僵态自检（任务 {tid}）：{SELF_CHECK_PROMPT}"
                if not text else
                f"[agora] dispatcher prompt（任务 {tid}）\n{text}")
        try:
            return bool(self._oc.inject(wsid, body))
        except Exception:
            return False

    def touch(self, sid):
        """last_seen 刷新入口：仅 worker 服务调用刷（dispatcher 查看不续命）；
        批量窗节流归 tasksvc.touch_seen 单源（§3 ≤5s，崩溃最多丢一窗）。"""
        if self._tasks is None:
            return
        for t in self._store.tasks_all():
            if sid == t.worker_sid:
                self._tasks.touch_seen(t.id, worker_sid=sid)

    def _root(self, tid):
        """沿 dispatcher_sid 上行至根任务（父＝worker_sid 命中本任务
        dispatcher_sid 的任务）；链头不在册就地升级到最上级。"""
        tasks = {t.id: t for t in self._store.tasks_all()}
        seen = set()
        while tid not in seen:
            seen.add(tid)
            d = tasks[tid].dispatcher_sid
            parent = next((x for x in tasks.values()
                           if x.worker_sid == d), None)
            if parent is None:
                break
            tid = parent.id
        return tid

    def _gap(self, count):
        """已连拍 count 次后的再拍退避窗（ms）：首档 1200s 逐档×4 封顶 24h。
        指数钳 15：count 数百时 4**count 先于 min 爆 float 上限（v2 B2）。"""
        return min(GAP0_MS * 4 ** min(max(count - 1, 0), 15), CAP_MS)

    def _esc_book(self, book, tid, kind):
        """退避账本查取（miss 时经事件面重建，v2 _esc_book 同口径——重启
        退避续命不提前重拍）：n=该 kind 历史升级次数、last=末次升级 ts_ms；
        无事件记 (0, 0)。仅在将要拍人的分支调用——出账路径不重建，防逐
        tick 扫事件面；事件面读故障按首拍处理，不阻塞拍人。"""
        ent = book.get(tid)
        if ent is None:
            try:
                evs = [e for e in self._store.events_for_task(tid)
                       if e.event == "escalate"
                       and (e.detail or {}).get("kind") == kind]
            except Exception:
                evs = []
            ent = (len(evs), evs[-1].ts_ms if evs else 0)
            book[tid] = ent
        return ent

    def _now(self):
        return self._clock()

    def _ev(self, ts_ms, actor, event, task=None, detail=None):
        return Event(seq=0, ts_ms=ts_ms, actor=actor, event=event,
                     task=task, detail=detail)
