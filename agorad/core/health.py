"""健康探活与升级拍人（F5，设计 §4.4）：定时探死＋边沿判僵、失控实时拍主会话。

tick 只探活动任务的直接下级会话（无全局巡检）；死下级→recover.reinject 自愈
（静默），失败→升级。父 worker 若正管着在飞链也优先重注入，并同步改绑嵌套
子任务；只有无法重注入的失管 dispatcher 才直接拍人。
worker 凭据自愈已推改拉（F25 修正）：inject_silent 推送通道证伪（v2.2.0
synthetic 不触发 prompt 钩子），改由插件 hook 缓存未命中经 /cred_for_sid 拉取。
边沿判僵（F5 决策记录 2026-10-28；F70 自检提示，不处置）：tick 顺带按 doctor
同口径（20min 无活动，report.stale_candidates 构造注入）算僵态候选，新进候选集→
stalled 升级＋worker prompt 自检一次（不强制打断，失败不重试）＋可选邮件
（email.json opt-in）；离开候选集复位，oc 不可达轮次
免判；同任务重复升级（复位后再僵再拍）间隔×4 指数退避、封顶 24h，正文带
连续升级计数（ag-0f537d 降噪，ag-4628f9 宿主排队忙/闲抖动误报实证）；
"僵"的处置仍归人工。env 已物理消失的候选改拍一次性 env-gone（F72，
ag-724152 /tmp 清理事故）：每丢失周期一次、持续丢失不拍 stalled 不注自检，
恢复在盘回常规 stalled 边沿，终态出账。升级正文由服务权威合成；任务/worker 供给文本以
"不可信原文"段携带（拍平换行，防伪造服务行）。escalate 沿 dispatcher_sid
上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）经 router post——
落库先行，注入即视为拍到（stalled 例外：synthetic 免通知通道，不弹窗）；
escalate 同时落 events.jsonl 审计行（F8）。
core 只 import stdlib，依赖全注入。
"""
import json
import shlex
import subprocess
import time
from pathlib import Path

# 升级事件类别（R9 初始集；gate 的质量连败/闸体变更引用此收口）
KIND_QUALITY = "quality"
KIND_GATEFILES = "gatefiles"
KIND_REINJECT = "reinject-fail"
KIND_DISPATCHER_DEAD = "dispatcher-dead"
KIND_STALLED = "stalled"
KIND_ENV_GONE = "env-gone"

_UNTRUSTED = "---- 不可信原文 ----"
_RAW_MAX = 500
# 重复升级退避（ag-0f537d）：首档＝4×tick 节奏（默认 AGORA_HEALTH_INTERVAL 300s），
# 逐档再×4，封顶 24h
_STALE_GAP0 = 1200.0
_STALE_CAP = 86400.0
# 活动面＝探活与 last_seen 的任务集（verify_failed/reopened 归在飞，同 task 口径）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}
_IN_FLIGHT = {"dispatched", "running"}  # 自愈重注入仅限 worker 在跑的两态

_KIND_NOTE = {
    KIND_QUALITY: "同一任务连续 3 次质量失败，已自动 blocked",
    KIND_GATEFILES: "任务 diff 触及闸体（构建/测试配置）",
    KIND_REINJECT: "下级会话死亡且重注入失败",
    KIND_DISPATCHER_DEAD: "派发方会话死亡，其在飞子链失管",
    KIND_STALLED: "在飞超 20min 无活动（僵态候选），请人工关注",
    KIND_ENV_GONE: "任务环境已不在盘——项目仓仍在则重派可重建 env（F71），"
                   "项目仓已亡则 cancel 归档",
}

# F70/F73 共用提示：单源防 board 手动面与 health 自动面漂移
SELF_CHECK_PROMPT = ("核对任务目标、当前动作与程序状态。若正常，简述当前进展后"
                     "继续；若异常或偏离目标，先安全停止当前动作，说明原因，"
                     "再选择安全的恢复方式；不要机械重启。")


def _subprocess_runner(cmd, input_text):
    """默认邮件外呼：正文走 stdin，30s 超时；非零退出/超时抛异常由调用方吞。"""
    r = subprocess.run(cmd, input=input_text, text=True, timeout=30,
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r


def send_email(store, tid, body_text, runner=None):
    """可选邮件外呼公共函数（F63 自 F5 决策记录抽出，msg/health 单实现）：opt-in。

    email.json 缺失/缺字段→静默跳过；失败/超时吞异常（邮件尽力而为），
    成败各落一行审计事件（actor=health：邮件基建归属本模块）。runner＝外呼缝
    （Health 与 main 装配的 Router 终态邮件共用），缺省真 subprocess。
    返回 bool：True＝发送成功；False＝未配置或发送失败（Router 据此不占去重窗）。
    """
    try:
        cfg = json.loads((store.home / ".agora" / "email.json")
                         .read_text(encoding="utf-8"))
        to, command = cfg["to"], cfg["command"]
    except Exception:
        return False  # 未配置＝零行为（F5 opt-in），返回值仍如实报未发
    try:
        (runner or _subprocess_runner)(shlex.split(command), body_text)
        event = "email_sent"
    except Exception:
        event = "email_failed"
    # shortcut: 同步外呼 ≤30s/封，终态与僵态皆稀有事件——health tick 批量同僵最坏
    # N×30s 阻塞定时线程、msg 终态邮件阻塞 httpd/gate 单写线程同量级（可接受
    # 上限），规模化再改异步聚合单邮件
    store.append_event({"ts": time.time(), "actor": "health",
                        "event": event, "task": tid, "to": to})
    return event == "email_sent"  # False＝确认败（含未配置）：调用方可据此重试


class Health:
    """tick 由 main 定时线程驱动；消息写面三线程并发（httpd/gate/health），
    消息号配给经 Store._mid_lock 串行（Req: F75 评审残留 D3）。"""

    def __init__(self, state, oc, router, recover, app, store,
                 runner=None, stale_candidates=None, env_activity=None,
                 draft_writer=None):
        """app 按引用注入：main_sid 是活绑定（R25-A），escalate 每次取当前值。

        stale_candidates＝report.stale_candidates（core 禁兄弟 import，构造注入）；
        env_activity＝report._env_activity（F59 同款构造注入）；draft_writer＝
        pages.write_stalled_draft（F59 草稿页写面）；runner＝邮件外呼缝（gate
        同款），缺省真 subprocess。"""
        self._state, self._oc, self._router = state, oc, router
        self._recover, self._app = recover, app
        self._store = store
        self._runner = runner or _subprocess_runner
        self._stale_candidates = stale_candidates
        self._env_activity = env_activity
        self._draft_writer = draft_writer
        self._stale_seen = set()  # 僵态候选边沿记忆（内存态，崩溃回退＝重拍一次）
        self._stale_esc = {}  # tid->(连拍次数 n, 末拍时刻)：重复升级退避（内存态）
        self._env_gone_seen = set()  # env 丢失周期边沿记忆（F72，内存态同上）

    def tick(self):
        """F75 错址修复扫描先行（启动＋每轮双触发），再探每个活动任务的直接
        下跳：死→自愈/上报；边沿判僵拍人＋F70 worker 自检提示，不处置。"""
        self.repair_dispatcher_routes()
        tasks = self._state.snapshot()["tasks"]
        oc_down = False
        busy = set()  # updated>idle＝回合进行中：僵态本轮免判（doctor 口径减 busy 免决）
        for tid, t in tasks.items():
            wsid = t.get("worker_sid")
            if t["state"] not in _ACTIVE or not wsid:
                continue
            try:
                data = self._oc.session(wsid)
            except Exception:  # oc 不可达≠会话死：本轮免判（含僵态），勿误杀
                oc_down = True
                continue
            if data is None:
                manages = any(x.get("dispatcher_sid") == wsid
                              and x["state"] in _IN_FLIGHT for x in tasks.values())
                if t["state"] in _IN_FLIGHT:
                    r = self._recover.reinject(tid, expect_wsid=wsid)
                    if r is not False:
                        continue  # True＝重建成功；None＝身份已易主让位（他车道接管，免误报 escalate，round-3）
                    self.escalate(KIND_DISPATCHER_DEAD if manages else KIND_REINJECT, tid)
                elif manages:
                    self.escalate(KIND_DISPATCHER_DEAD, tid)
            else:
                tm = data.get("time") or {}  # 缺形状→不 busy→照判（安全回退）
                if (tm.get("updated") or 0) > (tm.get("idle") or 0):
                    busy.add(tid)
        if not oc_down:
            self._stale_scan(tasks, busy)

    def _stale_scan(self, tasks, busy):
        """边沿判僵（F5 决策记录 2026-10-28）＋重复升级退避（ag-0f537d）。

        免判口径（二批）：busy（updated>idle，回合进行中）本轮从候选集剔除；
        离开 busy 再超阈重新边沿触发。_stale_seen 每轮同步为当前候选集
        （离开即移除，之后再僵再拍）；重复升级间隔×4 指数退避封顶 24h——
        首拍不变，宿主排队忙/闲抖动反复再僵时降噪（ag-4628f9 实证）；
        退避窗内边沿不拍不记（下轮到期重试，拍到才记不丢报）。
        拍人后向 worker 注入一次 prompt 自检提示（F70）：模型自行纠偏，不强制
        打断/取消/重派；注入失败同轮不重试、不阻断拍人。
        env 在册不在盘的候选改拍一次性 env-gone（F72，ag-724152）：每丢失周期
        只拍一次；持续丢失不拍 stalled、不注入自检（env 已失无从自纠）；
        恢复在盘回常规 stalled 边沿；终态出账。
        """
        if self._stale_candidates is None:
            return
        cand = [(tid, b) for tid, b
                in self._stale_candidates({"tasks": tasks}, time.time())
                if tid not in busy]
        now = time.time()
        for tid, _ in cand:
            env_dir = tasks[tid].get("env_dir")
            if env_dir and not Path(env_dir).is_dir():  # F72 在盘判定（勿用 env_activity：0.0 分不清缺失与不活跃）
                if tid not in self._env_gone_seen:  # 每个丢失周期只拍一次
                    self.escalate(KIND_ENV_GONE, tid)
                    self._env_gone_seen.add(tid)  # 拍到才记（同 _stale_seen 语义）
                continue
            if tid not in self._stale_seen:  # 边沿：在候选集而不在记忆集
                n, last = self._stale_esc.get(tid, (0, 0.0))
                if now - last < self._stale_gap(n):
                    continue  # 退避窗内：抑制本轮拍人，不记边沿下轮重试
                self.escalate(KIND_STALLED, tid, streak=n + 1)
                self._stale_esc[tid] = (n + 1, time.time())
                self._stale_seen.add(tid)  # 拍到才记：escalate 失败下轮重试不丢报
                wsid = tasks[tid].get("worker_sid")
                if wsid:  # F70 自检提示：模型自行纠偏；注入败不重试不阻断拍人
                    self.nudge_worker(tid)
        self._stale_seen &= {tid for tid, _ in cand}  # 离开候选集即移除（复位再拍）
        # 终态任务出账：防长驻 daemon 无界增长（终闸 B1；在飞任务退避记忆保留）
        self._stale_esc = {tid: v for tid, v in self._stale_esc.items()
                           if tasks.get(tid, {}).get("state") in _ACTIVE}
        # F72 出账：env 回到在盘或任务终态/除籍即结束丢失周期（下周期可再拍）；
        # 经任务全集判在失——busy 轮离开候选集不误出账（防忙闲抖动重拍）
        self._env_gone_seen = {tid for tid in self._env_gone_seen
                               if tasks.get(tid, {}).get("state") in _ACTIVE
                               and tasks[tid].get("env_dir")
                               and not Path(tasks[tid]["env_dir"]).is_dir()}

    def repair_dispatcher_routes(self):
        """F75 存量错址修复：active 顶层任务 dispatcher_sid=='root' 且 root_sid
        为真实会话（CLI 无 sid 派发覆写所致）→ 归属行回写 root_sid＋
        dispatcher_route_repaired 审计（含 from/to 与成因）＋积压补交
        （router.replay_root_backlog）。幂等：修后行不再匹配、补交按 replay_of
        锚去重；嵌套（链边归父 worker）、终态、root_sid 缺席或 =='root'（真
        headless）一律不动——不按 main_sid/最近活跃会话猜归属，防多窗口抢绑。
        返回修复数。"""
        n = 0
        for tid, t in self._state.snapshot()["tasks"].items():
            rs = t.get("root_sid")
            if (t["state"] in _ACTIVE and t.get("chain_depth", 1) == 1
                    and t.get("dispatcher_sid") == "root" and rs and rs != "root"):
                try:
                    # 先补交后回写（评审 B 册 D1）：replay 中途写败→行未修→下轮
                    # 仍匹配重试（replay_of 锚防重复副本）；先修后补会孤儿剩余积压
                    self._router.replay_root_backlog(tid, rs)
                    self._state.set_fields(tid, dispatcher_sid=rs)
                    self._store.append_event(
                        {"ts": time.time(), "actor": "health",
                         "event": "dispatcher_route_repaired", "task": tid,
                         "note": f"from=root to={rs} "
                                 "cause=cli_dispatch_without_sid"})
                    # 回写记账后再扫一次（评审环：首扫含阻塞 inject，窗口可达
                    # 秒级，窗内落库的 to=root 上行此轮即补——锚幂等不产重复副本）
                    self._router.replay_root_backlog(tid, rs)
                except Exception:  # 单任务修复面败不废整轮 tick：下轮重试，探活照跑
                    continue
                n += 1
        return n

    def nudge_worker(self, tid, text=None):
        """F70/F73 单一注入面：缺省＝自检，text＝board 派发方自定义提示。

        不强制打断（prompt 由 opencode 排队/转向）；无 worker 或注入异常
        返回 False，是否审计/报因归调用方。"""
        wsid = self._state.get(tid).get("worker_sid")
        if not wsid:
            return False
        body = (f"[agora] 僵态自检（任务 {tid}）：{SELF_CHECK_PROMPT}"
                if not text else
                f"[agora] dispatcher prompt（任务 {tid}）\n{text}")
        try:
            return bool(self._oc.inject(wsid, body))
        except Exception:
            return False

    def _stale_gap(self, count):
        """已连拍 count 次后的再拍退避窗（秒）：首档 1200s 逐档×4，封顶 24h。"""
        # 指数钳 15：count 数百时 4**count 先于 min 爆 float 上限（OverflowError
        # 每轮重抛会静默杀掉整条升级链，终闸 B2）
        return min(_STALE_GAP0 * 4 ** min(max(count - 1, 0), 15), _STALE_CAP)

    def touch(self, sid):
        """last_seen 刷新入口：仅 worker 服务调用刷——dispatcher 查看≠worker
        干活，查看不续命（二批决策；018676 真僵晚拍 40min 实证）。"""
        for tid, t in self._state.snapshot()["tasks"].items():
            if sid == t.get("worker_sid"):
                self._state.touch_seen(tid)  # 心跳只进内存：快照仅随真实迁移追加

    def _last_activity(self, sid):
        """末次活动摘要（F52）：role＋tool 名/标题＋距今秒；失败/缺失→None
        （尽力而为，同 email 口径：获取吞掉照常拍人）。"""
        try:
            m = self._oc.last_message(sid)
            if not m:
                return None
            role = m.get("role") or m.get("type") or "?"  # 2.2.2 落 type，旧版 role 兼容
            tool = ""
            for p in m.get("content") or []:
                if isinstance(p, dict) and p.get("type") == "tool" and p.get("name"):
                    title = (p.get("state") or {}).get("title")
                    tool = f"{p['name']}/{title}" if title else p["name"]
                    break
            created = (m.get("time") or {}).get("created") or 0
            age = max(0, int(time.time() * 1000 - created) // 1000) if created else None
            note = f"末次活动 {role}" + (f" tool={tool}" if tool else "") \
                + (f" 距今{age}s" if age is not None else "")
            return " ".join(note.split())  # 摘要含供给文本（tool 名/标题）：同 raw 口径拍平防伪造服务行
        except Exception:
            return None

    def _stalled_evidence(self, tid, t, note):
        """卡住证据包（F59）：只收事实读数零根因结论；单项取不到整项省略。

        返回（证据行集, 草稿页相对路径）；草稿写面失败→路径 None——
        降级纪律：拍人/草稿互不阻塞，通知照发。"""
        ev = {"tid": tid, "state": t["state"]}
        if t.get("model"):
            ev["model"] = t["model"]  # F55 字段，缺席省
        if t.get("last_seen"):
            ev["last_seen_age_s"] = max(0, int(time.time() - t["last_seen"]))
        if t.get("worker_sid"):
            try:  # busy＝F28 判据 updated>idle；oc 不可达省
                tm = (self._oc.session(t["worker_sid"]) or {}).get("time") or {}
                ev["busy"] = (tm.get("updated") or 0) > (tm.get("idle") or 0)
            except Exception:
                pass
        latest = self._env_activity(t.get("env_dir")) if self._env_activity else 0.0
        if latest > 0.0:  # env 缺席/被回收（活性 0.0）省
            ev["env_active_seconds_ago"] = max(0, int(time.time() - latest))
        vstate = (t.get("verify") or {}).get("state")
        if vstate:
            ev["verify_state"] = vstate
        ev["fail_streak"] = t.get("fail_streak", 0)
        if note:
            ev["last_msg"] = note  # F52 摘要原样并入，不重复取
        lines = [f"{k}: {' '.join(str(v).split())}" for k, v in ev.items()]
        draft = None
        if self._draft_writer:
            try:
                page = self._draft_writer(self._store.home, tid,
                                          t.get("dispatcher_sid"), lines)
                draft = str(Path(page).relative_to(self._store.home))
            except Exception:
                pass  # 草稿照写失败不阻塞拍人
        return lines, draft

    def escalate(self, kind, tid, raw=None, streak=None):
        """失控拍人：正文服务权威合成；raw 为任务/worker 供给文本（不可信）。

        stalled/env-gone 走免通知通道（synthetic，落转录不弹窗，gt-5bc312 用户
        裁定；F72 env-gone 同款），其余 kind 照常会话注入；stalled 边沿附末次
        活动摘要（F52，失败降级）＋证据包小节与草稿页路径（F59，降级纪律：
        证据永不阻塞拍人；env-gone 正文自足不给）。
        streak＝同任务连续升级计数（退避节奏见 _stale_scan），正文随带。"""
        t = self._state.get(tid)
        body = (f"失控[{kind}] 任务{tid}：{_KIND_NOTE[kind]}；"
                f"状态 {t['state']}，worker={t.get('worker_sid') or '-'}，"
                f"dispatcher={t.get('dispatcher_sid') or '-'}，"
                f"连败 {t.get('fail_streak', 0)}")
        if streak:
            body += f"，第 {streak} 次升级"
        note = None
        if kind == KIND_STALLED and t.get("worker_sid"):
            note = self._last_activity(t["worker_sid"])  # 加一次取末条（F52 裁定）
            if note:
                body += f"；{note}"
        if kind == KIND_STALLED:  # F59 证据包：逐行 key: value，缺失项整行省略
            lines, draft = self._stalled_evidence(tid, t, note)
            if lines:
                body += "\n证据包\n" + "\n".join(lines)
            if draft:
                body += f"\n草稿: {draft}"
        if raw:
            flat = " ".join(str(raw).split())  # 拍平换行：供给文本不得伪造服务合成行
            body += f"\n{_UNTRUSTED}\n{flat[:_RAW_MAX]}"
        root = self._root(tid)
        rt = self._state.get(root)
        self._store.append_event({"ts": time.time(), "actor": "health",
                                  "event": "escalate", "task": tid, "kind": kind})
        quiet = kind in (KIND_STALLED, KIND_ENV_GONE)
        self._router.post(rt.get("worker_sid"), ("up", root), "escalate", body,
                          silent=quiet)  # stalled/env-gone 免通知通道：不弹窗（gt-5bc312；F72 同款）
        if quiet:  # 僵态类邮件例外（§6 决策记录）：stalled 与 env-gone（F72 同款）
            send_email(self._store, tid, body, runner=self._runner)

    def _root(self, tid):
        """沿 dispatcher_sid 上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）。"""
        tasks = self._state.snapshot()["tasks"]
        seen = set()
        while (tid not in seen
               and tasks.get(tid, {}).get("dispatcher_sid") != self._app.main_sid):
            seen.add(tid)
            parent = next((p for p, x in tasks.items()
                           if x.get("worker_sid")
                           == tasks[tid].get("dispatcher_sid")), None)
            if parent is None:
                break  # 链头不在册（main 缺位）——就地升级到已达的最上级
            tid = parent
        return tid
