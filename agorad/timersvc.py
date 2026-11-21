"""Req: F145 — v3 timer 应用服务（opencode-agora v3 P5-T14，设计 §6）。

契约：after_seconds/due_at 互斥（既传/都不传拒）；30 天上限（相对 now 的
due 距离）；body 1-2000 可打印；每任务 ≤16 pending；client_key 幂等（同键
同载荷返原 timer——载荷含原始 after_seconds，经 due_ms-created_ms 重建比对；
异载荷 409）。权限：worker 限本任务建且仅取消自建（created_by）；dispatcher
归属终审（task.dispatcher_sid）；root 放行；越权拒＋auth_denied 审计。

角色寻址：target 仅 worker|dispatcher；触发时解析当前 sid（task.worker_sid/
dispatcher_sid 现值——换号跟随新 sid）；worker 目标仅 dispatched/running/
verify_failed/reopened 触发，其余态保持 pending/overdue；任务终态（merged/
closed/cancelled）自动取消＋审计（tasksvc 终态路径同 UoW 内调
cancel_task_timers_uow——幂等，无 pending 零写入）。

两级扫描（§6）：health 300s 归 T10；timer 30s（或 next due 提前醒——
next_wait_seconds 收敛 min(默认, due-now)）；每批 ≤32 按 due/id 升序；
overdue 启动即补发分批（catch_up 循环至无进展——缺 worker/状态门控的
overdue 不死循环、零数据库写入、不误 escalate）；时钟前跳只发一次（fired
终态无出边）、回拨等待不重发（due 判定按当前钟，已 fired 不回头）。

触发事务（§6）：pending→firing＋message（dedup_key=timer id，唯一约束）
＋receipt＋event 同一 UoW→提交后 Outbox 在线注入（dispatch）→fired 第二
UoW。三崩溃窗：firing 前回滚零残留（storage uow 原子性）；消息提交后按
timer_id 对账补记 fired 不产第二消息（reconcile_firing：firing 行＋消息在
案→fired；消息缺（理论不可达）→回 pending 重走，dedup 兜底）；在线结果
未落账允许 at-least-once 重投（messaging lease 语义，本服务不重复建消息）。

取消竞态：pending 可取消；firing/fired 返回 409 不伪造取消；cancelled
幂等回 ok。观测：projection(task)＝board 明细 pending/overdue/recent 三键
（/v3 正名面注入——legacy 兼容面形状钉死零扰动）；overdue_rows＝doctor
摘要 [[id, overdue_s]] 最僵在前；消息信封首行带 timer id 供去重。

分层（§1）：TimerRepo/Notifier 构造注入——不 import access/storage（架构
守卫 test_architecture.py），零 SQL；OC 调用必在 UoW 外（经 messaging
dispatch，提交后 at-least-once）。
"""
import secrets
import time
import typing

from agorad.contracts import ContractError
from agorad.domain import (Event, Task, Timer, TimerState, epoch_ms,
                           timer_transition)

MAX_PENDING_PER_TASK = 16   # 每任务 pending 上限（设计 §6）
BATCH = 32                  # 每批 ≤32（设计 §6）
MAX_AHEAD_MS = 30 * 86400 * 1000  # 30 天上限
BODY_MAX = 2000
KEY_MAX = 200
DEFAULT_INTERVAL_S = 30      # timer 扫描节奏（health 300s 两级之外）
WORKER_FIRE_STATES = frozenset({"dispatched", "running", "verify_failed",
                                "reopened"})
TASK_TERMINAL = frozenset({"merged", "closed", "cancelled"})

EV_CREATED = "timer_created"
EV_FIRED = "timer_fired"
EV_CANCELLED = "timer_cancelled"
EV_AUTOCANCELLED = "timer_autocancelled"
EV_RECONCILED = "timer_reconciled"
EV_DENIED = "auth_denied"


class TimerRepo(typing.Protocol):
    """storage 公开仓库接口（结构化；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def get_task(self, tid) -> Task | None: ...
    def get_timer(self, tid) -> Timer | None: ...
    def timers_all(self) -> list[Timer]: ...
    def timers_due(self, now_ms, limit=BATCH) -> list[Timer]: ...
    def timers_by_status(self, status) -> list[Timer]: ...
    def timers_for_task(self, task_id) -> list[Timer]: ...
    def next_due_ms(self): ...
    def message_for_timer(self, timer_id): ...
    def append_event(self, ev: Event): ...


class Notifier(typing.Protocol):
    """messaging.MESSAGINGSERVICE 结构化满足（record 身处调用方 UoW；
    dispatch 必在提交后——timer 消息走 WAKE 通道在线注入）。"""

    def record(self, *, ts_ms, from_sid, to_sid, task_id, kind, body,
               dedup_key=None, timer_id=None): ...
    def dispatch(self, mid, *, escalate_kind=None) -> bool: ...


class TimerService:
    """timer 域应用服务；无锁无线程（扫描节奏归装配线程，tick 驱动）。"""

    def __init__(self, store: TimerRepo, messaging: Notifier, *, clock=None,
                 injector=None, batch=BATCH, max_pending=MAX_PENDING_PER_TASK):
        self._store, self._msg = store, messaging
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._injector = injector  # tests.v3_harness.FaultInjector（崩窗）
        self._batch, self._max_pending = batch, max_pending

    # ---- timer_create（断言1/2/3）----

    def create(self, actor_sid, role, params) -> dict:
        after, due_at = params.get("after_seconds"), params.get("due_at")
        body, key = params.get("body"), params.get("client_key")
        now = self._clock()
        if (after is None) == (due_at is None):  # 互斥：恰传其一
            raise ContractError(
                "invalid_argument",
                "after_seconds/due_at 须恰传其一（互斥）",
                {"after_seconds": after, "due_at": due_at})
        if after is not None:
            if after <= 0 or after > MAX_AHEAD_MS // 1000:
                raise ContractError(
                    "invalid_argument",
                    f"after_seconds 须为 1-{MAX_AHEAD_MS // 1000} 秒"
                    f"（收到 {after!r}）", {"after_seconds": after})
            due = now + after * 1000
        else:
            due = due_at
            if due > now + MAX_AHEAD_MS:
                raise ContractError(
                    "invalid_argument",
                    f"due_at 超出 30 天上限（最远 {now + MAX_AHEAD_MS}）",
                    {"due_at": due_at})
            # due 在过去合法：建即 overdue，扫描/启动补发面接管
        if body is not None and (not isinstance(body, str) or not body
                                 or len(body) > BODY_MAX
                                 or not body.isprintable()):
            raise ContractError(
                "invalid_argument",
                f"body 须为 1-{BODY_MAX} 可打印字符（收到 {len(body or '')} 字符）")
        if key is not None and (not key or len(key) > KEY_MAX
                                or not key.isprintable()):
            raise ContractError(
                "invalid_argument",
                f"client_key 须为非空字符串（≤{KEY_MAX} 字符、无控制字符）")
        task = self._store.get_task(params["task"])
        if task is None:
            raise ContractError("not_found",
                                f"{params['task']}: 任务不存在")
        self._permit_create(actor_sid, role, task)
        pending = sum(1 for x in self._store.timers_for_task(task.id)
                      if x.state is TimerState.PENDING)
        if pending >= self._max_pending:
            raise ContractError(
                "conflict",
                f"{task.id}: pending timer {pending}>={self._max_pending}"
                "——先取消或等触发")
        if key is not None:  # 幂等扫描（入账前；载荷＝任务/目标/正文/原始时刻）
            for x in self._store.timers_all():
                if x.client_key != key:
                    continue
                if not self._same_payload(x, task.id, params, due):
                    raise ContractError(
                        "conflict",
                        f"client_key {key!r} 已用（timer {x.id}）且载荷不同"
                        "——同键载荷须一致")
                return self._row(x)
        tid = self._new_tid()
        timer = Timer(id=tid, task_id=task.id, target_role=params["target"],
                      due_ms=due, state=TimerState.PENDING, created_ms=now,
                      body=body or "", created_by=actor_sid, client_key=key)
        with self._store.uow():
            self._store.append_event(self._ev(
                now, actor_sid, EV_CREATED, task.id,
                {"timer": tid, "target": params["target"], "due_ms": due}))
            self._store.upsert_timer(timer)
        return self._row(timer)

    def _same_payload(self, existing, task_id, params, due):
        """同键载荷比对：task/target/body＋原始时刻（after_seconds 经
        due-created 重建；due_at 原值直比）。"""
        if (existing.task_id != task_id
                or existing.target_role != params["target"]
                or existing.body != (params.get("body") or "")):
            return False
        if params.get("due_at") is not None:
            return existing.due_ms == params["due_at"]
        after = params.get("after_seconds")
        return (existing.created_ms is not None
                and existing.due_ms == existing.created_ms + after * 1000)

    def _permit_create(self, actor_sid, role, task):
        ok = (role == "root"
              or (role == "dispatcher" and task.dispatcher_sid == actor_sid)
              or (role == "worker" and task.worker_sid == actor_sid))
        if not ok:
            self._deny(actor_sid, role, "timer_create", task.id,
                       "worker 限本任务建／dispatcher 归属终审／root 放行")

    # ---- timer_cancel（断言2/6）----

    def cancel(self, actor_sid, role, timer_id) -> dict:
        timer = self._store.get_timer(timer_id)
        if timer is None:
            raise ContractError("not_found", f"timer {timer_id} 不存在",
                                {"timer": timer_id})
        task = self._store.get_task(timer.task_id)
        if task is None:
            raise ContractError("not_found",
                                f"timer {timer_id} 任务行缺失")
        ok = (role == "root"
              or (role == "worker" and timer.created_by == actor_sid)
              or (role == "dispatcher" and task.dispatcher_sid == actor_sid))
        if not ok:
            self._deny(actor_sid, role, "timer_cancel", timer.task_id,
                       "worker 仅取消自建／dispatcher 归属终审／root 放行")
        if timer.state in (TimerState.FIRING, TimerState.FIRED):
            raise ContractError(
                "conflict",
                f"timer {timer_id} 已 {timer.state.value}——不伪造取消",
                {"timer": timer_id, "status": timer.state.value})
        if timer.state is TimerState.CANCELLED:  # 幂等：取消态回 ok
            return self._row(timer)
        now = self._clock()
        with self._store.uow():
            fresh = self._store.get_timer(timer_id)  # read-decide-write
            if fresh is None or fresh.state is not TimerState.PENDING:
                if fresh is not None and fresh.state is TimerState.CANCELLED:
                    return self._row(fresh)
                raise ContractError(  # 竞态窗：firing/fired 不伪造取消
                    "conflict",
                    f"timer {timer_id} 已 "
                    f"{(fresh.state if fresh else timer.state).value}"
                    "——不伪造取消")
            self._store.upsert_timer(
                timer_transition(fresh, TimerState.CANCELLED, now_ms=now))
            self._store.append_event(self._ev(
                now, actor_sid, EV_CANCELLED, fresh.task_id,
                {"timer": timer_id}))
        return {"timer": timer_id, "due_ms": timer.due_ms,
                "status": TimerState.CANCELLED.value}

    # ---- 扫描与触发（断言3/4/5/9）----

    def scan(self) -> int:
        """单批扫描：先对账 firing 残留，再按 due/id 升序触发 ≤batch 个
        pending。门控不过（缺 worker/态不合）零数据库写入——保持 overdue
        可见待环境恢复，不误 escalate。返回本轮触发数。"""
        now = self._clock()
        self.reconcile_firing(now)
        fired = 0
        for tm in self._store.timers_due(now, self._batch):
            if self._fire(tm, now):
                fired += 1
        return fired

    def catch_up(self) -> int:
        """启动补发（§6 overdue 启动即补发分批）：循环单批直至无进展。"""
        total = 0
        for _ in range(1000):  # 防御界：门控不过的 overdue 不构成进展
            n = self.scan()
            total += n
            if n == 0:
                break
        return total

    def reconcile_firing(self, now=None):
        """firing 残留对账（§6 崩窗2）：消息在案（timer_id 唯一）→补记
        fired 不产第二消息；消息缺（理论不可达）→回 pending 重走（dedup
        兜底）。"""
        now = self._clock() if now is None else now
        for tm in self._store.timers_by_status(TimerState.FIRING.value):
            has_msg = self._store.message_for_timer(tm.id) is not None
            to = (TimerState.FIRED if has_msg else TimerState.PENDING)
            with self._store.uow():
                fresh = self._store.get_timer(tm.id)  # read-decide-write
                if fresh is None or fresh.state is not TimerState.FIRING:
                    continue
                self._store.upsert_timer(
                    timer_transition(fresh, to, now_ms=now))
                self._store.append_event(self._ev(
                    now, "agorad", EV_RECONCILED, fresh.task_id,
                    {"timer": fresh.id, "action": to.value}))

    def next_wait_seconds(self, default=DEFAULT_INTERVAL_S):
        """扫描线程醒点：min(默认节奏, 距最近 pending due)——next due 提前
        醒（时钟注入可测）；无 pending＝默认；已 overdue＝0。"""
        due = self._store.next_due_ms()
        if due is None:
            return float(default)
        return max(0.0, min(float(default), (due - self._clock()) / 1000.0))

    def _fire(self, tm, now) -> bool:
        task = self._store.get_task(tm.task_id)
        if task is None:
            return False
        if tm.target_role == "worker":
            # 缺 worker（未派发）保持 overdue 可见；态门外（done/blocked/
            # todo）保持 pending/overdue——均零写入（断言3/9）
            if not task.worker_sid or task.state not in WORKER_FIRE_STATES:
                return False
            to_sid = task.worker_sid
        else:
            if not task.dispatcher_sid:
                return False
            to_sid = task.dispatcher_sid
        self._fire_fp("timer_fire", "before", timer=tm.id)  # 崩窗1：firing 前
        msg = None
        with self._store.uow():  # pending→firing＋message＋receipt＋event 同事务
            fresh = self._store.get_timer(tm.id)
            if fresh is None or fresh.state is not TimerState.PENDING:
                return False  # 竞态：已被取消/触发——幂等让位
            self._store.upsert_timer(
                timer_transition(fresh, TimerState.FIRING, now_ms=now))
            msg, _ = self._msg.record(
                ts_ms=now, from_sid="agorad", to_sid=to_sid,
                task_id=tm.task_id, kind="timer",
                body=self._compose_body(fresh), dedup_key=fresh.id,
                timer_id=fresh.id)
            self._store.append_event(self._ev(
                now, "agorad", EV_FIRED, tm.task_id,
                {"timer": tm.id, "to": to_sid}))
        self._fire_fp("timer_firing", "after", timer=tm.id)  # 崩窗2：提交后
        self._msg.dispatch(msg.id)  # 提交后 Outbox 在线注入（at-least-once）
        with self._store.uow():  # 崩窗3 后半：在线结果落账独立窗
            fresh = self._store.get_timer(tm.id)
            if fresh is not None and fresh.state is TimerState.FIRING:
                self._store.upsert_timer(timer_transition(
                    fresh, TimerState.FIRED, now_ms=self._clock()))
        self._fire_fp("timer_fire", "after", timer=tm.id)
        return True

    @staticmethod
    def _compose_body(timer):
        """信封首行带 timer id（断言8：供去重）；正文原样附后。"""
        head = f"[agora] timer {timer.id}"
        return head + ("\n" + timer.body if timer.body else "")

    # ---- 任务终态自动取消（断言3/6；tasksvc 终态路径身处其 UoW 调用）----

    def cancel_task_timers_uow(self, task_id, now_ms, reason) -> int:
        """pending 全数取消＋审计；幂等（无 pending 零写入）。身处调用方
        UoW（uow 不可嵌套）——不自行开事务。"""
        rows = [x for x in self._store.timers_for_task(task_id)
                if x.state is TimerState.PENDING]
        for tm in rows:
            self._store.upsert_timer(
                timer_transition(tm, TimerState.CANCELLED, now_ms=now_ms))
        if rows:
            self._store.append_event(self._ev(
                now_ms, "agorad", EV_AUTOCANCELLED, task_id,
                {"count": len(rows), "reason": reason,
                 "timers": [x.id for x in rows]}))
        return len(rows)

    # ---- 观测投影（断言8；/v3 正名面注入，legacy 兼容面零扰动）----

    def projection(self, task_id, now_ms=None):
        """board(task) 三键投影：pending/overdue（含 due）/recent（最近
        历史 8 条）。"""
        now = self._clock() if now_ms is None else now_ms
        rows = sorted(self._store.timers_for_task(task_id),
                      key=lambda x: (x.created_ms or 0, x.id))
        pending = [{"id": x.id, "due_ms": x.due_ms}
                   for x in rows if x.state is TimerState.PENDING]
        return {
            "pending": pending,
            "overdue": [p for p in pending if p["due_ms"] <= now],
            "recent": [{"id": x.id, "status": x.state.value,
                        "due_ms": x.due_ms, "fired_ms": x.fired_ms}
                       for x in reversed(rows)
                       if x.state is not TimerState.PENDING][:8]}

    def overdue_rows(self, now_ms=None):
        """doctor overdue 摘要 [[id, overdue_s]]（age_rank 同形，最僵在前）。"""
        now = self._clock() if now_ms is None else now_ms
        out = [[x.id, (now - x.due_ms) // 1000]
               for x in self._store.timers_by_status(TimerState.PENDING.value)
               if x.due_ms <= now]
        return sorted(out, key=lambda kv: -kv[1])

    # ---- 内部 ----

    @staticmethod
    def _row(timer):
        return {"timer": timer.id, "due_ms": timer.due_ms,
                "status": timer.state.value}

    def _new_tid(self):
        ids = {x.id for x in self._store.timers_all()}
        while (tid := "tm-" + secrets.token_hex(3)) in ids:
            pass
        return tid

    def _ev(self, ts_ms, actor, event, task=None, detail=None):
        return Event(seq=0, ts_ms=ts_ms, actor=actor, event=event, task=task,
                     detail=detail)

    def _deny(self, actor_sid, role, tool, task_id, why):
        with self._store.uow():  # 越权审计（断言2）
            self._store.append_event(self._ev(
                self._clock(), actor_sid, EV_DENIED, task_id,
                {"tool": tool, "note": f"role={role}"}))
        raise ContractError("forbidden", f"{tool} 拒绝：{why}")

    def _fire_fp(self, name, phase, **context):
        if self._injector is not None:
            self._injector.fire(name, phase, **context)
