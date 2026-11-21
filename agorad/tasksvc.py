"""Req: F135 — v3 task 应用服务（opencode-agora v3 P3-T8，设计 §3/§5）。

任务生命周期：create/dispatch/update_verify/cancel/stop_after_failed/
transition/bump_fail_streak/wait/reverify/deliver/touch_seen。语义自 v2
agorad/core/task.py 平移（F37 继承矩阵/F23 verify 双路/F95 幂等/F94 交付
投影/F98 交付失败边/F124 受控等待/F101 外部交付/F34 查重/F45 池配额/
F65 quick 历史判定）；鉴权牌面归 T10、HTTP/MCP 归 T5——本服务收
actor_sid＋role，归属细判（dispatcher_sid==caller 或 root）在服务内。

分层（§1）：TaskRepo/Gitx/SessionFactory/Notifier 全 Protocol 构造注入
——不 import access/storage（Storage 结构化满足 TaskRepo；架构守卫在
test_architecture.py），零 SQL。gitx/oc（子进程/网络）与凭据文件写恒在
UoW 外（§4）。终态/取消迁移在同一 UoW 内产 event＋message（§5 复合提交
——message 经 Notifier.record；提交后 dispatch(mid) at-least-once 补投）。
last_seen 活动心跳批量窗 ≤5s（§3）：touch_seen 逐调用只进内存，距上次
持久 ≥ 窗口才落一次库（gt-a18ef6 写放大口径——不逐调用独立写事务）。

T8 边界：done 后 merged/closed 链与 verify 执行体归 T9（本服务 transition
单跳驱动）；busy 闸（F28）/stalled 重派/rebind 血统归 T10；boot prompt
全文与 wiki 页渲染归装配/transport——dispatch 仅发最小开工信封。
"""
import dataclasses
import json
import os
import pathlib
import re
import secrets
import sys
import time
import typing

from agorad.contracts import ContractError
from agorad.domain import (Credential, DeliveryState, Event, InvalidTransition,
                           Task, VerifySpec, bump_fail_streak, epoch_ms,
                           reset_fail_streak, task_transition)

_MAX_DEPTH = 3
_QUOTA = 6  # F41/F45：池并发配额
_ACTIVE = frozenset({"dispatched", "running", "done", "verify_failed",
                     "reopened", "blocked"})
_AFTER_OK = frozenset({"merged", "closed", "cancelled"})  # cancelled 须非 dependency_failed
_FROZEN = frozenset({"cancelled", "closed", "merged"})    # cancel 不触碰（merged 不逆转）
_WAIT_DONE = frozenset({"merged", "closed", "cancelled", "blocked"})  # F124 解析终态集
_TERMINAL_NOTIFY = frozenset({"merged", "closed", "blocked", "cancelled"})
_REQ_DOC = pathlib.Path("docs") / "reqs" / "opencode-agora.md"
_REQ_ID_RE = re.compile(r"F[0-9]+")
_REQ_NUM_RE = re.compile(r"F(\d+)")
_MODEL_RE = re.compile(r"[\w.-]+/[\w.-]+")
SEEN_WINDOW_MS = 5000  # §3 last_seen 批量持久窗

EV_CREATED = "task_created"
EV_DISPATCHED = "dispatched"
EV_CANCELLED = "cancelled"
EV_DENIED = "auth_denied"
EV_REJECTED = "dispatch_rejected"
EV_REJECTED_VERIFY = "dispatch_rejected_verify_missing"
EV_VERIFY = "verify_registered"
EV_WAIT_SET = "wait_set"
EV_WAIT_CLEARED = "wait_cleared"
EV_WAIT_RESOLVED = "wait_resolved"
EV_DEPENDENCY_STOPPED = "dependency_stopped"


def _bad_timeout(v):
    """F51：正整数秒（bool 是 int 子类显式排除）；界按硬顶系数收。"""
    return (not isinstance(v, int) or isinstance(v, bool) or v <= 0
            or v > sys.float_info.max // 4)


def _hex40(v):
    """40 位小写十六进制归一；不合格返回 None。"""
    c = str(v or "").lower()
    return c if len(c) == 40 and all(ch in "0123456789abcdef" for ch in c) else None


class TaskRepo(typing.Protocol):
    """storage 公开仓库接口（结构化；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def upsert_task(self, t: Task): ...
    def get_task(self, tid): ...
    def tasks_all(self): ...
    def append_event(self, ev: Event): ...
    def put_credential(self, cred: Credential): ...
    def delete_credential(self, sid) -> bool: ...


class Gitx(typing.Protocol):
    """gitx 注入面（access.gitx 同形；调用必在 UoW 外）。"""

    def symref(self, repo_dir): ...
    def default_branch(self, repo_dir): ...
    def check_branch(self, repo_dir, branch): ...
    def local_branch_sha(self, repo_dir, branch): ...
    def build_env(self, project_dir, tid, branch=None): ...
    def build_env_at(self, project_dir, tid, branch=None, base_sha=None): ...
    def materialize_checkout(self, tree_dir, source_dir) -> str: ...
    def head(self, repo_dir, branch=None): ...
    def env_on_disk(self, env_dir) -> bool: ...
    def remove_env(self, env_dir): ...
    def is_ancestor(self, repo_dir, base, commit) -> bool: ...


class SessionFactory(typing.Protocol):
    """oc 注入面：会话创建（can_spawn 熔断——异常即拒派）。"""

    def create(self, env, boot, title=None, model=None): ...


class Notifier(typing.Protocol):
    """messaging.MESSAGINGSERVICE 结构化满足（record＋dispatch 两面）。"""

    def record(self, *, ts_ms, from_sid, to_sid, task_id, kind, body,
               dedup_key=None): ...
    def dispatch(self, mid, *, escalate_kind=None) -> bool: ...


class _GuardConflict(ContractError):
    """dispatch UoW 内卫兵拒（身份/复核）——回滚后补审计行再抛。"""


class TaskService:
    """task 域应用服务；无锁无线程（transport 单写者装配假设）。"""

    def __init__(self, store: TaskRepo, gitx: Gitx, oc: SessionFactory, *,
                 messaging: Notifier | None = None, home="", clock=None,
                 max_depth=_MAX_DEPTH, quota=_QUOTA,
                 seen_window_ms=SEEN_WINDOW_MS, timers=None):
        self._store, self._gitx, self._oc = store, gitx, oc
        self._msg = messaging
        self._timers = timers  # Req: F145 终态路径同 UoW 自动取消 pending timer
        self._home = str(home)
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._max_depth, self._quota = max_depth, quota
        self._seen_window_ms = seen_window_ms
        self._seen = {}       # tid -> candidate last_seen_ms（内存态）
        self._seen_flushed = {}  # tid -> 上次持久时点（批量窗节流锚）

    # ---- F2/F94/F95 建任务（spec＝contracts.validate_task_create 规范形） ----

    def create(self, actor_sid, role, spec) -> str:
        goal, title = spec["goal"], spec.get("title")
        ex, vf, dl = spec["execution"], spec["verify"], spec["delivery"]
        tid = self._new_tid()
        parent = self._worker_parent(tid, actor_sid, role)
        project_dir = ex["project_dir"]
        if project_dir is not None:
            project_dir = self._repo_key(project_dir)
        if role == "worker":
            self._worker_matrix(tid, actor_sid, vf, ex, dl, parent)
            parent_repo = self._repo_key(parent.project_dir)
            if project_dir is not None and project_dir != parent_repo:
                self._reject(tid, actor_sid, "forbidden",
                             f"worker 建单 project_dir 强制继承父任务仓"
                             f"（{parent_repo}），指定他径拒", ev=EV_DENIED)
            project_dir = parent_repo
            target_branch = parent.target_branch
            model, deliverable = parent.model, parent.deliverable
            verify_base_exempt, review_cmd = (parent.verify_base_exempt,
                                              parent.review_cmd)
        else:
            target_branch, model = dl["target_branch"], ex["model"]
            deliverable, review_cmd = dl["deliverable"], vf["review_cmd"]
            verify_base_exempt = vf["verify_base_exempt"]
        quick = bool(ex["quick"])
        verify_cmd, verify_pending, timeout = (vf["cmd"],
                                               bool(vf["verify_pending"]),
                                               vf["timeout"])
        if verify_base_exempt is not None and not isinstance(
                verify_base_exempt, bool):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"verify_base_exempt 须为布尔（收到 {verify_base_exempt!r}）")
        if verify_cmd is not None and verify_pending:
            self._reject(tid, actor_sid, "invalid_argument",
                         "verify_cmd 与 verify_pending 互斥——二选一")
        if quick and verify_cmd is not None:
            self._reject(tid, actor_sid, "invalid_argument",
                         "quick 验收按账本历史动态判定，勿自带命令")
        if quick and verify_pending:
            self._reject(tid, actor_sid, "invalid_argument",
                         "quick 与 verify_pending 互斥——二选一")
        if verify_cmd is not None and not verify_cmd.strip():
            self._reject(tid, actor_sid, "invalid_argument",
                         "verify 命令为空——task_create 登记非空命令")
        if timeout is not None and _bad_timeout(timeout):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"verify timeout 须为正整数秒（收到 {timeout!r}）")
        if model is not None and not _MODEL_RE.fullmatch(model):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"model 须为 providerID/modelID 形（收到 {model!r}）")
        if deliverable is not None and deliverable not in (
                "out_of_band", "external_branch"):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"deliverable 仅支持 out_of_band/external_branch"
                         f"（收到 {deliverable!r}）")
        if review_cmd is not None and not str(review_cmd).strip():  # Req: F114
            self._reject(tid, actor_sid, "invalid_argument",
                         "review_cmd 为空——传非空命令或不传")
        client_key = ex["client_key"]
        if client_key is not None and (
                not isinstance(client_key, str) or not client_key
                or len(client_key) > 200 or not client_key.isprintable()):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"client_key 须为非空字符串（≤200 字符、无控制字符；"
                         f"收到 {client_key!r}）")
        after = ex["after"]
        if after is not None and self._store.get_task(after) is None:
            self._reject(tid, actor_sid, "not_found",
                         f"after: 前任务 {after} 不存在")
        resolved = self._history_cmd(tid, actor_sid, project_dir, quick)
        now = self._now()  # v2 优先级：显式命令 > quick 历史判定 > worker 继承
        if verify_cmd is not None or resolved is not None:
            verify = VerifySpec(cmd=verify_cmd or resolved,
                                state="registered", registered_by=actor_sid,
                                registered_at_ms=now, timeout_s=timeout,
                                timeout_source="task" if timeout is not None
                                else "default")
        elif role == "worker":
            verify = parent.verify
        else:
            verify = VerifySpec(timeout_s=timeout,
                                timeout_source="task" if timeout is not None
                                else "default")
        fallback_note = None
        if role != "worker" and target_branch is not None:
            try:
                self._gitx.check_branch(project_dir, target_branch)
            except Exception as e:
                self._reject(tid, actor_sid, "invalid_argument",
                             f"target_branch: {e}")
        elif target_branch is None and role != "worker":
            # Req: F94 缺省钉支＝主检出当前 symref 实名（detached/解析失败回退
            # 仓默认支＋审计行）；worker 继承 legacy 支不落此路（显式传支即拒）。
            why = ""
            try:
                pinned = self._gitx.symref(project_dir)
            except Exception as e:
                pinned, why = None, str(e)
            if pinned and pinned != "HEAD":
                target_branch = pinned
            else:
                try:
                    target_branch = self._gitx.default_branch(project_dir)
                except Exception as e:
                    self._reject(tid, actor_sid, "invalid_argument",
                                 f"target_branch: {e}")
                fallback_note = (
                    f"主检出当前分支 detached（HEAD）——回退仓默认支 "
                    f"{target_branch}" if pinned == "HEAD" else
                    f"主检出当前分支解析失败（{why}）——回退仓默认支 "
                    f"{target_branch}")
        delivery = None  # Req: F94：out_of_band 不建投影。服务面缺省（None）
        # ＝ managed 初始行——v2「absent＝普通合并任务」保真；F130 边缘缺省
        # out_of_band 是 T5 适配层口径，服务层 None/out_of_band 语义分立。
        if deliverable == "external_branch":
            delivery = DeliveryState(mode="external_branch",
                                     target_branch=target_branch)
        elif deliverable != "out_of_band":
            delivery = DeliveryState(mode="managed",
                                     target_branch=target_branch)
        root_sid = actor_sid if role != "worker" else (
            parent.root_sid or parent.dispatcher_sid)  # F45 一次性定死
        if client_key is not None:  # Req: F95 幂等扫描（入账前）
            for x in self._store.tasks_all():
                if x.client_key != client_key:
                    continue
                if ((x.goal, self._repo_key(x.project_dir), x.target_branch,
                     x.deliverable, x.model, x.verify.cmd,
                     bool(x.verify_base_exempt), x.review_cmd)
                        != (goal, project_dir, target_branch, deliverable,
                            model, verify.cmd, bool(verify_base_exempt),
                            review_cmd)):
                    self._reject(tid, actor_sid, "conflict",
                                 f"client_key {client_key!r} 已用（任务 {x.id}）"
                                 "且载荷不同——同键载荷须逐字段一致")
                if role != "root" and actor_sid not in (x.root_sid,
                                                        x.dispatcher_sid):
                    self._reject(tid, actor_sid, "conflict",
                                 f"client_key {client_key!r} 已属任务 {x.id}"
                                 "——非其派发谱系调用方不得复用（key conflict）")
                return x.id  # 幂等命中：零新行/零事件
        if ex["req_id"] is not None:  # F34 发放面查重（幂等命中后）
            self._req_check(tid, actor_sid, project_dir, ex["req_id"])
        depth = 1 + max((t.chain_depth for t in self._store.tasks_all()
                         if t.worker_sid == actor_sid), default=0)
        now = self._now()
        head = " ".join(str((title or (goal.splitlines()[0][:40]
                                       if goal else tid)) or tid).split())
        task = Task(id=tid, state="todo", title=head, goal=goal,
                    req_id=ex["req_id"], model=model, quick=quick,
                    deliverable=deliverable, chain_depth=depth,
                    root_sid=root_sid, dispatcher_sid=actor_sid,
                    created_ms=now, updated_ms=now,
                    after=((after,) if after else ()), client_key=client_key,
                    project_dir=project_dir, target_branch=target_branch,
                    verify_pending=verify_pending,
                    verify_base_exempt=bool(verify_base_exempt),
                    review_cmd=review_cmd, verify=verify, delivery=delivery)
        registered = verify_cmd is not None or resolved is not None
        with self._store.uow():
            self._store.upsert_task(task)
            self._store.append_event(self._ev(now, actor_sid, EV_CREATED, tid))
            if fallback_note:
                self._store.append_event(self._ev(
                    now, actor_sid, "target_branch_fallback", tid,
                    {"note": fallback_note}))
            if registered:  # 原子登记（含 quick 历史路）审计行照落
                self._store.append_event(self._ev(now, actor_sid, EV_VERIFY, tid))
        return tid

    # ---- F3 派发 ----

    def dispatch(self, tid, actor_sid, role):
        t = self._get(tid)
        self._own(t, actor_sid, role)
        if t.state not in ("todo", "blocked"):
            self._reject(tid, actor_sid, "conflict",
                         f"{t.state} 不可派发（仅 todo/blocked；stalled "
                         "running 重派归 T10 健康面）")
        if t.verify.state != "registered" and not t.verify_pending:
            self._reject(tid, actor_sid, "conflict",
                         "verify 未登记——create 传 verify_cmd 原子登记，或建单"
                         " verify_pending=true 走探索路", ev=EV_REJECTED_VERIFY)
        if t.chain_depth >= self._max_depth:
            self._reject(tid, actor_sid, "conflict",
                         f"chain_depth>={self._max_depth}")
        pool = t.root_sid or t.dispatcher_sid  # F45 池标识
        active = sum(1 for x in self._store.tasks_all()
                     if x.id != tid and x.state in _ACTIVE
                     and (x.root_sid or x.dispatcher_sid) == pool)
        if active >= self._quota:
            self._reject(tid, actor_sid, "conflict",
                         f"quota[{pool}]: 活动任务 {active}>={self._quota}")
        dep = t.after[0] if t.after else None
        if dep is not None:
            seen, chain, cur = {tid}, [], dep  # W-2：派发前沿链查环
            while cur is not None:
                if cur in seen:
                    self._reject(tid, actor_sid, "conflict",
                                 f"after: 链成环（{' → '.join(chain + [cur])}）"
                                 "——修正任务依赖后重派")
                chain.append(cur)
                seen.add(cur)
                nxt = self._store.get_task(cur)
                cur = nxt.after[0] if nxt is not None and nxt.after else None
            pred = self._get(dep)
            failed_cancel = (pred.state == "cancelled"
                             and str(pred.cancel_reason or "").startswith(
                                 "dependency_failed"))
            if pred.state not in _AFTER_OK or failed_cancel:
                self._reject(tid, actor_sid, "conflict",
                             f"after: 前任务 {dep} 未成功或由 dispatcher 有意"
                             "跳过（B7）")
        delivery = t.delivery  # Req: F98 重派开新交付轮（身份字段不动）
        if delivery is not None and delivery.status == "failed":
            delivery = dataclasses.replace(  # Req: F105 残值必清（B1 同口径）
                delivery, status="pending", error=None, attempt=0, commit=None,
                base=None, local_status="pending", conflict_files=None)
        env, env_base, new_env = t.env_dir, t.env_base, False
        report = ""
        if not self._gitx.env_on_disk(env):  # F71：记录 env 不在盘＝重派新建路
            new_env = True
            mode = delivery.mode if delivery is not None else None
            if mode == "external_branch" and t.target_branch is None:
                self._reject(tid, actor_sid, "conflict",
                             "local_base: external_branch 须钉死 target_branch"
                             "（显式指定后重派）")
            base_sha = None
            if t.target_branch is not None:
                # ag-b5f2e9/F101：定基一律本地钉死支 tip 权威（managed/
                # external_branch 同口径），缺支拒派点名 local_base，绝不
                # 回退仓默认支。
                try:
                    base_sha = self._gitx.local_branch_sha(t.project_dir,
                                                           t.target_branch)
                except Exception as e:
                    self._reject(tid, actor_sid, "conflict", f"local_base: {e}")
            try:  # Protocol 对齐 access.gitx：有基线走 build_env_at（权威定基）
                if base_sha is not None:
                    env = self._gitx.build_env_at(t.project_dir, tid,
                                                  t.target_branch, base_sha)
                else:
                    env = self._gitx.build_env(t.project_dir, tid,
                                               t.target_branch)
            except Exception as e:
                self._reject(tid, actor_sid, "conflict", f"env_build: {e}")
            try:
                env_base = self._gitx.head(env)  # F111 评审链根锚
            except Exception as e:
                if new_env:
                    self._reclaim_env(tid, env, actor_sid, "tasksvc:dispatch_head")  # Req: ag-0e6ca3
                self._reject(tid, actor_sid, "conflict", f"env_head: {e}")
            try:  # Req: ag-0d2daa 检出后即物化（worker 首轮构建不撞空依赖）
                report = self._gitx.materialize_checkout(env, t.project_dir)
            except Exception as e:  # 物化败＝拒派并回收新 env（不交残树）
                if new_env:
                    self._reclaim_env(tid, env, actor_sid, "tasksvc:dispatch_materialize")  # Req: ag-0e6ca3
                self._reject(tid, actor_sid, "conflict",
                             f"checkout_materialize: {e}")
        boot = (f"[agora] {tid}: {t.title}\nenv: {env}\n"
                f"page: wiki/tasks/{tid}.md\ngoal: {t.goal}")
        try:
            wsid = self._oc.create(env, boot, title=f"{tid}: {t.title}",
                                   model=t.model)
        except Exception as e:  # can_spawn 熔断：本轮新建 env 归本轮回收
            if new_env:
                self._reclaim_env(tid, env, actor_sid, "tasksvc:dispatch_spawn")  # Req: ag-0e6ca3
            self._reject(tid, actor_sid, "conflict", f"can_spawn: {e}")
        token = secrets.token_hex(16)
        now = self._now()
        msg = None
        try:
            cred_path = self._write_credential(tid, token)  # 先落盘（败＝零残局）
            with self._store.uow():
                fresh = self._store.get_task(tid)  # 身份卫兵（read-decide-write）
                if (fresh.state not in ("todo", "blocked")
                        or fresh.worker_sid != t.worker_sid):
                    raise _GuardConflict(
                        "conflict", f"{tid}: running 不可重派（session_replaced）")
                if fresh.chain_depth >= self._max_depth:  # 守卫内复核（B7）
                    raise _GuardConflict(
                        "conflict", f"{tid}: chain_depth>={self._max_depth}")
                pool = fresh.root_sid or fresh.dispatcher_sid
                active = sum(1 for x in self._store.tasks_all()
                             if x.id != tid and x.state in _ACTIVE
                             and (x.root_sid or x.dispatcher_sid) == pool)
                if active >= self._quota:
                    raise _GuardConflict(
                        "conflict",
                        f"{tid}: quota[{pool}]: 活动任务 {active}>={self._quota}")
                if t.worker_sid:  # 重派废旧牌（v2 _revoke_worker 语义：防双绑）
                    self._store.delete_credential(t.worker_sid)
                self._store.put_credential(Credential(
                    sid=wsid, token=token, task_id=tid, created_ms=now))
                task = task_transition(fresh, "dispatched", now_ms=now)
                task = dataclasses.replace(
                    task, worker_sid=wsid, env_dir=env, env_base=env_base,
                    delivery=delivery if delivery is not None else fresh.delivery)
                self._store.upsert_task(self._merge_seen(task))
                self._store.append_event(self._ev(
                    now, actor_sid, EV_DISPATCHED, tid, {"worker": wsid}))
                if report:  # 物化审计（ag-0d2daa）
                    self._store.append_event(self._ev(
                        now, actor_sid, "checkout_materialize", tid,
                        {"note": report}))
                if self._msg is not None:  # 凭据指针随迁移同事务落消息
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid=actor_sid, to_sid=wsid, task_id=tid,
                        kind="process",
                        body=f"[agora] credential {tid}\nfile: {cred_path}",
                        dedup_key=f"credential:{tid}:{wsid}")
        except _GuardConflict as exc:  # 卫兵拒：补审计行（原报因保持）
            self._remove_credential_file(tid)  # 牌已废（DB 行随回滚消失）
            if new_env:
                self._reclaim_env(tid, env, actor_sid, "tasksvc:dispatch_guard")  # Req: ag-0e6ca3
            self._reject(tid, actor_sid, exc.code, exc.message)
        except BaseException:  # 清理窗全覆盖（存储/注入器/落盘异常同回收）
            self._remove_credential_file(tid)
            if new_env:
                self._reclaim_env(tid, env, actor_sid, "tasksvc:dispatch_cleanup")  # Req: ag-0e6ca3
            raise
        if msg is not None:
            self._msg.dispatch(msg.id)  # 提交后 at-least-once 补投
        return {"token": token, "worker_sid": wsid, "env": env,
                "credential": cred_path}

    # ---- F2/F23 verify 登记（探索路补登；登记即锁定，worker 永拒） ----

    def update_verify(self, tid, actor_sid, role, cmd, timeout=None):
        t = self._get(tid)
        self._own(t, actor_sid, role)
        if role not in ("root", "dispatcher"):
            self._reject(tid, actor_sid, "forbidden",
                         "worker 不得登记/修改验收命令——主会话派发方或 root"
                         " 登记", ev=EV_DENIED)
        if not str(cmd).strip():
            self._reject(tid, actor_sid, "invalid_argument",
                         "verify 命令为空——task_update 登记非空命令")
        if timeout is not None and _bad_timeout(timeout):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"verify timeout 须为正整数秒（收到 {timeout!r}）")
        now = self._now()
        verify = VerifySpec(
            cmd=cmd, state="registered", registered_by=actor_sid,
            registered_at_ms=now,
            timeout_s=(timeout if timeout is not None else t.verify.timeout_s),
            timeout_source=("task" if timeout is not None
                            else (t.verify.timeout_source
                                  or ("task" if t.verify.timeout_s is not None
                                      else "default"))))
        msg = None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = self._merge_seen(dataclasses.replace(
                fresh, verify=verify, updated_ms=now))
            self._store.upsert_task(task)
            self._store.append_event(self._ev(now, actor_sid, EV_VERIFY, tid))
            if self._msg is not None and task.worker_sid:
                # dedup 键掺登记后 updated_ms：transport 同次登记重试去重，
                # 二次登记（改命令）照推
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid=actor_sid, to_sid=task.worker_sid,
                    task_id=tid, kind="process",
                    body=f"[agora] verify 已登记: {cmd}",
                    dedup_key=f"verify_registered:{tid}:{task.updated_ms}")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return {"ok": True}

    # ---- F11 中止族 ----

    def cancel(self, tid, actor_sid, role, reason=None):
        t = self._get(tid)
        self._own(t, actor_sid, role)
        previous = str(t.cancel_reason or "")
        if t.state == "cancelled" and previous.startswith("dependency_failed"):
            # 自动停链不是成功跳过；显式再 cancel 一次＝确认有意跳过（二次确认）
            now, msg = self._now(), None
            with self._store.uow():
                fresh = self._store.get_task(tid)
                task = self._merge_seen(dataclasses.replace(
                    fresh, cancel_reason=f"dispatcher_confirmed:{previous}",
                    updated_ms=now))
                self._store.upsert_task(task)
                self._store.append_event(self._ev(
                    now, actor_sid, EV_CANCELLED, tid,
                    {"note": f"dependency_skip_confirmed:{previous}"}))
                if self._msg is not None and task.dispatcher_sid:
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid=actor_sid,
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=f"任务 {tid} 已由 {actor_sid} 确认为有意跳过；"
                             f"原原因 {previous}",
                        dedup_key=f"skip_confirmed:{tid}:{now}")
            if msg is not None:
                self._msg.dispatch(msg.id)
            return {"cancelled": 0, "reason": "dependency_skip_confirmed"}
        n = self._cancel_one(tid, actor_sid, reason)
        with self._store.uow():
            self._store.append_event(self._ev(
                self._now(), actor_sid, EV_CANCELLED, tid,
                {"note": f"cancelled={n}" if n else "noop"}))
        return {"cancelled": n}

    def _cancel_one(self, tid, sid, reason=None):
        """中止单任务并递归下级；返回级联取消数（frozen 不动、kids 照走）。"""
        t = self._get(tid)
        total = 0
        if t.state not in _FROZEN:
            if t.state == "done" and t.gate.in_gate:
                self._queue_cancel(tid, reason)  # 门内：原子排队，门尾消费（T9）
            else:
                total = 1 if self._settle_cancel(tid, sid, reason) else 0
        for kid in (x.id for x in self._store.tasks_all()
                    if x.dispatcher_sid == t.worker_sid):
            total += self._cancel_one(kid, sid, reason)
        return total

    def _queue_cancel(self, tid, reason):
        now = self._now()
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh is None or fresh.state != "done" or not fresh.gate.in_gate:
                return  # 竞态窗：已被门尾结算/易主——幂等不重复
            gate = dataclasses.replace(fresh.gate, pending_cancel=True,
                                       pending_cancel_reason=reason)
            self._store.upsert_task(self._merge_seen(dataclasses.replace(
                fresh, gate=gate, updated_ms=now)))

    def _settle_cancel(self, tid, sid, reason) -> bool:
        """单任务结算：非 done 走矩阵；驻留 done（in_gate=False）直结（F36 R2
        ——矩阵 done 无 cancelled 出口）。event＋message＋撤牌同 UoW（§5）。"""
        now, msg, task = self._now(), None, None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh is None or fresh.state in _FROZEN:
                return False  # 竞态：已被他方结算——幂等
            if fresh.state == "done":
                if fresh.gate.in_gate:
                    # 竞态：刚进门——身处本 UoW 内联排队（uow 不可嵌套），
                    # 门尾消费语义（F-4）不变
                    gate = dataclasses.replace(
                        fresh.gate, pending_cancel=True,
                        pending_cancel_reason=reason)
                    self._store.upsert_task(self._merge_seen(dataclasses.replace(
                        fresh, gate=gate, updated_ms=now)))
                    return False
                task = dataclasses.replace(fresh, state="cancelled",
                                           updated_ms=now)
            else:
                task = task_transition(fresh, "cancelled", now_ms=now)
            if reason is not None:
                task = dataclasses.replace(task, cancel_reason=reason)
            task = self._merge_seen(task)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, sid, EV_CANCELLED, tid,
                {"reason": reason} if reason else None))
            self._cancel_task_timers(tid, "cancelled", now)  # Req: F145
            if task.worker_sid:
                self._store.delete_credential(task.worker_sid)
                if self._msg is not None and task.dispatcher_sid:
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid=sid, to_sid=task.dispatcher_sid,
                        task_id=tid, kind="result",
                        body=f"任务 {tid} 已取消（actor={sid}）"
                             + (f"；原因 {reason}" if reason else ""),
                        dedup_key=f"transition:{tid}:cancelled")
        if msg is not None:
            self._msg.dispatch(msg.id)
        self._remove_credential_file(tid)  # 撤牌双删（DB 行外同清 secret 文件）
        self._reclaim_env(tid, task.env_dir, sid, "tasksvc:settle_cancel")  # Req: ag-0e6ca3
        return True

    def stop_after_failed(self, tid):
        """前驱终态失败：停止整条 after 后继链（dependency_failed 带指针）。"""
        stopped, seen, pending = [], {tid}, [tid]  # W-2/F38：显式栈防深链递归
        while pending:
            parent = pending.pop()
            for x in self._store.tasks_all():
                if not x.after or x.after[0] != parent or x.id in seen:
                    continue
                seen.add(x.id)
                if x.state not in _FROZEN:
                    reason = f"dependency_failed:{tid}"
                    self._cancel_one(x.id, "agorad", reason)
                    stopped.append(x.id)
                    with self._store.uow():
                        self._store.append_event(self._ev(
                            self._now(), "gate", EV_DEPENDENCY_STOPPED, x.id,
                            {"note": reason}))
                pending.append(x.id)
        return stopped

    # ---- 状态机接线（T9 门体单跳驱动面） ----

    def _cancel_task_timers(self, tid, state, now):
        """Req: F145 任务终态自动取消 pending timer（身处调用方 UoW；幂等）。"""
        if (self._timers is not None and state in ("merged", "closed",
                                                   "cancelled")):
            self._timers.cancel_task_timers_uow(tid, now, f"task_{state}")

    def transition(self, tid, to, *, actor="agorad", delivery_failure=False,
                   body=None):
        """单跳迁移＋审计事件＋终态 result 消息（同一 UoW，§5）；非法迁移
        拒且零写入。done→blocked 须显式 delivery_failure=True（F98）。"""
        now, msg = self._now(), None
        self._get(tid)  # not_found 契约与其余入口一致（防 None 入 domain）
        try:
            with self._store.uow():
                fresh = self._store.get_task(tid)
                task = self._merge_seen(task_transition(
                    fresh, to, delivery_failure=delivery_failure, now_ms=now))
                self._store.upsert_task(task)
                self._store.append_event(self._ev(now, actor, to, tid))
                self._cancel_task_timers(tid, to, now)  # Req: F145 终态自动取消
                if (to in _TERMINAL_NOTIFY and self._msg is not None
                        and task.dispatcher_sid):
                    # dedup 键掺迁移后 updated_ms：同迁移重放（gate 重试/崩溃
                    # 重排窗）同键去重不重推，跨轮迁移（blocked 循环）照推
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid=actor, to_sid=task.dispatcher_sid,
                        task_id=tid, kind="result",
                        body=body or f"任务 {tid} {to}",
                        dedup_key=f"transition:{tid}:{to}:{task.updated_ms}")
        except InvalidTransition as e:
            self._reject(tid, actor, "conflict", str(e))
        if msg is not None:
            self._msg.dispatch(msg.id)
        return {"ok": True, "state": to}

    def bump_fail_streak(self, tid, *, actor="gate"):
        """质量连败＋1；≥3 且非终态/非 blocked 自动 blocked（复用 domain，
        服务层落库＋终态消息）。"""
        now, msg = self._now(), None
        self._get(tid)  # not_found 契约一致（防 None 入 domain）
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = self._merge_seen(bump_fail_streak(fresh, now_ms=now))
            self._store.upsert_task(task)
            if task.state == "blocked":
                self._store.append_event(self._ev(now, actor, "blocked", tid))
                if self._msg is not None and task.dispatcher_sid:
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid=actor,
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=f"任务 {tid} blocked（质量连败 {task.fail_streak}）",
                        dedup_key=f"transition:{tid}:blocked:{task.updated_ms}")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return {"fail_streak": task.fail_streak, "state": task.state}

    # ---- Req: F124 受控等待 ----

    def wait(self, tid, actor_sid, role, wait_on):
        """仅 root/本任务 dispatcher（worker 全局拒）；wait_on 空/null＝清除；
        依赖已终态立即 resolved＋双消息（worker process＋dispatcher result）。"""
        t = self._get(tid)
        if role not in ("root", "dispatcher"):
            self._reject(tid, actor_sid, "forbidden",
                         "worker 需等待经 post(kind=question) 请派发方",
                         ev=EV_DENIED)
        self._own(t, actor_sid, role)
        if t.state != "running":
            self._reject(tid, actor_sid, "conflict",
                         f"任务 {tid} 当前 {t.state}——受控等待仅 running 任务"
                         "可登记（不改状态机）")
        now = self._now()
        if not wait_on:  # 空串/null＝清除（人工恢复）
            with self._store.uow():
                fresh = self._store.get_task(tid)
                self._store.upsert_task(self._merge_seen(dataclasses.replace(
                    fresh, waiting_on=None, updated_ms=now)))
                self._store.append_event(self._ev(
                    now, actor_sid, EV_WAIT_CLEARED, tid))
            return {"ok": True, "waiting_on": None}
        if wait_on == tid:
            self._reject(tid, actor_sid, "invalid_argument",
                         "wait_on 不得为任务自身")
        dep = self._store.get_task(wait_on)
        if dep is None:
            self._reject(tid, actor_sid, "not_found",
                         f"依赖任务 {wait_on} 不存在")
        if dep.state in _WAIT_DONE:
            msgs = []
            with self._store.uow():  # 事件＋双消息同事务
                fresh = self._store.get_task(tid)
                self._store.append_event(self._ev(
                    now, actor_sid, EV_WAIT_RESOLVED, tid,
                    {"dependency": wait_on,
                     "note": f"dependency_state={dep.state}"}))
                if self._msg is not None:
                    verdict = ("成功——可继续执行"
                               if dep.state in ("merged", "closed")
                               else "未成功——需处置（按裁定继续或取消）")
                    text = (f"受控等待解除：任务 {tid} 等待的依赖 {wait_on}"
                            f" 已结束（{dep.state}，{verdict}）")
                    if fresh.worker_sid:
                        msgs.append(self._msg.record(
                            ts_ms=now, from_sid=actor_sid,
                            to_sid=fresh.worker_sid, task_id=tid,
                            kind="process", body=text)[0])
                    if fresh.dispatcher_sid:
                        msgs.append(self._msg.record(
                            ts_ms=now, from_sid=actor_sid,
                            to_sid=fresh.dispatcher_sid, task_id=tid,
                            kind="result", body=text)[0])
            for m in msgs:
                self._msg.dispatch(m.id)
            return {"ok": True, "resolved": True,
                    "dependency_state": dep.state}
        self._wait_cycle(tid, wait_on, actor_sid)
        with self._store.uow():
            fresh = self._store.get_task(tid)
            self._store.upsert_task(self._merge_seen(dataclasses.replace(
                fresh, waiting_on=wait_on, updated_ms=now)))
            self._store.append_event(self._ev(
                now, actor_sid, EV_WAIT_SET, tid, {"dependency": wait_on}))
        return {"ok": True, "waiting_on": wait_on}

    def clear_wait_if(self, tid, dep) -> bool:
        """CAS 清除（health tick 解析面）：waiting_on==dep 才清——并发换登记
        不抹（活等待丢失防护）。"""
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh is None or fresh.waiting_on != dep:
                return False
            self._store.upsert_task(self._merge_seen(dataclasses.replace(
                fresh, waiting_on=None, updated_ms=self._now())))
            return True

    def _wait_cycle(self, tid, wait_on, sid):
        """等待边组环检测（waiting_on＋after 双边下钻，报路径；显式栈防深链）。"""
        tasks = {x.id: x for x in self._store.tasks_all()}
        seen, stack = {tid}, [(wait_on, [tid])]
        while stack:
            cur, path = stack.pop()
            if cur == tid:
                self._reject(tid, sid, "invalid_argument",
                             f"wait: 依赖成环（{' → '.join(path + [cur])}）"
                             "——修正等待/前驱后重试")
            if cur in seen:
                continue
            seen.add(cur)
            row = tasks.get(cur)
            for nxt in ((row.waiting_on if row else None),
                        (row.after[0] if row and row.after else None)):
                if nxt:
                    stack.append((nxt, path + [cur]))

    # ---- reverify / task_deliver（F101 外部交付确认） ----

    def reverify(self, tid, actor_sid, role):
        """done 驻留重排回验证门：仅 state==done 可重排；external_branch
        awaiting_external 驻留＝设计内稳态（直通不重验）。"""
        t = self._get(tid)
        if role not in ("root", "dispatcher"):
            self._reject(tid, actor_sid, "forbidden", "worker 全局角色永拒",
                         ev=EV_DENIED)
        self._own(t, actor_sid, role)
        if t.state != "done":
            self._reject(tid, actor_sid, "conflict",
                         f"任务 {tid} 当前 {t.state}——仅 done 驻留可 reverify"
                         " 重排（在飞等 worker post(done)；终态无需重验）")
        d = t.delivery
        if (d is not None and d.mode == "external_branch"
                and d.status == "awaiting_external"):
            self._reject(tid, actor_sid, "conflict",
                         f"任务 {tid} done＋awaiting_external 外部交付驻留——"
                         "门体直通不重验；确认/失败走 task_deliver")
        with self._store.uow():
            self._store.append_event(self._ev(
                self._now(), actor_sid, "reverify", tid))
        return {"ok": True, "requeued": True}

    def deliver(self, tid, actor_sid, role, outcome, commit, base=None,
                reason=""):
        """external_branch 交付确认三 outcome：delivered＝done→merged→closed
        全链；failed/non_ff＝done→blocked 交付失败边（F98）。外部 Git 判定经
        gitx.is_ancestor（桩即 T8 面，真执行体归 T9）。"""
        t = self._get(tid)
        if role not in ("root", "dispatcher"):
            self._reject(tid, actor_sid, "forbidden", "worker 全局角色永拒",
                         ev=EV_DENIED)
        self._own(t, actor_sid, role)
        if outcome not in ("delivered", "non_ff", "failed"):
            self._reject(tid, actor_sid, "invalid_argument",
                         f"outcome 须为 delivered/non_ff/failed"
                         f"（收到 {outcome!r}）")
        c = _hex40(commit)
        if c is None:
            self._reject(tid, actor_sid, "invalid_argument",
                         f"commit 须为纯 40 位十六进制完整 sha"
                         f"（收到 {str(commit)[:20]!r}）")
        d = t.delivery
        if d is None or d.mode != "external_branch":
            self._reject(tid, actor_sid, "conflict",
                         f"任务 {tid} 非 external_branch 交付模式——"
                         "task_deliver 仅受理外部交付确认")
        if t.state != "done" or d.status != "awaiting_external":
            self._reject(tid, actor_sid, "conflict",
                         f"任务 {tid} 当前 {t.state}/{d.status}——仅 done＋"
                         "awaiting_external 可确认交付")
        if c != d.commit:
            self._reject(tid, actor_sid, "conflict",
                         f"过期 commit：在案 handoff "
                         f"{str(d.commit)[:12]}，收到 {c[:12]}"
                         "——stale/late 确认拒绝，状态零改写")
        if outcome == "delivered":
            return self._deliver_ok(tid, actor_sid, d, c, reason)
        if outcome == "failed":
            return self._deliver_fail(
                tid, actor_sid, d, str(reason or "").strip()
                or "external_delivery_failed")
        if base is None:
            self._reject(tid, actor_sid, "invalid_argument",
                         "non_ff 须携带 base——外部远端实际 tip 的完整 40 位"
                         " sha（外部派发方 fetch 后供给）")
        b = _hex40(base)
        if b is None:
            self._reject(tid, actor_sid, "invalid_argument",
                         f"base 须为纯 40 位十六进制完整 sha"
                         f"（收到 {str(base)[:20]!r}）")
        try:  # Req: F110 前置 diverged 预判（一次祖先判定替代无效重排）
            if self._gitx.is_ancestor(t.project_dir, b, c):
                self._reject(tid, actor_sid, "conflict",
                             "non_ff 的 base 已是 handoff 祖先——推送应可 "
                             "fast-forward，请核对 non_ff 来源与实际远端 tip"
                             "（零状态改写）")
        except RuntimeError as e:
            return self._deliver_fail(tid, actor_sid, d, "external_base",
                                      str(e))
        return self._deliver_fail(
            tid, actor_sid, d, "diverged",
            "外部基与 handoff 真分叉（F110 换基重排废除）——重派 env 按新 tip"
            " 重建或人工调和；不进门队列、不烧集成 verify")

    def _deliver_ok(self, tid, sid, d, commit, reason):
        now, msgs = self._now(), []
        with self._store.uow():
            fresh = self._store.get_task(tid)
            nd = dataclasses.replace(d, status="delivered",
                                     local_status="not_required", error=None,
                                     conflict_files=None)  # Req: F105 残值必清
            # Req: F144 v2 external 确认不写 delivered_at（managed 专属）——
            # conflict_files=None 显式在键归 adapter 写点规则
            task = task_transition(fresh, "merged", now_ms=now)
            task = reset_fail_streak(task, now_ms=now)
            task = task_transition(task, "closed", now_ms=now + 1)
            task = dataclasses.replace(task, delivery=nd, commit=commit)
            task = self._merge_seen(task)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, sid, "merged", tid,
                {"commit": commit, "note": "external_delivered"
                 + (f"；reason={str(reason).strip()}" if str(reason).strip()
                    else "")}))
            self._store.append_event(self._ev(now + 1, sid, "closed", tid))
            self._cancel_task_timers(tid, "closed", now)  # Req: F145（merged→
            # closed 全链同 UoW——一次收口，幂等面由 pending 过滤承担）
            if fresh.worker_sid:
                self._store.delete_credential(fresh.worker_sid)
            if self._msg is not None and task.dispatcher_sid:
                # Req: F144 closed ts=merged+1ms（merged 窗通知可落窗内）；
                # external deliver 不发 worker milestone＝v2 同款
                for ts_ms, state, text in (
                        (now, "merged", f"任务 {tid} merged @{commit[:7]} "
                         "external delivered——外部派发方确认交付"
                         "（local=not_required）"),
                        (now + 1, "closed", f"任务 {tid} 已 closed；"
                         f"commit={commit}")):
                    msgs.append(self._msg.record(
                        ts_ms=ts_ms, from_sid=sid,
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=text,
                        dedup_key=f"transition:{tid}:{state}:"
                        f"{task.updated_ms}")[0])
        for m in msgs:
            self._msg.dispatch(m.id)
        self._remove_credential_file(tid)  # 撤牌双删（DB 行外同清 secret 文件）
        self._reclaim_env(tid, task.env_dir, sid, "tasksvc:deliver_ack")  # Req: ag-0e6ca3
        return {"ok": True, "outcome": "delivered"}

    def _deliver_fail(self, tid, sid, d, error, reason=""):
        now, msg = self._now(), None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            nd = dataclasses.replace(d, status="failed", error=error,
                                     conflict_files=None)  # Req: F105 残值必清
            task = task_transition(fresh, "blocked", delivery_failure=True,
                                   now_ms=now)  # F98 交付失败专用边
            task = self._merge_seen(dataclasses.replace(task, delivery=nd))
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, sid, "delivery_failed", tid,
                {"error": error, "note": str(reason)[:200]}))
            if self._msg is not None and task.dispatcher_sid:
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid=sid, to_sid=task.dispatcher_sid,
                    task_id=tid, kind="result",
                    body=f"任务 {tid} 外部交付失败（{error}）——已 blocked"
                         "（非质量问题）；核对外部远端后重派或取消",
                    dedup_key=f"transition:{tid}:blocked:{now}")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return {"ok": True, "outcome": "failed", "error": error}

    # ---- last_seen 活动心跳（§3 批量窗 ≤5s） ----

    def touch_seen(self, tid, worker_sid=None, now_ms=None):
        """逐调用只进内存；距上次持久 ≥ 窗口才落一次库（摊销——不逐调用
        独立写事务，gt-a18ef6 写放大口径）。worker_sid 给定时做绑定校验
        （非本任务在役 worker 拒刷——防伪造活性；None＝内部代刷，T10 health
        观测面），返回是否受理。"""
        with self._store.uow():
            t = self._store.get_task(tid)
        if t is None or (worker_sid is not None
                         and t.worker_sid != worker_sid):
            return False
        ms = now_ms if now_ms is not None else self._now()
        if ms > self._seen.get(tid, -1):
            self._seen[tid] = ms
        if ms - self._seen_flushed.get(tid, 0) >= self._seen_window_ms:
            with self._store.uow():
                fresh = self._store.get_task(tid)
                if fresh is not None:
                    self._store.upsert_task(self._merge_seen(fresh))
            self._seen_flushed[tid] = ms
        return True

    def _merge_seen(self, task):
        cand = self._seen.get(task.id)
        if cand is not None and (task.last_seen_ms is None
                                 or cand > task.last_seen_ms):
            return dataclasses.replace(task, last_seen_ms=cand)
        return task

    # ---- 内部 ----

    def _now(self):
        return self._clock()

    def _ev(self, ts_ms, actor, event, task=None, detail=None):
        return Event(seq=0, ts_ms=ts_ms, actor=actor, event=event, task=task,
                     detail=detail)

    def _get(self, tid):
        t = self._store.get_task(tid)
        if t is None:
            raise ContractError("not_found", f"{tid}: 任务不存在", {"task": tid})
        return t

    def _own(self, t, sid, role):
        if role != "root" and t.dispatcher_sid != sid:
            self._reject(t.id, sid, "forbidden",
                         f"{t.id}: 拒绝（role={role} 非本任务派发方）",
                         ev=EV_DENIED)

    def _reject(self, tid, sid, code, reason, ev=EV_REJECTED, detail=None):
        with self._store.uow():  # 审计留痕（调用点必在事务外）
            task = tid if self._store.get_task(tid) is not None else None
            # FK：行未入库（create 拒绝）不带 task_id，tid 入 detail 留痕
            note = {"note": str(reason)[:200]}  # 回显截断（防事件膨胀/注入）
            if task is None:
                note["task"] = tid
            self._store.append_event(self._ev(
                self._now(), sid, ev, task, note))
        raise ContractError(code, f"{tid}: {reason}" if tid else reason,
                            detail if detail is not None else {})

    @staticmethod
    def _repo_key(path):
        """F90 别名归一；None（缺省 absent）原样返回——幂等扫描/继承两用。"""
        return None if path is None else str(pathlib.Path(path).resolve())

    def _new_tid(self):
        ids = {t.id for t in self._store.tasks_all()}
        while (tid := "ag-" + secrets.token_hex(3)) in ids:
            pass
        return tid

    def _worker_parent(self, tid, sid, role):
        """F37 父任务＝该 worker 现役任务（worker_sid==sid、非冻结）取最近
        创建；叶子层（depth+1≥max）建单即拒。"""
        if role != "worker":
            return None
        parent = max((t for t in self._store.tasks_all()
                      if t.worker_sid == sid and t.state not in _FROZEN),
                     key=lambda t: t.created_ms or 0, default=None)
        if parent is None:
            self._reject(tid, sid, "forbidden",
                         "worker 建单须有现役任务上下文（继承父仓与验证配置）",
                         ev=EV_DENIED)
        if parent.chain_depth + 1 >= self._max_depth:
            self._reject(tid, sid, "forbidden",
                         f"叶子层无嵌套派发权——父任务链深 "
                         f"{parent.chain_depth}，子任务 chain_depth 将达 "
                         f"{self._max_depth} 无派发面；拆解需求回报上级建"
                         "兄弟任务", ev=EV_DENIED)
        return parent

    def _worker_matrix(self, tid, sid, vf, ex, dl, parent):
        """F37 worker 写面拒绝矩阵：验收/预算/豁免/门审/支/模型/契约全派发方
        持有（显式传入即拒——verify_base_exempt 含 False）；探索父（verify
        未登记）建子单拒。"""
        faces = (
                ("verify_cmd", vf["cmd"], "worker 不得登记验收命令"),
                ("timeout", vf["timeout"], "worker 不得设置任务级 verify 预算"),
                ("verify_pending", vf["verify_pending"] or None,
                 "verify_pending 豁免旗由派发方持有——缺省继承父任务配置"),
                ("verify_base_exempt", vf["verify_base_exempt"],
                 "worker 不得设置 base-red 探针豁免旗——缺省继承父任务"),
                ("review_cmd", vf["review_cmd"], "worker 不得登记门审命令"),
                ("target_branch", dl["target_branch"],
                 "worker 不得改变后代合并/push 策略——缺省继承父任务"),
                ("model", ex["model"], "worker 不得改变 worker 模型面——缺省"
                 "继承父任务"),
                ("deliverable", dl["deliverable"], "worker 不得指定 "
                 "deliverable（review-only 标记）——缺省继承父任务"))
        for label, value, why in faces:
            if value is not None:
                self._reject(tid, sid, "forbidden", f"{why}（{label}）",
                             ev=EV_DENIED)
        if parent.verify.state == "pending":  # F46 H-1：探索父登记前 fail-fast
            self._reject(tid, sid, "forbidden",
                         "父任务验收未登记（探索中）——子任务待父 verify 登记"
                         "后建单", ev=EV_DENIED)

    def _req_check(self, tid, sid, project_dir, req_id):
        """F34 发放面查重：任务行按数值比（拼写变体不构成新号）；种子＝扫
        project_dir 台账现存最大 F<n>（缺文档视 0）。"""
        if not _REQ_ID_RE.fullmatch(req_id):
            self._reject(tid, sid, "invalid_argument",
                         f"req_id 须为 F<数字> 形态（收到 {req_id!r}）")
        num = int(req_id[1:])
        for x in self._store.tasks_all():
            if x.req_id and int(x.req_id[1:]) == num:
                self._reject(tid, sid, "conflict",
                             f"req_id {req_id} 已发放（任务 {x.id}）")
        try:
            text = (pathlib.Path(project_dir) / _REQ_DOC).read_text(
                encoding="utf-8")
        except (OSError, UnicodeDecodeError, TypeError):
            text = ""
        seed = max((int(n) for n in _REQ_NUM_RE.findall(text)), default=0)
        if num <= seed:
            self._reject(tid, sid, "conflict",
                         f"req_id {req_id} 已在案（docs/reqs 现存最大 F{seed}）")

    def _history_cmd(self, tid, sid, project_dir, quick):
        """F65 quick 历史判定：同仓 merged 非 quick、verify.cmd 非空、多数
        （唯一最高频且 ≥2）原子登记；否则拒报候选。卫生闸过滤（F99）归装配
        注入面（T9+）。"""
        if not quick:
            return None
        counts = {}
        for t in self._store.tasks_all():
            if (t.state in ("merged", "closed") and t.commit and not t.quick
                    and t.project_dir is not None
                    and self._repo_key(t.project_dir) == project_dir
                    and t.verify.cmd.strip()):
                cmd = t.verify.cmd.strip()
                counts[cmd] = counts.get(cmd, 0) + 1
        evid = ""
        if counts:
            top = max(counts.values())
            wins = [c for c, n in counts.items() if n == top]
            if top >= 2 and len(wins) == 1:
                return wins[0]
            evid = "（候选: " + " / ".join(
                f"{c}×{counts[c]}"
                for c in sorted(counts, key=lambda k: (-counts[k], k))) + "）"
        self._reject(tid, sid, "conflict",
                     "quick 验收按账本历史动态判定：证据不足" + evid)

    def _write_credential(self, tid, token):
        """原子建 0600（os.open 带模式——无「先建后 chmod」世界可读窗；同
        access.store.write_ephemeral 口径）；tid 形态断言（纵深：防迁移器
        引入外部 id 后路径逃逸）。"""
        self._check_tid(tid)
        p = pathlib.Path(self._home) / "credentials" / f"{tid}.token"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = (json.dumps({"token": token}) + "\n").encode("utf-8")
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as e:
            self._reject(tid, "agorad", "internal", f"credential_write: {e}")
        return str(p)

    def _remove_credential_file(self, tid):
        self._check_tid(tid)
        try:
            (pathlib.Path(self._home) / "credentials"
             / f"{tid}.token").unlink()
        except OSError:
            pass

    @staticmethod
    def _check_tid(tid):
        if not isinstance(tid, str) or not tid or "/" in tid or "\\" in tid:
            raise ContractError("invalid_argument",
                                f"tid 形态非法（收到 {tid!r}）", {"task": tid})

    def _reclaim_env(self, tid, env_dir, actor, site):  # Req: ag-0e6ca3
        """env 回收前置审计（task._reclaim_env 同款）：任务 id＋resolve 后路径
        ＋调用点标签先落事件流再 remove_env。
        须在 UoW 外调用（内部开自持 uow 落审计，嵌套即 StorageError）；审计
        尽力而为（B 册终闸：多线程 uow 竞争下审计失败拦断回收＝终局已落账
        而 env 无痕滞留/取消对账面报错）——败不断回收，顺序仍先审计后动手。"""
        if not env_dir:
            return
        try:
            with self._store.uow():
                self._store.append_event(self._ev(
                    self._now(), actor, "env_removed", tid,
                    {"env": str(pathlib.Path(env_dir).resolve()),
                     "site": site}))
        except Exception:
            pass  # F54 尽力而为口径：观测面病不断权威回收链
        try:
            self._gitx.remove_env(env_dir)
        except Exception as e:  # 回收败留痕后继续，不拦断级联/通知
            with self._store.uow():
                self._store.append_event(self._ev(
                    self._now(), actor, "reclaim_failed", tid,
                    {"note": str(e)[:200]}))
