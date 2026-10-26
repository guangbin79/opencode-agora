"""T2 core/state 契约：迁移矩阵/fail_streak/审计事件（Store 真 tmp 夹具，无 mock）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.state import (
    EV_BLOCKED,
    EV_CANCELLED,
    EV_CLOSED,
    EV_DISPATCHED,
    EV_DONE,
    EV_MERGED,
    EV_REOPENED,
    EV_RUNNING,
    EV_VERIFY_FAILED,
    LEGAL,
    InvalidTransition,
    State,
)


class TestCoreState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.seed({"t1": "todo"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, states, streak=0):
        # 经真 Store.append_state 播种（core 零 I/O，只注入对象）；末行快照＝权威
        self.store.append_state({
            "version": 1,
            "tasks": {
                tid: {"state": st, "fail_streak": streak, "title": "x",
                      "created": 0.0, "updated": 0.0}
                for tid, st in states.items()
            },
            "mail_read": {},
        })
        self.st = State(self.store)

    # ---- brief 原文两例（逐字） ----

    def test_illegal_transition_rejected(self):  # Req: F2
        with self.assertRaises(InvalidTransition): self.st.transition("t1", "merged")  # from todo

    def test_fail_streak_blocks_at_3_and_resets_on_redispatch(self):  # Req: F7
        for _ in range(3): self.st.fail_streak_bump("t1")
        self.assertEqual(self.st.get("t1")["state"], "blocked")

    # ---- 迁移矩阵 ----

    def test_legal_matrix_all_edges_pass(self):  # Req: F2
        for src, dsts in LEGAL.items():
            for dst in sorted(dsts):
                tid = f"m_{src}_{dst}"
                self.seed({tid: src})
                self.st.transition(tid, dst)
                self.assertEqual(self.st.get(tid)["state"], dst, f"{src}->{dst}")

    def test_illegal_matrix_all_rejected(self):  # Req: F2
        for src in LEGAL:
            for dst in LEGAL:
                if dst in LEGAL[src]:
                    continue
                tid = f"x_{src}"
                self.seed({tid: src})
                with self.assertRaises(InvalidTransition, msg=f"{src}->{dst}"):
                    self.st.transition(tid, dst)

    def test_invalid_transition_carries_legal_successors(self):  # Req: F2
        with self.assertRaises(InvalidTransition) as cm:
            self.st.transition("t1", "merged")
        self.assertEqual(cm.exception.legal, {"dispatched", "cancelled"})

    def test_blocked_dual_exits_and_redispatch_resets_streak(self):  # Req: F5
        self.seed({"b1": "blocked"}, streak=2)
        self.st.transition("b1", "dispatched")  # 重派＝对同任务再 dispatch
        got = self.st.get("b1")
        self.assertEqual(got["state"], "dispatched")
        self.assertEqual(got["fail_streak"], 0)
        self.seed({"b2": "blocked"})
        self.st.transition("b2", "cancelled")
        self.assertEqual(self.st.get("b2")["state"], "cancelled")

    def test_verify_failed_continue_fix_then_done(self):  # Req: F7
        self.seed({"v1": "verify_failed", "v2": "reopened"})
        self.st.transition("v1", "running"); self.st.transition("v1", "done")
        self.st.transition("v2", "running"); self.st.transition("v2", "done")
        self.assertEqual(self.st.get("v1")["state"], "done")
        self.assertEqual(self.st.get("v2")["state"], "done")

    # ---- fail_streak 口径 ----

    def test_streak_below_3_no_block_and_reset_clears(self):  # Req: F7
        self.st.fail_streak_bump("t1"); self.st.fail_streak_bump("t1")
        self.assertEqual(self.st.get("t1")["fail_streak"], 2)
        self.assertEqual(self.st.get("t1")["state"], "todo")  # 未到 3 不 block
        self.st.fail_streak_reset("t1")
        self.assertEqual(self.st.get("t1")["fail_streak"], 0)

    def test_streak_survives_reload(self):  # Req: F6
        self.st.fail_streak_bump("t1"); self.st.fail_streak_bump("t1")
        self.assertEqual(State(self.store).get("t1")["fail_streak"], 2)  # kill -9 不丢

    def test_terminal_state_never_auto_blocks(self):  # Req: F7
        self.seed({"z1": "cancelled", "z2": "closed"})
        for _ in range(3):
            self.st.fail_streak_bump("z1"); self.st.fail_streak_bump("z2")
        self.assertEqual(self.st.get("z1")["state"], "cancelled")
        self.assertEqual(self.st.get("z2")["state"], "closed")

    # ---- 快照/mail_read/审计事件名 ----

    def test_transition_merges_fields_and_persists(self):  # Req: F2
        self.st.transition("t1", "dispatched", worker_sid="w1", dispatcher_sid="d1")
        got = State(self.store).get("t1")  # 重载验证真落盘
        self.assertEqual((got["state"], got["worker_sid"], got["dispatcher_sid"]),
                         ("dispatched", "w1", "d1"))

    def test_mail_read_cursor_in_snapshot(self):  # Req: F4
        self.st.touch_mail("s1")
        first = self.st.snapshot()["mail_read"]["s1"]
        self.st.touch_mail("s1")
        self.assertGreaterEqual(self.st.snapshot()["mail_read"]["s1"], first)
        self.assertEqual(State(self.store).snapshot()["mail_read"], {})  # 内存态，崩溃回滚接受

    def test_audit_event_constants(self):  # Req: F8
        self.assertEqual(
            {EV_DISPATCHED, EV_RUNNING, EV_DONE, EV_VERIFY_FAILED, EV_REOPENED,
             EV_MERGED, EV_CLOSED, EV_BLOCKED, EV_CANCELLED},
            {"dispatched", "running", "done", "verify_failed", "reopened",
             "merged", "closed", "blocked", "cancelled"},
        )


if __name__ == "__main__":
    unittest.main()
