"""权威任务状态机＋内存快照＋fail_streak＋审计事件名（设计 §4.1/§4.8）。

单写者内存直接迁移、无事件推导；每次迁移/计数变更 append 一行全量快照
（含 mail_read 游标）；last_seen/mail_read 为内存态、崩溃回退接受；
fail_streak 随快照行持久化，kill -9 不丢。Store 对象构造注入，零磁盘接触。
三线程（httpd 消费/gate 门体/health 定时）并发 mutate＋append：内置锁使
内存迁移与 append 同临界区（§4.8 防线）；cancel 排队（queue_cancel）与
门尾消费（gate_tail）各为单一原子操作——pending_cancel 不悬挂。
"""
import copy
import threading
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
# （cancel／对同任务再 dispatch）；running 第三出口 dispatched 仅限 stalled
# 僵态重派（ag-d1d8c1，门侧先判僵再迁）；done→verify_failed（verify 败）与
# reopened（集成败/非 ff）→running 续修再 done。verify_failed→blocked 不在
# 此列——它只由 fail_streak≥3 自动触发，非手工边。
LEGAL = {
    "todo": {"dispatched", "cancelled"},
    "dispatched": {"running", "cancelled"},
    "running": {"done", "cancelled", "dispatched"},
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
    """权威任务状态：内存迁移＋append_state 落盘；末行快照＝权威（重启 load 恢复）。

    全部读写持内置锁（RLock：gate_tail 结算内重入迁移）；持久化在锁内完成，
    快照行序＝迁移序（§4.8）。
    """

    @staticmethod
    def _normalize_snapshot(snap: dict) -> dict:
        """F91 快照入口：版本/坏行 fail-fast，旧行缺既定字段补缺省。

        只规范内存视图，不回写账本；缺 verify/chain_depth/fail_streak 的最小
        旧行可继续迁移，坏类型在启动点带 tid＋字段报错。"""
        version = snap.get("version")
        tasks = snap.get("tasks", {})
        if version != 1:
            raise ValueError(
                f"snapshot version 须为 1（收到 {version!r}，"
                f"tasks={sorted(tasks)}）")
        if not isinstance(tasks, dict):
            raise ValueError(f"snapshot tasks 须为 dict（收到 {type(tasks).__name__}）")
        out = {}
        for tid, row in tasks.items():
            if not isinstance(row, dict):
                raise ValueError(
                    f"{tid}: 任务行须为 dict（收到 {type(row).__name__}）")
            r = dict(row)
            if r.get("state") not in LEGAL:
                raise ValueError(
                    f"{tid}: state 未知（收到 {r.get('state')!r}，"
                    f"合法={sorted(LEGAL)}）")
            verify = r.get("verify")
            if verify is None:
                verify = {}
            if not isinstance(verify, dict):
                raise ValueError(
                    f"{tid}: verify 须为 dict（收到 {verify!r}）")
            verify = dict(verify)
            verify.setdefault("cmd", "")
            if not isinstance(verify["cmd"], str):
                raise ValueError(
                    f"{tid}: verify.cmd 须为字符串（收到 {verify['cmd']!r}）")
            verify.setdefault("state", "pending")
            verify.setdefault("registered_by", None)
            verify.setdefault("registered_at", None)
            verify.setdefault("timeout", None)
            r["verify"] = verify
            if "chain_depth" not in r:
                r["chain_depth"] = 1
            elif (isinstance(r["chain_depth"], bool)
                    or not isinstance(r["chain_depth"], int)
                    or r["chain_depth"] <= 0):
                raise ValueError(
                    f"{tid}: chain_depth 须为正整数（收到 {r['chain_depth']!r}）")
            if "fail_streak" not in r:
                r["fail_streak"] = 0
            elif (isinstance(r["fail_streak"], bool)
                    or not isinstance(r["fail_streak"], int)
                    or r["fail_streak"] < 0):
                raise ValueError(
                    f"{tid}: fail_streak 须为非负整数（收到 {r['fail_streak']!r}）")
            r.setdefault("quick", False)
            r.setdefault("verify_pending", False)
            r.setdefault("model", None)
            r.setdefault("deliverable", None)
            r.setdefault("delivery", None)  # Req: F94 旧行无投影＝None（仅内存，不回写）
            r.setdefault("client_key", None)  # Req: F95 旧行无幂等键＝None（仅内存，不回写）
            if "root_sid" not in r:
                r["root_sid"] = r.get("dispatcher_sid")
            out[tid] = r
        mail = snap.get("mail_read", {})
        if not isinstance(mail, dict):
            raise ValueError(
                f"snapshot mail_read 须为 dict（收到 {type(mail).__name__}）")
        return {"version": 1, "tasks": out, "mail_read": dict(mail)}

    def __init__(self, store):
        self._store = store
        self._lock = threading.RLock()
        snap = store.load_state()
        snap = (self._normalize_snapshot(snap) if snap is not None
                else {"version": 1, "tasks": {}, "mail_read": {}})
        self._tasks = {tid: dict(t) for tid, t in snap["tasks"].items()}
        self._mail_read = dict(snap["mail_read"])

    def get(self, tid):
        with self._lock:
            return copy.deepcopy(self._tasks[tid])

    def add(self, tid, row):
        """播种新任务行（task_create；行构造归调用方）——迁移矩阵只管既有行。"""
        with self._lock:
            self._tasks[tid] = dict(row)
            self._persist()

    def set_fields(self, tid, **fields):
        """非迁移字段直改（verify 登记/pending-cancel）——不越迁移矩阵。"""
        with self._lock:
            self._tasks[tid].update(fields)
            self._tasks[tid]["updated"] = time.time()
            self._persist()

    def snapshot(self):
        with self._lock:
            return self._snap()

    def touch_mail(self, sid, ts=None):
        """推进 mail_read 游标；缺省 now，显式 ts 只前进不回退（F87 评审 Important）。"""
        with self._lock:
            mark = time.time() if ts is None else ts
            if mark > self._mail_read.get(sid, 0.0):
                self._mail_read[sid] = mark

    def touch_seen(self, tid):
        """last_seen 心跳只进内存（gt-a18ef6 写放大根因修复）：O(调用数) 全量
        快照落盘曾致账本 4.4GB——追加仅随真实迁移；崩溃回退按模块头语义
        接受（同 touch_mail 口径）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if t is not None:
                t["last_seen"] = time.time()

    def transition(self, tid, to, **fields):
        with self._lock:
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

    def queue_cancel(self, tid, cancel_reason=None) -> bool:
        """门临界段 cancel 排队（持锁判定＋置位一体）：True=待门尾消费。

        done 一律排队（迁移矩阵 done 无 cancelled 出口，必经门尾）；与 gate_tail
        同锁互斥——门尾结算前后到达的排队要么被取走、要么待下一门跑/重启重排
        （F-4）消费，判定与置位不撕裂。
        """
        with self._lock:
            t = self._tasks[tid]
            if t["state"] != EV_DONE:
                return False
            t["pending_cancel"] = True
            if cancel_reason is not None:
                t["pending_cancel_reason"] = cancel_reason
            else:
                t.pop("pending_cancel_reason", None)  # 后来的主动取消覆盖自动停链原因
            t["updated"] = time.time()
            self._persist()
            return True

    def settle_parked_cancel(self, tid, cancel_reason=None) -> bool:
        """F36 R2：驻留 done（门已走完，in_gate=False）的排队取消直结 cancelled。

        verify_overrun 驻留后门尾不再可达——排队必悬挂至重启重跑整轮
        verify，取消方直结防悬挂。在门内（in_gate=True）不直结：门尾消费
        语义（F-4）不变。
        """
        with self._lock:
            t = self._tasks[tid]
            if t["state"] != EV_DONE or t.get("in_gate"):
                return False
            t.pop("pending_cancel", None)
            t.pop("pending_cancel_reason", None)
            t["state"] = EV_CANCELLED
            t["in_gate"] = False
            t["updated"] = time.time()
            if cancel_reason:
                t["cancel_reason"] = cancel_reason
            self._persist()
            return True

    def clear_gate_latches(self):
        """启动清残留门闩：门线程不跨进程存活，kill -9 残闩是撒谎闩（二批决策）。"""
        with self._lock:
            for tid, t in self._tasks.items():
                if t.get("in_gate"):
                    t["in_gate"] = False
                    self._store.append_event({"ts": time.time(), "actor": "agorad",
                                              "event": "gate_latch_reset", "task": tid})
            self._persist()

    def gate_tail(self, tid) -> dict:
        """门尾原子结算：pending 取走＋终态写入同临界区（cancel×门尾 TOCTOU 封闭）。

        pending 且非 merged→cancelled；merged→closed（不逆转历史）；余者仅清
        门闩。返回结算后行快照（含 env/会话指针，供门侧副作用）。
        """
        with self._lock:
            t = self._tasks[tid]
            pending = bool(t.pop("pending_cancel", False))
            cancel_reason = t.pop("pending_cancel_reason", None)
            if pending and t["state"] != EV_MERGED:
                fields = {"cancel_reason": cancel_reason} if cancel_reason else {}
                if t["state"] == EV_DONE:
                    # F36 R2：驻留 done（verify_overrun 超硬顶）＋排队取消——
                    # 矩阵 done 无 cancelled 出口，门尾直结防 InvalidTransition 吞取消
                    t["state"] = EV_CANCELLED
                    t["in_gate"] = False
                    t["updated"] = time.time()
                    t.update(fields)
                    self._persist()
                else:
                    self.transition(tid, EV_CANCELLED, in_gate=False, **fields)
            elif t["state"] == EV_MERGED:
                self.transition(tid, EV_CLOSED, in_gate=False)
            else:
                t["in_gate"] = False
                t["updated"] = time.time()
                self._persist()
            return copy.deepcopy(t)

    def fail_streak_bump(self, tid):
        with self._lock:
            t = self._tasks[tid]
            t["fail_streak"] = t.get("fail_streak", 0) + 1
            t["updated"] = time.time()
            if t["fail_streak"] >= 3 and t["state"] not in TERMINAL | {"blocked"}:
                t["state"] = EV_BLOCKED  # 连续 3 次质量败→自动 blocked
            self._persist()

    def fail_streak_reset(self, tid):
        with self._lock:
            t = self._tasks[tid]
            t["fail_streak"] = 0
            t["updated"] = time.time()
            self._persist()

    def _snap(self):
        return {"version": 1, "tasks": copy.deepcopy(self._tasks),
                "mail_read": dict(self._mail_read)}

    def _persist(self):
        self._store.append_state(self._snap())  # 调用方持锁：迁移＋append 同临界区
