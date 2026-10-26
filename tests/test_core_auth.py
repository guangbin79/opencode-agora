"""T3 会话牌与 enforce：真盘持久化（新 Auth 实例读盘恢复），无 mock。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.auth import Auth

TOOLS = ["task_create", "task_dispatch", "task_update", "task_cancel",
         "post", "check_mail", "board", "doctor", "wiki_read", "wiki_write", "enforce"]
WORKER_OK = {"post", "check_mail", "wiki_read", "wiki_write", "task_dispatch"}
# 目标任务快照行（State.snapshot()["tasks"][tid] 形态），供 task 域工具判定
ROW = {"state": "running", "dispatcher_sid": "d1", "worker_sid": "w1"}


class TestCoreAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st = Store(self.tmp)
        self.au = Auth(self.st)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _events(self):
        p = self.tmp / "events.jsonl"
        return [__import__("json").loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_may_matrix_full_cross_product(self):  # Req: F1
        for role in ("root", "dispatcher", "worker"):
            for tool in TOOLS:
                got = self.au.may(role, tool, "t1", dict(ROW))
                if role == "root":
                    want = True  # root→enforce/任意
                elif role == "dispatcher":
                    want = tool != "enforce"  # 仅 enforce 拒；task 域见下例行存在性用例
                else:
                    want = tool in WORKER_OK  # worker→五工具；task_cancel/update/enforce 恒 False
                self.assertEqual(got, want, f"{role}×{tool}")

    def test_may_task_scoped_needs_task_row(self):  # Req: F1
        # dispatcher 的 task_update(自己任务)/task_cancel(子链) 为 task 域：无任务行即拒
        self.assertFalse(self.au.may("dispatcher", "task_update", "t1", None))
        self.assertFalse(self.au.may("dispatcher", "task_cancel", "t1", None))
        self.assertTrue(self.au.may("dispatcher", "task_update", "t1", dict(ROW)))
        self.assertTrue(self.au.may("dispatcher", "task_cancel", "t1", dict(ROW)))

    def test_issue_identify_survives_restart(self):  # Req: F6
        tok = self.au.issue("s1", "worker")
        self.assertEqual(self.au.identify(tok), ("s1", "worker"))
        # kill -9 后新实例从盘恢复仍有效
        self.assertEqual(Auth(Store(self.tmp)).identify(tok), ("s1", "worker"))

    def test_revoke_makes_identify_none(self):  # Req: F1
        tok = self.au.issue("s1", "dispatcher")
        self.assertTrue(self.au.revoke(tok))
        self.assertIsNone(self.au.identify(tok))
        self.assertFalse(self.au.revoke(tok))  # 再废＝False
        self.assertIsNone(Auth(Store(self.tmp)).identify(tok))  # 撤销亦落盘

    def test_rotate_root_kills_old_roots_only(self):  # Req: F1
        old = self.au.issue("m1", "root")
        wk = self.au.issue("w1", "worker")
        new = self.au.rotate_root()
        self.assertEqual(self.au.identify(new)[1], "root")
        self.assertIsNone(self.au.identify(old))  # 旧牌即废
        self.assertEqual(self.au.identify(wk), ("w1", "worker"))  # 非 root 牌不受累
        evs = [e["event"] for e in self._events()]
        self.assertIn("root_rotated", evs)  # 敏感操作审计

    def test_set_enforce_non_root_denied_and_audited(self):  # Req: F1
        self.assertFalse(self.au.set_enforce(False, "worker"))
        self.assertFalse(self.au.set_enforce(False, "dispatcher"))
        self.assertTrue(self.au.enforce_on())  # 旗标未被非 root 触碰
        denials = [e for e in self._events() if e["event"] == "auth_denied"]
        self.assertEqual(len(denials), 2)
        self.assertEqual({e["actor"] for e in denials}, {"worker", "dispatcher"})

    def test_set_enforce_root_toggles_and_audits(self):  # Req: F1
        self.assertTrue(self.au.set_enforce(False, "root"))
        self.assertFalse(self.au.enforce_on())
        self.assertTrue(self.au.set_enforce(True, "root"))
        self.assertTrue(self.au.enforce_on())
        self.assertEqual([e["event"] for e in self._events()], ["enforce_off", "enforce_on"])

    def test_enforce_defaults_on_after_restart(self):  # Req: F6
        self.au.set_enforce(False, "root")
        self.assertTrue(Auth(Store(self.tmp)).enforce_on())  # 旗标不落盘，重启默认 on


if __name__ == "__main__":
    unittest.main()
