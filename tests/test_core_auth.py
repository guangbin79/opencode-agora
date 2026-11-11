"""T3 会话牌与 enforce：真盘持久化（新 Auth 实例读盘恢复），无 mock。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.auth import Auth

TOOLS = ["task_create", "task_dispatch", "task_update", "task_cancel",
         "post", "check_mail", "board", "doctor", "wiki_read", "wiki_write", "enforce"]
WORKER_OK = {"post", "check_mail", "wiki_read", "wiki_write", "task_create",
             "task_dispatch", "task_update", "task_cancel"}
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
                    want = tool in WORKER_OK  # worker 可管己建子任务；归属由 TaskService 终审
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

    def test_rebind_accumulates_lineage_and_persists(self):  # Req: F16
        tok = self.au.issue("s1", "root")
        self.au.rebind("s1", "s2")
        self.assertEqual(self.au.identify(tok), ("s2", "root"))
        self.assertTrue(self.au.same_lineage("s2", "s1"))  # 曾用名入档
        self.assertFalse(self.au.same_lineage("s1", "s2"))  # 旧名已不持牌
        self.au.rebind("s2", "s3")  # 链式换号：血统累积
        self.assertTrue(self.au.same_lineage("s3", "s2"))
        self.assertTrue(self.au.same_lineage("s3", "s1"))
        self.assertTrue(Auth(Store(self.tmp)).same_lineage("s3", "s1"))  # 血统随牌落盘

    def test_rebind_lineage_capped_at_16_fifo(self):  # Req: gt-83185d R1
        tok = self.au.issue("s0", "root")
        for i in range(20):  # 连续换号 20 跳（随机新 X-Agora-Sid 面）
            self.au.rebind(f"s{i}", f"s{i + 1}")
        self.assertEqual(self.au.identify(tok), ("s20", "root"))
        self.assertTrue(self.au.same_lineage("s20", "s19"))  # 最近记录保留
        self.assertTrue(self.au.same_lineage("s20", "s4"))  # 恰留 16 条：s4 最旧在册
        self.assertFalse(self.au.same_lineage("s20", "s3"))  # 最旧出局

    def test_live_token_prefers_non_root_and_reports_poison(self):  # ag-588eb8
        self.au.issue("s1", "root")  # 先签 root（占位早于窗口牌＝token_for 首匹配陷阱）
        self.au.issue("s1", "dispatcher")  # 双角色并存：同一 sid 两块牌
        tok, role = self.au.live_token("s1")
        self.assertEqual(role, "dispatcher")  # 非 root 活牌优先（token_for 首匹配会中 root）
        self.assertEqual(self.au.identify(tok)[1], "dispatcher")
        self.au.issue("s2", "root")  # 仅持 root＝污染态
        tok, role = self.au.live_token("s2")
        self.assertEqual((tok is not None, role), (True, "root"))
        self.assertIsNone(self.au.live_token("s-none"))  # 无牌 None

    def test_rebind_prefer_role_picks_role_token_over_insertion_order(self):  # ag-588eb8
        self.au.issue("s1", "dispatcher")  # 窗口牌在前
        tok_r = self.au.issue("s1", "root")  # rotate_root 重签即落队尾（root 后至）
        tok_d = self.au.token_for("s1")
        self.assertEqual(self.au.identify(tok_d), ("s1", "dispatcher"))
        self.au.rebind("s1", "s2", prefer_role="root")  # 双角色：点名改绑 root 牌
        self.assertEqual(self.au.identify(tok_r), ("s2", "root"))
        self.assertEqual(self.au.identify(tok_d), ("s1", "dispatcher"))  # 窗口牌不动
        self.au.rebind("s2", "s3")  # 缺省＝首匹配（既有语义不回归）
        self.assertEqual(self.au.identify(tok_r), ("s3", "root"))

    def test_lineage_resets_on_rotate_and_old_format_compat(self):  # Req: F16
        self.au.issue("s1", "root")
        self.au.rebind("s1", "s2")
        self.au.rotate_root()  # 轮换发新牌：血统从零起（rotate_root 豁免边角）
        self.assertFalse(self.au.same_lineage("s2", "s1"))
        self.st.write_ephemeral("tokens", {"t0": ["s9", "worker"]})  # 旧格式缺血统字段
        au = Auth(Store(self.tmp))
        self.assertFalse(au.same_lineage("s9", "any"))  # 缺字段＝空集
        au.rebind("s9", "s10")  # 旧格式 rebind 起档不炸
        self.assertTrue(au.same_lineage("s10", "s9"))

    def test_concurrent_issue_and_revoke_serialize_and_reload(self):  # Req: F86
        """Auth 变更并发落盘：无锁实现共享 tokens.tmp 必炸；串行实现终态可重载。

        首个 os.replace 等第二个 Auth 写者进入以暴露并发；串行实现下首写
        超时独自完成、次写随后串行，重载后旧牌已废、新牌仍有效。"""
        import os
        import threading

        old = self.au.issue("s-old", "worker")
        orig_replace = os.replace
        entered = 0
        guard = threading.Lock()
        second_here = threading.Event()
        new_tok = []
        errs = []

        def overlap_aware_replace(src, dst):
            nonlocal entered
            with guard:
                entered += 1
                first = entered == 1
            if first:
                second_here.wait(1.0)
            else:
                second_here.set()
            return orig_replace(src, dst)

        def issue():
            try:
                new_tok.append(self.au.issue("s-new", "worker"))
            except Exception as e:
                errs.append(repr(e))

        def revoke():
            try:
                self.au.revoke(old)
            except Exception as e:
                errs.append(repr(e))

        os.replace = overlap_aware_replace
        try:
            ts = [threading.Thread(target=issue), threading.Thread(target=revoke)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            os.replace = orig_replace
        self.assertEqual(errs, [])
        fresh = Auth(Store(self.tmp))
        self.assertIsNone(fresh.identify(old))
        self.assertEqual(fresh.identify(new_tok[0]), ("s-new", "worker"))


if __name__ == "__main__":
    unittest.main()
