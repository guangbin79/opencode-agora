"""T6 core/msg 契约：链边寻址/落库先行/幂等补拉（真 Store tmp 夹具＋假 oc）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.msg import NotChainEdge, Router
from agorad.core.peer import PeerBoard, Rejected as PeerRejected
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

    def test_inject_error_persist_only(self):  # Req: F4
        """inject 异常（凭据轮换 401/网络故障）＝失败口径：不抛、仅落库、补拉可取——
        done 链路（httpd 同步 post）不得因投递面异常断裂。"""
        def boom(sid, text):
            raise RuntimeError("HTTP 401")

        self.oc.inject = boom
        mid = self.r.post("w1", ("up", "t1"), "done", "sha1")
        (m,) = self.store.read_messages()
        self.assertEqual((m["id"], m["kind"], m["body"]), (mid, "done", "sha1"))
        self.assertEqual([x["id"] for x in self.r.check_mail("d1")], [mid])
        self.assertTrue(self.r.dedup_key(mid))  # 补拉即送达：不再重交

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

    # ---- F16 血统链边 ----

    def test_lineage_dispatcher_edge_continuity(self):  # Req: F16
        """换号后 dispatcher（现 sid 血统含旧 dispatcher_sid）down 边仍达 worker；
        归因仍记真实新 sid；worker 边不加血统；无血缘仍拒。"""
        r = Router(self.store, self.state, self.oc,
                   same_lineage=lambda cur, old: (cur, old) == ("d2", "d1"))
        mid = r.post("d2", ("down", "t1"), "note", "hi")
        (m,) = [x for x in self.store.read_messages() if x["id"] == mid]
        self.assertEqual((m["from"], m["to"]), ("d2", "w1"))
        self.assertEqual(self.oc.calls[-1],
                         ("w1", "[agora] task t1 note from d2\nhi"))
        with self.assertRaises(NotChainEdge):  # worker 边不加：up 方向血统不放行
            r.post("d2", ("up", "t1"), "note", "x")
        with self.assertRaises(NotChainEdge):  # 无血缘 sid 仍拒
            r.post("stranger", ("down", "t1"), "note", "x")

    def test_rebound_dispatcher_pulls_mail_addressed_to_old_sid(self):
        """父 worker 死亡期间结果发往旧 sid；重注入换号后凭血统补拉，不丢闭环结果。"""
        self.oc.online = False
        mid = self.r.post("w1", ("up", "t1"), "result", "child closed")
        r = Router(self.store, self.state, self.oc,
                   same_lineage=lambda cur, old: (cur, old) == ("d2", "d1"))
        self.assertEqual([m["id"] for m in r.check_mail("d2")], [mid])
        self.assertEqual(r.check_mail("d2"), [])
        self.assertEqual(r.check_mail("stranger"), [])  # 无血统不能读取他号邮件

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

    # ---- 免通知通道（gt-5bc312 stalled 降噪）----

    def test_silent_post_quiet_channel_fallback_to_pull(self):  # Req: F4/F5
        """silent post 走 inject_silent（不触发桌面弹出）；通道 False/异常同
        离线口径：不抛、落库、补拉恰一次。"""
        self.oc.inject_silent = lambda sid, text: False
        mid = self.r.post("w1", ("up", "t1"), "escalate", "quiet", silent=True)
        self.assertEqual(self.oc.calls, [])  # 弹出源未触碰
        (m,) = self.store.read_messages()
        self.assertEqual((m["id"], m["body"]), (mid, "quiet"))  # 照常落库
        self.assertEqual([x["id"] for x in self.r.check_mail("d1")], [mid])
        self.assertEqual(self.r.check_mail("d1"), [])  # 补拉不重复

    def test_silent_post_raise_swallowed_persist_only(self):  # Req: F4/F5
        """inject_silent 抛异常（凭据轮换 401/网络故障）同离线口径：不抛、
        落库、补拉恰一次——投递面异常不断链。"""
        def boom(sid, text):
            raise RuntimeError("HTTP 401")

        self.oc.inject_silent = boom
        mid = self.r.post("w1", ("up", "t1"), "escalate", "hush", silent=True)
        self.assertEqual([x["id"] for x in self.r.check_mail("d1")], [mid])
        self.assertTrue(self.r.dedup_key(mid))

    def test_silent_post_online_delivered_once_no_pull(self):  # Req: F4/F5
        def spy(sid, text):
            self.oc.calls.append(("silent", sid, text))
            return True

        self.oc.inject_silent = spy
        mid = self.r.post("w1", ("up", "t1"), "escalate", "hush", silent=True)
        self.assertEqual(self.oc.calls,
                         [("silent", "d1", "[agora] task t1 escalate from w1\nhush")])
        self.assertEqual(self.r.check_mail("d1"), [])  # 静默已达，补拉不重交
        self.assertTrue(self.r.dedup_key(mid))


class TestPeerBoard(unittest.TestCase):
    """F22 同仓协作面：peer 写读回路/分区/上限/is_stale/权限/审计（真 Store tmp 夹具）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1,
            "tasks": {
                "t1": {"state": "running", "fail_streak": 0, "title": "A",
                       "project_dir": "/repoA/",  # 尾斜杠：分区 normpath 归一
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d1", "worker_sid": "w1"},
                "t2": {"state": "running", "fail_streak": 0, "title": "B",
                       "project_dir": "/repoB",
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d2", "worker_sid": "w2"},
            },
            "mail_read": {},
        })
        self.state = State(self.store)
        self.pb = PeerBoard(self.store, self.state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_board_roundtrip(self):  # Req: F22
        mid = self.pb.post("w1", "hello peers")
        r = self.pb.board(self.state.snapshot(), self.pb.partition("w1"))
        self.assertEqual(r["repo"], "/repoA")  # 尾斜杠归一
        self.assertEqual([(t["tid"], t["title"], t["state"]) for t in r["tasks"]],
                         [("t1", "A", "running")])  # 在飞四字段
        (m,) = r["messages"]
        self.assertEqual((m["id"], m["author"], m["author_state"], m["body"]),
                         (mid, "t1", "running", "hello peers"))
        self.assertGreaterEqual(m["age_seconds"], 0)
        self.assertFalse(m["is_stale"])
        self.assertTrue(m["untrusted"])  # 注入防线：数据非指令
        (raw,) = self.store.read_messages()
        self.assertEqual(raw["to"], "")  # 消息库全员有 to 不变量（check_mail 直取）

    def test_partition_isolation(self):  # Req: F22
        self.pb.post("w1", "from A")
        r = self.pb.board(self.state.snapshot(), "/repoB")
        self.assertEqual([t["tid"] for t in r["tasks"]], ["t2"])  # 跨仓任务不可见
        self.assertEqual(r["messages"], [])  # 跨仓留言不可见
        r_all = self.pb.board(self.state.snapshot())  # root/dispatcher 诊断＝全仓
        self.assertEqual(sorted(t["tid"] for t in r_all["tasks"]), ["t1", "t2"])

    def test_length_cap_500(self):  # Req: F22 护栏 a
        self.pb.post("w1", "x" * 600)
        (m,) = [x for x in self.store.read_messages() if x["kind"] == "peer"]
        self.assertEqual(len(m["body"]), 500)

    def test_soft_limit_10_then_reject(self):  # Req: F22 护栏 c
        for i in range(10):
            self.pb.post("w1", f"m{i}")
        with self.assertRaises(PeerRejected) as cm:
            self.pb.post("w1", "m10")
        self.assertIn("knowledge index", str(cm.exception))
        self.assertEqual(len([x for x in self.store.read_messages()
                              if x["kind"] == "peer"]), 10)

    def test_is_stale_after_author_terminal(self):  # Req: F22 护栏 b
        self.pb.post("w1", "note")
        for to in ("done", "merged", "closed"):
            self.state.transition("t1", to)
        r = self.pb.board(self.state.snapshot(), "/repoA")
        self.assertEqual([t["tid"] for t in r["tasks"]], [])  # 终态不在飞摘要
        (m,) = r["messages"]  # 数据保留：留言仍在，消费方据 is_stale 降权
        self.assertTrue(m["is_stale"])
        self.assertEqual(m["author_state"], "closed")

    def test_write_worker_author_required(self):  # Req: F22 权限（核心侧：作者可定）
        with self.assertRaises(PeerRejected):
            self.pb.post("stranger", "x")  # 无任务行＝作者不可定

    def test_audit_event(self):  # Req: F22 护栏审计
        mid = self.pb.post("w1", "audited")
        ev = [e for e in self.store.read_events() if e["event"] == "peer_post"]
        self.assertEqual([(e["task"], e["repo"], e["mid"]) for e in ev],
                         [("t1", "/repoA", mid)])


if __name__ == "__main__":
    unittest.main()
