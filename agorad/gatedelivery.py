"""Req: F137 — v3 交付执行体（opencode-agora v3 P3-T9b，设计 §4 事务纪律＋交付不变量）。

GateDeliveryService 以 T9a GateVerifyService 为基类装配交付协议三形态：
①managed 本地权威交付（F100）：verify 全绿后进入交付循环——本地钉死支 tip
定基＋detached 组树＋ff-only（真非 ff 换基重排重验，≤3 次总 attempt 预算）
→集成 verify 复跑（复用基类执行面与分类路由——绝不带病推进）→仓级交付锁
内 advance_local_target CAS ff-only 推进（前置 expected-old：他写者推进＝
non-ff 整链重试；脏/占用＝local_target_dirty/busy 终局；绝不 force/stash/
换支/清洗）；幻影双守卫（G1 空集 rebase 识别＋G2 推进后终检 contains 才
merged）；F105 同基假冲突自愈（最新基重建再 rebase 恰一次、attempt 不变；
同基禁盲试、基不可得径直终局）＋终局 conflict_files 记录。
②external_branch（F101）：钉死支 tip 定基集成→ff-only 合入→集成 verify
绿即驻留 done＋awaiting_external（env/凭据保留、不合不推不回收）；非 ff
即终局 diverged（ag-f12220 自动变基废除）；确认权在外部派发方（tasksvc
.deliver：delivered→merged→closed 全链、failed→blocked 失败桶、non_ff
祖先预判 diverged/协议矛盾拒零改写）。
③out_of_band：done body＝交付物引用（零提交、锚/vacuous 三检豁免），门体
只跑探针＋env verify，绿即 merged→closed 直接收口——零 Git 交付动作。

quick 包络（F65/F67）：合并前 merge-base 累计 diff 超文件/行阈值按 verify
红既有路径退回。干净检查（F97）：advance dirty 脏面全为「根级未跟踪隐藏
整目录」→中央名册幂等入册＋exclude 写入＋同轮重试一次；混合脏面零部分
豁免。交付轮残值（D3）：重派/新轮/确认面 reset conflict_files/attempt/
base/error。崩溃恢复 recover()：failed+done 只补 blocked 绝不自动重试；
pending+done+commit 本地 contains 判定（F109 零网络）已含即收口；外部驻
留直通零接触。

分层（§1）：gitx（含交付锁/推进/名册面）经 Protocol 构造注入——不 import
access/storage、零 SQL；git 操作与 runner 恒在 UoW 外（§4 禁子进程），
状态回写在重读后（read-decide-write 同事务）。

T9b 边界：不实现 HTTP/MCP/门队列调度（T13）；不改 v2 gate.py 生产路径；
verify/review 执行体细节复用 T9a（基类单源）；legacy 无投影行保持 T9a
驻留 green（迁移行交付投影归 T11）。
"""
import dataclasses
import pathlib
import time

from agorad.domain import (DeliveryState, task_transition, reset_fail_streak)
from agorad.gateverify import (GateVerifyService, Gitx as VerifyGitx,
                               with_shell_exports)

_TAIL = 2000              # 通知面输出尾截断（v2 同口径）
_DELIVERY_ATTEMPTS = 3    # F98/F100 non-ff 竞争重试总预算（含首次）
_LOCK_POLL = 0.05         # 交付锁轮询步长（v2 同口径）
DELIVERY_FAILPOINTS = ("publish",)  # 崩溃窗：交付推进前后

# ag-54bd98 幻影合并双守卫共用弹回文案（G1 空集重放/G2 落账 commit 不在
# 目标支），worker 确认交付在 boot 分支后重 post(done) 自愈
_PHANTOM_BOUNCE = ("合并前置失败：env 分支相对目标无新增提交——确认交付提交在"
                   " env boot 分支上后重新 post(done, commit)")


def _auto_ignore_patterns(records):  # Req: F97
    """全脏面皆「根级未跟踪隐藏目录」（`?? .dir/` 整目录形态）→返回目录名册
    条目；任一条不匹配（跟踪改动/非隐藏/根级散文件/嵌套新文件）→None 不豁免。
    目录判定吃 porcelain 的尾 `/`：git 对未跟踪目录整目录折叠正是该形态。"""
    out = []
    for rec in records:
        if not rec.startswith("?? "):
            return None
        path = rec[3:]
        base = path[:-1] if path.endswith("/") else ""
        if (not base or "/" in base or not base.startswith(".")
                or base in (".", "..")
                or any(ord(c) < 32 or c == "\x7f" for c in base)
                or any(c in "\\*?[]\ufffd" for c in base)):
            return None  # 控制字符写劈名册行、glob 元字符入册即活模式，不自动豁免
        if base + "/" not in out:
            out.append(base + "/")
    return out or None


def _append_roster(path, patterns):  # Req: F97
    """中央名册幂等追加：字节面比对防重（仅按 \\n 切行——\\r 等归入行内容，
    splitlines 会劈行击穿幂等判定）、既有文本无尾换行先补防粘连；缺文件＝
    新建，父目录缺＝建。shortcut: 跨仓并发 append 的 RMW 竞窗＝v2 平移保真
    （本轮 exclude 已独立落盘生效，丢行下轮幂等补回），收口归 T13 装配面。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = p.read_bytes() if p.exists() else b""
    have = set(data.split(b"\n"))
    missing = [e for e in patterns if e.encode("utf-8") not in have]
    if not missing:
        return
    if data and not data.endswith(b"\n"):
        data += b"\n"
    p.write_bytes(data + "\n".join(missing).encode("utf-8") + b"\n")


class DeliveryGitx(VerifyGitx):
    """gitx 交付面（access.gitx 结构化满足；调用必在 UoW 外）。advance 返回
    口径＝access：synced|non-ff|dirty|failed（占用/机械病归 failed→
    local_target_busy 终局）；锁协议＝RepoDeliveryLock（acquire(blocking=
    False)/release）。"""

    def local_branch_sha(self, repo_dir, branch) -> str: ...
    def build_integration_at(self, target_dir, integration_dir,
                             base_sha=None): ...
    def rebase_onto(self, env_dir, integration, conflicts=None) -> str: ...
    def advance_local_target(self, repo_dir, branch, sha,
                             expected_old) -> str: ...
    def delivery_lock(self, repo_dir): ...
    def diff_stats(self, env_dir, branch=None) -> tuple[int, int, str]: ...
    def status_records(self, repo_dir) -> list[str]: ...
    def apply_ignore_roster(self, repo_dir, patterns): ...
    def env_on_disk(self, env_dir) -> bool: ...


class GateDeliveryService(GateVerifyService):
    """交付执行体（T9b 下篇）；transport 单写者装配假设（同 T9a）。"""

    def __init__(self, store, gitx, runner, *, messaging=None, tasksvc=None,
                 anchors=None, artifacts=None, home="", clock=None,
                 injector=None, verify_timeout=300, verify_ceiling=None,
                 verify_profiles=None, quick_max_files=3, quick_max_lines=30,
                 lock_wait_s=300.0, roster_path=""):
        super().__init__(store, gitx, runner, messaging=messaging,
                         tasksvc=tasksvc, anchors=anchors, artifacts=artifacts,
                         home=home, clock=clock, injector=injector,
                         verify_timeout=verify_timeout,
                         verify_ceiling=verify_ceiling,
                         verify_profiles=verify_profiles)
        self._quick_max_files = quick_max_files
        self._quick_max_lines = quick_max_lines
        self._lock_wait_ms = max(0.0, float(lock_wait_s)) * 1000
        self._roster_path = roster_path
        if injector is not None:
            for name in DELIVERY_FAILPOINTS:
                injector.register(name)

    # ---- out_of_band 受理豁免（断言7） ----

    def accept_done(self, tid, worker_sid, commit):
        t = self._store.get_task(tid)
        if t is not None and t.deliverable == "out_of_band":
            return self._accept_out_of_band(tid, worker_sid, commit)
        return super().accept_done(tid, worker_sid, commit)

    def _accept_out_of_band(self, tid, worker_sid, ref):
        """review-only（deliverable=out_of_band）done 受理豁免分支（v2
        _accept_out_of_band 平移）：零提交＋交付物在案——sha 形状/锚/F69
        vacuous 三检跳过（结构性无提交无锚）；state/verify/children 三闸
        照常；交付物引用＝done body 本体（审计 note 留痕）。"""
        t = self._store.get_task(tid)
        if t.state != "running":
            return self._deny(tid, "state", f"任务 {tid} 当前 {t.state} 非 "
                            "running——board 查看状态，仅 running 受理 done")
        if t.verify.state != "registered":
            return self._deny(tid, "verify-unregistered",
                            f"任务 {tid} verify 未登记——post 通知派发方 "
                            "task_update 登记 verify 命令，登记后重新 "
                            "post(done, body=交付物引用)")
        if not t.verify.cmd.strip():
            return self._deny(tid, "verify-empty", f"任务 {tid} verify 命令为"
                            "空——task_update 登记非空命令后重新 post(done)")
        ref = str(ref).strip()
        if not ref:
            return self._deny(tid, "body-format",
                            f"任务 {tid} review-only done body 须为交付物引用"
                            "（收到空 body）——post(done, body=路径/链接/结论)")
        if self._open_children(worker_sid):
            return self._deny(tid, "children", "嵌套子任务未结案——等待 "
                            "result 回传，或由你取消子任务；成功/有意跳过后"
                            "再 post(done)")
        now, raced = self._now(), None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh.state != "running":  # 受理窗竞速
                raced = f"任务 {tid} 当前 {fresh.state} 非 running——仅 running 受理 done"
            else:
                task = task_transition(fresh, "done", now_ms=now)  # 零提交
                self._store.upsert_task(task)
                self._store.append_event(self._ev(
                    now, worker_sid, "done", tid, {"note": ref[:200]}))
        if raced is not None:
            return self._deny(tid, "state", raced)
        return "accepted"

    # ---- 门体钩（T9a run 的两个扩展点） ----

    def _pre_pipeline(self, tid, t, runner):
        """门闩后、崩溃残留探针前：out_of_band 豁免门体（探针＋env verify＋
        直接收口）；managed failed+done 崩窗＝只补 blocked 绝不自动重试。"""
        if t.deliverable == "out_of_band":
            return self._run_out_of_band(tid, t, runner)
        d = t.delivery
        if d is not None and d.mode == "managed" and d.status == "failed":
            now, patched = self._now(), False
            with self._store.uow():
                fresh = self._store.get_task(tid)
                if fresh.state == "done":  # 崩窗：failed 落账而 blocked 未达
                    self._store.upsert_task(task_transition(
                        fresh, "blocked", delivery_failure=True, now_ms=now))
                    self._store.append_event(self._ev(
                        now, "gate", "delivery_failed_recovery", tid))
                    patched = True
            if patched:
                return "blocked"
        return None

    def _landed_probe(self, tid, t):
        """交付协议行收口拆分（评审环 R1 B2）：managed 崩溃残留经
        _finish_delivered——投影 delivered/synced 一并落账（基类 _land 不写
        投影，假 pending 残值会泄漏给迁移/确认面）；external 权威在外部
        派发方——无本地 landed 补终态探针（v2 口径），照走管线 handoff。"""
        d = t.delivery
        if d is None:
            return super()._landed_probe(tid, t)  # legacy 行：基类 _land 口径
        if d.mode != "managed" or not t.commit or not t.target_branch:
            return None
        try:
            landed = self._gitx.contains(t.project_dir, t.commit,
                                         t.target_branch)
        except RuntimeError:
            return None  # 探针病照走验证路径（其内自有失败面）
        if not landed:
            return None  # 门重排链路续跑（恰一次）
        return self._finish_delivered(tid, t.commit, None, d.attempt or 0)

    def _integration(self, tid, t, env, target, integration, runner):
        if t.delivery is not None:  # 交付协议行：集成组树/复验归交付循环
            return None  # （每 attempt 组树＋复验——推进前权威口径）
        return super()._integration(tid, t, env, target, integration, runner)

    def _after_green(self, tid, t, runner):
        """verify/review 全绿出口＝交付推进（断言8 先过 quick 包络）。"""
        d = t.delivery
        if d is None:
            return super()._after_green(tid, t, runner)  # legacy 无投影行驻留
        if t.quick and self._quick_envelope(tid, t):
            return "verify_failed"
        if d.mode == "external_branch":
            return self._deliver_external(tid, t, runner)
        return self._deliver_managed(tid, t, runner)

    # ---- out_of_band 门体（断言7：零 Git 交付动作） ----

    def _run_out_of_band(self, tid, t, runner):
        """探针不豁免（F107）→env verify 分类→绿即 merged→closed 直接收口
        （不合不推——交付物引用即 done body）。"""
        env, target = t.env_dir, t.project_dir
        integration = t.gate.gate_env or f"{env}__gate"
        probe = self._base_probe(tid, t, target, integration, runner)
        if probe is not None:
            return probe
        self._phase(tid, "verify")
        outcome = self._env_verify(tid, env, t.verify.cmd, runner)
        if outcome is not None:
            return outcome
        self._audit(tid, "verify_green")
        return self._close_out_of_band(tid)

    def _close_out_of_band(self, tid):
        """review-only 直接收口：merged（note=out_of_band 零合入留痕）→closed
        两跳同事务＋撤牌回收（口径对齐 _land）。"""
        now, msgs = self._now(), []
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = task_transition(fresh, "merged", now_ms=now)
            task = reset_fail_streak(task, now_ms=now)
            task = task_transition(task, "closed", now_ms=now)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, "gate", "merged", tid, {"note": "out_of_band"}))
            self._store.append_event(self._ev(now, "gate", "closed", tid))
            if self._msg is not None and task.dispatcher_sid:
                for state, text in (
                        ("merged", "review-only 交付（verify 绿，零合入）"),
                        ("closed", f"任务 {tid} 已 closed；交付物引用见 done "
                         "审计")):
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=text,
                        dedup_key=f"transition:{tid}:{state}:"
                        f"{task.updated_ms}")[0])
        for m in msgs:
            self._msg.dispatch(m.id)
        self._revoke(tid, task)
        return "merged"

    # ---- quick 包络（断言8：F65/F67） ----

    def _quick_envelope(self, tid, t):
        """合并前机械量累计 diff（merge-base..env numstat）：超文件/行阈值按
        verify 红既有路径退回（计连败弹 worker）。"""
        files, lines, detail = self._gitx.diff_stats(t.env_dir,
                                                     t.target_branch)
        if files > self._quick_max_files or lines > self._quick_max_lines:
            self._quality_fail(
                tid, "verify_failed",
                f"quick envelope exceeded: {files} files "
                f"(max {self._quick_max_files}) / {lines} lines "
                f"(max {self._quick_max_lines})"
                "——缩减改动后重新 post(done, commit)"
                f"\n输出尾部:\n{detail[-_TAIL:]}",
                reason="quick envelope exceeded")
            return True
        return False

    # ---- managed 本地权威交付（断言1/2/3/4/5/9） ----

    def _deliver_managed(self, tid, t, runner):
        """本地钉死支 tip 定基集成→ff/rebase→集成 verify（推进前复跑——
        复用基类执行面）→仓级锁内 CAS ff-only 推进。干净 non-ff（他写者
        推进）按 attempt 预算整链重试（换基重排＋重验）≤3 次；rebase 内容
        冲突经 F105 假冲突自愈判别后终局 failed/rebase_conflict（＋
        conflict_files）；脏/占用＝local_target_dirty/busy 终局；幻影双守卫
        （G1 空集/G2 推进后终检）；全绿唯一成功出口＝_finish_delivered。"""
        branch, env, target = t.target_branch, t.env_dir, t.project_dir
        integration = t.gate.gate_env or f"{env}__gate"
        if branch is None:
            return self._delivery_fail(
                tid, 1, "local_base",
                "managed 未钉死 target_branch——交付协议不回退仓默认支")
        e0 = t.rebased_from or t.commit  # done sha＝交付权威（轮前快照，v2 同源）
        for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
            try:
                base = self._gitx.local_branch_sha(target, branch)
            except RuntimeError as e:
                return self._delivery_fail(tid, attempt, "local_base", str(e))
            self._phase(tid, "integration", attempt=attempt)
            try:
                self._gitx.build_integration_at(target, integration,
                                                base_sha=base)
            except (RuntimeError, ValueError) as e:  # 基对象缺失＝交付面终局
                return self._delivery_fail(tid, attempt, "local_base", str(e))
            before = self._gitx.head(integration)
            r = None  # R＝重放后交付 sha（None＝ff 直通）
            if self._gitx.merge_ff(env, integration) != "ok":
                conflicts: list = []
                if self._gitx.rebase_onto(env, integration,
                                          conflicts=conflicts) == "conflict":
                    latest = self._refresh_base(tid, target, branch, base)
                    if latest is None:  # 同基/基不可得：径直终局（禁盲试）
                        return self._delivery_fail(
                            tid, attempt, "rebase_conflict",
                            conflict_files=conflicts)
                    base = latest  # F105：最新基重建再 rebase 恰一次（attempt 不变）
                    try:
                        self._gitx.build_integration_at(target, integration,
                                                        base_sha=base)
                    except (RuntimeError, ValueError) as e:
                        return self._delivery_fail(tid, attempt,
                                                   "local_base", str(e))
                    before = self._gitx.head(integration)
                    conflicts = []
                    if self._gitx.rebase_onto(env, integration,
                                              conflicts=conflicts) == "conflict":
                        return self._delivery_fail(
                            tid, attempt, "rebase_conflict",
                            conflict_files=conflicts)
                r = self._gitx.head(integration)
                if r == before and not self._gitx.is_ancestor(target, e0,
                                                              base):
                    # G1 幻影空集重放（ag-54bd98）：env 无交付且本地基亦不含
                    self._audit(tid, "empty_rebase_noop")
                    self._quality_fail(tid, "reopened", _PHANTOM_BOUNCE,
                                       reason="空集 rebase 无交付")
                    return "reopened"
                now = self._now()
                with self._store.uow():  # 先落库再验证（崩溃幂等）
                    fresh = self._store.get_task(tid)
                    self._store.upsert_task(dataclasses.replace(
                        fresh, commit=r, rebased_from=e0, updated_ms=now))
                    self._store.append_event(self._ev(
                        now, "gate", "rebased", tid,
                        {"from": e0, "to": r}))
            deliver = self._gitx.head(integration)  # ff 直通＝env 头；重放＝R
            report = self._gitx.materialize_checkout(integration, target)
            if report:
                self._audit(tid, "checkout_materialize", note=report)
            rc, out, art = self._exec(  # 推进前复跑集成 verify（断言1）
                tid, "integration_verify", "integration", integration,
                with_shell_exports(t.verify.cmd,
                                   {"AGORA_INTEGRATION_BASE": base}),
                runner, extra={"integration_base": base})
            outcome = self._route_int(
                tid, rc, out, art,
                ("rebase 后集成 verify 失败" if r else "集成 verify 失败",
                 "rebase 后集成" if r else "集成"))
            if outcome is not None:
                return outcome
            outcome = self._publish(tid, target, branch, deliver, base,
                                    attempt)
            if outcome != "retry":  # retry＝attempt 预算内整链重走（换基重排重验）
                return outcome
        return self._delivery_fail(
            tid, _DELIVERY_ATTEMPTS, "retry_exhausted",
            note=f"{_DELIVERY_ATTEMPTS} 次总尝试内本地目标支持续被推进"
                 "或交付锁不可得")

    def _publish(self, tid, target, branch, deliver, base, attempt):
        """发布面：仓级交付锁有界等待→锁内重读 tip（他写者推进＝non-ff 整链
        重试）→advance_local_target CAS ff-only 推进（expected-old 前置）→
        F97 脏面自动豁免同轮重试一次→G2 幻影终检→_finish_delivered。落账/
        通知恒在锁外（评审环 R1：锁内 transport I/O 拉长临界区烧他仓 attempt）。"""
        self._fire("publish", "before", tid=tid, attempt=attempt)
        lock = self._gitx.delivery_lock(target)
        if not self._acquire_lock(lock):
            self._audit(tid, "delivery_lock_timeout", attempt=attempt)
            return "retry"  # 等锁让位＝一次失败尝试（F96/F98 口径）
        st, fail = None, None  # fail＝锁内捕获、出锁后落账（终局不占临界区）
        try:
            try:
                tip = self._gitx.local_branch_sha(target, branch)  # 锁内重读
            except (RuntimeError, ValueError) as e:
                fail = (attempt, "local_base", str(e))
            if fail is None:
                if tip != base:  # 他写者已推进：释放后整链重试（重排重验）
                    self._audit(tid, "delivery_nonff", attempt=attempt,
                                note=f"{base[:12]}→{tip[:12]}")
                    return "retry"
                self._phase(tid, "publish", attempt=attempt)
                try:
                    st = self._gitx.advance_local_target(target, branch,
                                                         deliver, base)
                except (RuntimeError, ValueError) as e:  # 锁内环境病＝终局
                    fail = (attempt, "local_base", str(e))  # （F98 D2 口径）
                if fail is None and st == "dirty":  # Req: F97 全根级未跟踪
                    if self._auto_ignore_face(tid, target):  # 隐藏整目录→入册
                        try:  # 同轮重试一次
                            st = self._gitx.advance_local_target(
                                target, branch, deliver, base)
                        except (RuntimeError, ValueError) as e:
                            fail = (attempt, "local_base", str(e))
        finally:
            lock.release()
        if fail is not None:
            return self._delivery_fail(tid, *fail)
        if st == "synced":
            self._fire("publish", "after", tid=tid, attempt=attempt)
            # G2 幻影守卫：merged ⇒ 目标支真含落账 commit（探针病如实上抛走
            # 门内异常面 reopened——非质量败，不冤 worker 计连败，评审环 R1）
            landed = self._gitx.contains(target, deliver, branch)
            if not landed:
                self._audit(tid, "phantom_merge_blocked")
                self._quality_fail(tid, "reopened", _PHANTOM_BOUNCE,
                                   reason="目标支不含落账 commit")
                return "reopened"
            return self._finish_delivered(tid, deliver, base, attempt)
        if st == "non-ff":  # 锁内 CAS 落空/分叉：整链重试
            self._audit(tid, "delivery_nonff", attempt=attempt, note=st)
            return "retry"
        # dirty/failed：终局——本地权威未推进，绝不伪装 pending
        return self._delivery_fail(
            tid, attempt,
            "local_target_dirty" if st == "dirty" else "local_target_busy")

    def _refresh_base(self, tid, target, branch, base):
        """F105 假冲突自愈判据：最新基重读。基前进＝审计 rebase_conflict_retry
        返回新基（调用方重建再 rebase 恰一次，attempt 不变）；同基（禁盲试）
        ＝None；基不可得＝审计后 None（径直终局）。"""
        try:
            latest = self._gitx.local_branch_sha(target, branch)
        except RuntimeError as e:
            self._audit(tid, "rebase_conflict_base_unavailable",
                        note=str(e)[:200])
            return None
        if latest == base:
            self._audit(tid, "rebase_conflict_same_base")  # 同基禁盲试
            return None
        self._audit(tid, "rebase_conflict_retry",
                    **{"from": base, "to": latest})
        return latest

    def _auto_ignore_face(self, tid, target):  # Req: F97
        """advance dirty 自动豁免面：脏面全为「根级未跟踪隐藏整目录」→中央
        名册幂等追加＋exclude 写入（同轮重试一次）；混合脏面/名册面缺席＝
        None 零部分豁免；取数病留痕（不静默废 F97）。"""
        try:
            records = self._gitx.status_records(target)
        except (RuntimeError, ValueError) as e:
            self._audit(tid, "auto_ignore_probe_failed", note=str(e)[:200])
            return None
        patterns = _auto_ignore_patterns(records)
        if not patterns or not self._roster_path:
            return None
        _append_roster(self._roster_path, patterns)
        self._gitx.apply_ignore_roster(target, patterns)
        self._audit(tid, "auto_ignore", note=" ".join(patterns))
        return patterns

    def _acquire_lock(self, lock):
        """有界等锁（F96/F98 口径）：非阻塞探测＋短步长轮询，到点让位＝一次
        失败尝试，绝不无限阻塞串行门；预算走墙钟 monotonic（与域时钟注入
        解耦——冻结 clock 不致死循环）；lock_wait_s 构造旋钮（测试秒级）。"""
        deadline = time.monotonic() + self._lock_wait_ms / 1000
        while True:
            if lock.acquire(blocking=False):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL)

    # ---- external_branch 外部交付 handoff（断言6） ----

    def _deliver_external(self, tid, t, runner):
        """钉死本地支 tip 定基集成→ff-only 合入（非 ff 即终局 diverged——
        ag-f12220 自动变基废除，不 rebase）→集成 verify→绿即驻留 done＋
        awaiting_external（不合目标支、不推 origin、不回收 env/凭据）。"""
        d = t.delivery
        attempt = d.attempt or 1
        branch, env, target = t.target_branch, t.env_dir, t.project_dir
        integration = t.gate.gate_env or f"{env}__gate"
        if branch is None:
            return self._delivery_fail(
                tid, attempt, "local_base",
                "target_branch 未钉死——external_branch 不回退仓默认支")
        try:
            base = self._gitx.local_branch_sha(target, branch)
        except RuntimeError as e:
            return self._delivery_fail(tid, attempt, "local_base", str(e))
        self._phase(tid, "integration", attempt=attempt)
        try:  # 外部基由派发方供给或本地钉死支——不可解析＝交付面终局
            self._gitx.build_integration_at(target, integration, base_sha=base)
        except (RuntimeError, ValueError) as e:
            return self._delivery_fail(tid, attempt, "external_base", str(e))
        if self._gitx.merge_ff(env, integration) != "ok":  # ff-only
            return self._delivery_fail(
                tid, attempt, "diverged",
                note="交付分支与 worker 线索分叉（非 ff，自动变基已废除）"
                     "——env 为内容超集时外部 force-with-lease 推送，否则"
                     "取消重开（env 按分支现状重建）或人工调和")
        deliver = self._gitx.head(integration)  # ff 直通＝integration 头
        report = self._gitx.materialize_checkout(integration, target)
        if report:
            self._audit(tid, "checkout_materialize", note=report)
        rc, out, art = self._exec(
            tid, "integration_verify", "integration", integration,
            with_shell_exports(t.verify.cmd,
                               {"AGORA_INTEGRATION_BASE": base}),
            runner, extra={"integration_base": base})
        outcome = self._route_int(tid, rc, out, art,
                                  ("集成 verify 失败", "集成"))
        if outcome is not None:
            return outcome
        self._phase(tid, "publish", attempt=attempt)  # handoff 即外部交付发布
        return self._handoff_external(tid, branch, deliver, base, attempt)

    def _handoff_external(self, tid, branch, sha, base, attempt):
        """绿 handoff 唯一出口：投影 awaiting_external（commit/base/attempt
        机器可读、local_status=not_required——外部权威，本地无同步义务）＋
        驻留 done＋env/凭据保留；通知面带齐四要素，确认权在外部派发方
        （task_deliver）。"""
        now, msg = self._now(), None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            nd = dataclasses.replace(
                fresh.delivery, status="awaiting_external", commit=sha,
                base=base, attempt=attempt, local_status="not_required",
                error=None,
                conflict_files=None)  # Req: F105 上轮终局残值必清（B1）
            task = reset_fail_streak(fresh, now_ms=now)
            task = dataclasses.replace(task, delivery=nd)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, "gate", "awaiting_external", tid,
                {"commit": sha, "base": base, "attempt": attempt}))
            if self._msg is not None and task.dispatcher_sid:
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid="gate", to_sid=task.dispatcher_sid,
                    task_id=tid, kind="result",
                    body=f"任务 {tid} handoff 就绪（awaiting_external）："
                         f"commit={sha} target={branch} base={base} "
                         f"attempt={attempt}"
                         "——外部确认经 task_deliver(delivered/non_ff/failed)",
                    dedup_key=f"awaiting_external:{tid}:{sha}")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return "awaiting_external"

    # ---- 终局/成功公共面 ----

    def _delivery_fail(self, tid, attempt, error, note="",
                       conflict_files=None):
        """交付失败终局（F98 专用边）：delivery.status=failed＋error/attempt
        （＋F105 conflict_files）＋done→blocked（delivery_failure=True）——
        非质量败：不计 fail_streak、不弹 worker、不自动取消/重派；env 留
        现场，处置权在派发方。"""
        now, msg = self._now(), None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            nd = dataclasses.replace(
                fresh.delivery or DeliveryState(), status="failed",
                error=error, attempt=attempt,
                conflict_files=list(conflict_files) if conflict_files else None)
            task = task_transition(fresh, "blocked", delivery_failure=True,
                                   now_ms=now)
            task = dataclasses.replace(task, delivery=nd)
            self._store.upsert_task(task)
            detail = {"error": error, "attempt": attempt}
            if note:
                detail["note"] = str(note)[:200]
            self._store.append_event(self._ev(
                now, "gate", "delivery_failed", tid, detail))
            if self._msg is not None and task.dispatcher_sid:
                body = (f"任务 {tid} 交付失败（{error}）——已 blocked"
                        "（非质量问题，不计连败）；核对远端/网络后重派或取消")
                if conflict_files:
                    body += "；冲突文件: " + ", ".join(conflict_files)
                if note:
                    body += "；" + str(note)[:200]
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid="gate", to_sid=task.dispatcher_sid,
                    task_id=tid, kind="result", body=body,
                    dedup_key=f"transition:{tid}:blocked:{now}")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return "blocked"

    def _finish_delivered(self, tid, sha, base, attempt):
        """managed 本地权威落地唯一成功出口（F100）：投影 delivered/synced
        →merged→closed 两跳同事务（tasksvc._deliver_ok 同款）＋撤牌回收；
        base=None＝崩溃窗口补终态（保留在案基线与尝试计数）。"""
        now, msgs = self._now(), []
        with self._store.uow():
            fresh = self._store.get_task(tid)
            nd = dataclasses.replace(
                fresh.delivery or DeliveryState(), status="delivered",
                commit=sha, local_status="synced", error=None,
                attempt=attempt, conflict_files=None)
            if base is not None:
                nd = dataclasses.replace(nd, base=base)
            task = task_transition(fresh, "merged", now_ms=now)
            task = reset_fail_streak(task, now_ms=now)
            task = task_transition(task, "closed", now_ms=now)
            task = dataclasses.replace(task, delivery=nd, commit=sha)
            self._store.upsert_task(task)
            detail = {"commit": sha}
            if task.rebased_from:
                detail["rebased_from"] = task.rebased_from
            self._store.append_event(self._ev(
                now, "gate", "merged", tid, detail))
            self._store.append_event(self._ev(now, "gate", "closed", tid))
            if self._msg is not None and task.dispatcher_sid:
                for state, text in (
                        ("merged", f"@{sha[:7]} local=synced——本地目标支已"
                         "交付，环境已回收"),
                        ("closed", f"任务 {tid} 已 closed；commit={sha}")):
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid,
                        kind="result", body=text,
                        dedup_key=f"transition:{tid}:{state}:"
                        f"{task.updated_ms}")[0])
        for m in msgs:
            self._msg.dispatch(m.id)
        self._revoke(tid, task)
        return "merged"

    # ---- 崩溃恢复面（断言11） ----

    def recover(self):
        """启动恢复面：交付投影非终态任务的崩溃收敛。failed+done＝只补
        blocked 绝不自动重试；managed pending+done+commit＝本地 contains
        判定（F109 零网络——恒本地 contains/is_ancestor，不 fetch 不推）：
        已含即收口（零推进零推送）＋门尾结算；未含留给门重排链路（reverify
        恰一次）续跑，不重复排队；缺 commit/权威不可得＝留痕交门重排兜底，
        不猜。external awaiting_external/delivered＝确认面权威直通；终局
        blocked/closed 行零接触。逐任务隔离：单任务恢复败只留痕不拦后续。"""
        for t in self._store.tasks_all():
            try:
                if t.deliverable == "out_of_band" or t.delivery is None:
                    continue  # review-only/legacy 无投影行零接触
                d = t.delivery
                if d.status == "failed":
                    if t.state == "done":  # 崩窗：failed 落账而 blocked 未达
                        now = self._now()
                        with self._store.uow():
                            fresh = self._store.get_task(t.id)
                            if fresh.state == "done":
                                self._store.upsert_task(task_transition(
                                    fresh, "blocked", delivery_failure=True,
                                    now_ms=now))
                                self._store.append_event(self._ev(
                                    now, "gate", "delivery_failed_recovery",
                                    t.id))
                    continue  # 终局：恢复面绝不重试
                if d.mode != "managed":
                    continue  # external 驻留/已交付＝确认面权威，直通
                if d.status == "delivered":
                    continue  # 确认面同事务闭环（merged→closed）——零接触
                if d.status != "pending":  # 未知态＝可上报恢复错误（v2 口径）
                    self._audit(t.id, "delivery_recover_unknown_status",
                                note=str(d.status)[:200])
                    continue
                if t.state != "done":
                    continue  # 在飞/续修态归各自车道（reinject/门）
                if not t.commit or not t.target_branch:
                    self._audit(t.id, "delivery_recover_missing_commit")
                    continue
                try:  # F109：权威判定一律本地钉死支（零网络）
                    landed = self._gitx.contains(t.project_dir, t.commit,
                                                 t.target_branch)
                except Exception as e:
                    self._audit(t.id, "delivery_recover_probe_failed",
                                note=str(e)[:200])
                    continue
                if not landed:
                    continue  # 门重排链路续跑：不重复排队、不发明 commit
                self._audit(t.id, "delivery_recovered")
                self._finish_delivered(t.id, t.commit, None,
                                       d.attempt or 0)
                self._tail(t.id)  # 门尾结算（清残留门闩/pending-cancel 消费）
            except Exception as e:  # 逐任务隔离：不拦后续任务收敛
                try:
                    self._audit(t.id, "delivery_recover_task_failed",
                                note=str(e)[:200])
                except Exception:
                    pass  # 审计自身病不得中断整轮收敛
