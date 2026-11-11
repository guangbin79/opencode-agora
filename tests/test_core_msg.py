"""T6 core/msg 契约：链边寻址/落库先行/幂等补拉（真 Store tmp 夹具＋假 oc）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.health import send_email
from agorad.core.msg import NotChainEdge, Router
from agorad.core.peer import PeerBoard, Rejected as PeerRejected
from agorad.core.state import State


class FakeOc:
    def __init__(self, online=True):
        self.online = online
        self.calls = []
        self.events = []  # F42：silent/inject 按发生序，断言投递顺序

    def inject(self, sid, text):
        self.calls.append((sid, text))
        self.events.append(("inject", sid, text))
        return self.online

    def inject_silent(self, sid, text):
        self.events.append(("silent", sid, text))
        return self.online

    def inject_notify(self, sid, text):
        self.events.append(("notify", sid, text))
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

    def test_check_mail_waits_for_inflight_inject(self):  # Req: F87
        """消息已落库、在线注入进行中：check_mail 不得抢交同一消息。

        旧实现 check_mail 先把 mid 标 delivered，阻塞注入完成后又视为在线
        送达——同一消息双通道各交一次；inflight 语义下补拉让位在线注入。"""
        import threading

        entered = threading.Event()
        release = threading.Event()

        orig_inject = self.oc.inject

        def blocking_inject(sid, text):
            entered.set()
            release.wait(2.0)
            return orig_inject(sid, text)

        self.oc.inject = blocking_inject
        mids = []
        t = threading.Thread(
            target=lambda: mids.append(
                self.r.post("w1", ("up", "t1"), "process", "in-flight")))
        t.start()
        self.assertTrue(entered.wait(2.0))
        self.assertEqual(self.r.check_mail("d1"), [])  # 在线注入持有交付权
        release.set()
        t.join()
        self.assertEqual(len(self.oc.calls), 1)
        self.assertEqual(self.r.check_mail("d1"), [])

    def test_failed_inflight_inject_still_pullable(self):  # Req: F87
        """inflight 注入失败必须释放交付权，下一次 check_mail 可补拉。"""
        import threading

        entered = threading.Event()
        release = threading.Event()

        def failing_inject(sid, text):
            entered.set()
            release.wait(2.0)
            return False

        self.oc.inject = failing_inject
        mids = []
        t = threading.Thread(
            target=lambda: mids.append(
                self.r.post("w1", ("up", "t1"), "done", "sha-fail")))
        t.start()
        self.assertTrue(entered.wait(2.0))
        self.assertEqual(self.r.check_mail("d1"), [])
        release.set()
        t.join()
        self.assertEqual([m["id"] for m in self.r.check_mail("d1")], mids)

    def test_check_mail_waits_for_inflight_digest_flush(self):  # Req: F87
        """digest 冲刷中的 note 不得同时被补拉——聚合本体只交付一次。"""
        import threading

        entered = threading.Event()
        release = threading.Event()

        orig_silent = self.oc.inject_silent

        def blocking_silent(sid, text):
            entered.set()
            release.wait(2.0)
            return orig_silent(sid, text)

        self.oc.inject_silent = blocking_silent
        t = threading.Thread(target=lambda: [
            self.r.post("w1", ("up", "t1"), "note", f"note {i}")
            for i in range(5)  # 满 _DIGEST_N 即冲
        ])
        t.start()
        self.assertTrue(entered.wait(2.0))
        self.assertEqual(self.r.check_mail("d1"), [])  # 冲刷中让位聚合投递
        release.set()
        t.join()
        self.assertEqual(len(self.oc.events), 1)
        self.assertIn("5 条过程消息", self.oc.events[0][2])
        self.assertEqual(self.r.check_mail("d1"), [])

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


class TestF42NoteDigest(unittest.TestCase):
    """F42 消息分级批量注入：worker→dispatcher 的 note 进缓冲聚合 digest 经
    inject_silent 免通知投递（落转录不调度回合）；关键消息保持实时且先冲后发。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1,
            "tasks": {
                "t1": {"state": "running", "fail_streak": 0, "title": "x",
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d1", "worker_sid": "w1"},
            },
            "mail_read": {},
        })
        self.state = State(self.store)
        self.oc = FakeOc()
        self.now = [1000.0]
        self.r = Router(self.store, self.state, self.oc,
                        clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _note(self, body):
        return self.r.post("w1", ("up", "t1"), "note", body)

    def test_five_notes_one_silent_digest_with_all(self):  # Req: F42
        mids = [self._note(f"n{i}") for i in range(5)]
        self.assertEqual(self.oc.events,
                         [("silent", "d1",
                           "[agora] 5 条过程消息（t1）\nn0\nn1\nn2\nn3\nn4")])
        self.assertEqual(self.oc.calls, [])  # 常规通道零触碰
        self.assertEqual(len(self.store.read_messages()), 5)  # 本体先行落库
        self.assertTrue(all(self.r.dedup_key(m) for m in mids))  # digest 达＝已投
        self.assertEqual(self.r.check_mail("d1"), [])  # 补拉不重交

    def test_critical_after_notes_digest_first(self):  # Req: F42
        self._note("a")
        self._note("b")
        self.r.post("w1", ("up", "t1"), "result", "final")
        self.assertEqual(self.oc.events,
                         [("silent", "d1", "[agora] 2 条过程消息（t1）\na\nb"),
                          ("inject", "d1",
                           "[agora] task t1 result from w1\nfinal")])

    def test_age_over_120_flushes_single_note(self):  # Req: F42
        self._note("early")
        self.now[0] += 121  # 假时钟：过龄待发在下次任一投递时惰性冲刷
        self._note("late")
        self.assertEqual(self.oc.events,
                         [("silent", "d1", "[agora] 1 条过程消息（t1）\nearly")])

    def test_below_thresholds_zero_inject(self):  # Req: F42
        for i in range(4):
            self._note(f"n{i}")
        self.assertEqual(self.oc.events, [])
        self.assertEqual(self.oc.calls, [])
        self.assertEqual([m["body"] for m in self.r.check_mail("d1")],
                         ["n0", "n1", "n2", "n3"])  # 本体已落库：补拉兜底不改动

    def test_digest_offline_bodies_stay_for_pull(self):  # Req: F42
        self.oc.online = False  # inject_silent 回 False：离线口径
        mids = [self._note(f"n{i}") for i in range(5)]
        self.assertEqual([k for k, _, _ in self.oc.events], ["silent"])
        self.assertEqual([m["id"] for m in self.r.check_mail("d1")], mids)
        self.assertEqual(self.r.check_mail("d1"), [])

    def test_check_mail_pulled_notes_not_redelivered_in_digest(self):  # Req: F46 H-2
        self._note("early0")  # 缓冲窗口内 2 条
        self._note("early1")
        self.assertEqual(self.oc.events, [])
        self.r.check_mail("d1")  # 补拉即送达：mid 入 _delivered，缓冲未清
        for i in range(3):
            self._note(f"n{i}")  # 凑满阈值触发 flush
        self.assertEqual(self.oc.events,  # digest 只含后 3 条（已补拉 mid 对账剔除）
                         [("silent", "d1", "[agora] 3 条过程消息（t1）\nn0\nn1\nn2")])

    def test_down_note_stays_realtime(self):  # Req: F42
        self.r.post("d1", ("down", "t1"), "note", "hi")  # 下行 note 不分级
        self.assertEqual(self.oc.events,
                         [("inject", "w1", "[agora] task t1 note from d1\nhi")])


class TestLifecycleNotify(unittest.TestCase):
    """任务生命周期迁移→dispatcher 实时单行通知（ag-acae3b）：终态直投不经
    F42 digest 缓冲（kind=result 天然旁路）；同迁移重放不重推；dispatcher
    未登记静默跳过——通知面异常不得断裂门链。F63：终态迁移经注入 mail 外呼
    一封（正文＝服务合成），(tid,60s) 窗吞相邻双终态第二封，非终态不发。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1,
            "tasks": {
                "t1": {"state": "running", "fail_streak": 0, "title": "x",
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d1", "worker_sid": "w1"},
            },
            "mail_read": {},
        })
        self.state = State(self.store)
        self.oc = FakeOc()
        self.mailed = []  # F63 邮件外呼记录：[(tid, body)]
        self.r = Router(self.store, self.state, self.oc,
                        mail=lambda tid, body: self.mailed.append((tid, body)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_terminal_direct_inject_not_buffered(self):  # Req: ag-acae3b
        """分级：终态迁移实时直投（inject 弹通道），不进 F42 note 缓冲。"""
        mid = self.r.notify_transition("t1", "merged", "@abc1234")
        self.assertEqual(self.oc.events,
                         [("inject", "d1",
                           "[agora] task t1 result from agorad\n任务 t1 merged @abc1234")])
        self.assertEqual(self.r._pending, {})  # digest 缓冲零残留
        (m,) = [x for x in self.store.read_messages() if x["id"] == mid]
        self.assertEqual((m["kind"], m["from"], m["to"], m["body"]),
                         ("result", "agorad", "d1", "任务 t1 merged @abc1234"))

    def test_detail_optional_single_line(self):  # Req: ag-acae3b
        self.r.notify_transition("t1", "blocked", "")
        self.assertEqual(self.oc.events,
                         [("inject", "d1",
                           "[agora] task t1 result from agorad\n任务 t1 blocked")])

    def test_replay_same_transition_not_repushed(self):  # Req: ag-acae3b
        """幂等：同迁移（键含 state.updated）重放（gate 重试/崩溃重排窗口）只推一次。"""
        self.r.notify_transition("t1", "merged", "@abc1234")
        self.r.notify_transition("t1", "merged", "@abc1234")
        self.assertEqual(len(self.oc.events), 1)
        self.assertEqual(len(self.store.read_messages()), 1)  # 重放零落库

    def test_repeat_migration_after_state_change_pushes_again(self):  # Req: ag-acae3b
        """同因重复迁移不吞（评审环 B 册）：worker 修复→再 done→门再败＝新迁移。"""
        self.r.notify_transition("t1", "reopened", "同步冲突")
        self.state.transition("t1", "done")  # worker 修复重新 done（updated 必变）
        self.state.transition("t1", "reopened")  # 门内再次同因弹回
        self.r.notify_transition("t1", "reopened", "同步冲突")
        self.assertEqual([e[2].splitlines()[1] for e in self.oc.events],
                         ["任务 t1 reopened 同步冲突", "任务 t1 reopened 同步冲突"])

    def test_distinct_migration_pushes_again(self):  # Req: ag-acae3b
        self.r.notify_transition("t1", "reopened", "同步冲突")
        self.r.notify_transition("t1", "merged", "@abc1234")  # 另一迁移照推
        self.assertEqual([e[2].splitlines()[1] for e in self.oc.events],
                         ["任务 t1 reopened 同步冲突", "任务 t1 merged @abc1234"])

    def test_offline_replay_stays_single_message(self):  # Req: ag-acae3b
        """离线：首推落库兜底，重放不再追加——补拉恰一条。"""
        self.oc.online = False
        self.r.notify_transition("t1", "closed", "@abc1234")
        self.r.notify_transition("t1", "closed", "@abc1234")
        (m,) = self.store.read_messages()
        self.assertEqual([x["id"] for x in self.r.check_mail("d1")], [m["id"]])

    def test_unregistered_dispatcher_swallowed(self):  # Req: ag-acae3b
        """dispatcher 未登记＝非链边：静默跳过不抛（通知面不得断裂门链）。"""
        self.state.add("t9", {"state": "done", "fail_streak": 0, "title": "x",
                              "created": 0.0, "updated": 0.0,
                              "dispatcher_sid": None, "worker_sid": "w9"})
        self.assertEqual(self.r.notify_transition("t9", "merged", "@abc1234"), None)
        self.assertEqual(self.store.read_messages(), [])

    # ---- F63 终态邮件必达 ----

    def test_terminal_email_body_matches_session_line(self):  # Req: F63
        """终态迁移触发邮件：正文与会话行同源服务合成（tid＋终态＋detail/sha）。"""
        body = "任务 t1 已 closed；commit=abc1234"
        self.r.notify_transition("t1", "closed", body=body)
        self.assertEqual(self.mailed, [("t1", body)])

    def test_replay_same_transition_email_deduped(self):  # Req: F63
        """重放去重恰一封：(tid,to,updated) 键在邮件外呼前置门，重放零二发。"""
        self.r.notify_transition("t1", "cancelled", body="任务 t1 已取消")
        self.r.notify_transition("t1", "cancelled", body="任务 t1 已取消")
        self.assertEqual(len(self.mailed), 1)
        self.assertEqual(len(self.store.read_messages()), 1)  # 会话行同键去重

    def test_merged_then_closed_window_single_email(self):  # Req: F63
        """(tid,60s) 窗只吞 merged→closed 门尾相邻对：窗内 closed 不发；会话行照推。"""
        self.r.notify_transition("t1", "merged", "@abc1234")
        self.r.notify_transition("t1", "closed", "commit=abc1234")
        self.assertEqual([b for _, b in self.mailed], ["任务 t1 merged @abc1234"])
        self.assertEqual(len(self.oc.events), 2)  # 两迁移会话行各推不吞

    def test_blocked_then_merged_same_window_both_sent(self):  # Req: F63（终闸 B 册）
        """窗不跨终态对吞：blocked→重派再走完门→merged 60s 内各自必达（含 sha 那封不丢）。"""
        self.r.notify_transition("t1", "blocked", body="任务 t1 已 blocked")
        self.state.transition("t1", "done")  # 重派后快走完门（updated 必变）
        self.state.transition("t1", "merged")
        self.r.notify_transition("t1", "merged", "@abc1234")  # blocked 后 60s 内照发
        self.assertEqual(self.mailed, [("t1", "任务 t1 已 blocked"),
                                       ("t1", "任务 t1 merged @abc1234")])

    def test_window_expiry_refires_after_60s(self):  # Req: F63
        """窗过期再发：距上次邮件 ≥60s 的后续终态不吞（假时钟注入）。"""
        now = [1000.0]
        r = Router(self.store, self.state, self.oc, clock=lambda: now[0],
                   mail=lambda tid, b: self.mailed.append((tid, b)))
        r.notify_transition("t1", "merged", "@abc1234")
        now[0] += 30
        r.notify_transition("t1", "closed", "commit=abc1234")  # 窗内吞
        now[0] += 31
        r.notify_transition("t1", "blocked", "请重派或取消")  # 跨窗再发
        self.assertEqual([b.split()[-1] for _, b in self.mailed],
                         ["@abc1234", "请重派或取消"])

    def test_non_terminal_no_email(self):  # Req: F63
        """非终态（reopened 拒因摘要）不发邮件。"""
        self.r.notify_transition("t1", "reopened", "同步冲突")
        self.assertEqual(self.mailed, [])

    def test_email_exception_swallowed_session_intact(self):  # Req: F63
        """邮件缝异常不抛不断链：会话行照常落库直投（终态优先）。"""
        def boom(tid, body):
            raise RuntimeError("mail down")
        r = Router(self.store, self.state, self.oc, mail=boom)
        mid = r.notify_transition("t1", "cancelled", body="任务 t1 已取消")
        (m,) = [x for x in self.store.read_messages() if x["id"] == mid]
        self.assertEqual(m["body"], "任务 t1 已取消")

    def test_session_failure_does_not_swallow_email(self):  # Req: F63
        """dispatcher 未登记（会话面败）不吞邮件兜底：必达恰在故障场景兑现。"""
        self.state.add("t9", {"state": "done", "fail_streak": 0, "title": "x",
                              "created": 0.0, "updated": 0.0,
                              "dispatcher_sid": None, "worker_sid": "w9"})
        self.assertEqual(self.r.notify_transition("t9", "cancelled", body="任务 t9 已取消"),
                         None)  # 会话行落库零
        self.assertEqual(self.mailed, [("t9", "任务 t9 已取消")])  # 邮件照发

    def test_failed_send_window_not_consumed_retry_on_next(self):  # Req: F63
        """mail 返回 False＝确认败不占窗：merged 败后 closed 照发（必达重试）。"""
        attempts = []
        results = iter([False, True])
        r = Router(self.store, self.state, self.oc,
                   mail=lambda tid, b: attempts.append(b) or next(results))
        r.notify_transition("t1", "merged", "@abc1234")  # 败：窗不占
        r.notify_transition("t1", "closed", "commit=abc1234")  # 窗未占：重试再发
        self.assertEqual(len(attempts), 2)  # 第二封未被窗吞

    def test_email_json_missing_silent(self):  # Req: F63
        """email.json 缺失＝静默跳过不抛（opt-in 与 stalled 同语义，真 send_email）。"""
        r = Router(self.store, self.state, self.oc,
                   mail=lambda tid, b: send_email(self.store, tid, b))
        r.notify_transition("t1", "closed", "commit=abc1234")
        self.assertEqual(self.store.read_events("t1"), [])  # 零审计行＝零外呼


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

    def test_board_projection_carries_req_id(self):  # Req: F83
        """撞号改号双查面：在飞摘要投影 req_id——worker 唯一可读的在飞
        查重面（F34 发放集不可见），改号前据此避让服务端已发放号。"""
        self.state.set_fields("t1", req_id="F83")
        r = self.pb.board(self.state.snapshot(), "/repoA")
        self.assertEqual([(t["tid"], t.get("req_id")) for t in r["tasks"]],
                         [("t1", "F83")])  # 已发放号在飞可见
        r2 = self.pb.board(self.state.snapshot(), "/repoB")
        self.assertEqual([(t["tid"], t.get("req_id")) for t in r2["tasks"]],
                         [("t2", None)])  # 无号行缺省 None（键恒在）

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


class TestF75Replay(TestCoreMsg):
    """Req: F75 积压补交：to=='root' 错址窗口消息生成副本（replay_of 锚幂等）、
    原件一字不改、kind 分级投递、失败留库补拉、跨 Router 重建不重复。"""

    def root_msg(self, kind, body, task="t1", mfrom="w1", to="root"):
        return self.store.write_message(
            {"ts": 1.0, "task": task, "kind": kind, "from": mfrom, "to": to}, body)

    def test_online_replay_split_by_kind(self):  # Req: F75②
        q = self.root_msg("question", "q-body")
        self.root_msg("note", "n-body")
        self.root_msg("verify", "v-body")
        self.root_msg("question", "direct", to="d1")  # 非错址行不补交
        self.assertEqual(self.r.replay_root_backlog("t1", "d1"), 3)
        injects = [(s, t) for k, s, t in self.oc.events if k == "inject"]
        self.assertEqual([s for s, _ in injects], ["d1", "d1"])  # question/verify 真实 prompt
        self.assertIn("q-body", injects[0][1])
        self.assertIn("v-body", injects[1][1])
        silents = [(s, t) for k, s, t in self.oc.events if k == "silent"]
        self.assertEqual([s for s, _ in silents], ["d1"])  # note 走 silent 免弹窗
        self.assertIn("n-body", silents[0][1])
        msgs = {m["id"]: m for m in self.store.read_messages()}
        self.assertEqual(msgs[q]["to"], "root")  # 原件一字不改
        copy = next(m for m in msgs.values() if m.get("replay_of") == str(q))
        self.assertEqual((copy["kind"], copy["from"], copy["to"], copy["body"]),
                         ("question", "w1", "d1", "q-body"))
        self.assertGreater(float(copy["ts"]), 1.0)  # ts 取当前：越过既有游标

    def test_online_replay_escalate_goes_notify(self):  # Req: F76
        """escalate 错址补交同步走通知通道（synthetic resume:true 唤醒派发方）；
        note 仍走 silent 免弹窗；失败仍留库补拉（离线口径不变）。"""
        self.root_msg("escalate", "e-body")
        self.root_msg("note", "n-body")
        self.assertEqual(self.r.replay_root_backlog("t1", "d1"), 2)
        kinds = {k for k, _, _ in self.oc.events}
        self.assertEqual(kinds, {"notify", "silent"})  # 分级面：escalate→notify、note→silent
        notify = [t for k, _, t in self.oc.events if k == "notify"]
        self.assertIn("e-body", notify[0])
        self.oc.online = False
        self.root_msg("escalate", "later")
        self.r.replay_root_backlog("t1", "d1")  # 通道败：留库
        got = self.r.check_mail("d1")
        self.assertIn("later", [m["body"] for m in got])

    def test_offline_replay_pulled_by_check_mail(self):  # Req: F75③
        self.oc.online = False
        q = self.root_msg("question", "later")
        self.r.replay_root_backlog("t1", "d1")  # 注入失败留库
        got = self.r.check_mail("d1")
        self.assertEqual([(m["replay_of"], m["body"]) for m in got],
                         [(str(q), "later")])  # 副本可被真实派发者补拉
        orig = next(m for m in self.store.read_messages() if m["id"] == q)
        self.assertEqual(orig["to"], "root")  # 原件保留 to:root

    def test_replay_idempotent_across_router_rebuild(self):  # Req: F75④
        self.root_msg("note", "once")
        self.assertEqual(self.r.replay_root_backlog("t1", "d1"), 1)
        n = len(self.store.read_messages())
        self.r = Router(self.store, self.state, self.oc)  # 服务重启形态
        self.assertEqual(self.r.replay_root_backlog("t1", "d1"), 0)
        self.assertEqual(len(self.store.read_messages()), n)  # 绝不重复生成


if __name__ == "__main__":
    unittest.main()
