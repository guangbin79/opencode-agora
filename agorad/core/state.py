"""权威任务状态机＋内存快照＋fail_streak＋审计事件名（设计 §4.1/§4.8）。

单写者内存直接迁移、无事件推导；每次迁移/计数变更 append 一行全量快照
（含 mail_read 游标）；last_seen/mail_read 为内存态、崩溃回退接受；
fail_streak 随快照行持久化，kill -9 不丢。Store 对象构造注入，零磁盘接触。
"""
import copy
import time

# 审计事件名常量：事件名＝里程碑迁移后的状态名（F8：迁移状态＋审计）
EV_DISPATCHED = "dispatched"
EV_RUNNING = "running"
EV_DONE = "done"
EV_VERIFY_FAILED = "verify_failed"
EV_REOPENED = "reopened"
EV_MERGED = "merged"
EV_CLOSED = "closed"
EV_BLOCKED = "blocked"
EV_CANCELLED = "cancelled"

# 迁移矩阵：主链 todo→dispatched→running→done→merged→closed；blocked 双出口
# （cancel／对同任务再 dispatch）；done→verify_failed（verify 败）与 reopened
# （集成败/非 ff）→running 续修再 done。verify_failed→blocked 不在此列——
# 它只由 fail_streak≥3 自动触发，非手工边。
LEGAL = {
    "todo": {"dispatched", "cancelled"},
    "dispatched": {"running", "cancelled"},
    "running": {"done", "cancelled"},
    "done": {"merged", "verify_failed", "reopened"},
    "verify_failed": {"running", "cancelled"},
    "reopened": {"running", "cancelled"},
    "merged": {"closed"},
    "blocked": {"cancelled", "dispatched"},
    "cancelled": set(),
    "closed": set(),
}
TERMINAL = {"cancelled", "closed"}


class InvalidTransition(Exception):
    def __init__(self, tid, frm, to, legal):
        self.tid, self.frm, self.to, self.legal = tid, frm, to, legal
        super().__init__(f"{tid}: {frm}->{to} 非法（合法后继：{sorted(legal)}）")


class State:
    """权威任务状态：内存迁移＋append_state 落盘；末行快照＝权威（重启 load 恢复）。"""

    def __init__(self, store):
        self._store = store
        snap = store.load_state() or {}
        self._tasks = {tid: dict(t) for tid, t in snap.get("tasks", {}).items()}
        self._mail_read = dict(snap.get("mail_read", {}))

    def get(self, tid):
        return copy.deepcopy(self._tasks[tid])

    def add(self, tid, row):
        """播种新任务行（task_create；行构造归调用方）——迁移矩阵只管既有行。"""
        self._tasks[tid] = dict(row)
        self._persist()

    def set_fields(self, tid, **fields):
        """非迁移字段直改（verify 登记/pending-cancel）——不越迁移矩阵。"""
        self._tasks[tid].update(fields)
        self._tasks[tid]["updated"] = time.time()
        self._persist()

    def snapshot(self):
        return {"version": 1, "tasks": copy.deepcopy(self._tasks),
                "mail_read": dict(self._mail_read)}

    def touch_mail(self, sid):
        self._mail_read[sid] = time.time()

    def transition(self, tid, to, **fields):
        t = self._tasks[tid]
        legal = LEGAL.get(t["state"], set())
        if to not in legal:
            raise InvalidTransition(tid, t["state"], to, legal)
        t["state"] = to
        t["updated"] = time.time()
        t.update(fields)
        if to == EV_DISPATCHED:
            t["fail_streak"] = 0  # 对同任务再 dispatch 清零连败
        self._persist()

    def fail_streak_bump(self, tid):
        t = self._tasks[tid]
        t["fail_streak"] = t.get("fail_streak", 0) + 1
        t["updated"] = time.time()
        if t["fail_streak"] >= 3 and t["state"] not in TERMINAL | {"blocked"}:
            t["state"] = EV_BLOCKED  # 连续 3 次质量败→自动 blocked
        self._persist()

    def fail_streak_reset(self, tid):
        t = self._tasks[tid]
        t["fail_streak"] = 0
        t["updated"] = time.time()
        self._persist()

    def _persist(self):
        self._store.append_state(self.snapshot())
