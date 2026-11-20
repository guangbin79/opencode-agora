"""Req: F131 — v3 domain 实体与状态机（opencode-agora v3 P1-T4）。

断言清单（任务页 ag-ba4a47）：①实体字段集与设计 §3 一致（含 legacy_extra_json
只读位）；②任务状态机全量移植 v2 LEGAL（合法逐一通过／非法逐一 InvalidTransition
且零变更／dispatched 清 fail_streak／连败≥3 自动 blocked／done→blocked 仅交付
失败专用边）；③时间毫秒往返锚；④DeliveryPolicy 封闭映射；⑤Timer/Approval
枚举与非法迁移拒绝；⑥架构守卫在 tests/test_architecture.py。
"""
import dataclasses
import inspect
import unittest

from agorad import domain
from agorad.core import state as v2state

POLICY = domain.DeliveryPolicy()
ALL_TASK_STATES = sorted(domain.TASK_LEGAL)


def task(**kw) -> domain.Task:
    return domain.Task(id=kw.pop("id", "ag-dom01"), **kw)


def timer(**kw) -> domain.Timer:
    return domain.Timer(id=kw.pop("id", "tm-1"), **kw)


def approval(**kw) -> domain.Approval:
    return domain.Approval(id=kw.pop("id", "ap-1"), **kw)


def field_names(cls) -> set:
    return {f.name for f in dataclasses.fields(cls)}


class TestEntities(unittest.TestCase):
    """断言①：实体字段集与设计 §3 一致。"""

    def test_task_field_set(self):
        # 类目注释见 agorad/domain.py Task（§3：身份/归属/生命周期/依赖/幂等/活动）
        self.assertEqual(field_names(domain.Task), {
            "id", "state", "title", "goal", "req_id", "model", "quick",
            "deliverable", "chain_depth", "root_sid", "dispatcher_sid",
            "worker_sid", "created_ms", "updated_ms", "fail_streak",
            "cancel_reason", "after", "waiting_on", "client_key",
            "last_seen_ms", "project_dir", "target_branch", "env_dir",
            "env_base", "commit", "rebased_from", "verify",
            "verify_pending", "verify_base_exempt", "review_cmd",
            "delivery", "gate", "probe", "legacy_extra_json"})

    def test_value_object_field_sets(self):
        expect = {
            domain.VerifySpec: {"cmd", "state", "registered_by",
                                "registered_at_ms", "timeout_s",
                                "timeout_source"},
            domain.DeliveryState: {"mode", "status", "commit", "base",
                                   "target_branch", "attempt",
                                   "local_status", "error"},
            domain.GateState: {"in_gate", "gate_env", "pending_cancel",
                               "pending_cancel_reason", "gate_artifact",
                               "review_cache", "review_fail_streak",
                               "gate_resource"},
            domain.Timer: {"id", "task_id", "target_role", "due_ms",
                           "state", "created_ms", "fired_ms"},
            domain.Approval: {"id", "task_id", "requester_sid", "backend",
                              "state", "question", "metadata",
                              "native_request_id", "attempted_decision",
                              "deadline_ms", "orphan_reason", "created_ms",
                              "resolved_ms"},
            domain.Message: {"id", "ts_ms", "task_id", "kind", "from_sid",
                             "to_sid", "body", "dedup_key", "replay_of"},
            domain.DeliveryReceipt: {"message_id", "status", "attempts",
                                     "last_error", "email_status",
                                     "lease_until_ms", "updated_ms"},
            domain.MailCursor: {"sid", "last_read_ms"},
            domain.Event: {"seq", "ts_ms", "actor", "event", "task",
                           "detail"},
            domain.Artifact: {"task_id", "phase", "path", "cmd", "rc",
                              "failure_kind", "duration_s", "bytes", "error",
                              "started_at_ms", "ended_at_ms"},
            domain.Credential: {"sid", "token", "task_id", "lineage",
                                "created_ms"},
        }
        for cls, names in expect.items():
            self.assertEqual(field_names(cls), names, cls.__name__)

    def test_legacy_extra_readonly(self):
        t = task()
        self.assertEqual(t.legacy_extra_json, "{}")  # 缺省空——域逻辑不读
        with self.assertRaises(dataclasses.FrozenInstanceError):
            t.legacy_extra_json = '{"old_field": 1}'


class TestTaskStateMachine(unittest.TestCase):
    """断言②：v2 LEGAL 全量移植。"""

    def test_matrix_equals_v2(self):
        self.assertEqual(
            {k: set(v) for k, v in domain.TASK_LEGAL.items()},
            {k: set(v) for k, v in v2state.LEGAL.items()})

    def test_every_legal_transition_passes(self):
        for frm, targets in domain.TASK_LEGAL.items():
            for to in targets:
                # done→blocked 是合法边但为交付失败专用——须携带专用旗
                kw = ({"delivery_failure": True}
                      if (frm, to) == ("done", "blocked") else {})
                t = domain.task_transition(task(state=frm), to,
                                           now_ms=1_700_000_000_000, **kw)
                self.assertEqual((t.state, t.updated_ms), (to, 1_700_000_000_000))

    def test_every_illegal_transition_rejected_unchanged(self):
        for frm in ALL_TASK_STATES:
            for to in ALL_TASK_STATES:
                if to in domain.TASK_LEGAL[frm]:
                    continue
                t0 = task(state=frm, fail_streak=2, cancel_reason="keep")
                with self.assertRaises(domain.InvalidTransition, msg=f"{frm}->{to}"):
                    domain.task_transition(t0, to)
                self.assertEqual(t0, task(state=frm, fail_streak=2,
                                          cancel_reason="keep"))  # 零变更

    def test_dispatched_resets_fail_streak(self):
        t = domain.task_transition(task(state="blocked", fail_streak=3),
                                   "dispatched")
        self.assertEqual(t.fail_streak, 0)

    def test_fail_streak_auto_block(self):
        t = task(state="verify_failed")
        for _ in range(2):
            t = domain.bump_fail_streak(t)
        self.assertEqual(t.state, "verify_failed")  # 连败 2 未达阈值
        t = domain.bump_fail_streak(t)
        self.assertEqual((t.state, t.fail_streak), ("blocked", 3))  # ≥3 自动
        t = domain.bump_fail_streak(task(state="blocked", fail_streak=3))
        self.assertEqual((t.state, t.fail_streak), ("blocked", 4))  # 不重复迁
        t = domain.bump_fail_streak(task(state="closed", fail_streak=2))
        self.assertEqual(t.state, "closed")  # 终态不自动 blocked
        self.assertEqual(domain.reset_fail_streak(t).fail_streak, 0)

    def test_done_to_blocked_is_delivery_failure_edge(self):
        t0 = task(state="done")
        with self.assertRaises(domain.InvalidTransition):
            domain.task_transition(t0, "blocked")  # 无旗＝矩阵内也拒
        t = domain.task_transition(t0, "blocked", delivery_failure=True)
        self.assertEqual(t.state, "blocked")

    def test_delivery_failure_flag_rejected_off_edge(self):
        # R4：专用旗落在非 done→blocked 边＝误用，同罪拒绝（评审环）
        with self.assertRaises(domain.InvalidTransition):
            domain.task_transition(task(state="running"), "done",
                                   delivery_failure=True)


class TestTimeSpec(unittest.TestCase):
    """断言③：epoch 毫秒 INTEGER↔v2 浮点秒往返。"""

    def test_roundtrip_and_shapes(self):
        for secs in (0.0, 1.5, 2.046, 1_763_728_000.123, 99.999):
            self.assertEqual(domain.epoch_seconds(domain.epoch_ms(secs)), secs)
        self.assertIsInstance(domain.epoch_ms(1.5), int)
        self.assertEqual(domain.epoch_ms(1.5), 1500)
        self.assertIsInstance(domain.epoch_seconds(1500), float)
        self.assertEqual(domain.epoch_seconds(1500), 1.5)


class TestDeliveryPolicy(unittest.TestCase):
    """断言④：digest/wake/notify+email 封闭映射。"""

    def test_note_digests(self):
        d = POLICY.decision("note")
        self.assertEqual(d.mode, domain.Delivery.DIGEST)
        self.assertFalse(d.email)

    def test_quiet_escalates_notify_and_email(self):
        self.assertEqual(len(POLICY.QUIET_ESCALATES), 6)  # 六类封闭集合
        for kind in POLICY.QUIET_ESCALATES:
            d = POLICY.decision("escalate", escalate_kind=kind)
            self.assertEqual(
                (d.mode, d.email, d.email_window),
                (domain.Delivery.NOTIFY_EMAIL, True, False), kind)

    def test_loud_escalates_wake(self):
        for kind in ("quality", "gatefiles", "reinject-fail",
                     "dispatcher-dead"):
            d = POLICY.decision("escalate", escalate_kind=kind)
            self.assertEqual((d.mode, d.email), (domain.Delivery.WAKE, False),
                             kind)

    def test_terminal_result_wakes_with_email_window(self):
        for state in POLICY.TERMINAL_EMAIL_STATES:
            d = POLICY.decision("result", task_state=state)
            self.assertEqual((d.mode, d.email, d.email_window),
                             (domain.Delivery.WAKE, True, True), state)
        d = POLICY.decision("result", task_state="running")  # 非终态不邮件
        self.assertEqual((d.mode, d.email), (domain.Delivery.WAKE, False))

    def test_timer_and_approval_wake(self):
        for kind in ("timer", "approval", "question"):  # question＝缺省唤醒
            self.assertEqual(POLICY.decision(kind).mode, domain.Delivery.WAKE)

    def test_no_arbitrary_severity(self):
        self.assertNotIn("severity",
                         inspect.signature(POLICY.decision).parameters)
        with self.assertRaises(ValueError):
            POLICY.decision("urgent")  # 集合外 kind 拒
        with self.assertRaises(ValueError):
            POLICY.decision("escalate", escalate_kind="critical")  # 集合外类别拒


class TestTimerApprovalMachines(unittest.TestCase):
    """断言⑤：Timer/Approval 枚举与非法迁移拒绝。"""

    def test_timer_enum_and_legal(self):
        self.assertEqual({s.value for s in domain.TimerState},
                         {"pending", "firing", "fired", "cancelled"})
        t = domain.timer_transition(timer(), domain.TimerState.FIRING)
        t = domain.timer_transition(t, domain.TimerState.PENDING)  # 崩溃窗回退
        t = domain.timer_transition(t, domain.TimerState.FIRING)
        t = domain.timer_transition(t, domain.TimerState.FIRED, now_ms=7)
        self.assertEqual((t.state, t.fired_ms),
                         (domain.TimerState.FIRED, 7))
        domain.timer_transition(timer(), domain.TimerState.CANCELLED)

    def test_timer_illegal_rejected(self):
        fired = timer(state=domain.TimerState.FIRED)
        for to in domain.TimerState:
            with self.assertRaises(domain.InvalidTransition):
                domain.timer_transition(fired, to)  # fired 无出边（含回 pending）
        with self.assertRaises(domain.InvalidTransition):  # 事务内态不可取消
            domain.timer_transition(timer(state=domain.TimerState.FIRING),
                                    domain.TimerState.CANCELLED)

    def test_approval_enum_and_legal(self):
        self.assertEqual(len(domain.ApprovalState), 10)
        self.assertEqual(
            {s.value for s in domain.ApprovalState},
            {"pending", "resolving", "approved", "rejected",
             "rejected_sibling", "expired", "expired_sibling",
             "resolved_external", "orphaned", "cancelled"})
        a = domain.approval_transition(approval(), domain.ApprovalState.RESOLVING)
        self.assertIsNone(a.resolved_ms)  # 在途未终局
        back = domain.approval_transition(a, domain.ApprovalState.PENDING)
        self.assertEqual(back.state, domain.ApprovalState.PENDING)  # 可重试
        done = domain.approval_transition(a, domain.ApprovalState.APPROVED,
                                          now_ms=9)
        self.assertEqual(done.resolved_ms, 9)
        domain.approval_transition(approval(),
                                   domain.ApprovalState.REJECTED_SIBLING)
        lost = domain.approval_transition(approval(),
                                          domain.ApprovalState.ORPHANED,
                                          orphan_reason="oc_restart")
        self.assertEqual(lost.orphan_reason, "oc_restart")
        with self.assertRaises(ValueError):  # R4：终态审计不留悬空 reason
            domain.approval_transition(approval(),
                                       domain.ApprovalState.ORPHANED)

    def test_approval_terminal_no_exit(self):
        # 终态无出边（不可 resolving 等）；sibling 记档自 pending 直接落终态
        for frm in domain.TERMINAL_APPROVAL_STATES:
            for to in domain.ApprovalState:
                a0 = approval(state=frm)
                with self.assertRaises(domain.InvalidTransition,
                                       msg=f"{frm.value}->{to.value}"):
                    domain.approval_transition(a0, to)
                self.assertEqual(a0.state, frm)  # 零变更
        with self.assertRaises(domain.InvalidTransition):  # resolving 无 sibling 边
            domain.approval_transition(
                approval(state=domain.ApprovalState.RESOLVING),
                domain.ApprovalState.REJECTED_SIBLING)


if __name__ == "__main__":
    unittest.main()
