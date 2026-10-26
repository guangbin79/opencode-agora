"""T6 core/msg 契约：链边寻址/落库先行/幂等补拉（真 Store tmp 夹具＋假 oc）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.msg import NotChainEdge, Router
from agorad.core.state import State


class FakeOc:
    def __init__(self, online=True):
        self.online = online
        self.calls = []

    def inject(self, sid, text):
        self.calls.append((sid, text))
        return self.online


class TestCoreMsg(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1,
            "tasks": {
                "t1": {"state": "running", "fail_streak": 0, "title": "x",
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d1", "worker_sid": "w1"},
                "t2": {"state": "todo", "fail_streak": 0, "title": "x",
                       "created": 0.0, "updated": 0.0},  # 未派发：无边
            },
            "mail_read": {},
        })
        self.state = State(self.store)
        self.oc = FakeOc()
        self.r = Router(self.store, self.state, self.oc)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- brief 原文四例 ----

    def test_online_inject_once_and_persisted(self):  # Req: F4
        lib_size_at_inject = []
        orig = self.oc.inject

        def spy(sid, text):
            lib_size_at_inject.append(len(self.store.read_messages()))
            return orig(sid, text)

        self.oc.inject = spy
        mid = self.r.post("w1", ("up", "t1"), "process", "hello")
        self.assertEqual(self.oc.calls, [("d1", "[agora] task t1 process from w1\nhello")])
        self.assertEqual(lib_size_at_inject, [1])  # 铁则：inject 前已落库
        (m,) = self.store.read_messages()
        self.assertEqual((m["id"], m["from"], m["to"], m["task"], m["kind"]),
                         (mid, "w1", "d1", "t1", "process"))
        self.assertEqual(m["body"], "hello")

    def test_offline_persist_only_then_pull(self):  # Req: F4
        self.oc.online = False
        mid = self.r.post("w1", ("up", "t1"), "process", "later")
        self.assertEqual(len(self.store.read_messages()), 1)  # 仅落库不丢
        got = self.r.check_mail("d1")  # 恢复后补拉
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["id"], got[0]["body"], got[0]["from"]),
                         (mid, "later", "w1"))
        self.assertEqual(self.r.check_mail("d1"), [])  # 拉取即送达，不重复

    def test_same_msg_id_resend_not_reinjected(self):  # Req: F4
        mid = self.r.post("w1", ("up", "t1"), "process", "once")
        self.assertTrue(self.r.dedup_key(mid))  # 重发：已投递→True 不再投递
        self.assertEqual(len(self.oc.calls), 1)
        self.assertEqual(self.r.check_mail("d1"), [])  # 在线已达，补拉不重交
        self.assertFalse(self.r.dedup_key(999))  # 未投递过→False
        self.assertTrue(self.r.dedup_key(999))

    def test_offchain_post_raises(self):  # Req: F4
        n = len(self.store.read_messages())
        bad = [("w1", ("down", "t1")),      # 自发自收非边
               ("w1", ("up", "t2")),        # 对方未登记无边
               ("stranger", ("up", "t1")),  # 非任务另一方
               ("d1", ("side", "t1")),      # 方向非法
               ("w1", ("up", "nope"))]      # 任务不存在
        for from_sid, edge in bad:
            with self.assertRaises(NotChainEdge, msg=f"{from_sid}->{edge}"):
                self.r.post(from_sid, edge, "process", "x")
        self.assertEqual(len(self.store.read_messages()), n)  # 拒绝即零落库

    # ---- 游标推进与排序 ----

    def test_check_mail_cursor_and_order(self):  # Req: F4
        self.oc.online = False
        m1 = self.r.post("w1", ("up", "t1"), "process", "a")
        m2 = self.r.post("w1", ("up", "t1"), "process", "b")
        m3 = self.r.post("d1", ("down", "t1"), "process", "c")
        got = self.r.check_mail("d1")
        self.assertEqual([m["id"] for m in got], sorted([m1, m2]))  # 升序且只含自己
        self.assertIn("ts", got[0])
        cursor = self.state.snapshot()["mail_read"]["d1"]
        self.assertGreater(cursor, 0.0)  # 游标已推进
        self.assertEqual([m["id"] for m in self.r.check_mail("w1")], [m3])
        self.assertEqual(self.r.check_mail("d1"), [])  # 游标后旧件不再交


if __name__ == "__main__":
    unittest.main()
