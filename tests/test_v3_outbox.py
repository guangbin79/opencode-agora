"""Req: F134 — v3 message/outbox 应用服务（opencode-agora v3 P2-T7）。

断言清单（任务页 ag-479e18，逐条机械判定）：1 写路径同事务（三表零残留
＋commit 先于 OC）；2 receipt 生命周期（含 lease 30s 时钟注入）；3
message_pull 算法；4 换号血统补拉；5 dedup_key 幂等；6 note digest；
7 at-least-once 崩溃窗；8 引用行不悬空；9 非 UTF-8 body 往返。断言10
（架构守卫）在 test_architecture.py：messaging 分层锁＋SQL 圈禁自动覆盖。
OC 面按 tests/stub_opencode.py 桩约定（inject/inject_silent/inject_notify
三通道、异常/False 两失败面）；FaultInjector/临时 home 复用 v3_harness。
"""
import unittest

from agorad import domain
from agorad.messaging import (DIGEST_N, DIGEST_T_MS, LEASE_MS,
                              MessagingService)
from agorad.storage import Storage
from tests import v3_harness as harness


class OcStub:
    """OC 桩（stub_opencode 桩约定的单测内联形态）：记录注入通道/sid/文本
    与注入时点已提交的 messages 行数（WAL 读者快照——commit-先于-inject
    顺序断言面）；mode＝raise 抛异常／false 返回 False。"""

    def __init__(self, storage=None, mode=None):
        self.calls = []  # (channel, sid, text, seen_committed_messages)
        self.storage = storage
        self.mode = mode

    def _seen(self):
        if self.storage is None:
            return None
        reader = self.storage.open_reader()
        try:
            return reader.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            reader.close()

    def _record(self, channel, sid, text):
        self.calls.append((channel, sid, text, self._seen()))
        if self.mode == "raise":
            raise RuntimeError("oc down")
        return self.mode != "false"

    def inject(self, sid, text):
        return self._record("inject", sid, text)

    def inject_silent(self, sid, text):
        return self._record("silent", sid, text)

    def inject_notify(self, sid, text):
        return self._record("notify", sid, text)


class FakeClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


class _FileExporter:
    """导出成功面：真写 state/messages/<id>.md（v2 store 布局），返回相对路径。"""

    def __init__(self, home):
        self.home = home

    def export(self, msg):
        rel = f"state/messages/{msg.id:08d}.md"
        path = self.home.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = msg.body.decode("utf-8", "replace") if isinstance(
            msg.body, (bytes, bytearray)) else str(msg.body)
        path.write_text(f"id: {msg.id}\ntask: {msg.task_id}\n\n{body}",
                        encoding="utf-8")
        return rel


class _FailingExporter:
    def export(self, msg):
        raise OSError("disk full")


def count(storage, table):
    reader = storage.open_reader()
    try:
        return reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        reader.close()


class _Base(unittest.TestCase):
    TID = "ag-t7"

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = (harness.FaultInjector()
                         .register("uow").register("dispatch"))
        self.storage = Storage(self.home.state / "agora.db",
                               injector=self.injector)
        self.addCleanup(self.storage.close)
        self.clock = FakeClock()
        with self.storage.uow():
            self.storage.upsert_task(domain.Task(id=self.TID, state="running"))
        self.oc = OcStub(self.storage)
        self.svc = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)

    def service(self, **over):
        kw = dict(home=str(self.home.path), clock=self.clock,
                  injector=self.injector)
        kw.update(over)
        return MessagingService(self.storage, self.oc, **kw)


class TestWritePath(_Base):  # 断言1
    def test_fault_mid_uow_zero_residue_then_commit_before_oc(self):
        self.injector.arm("uow", "after")  # 写完、COMMIT 前中途炸
        with self.assertRaises(harness.FaultInjected):
            self.svc.post("w", "d", self.TID, "note", "hello")
        for table in ("messages", "delivery_receipts", "events"):
            self.assertEqual(count(self.storage, table), 0, table)  # 三表零残留
        self.assertEqual(self.oc.calls, [])  # 未提交→OC 零调用
        self.injector.disarm("uow")
        self.svc.post("w", "d", self.TID, "question", "hi")  # wake 通道
        [(channel, sid, text, seen)] = self.oc.calls
        self.assertEqual((channel, sid), ("inject", "d"))
        self.assertEqual(seen, 1)  # 注入时点消息行已可见＝commit 先于 OC
        self.assertIn("hi", text)


class TestReceiptLifecycle(_Base):  # 断言2
    def test_success_delivered(self):
        self.svc.post("w", "d", self.TID, "question", "b1")
        r = self.storage.get_receipt(1)
        self.assertEqual((r.status, r.attempts, r.last_error),
                         ("delivered", 1, None))

    def test_oc_false_back_to_pending_with_accounting(self):
        self.oc.mode = "false"
        self.svc.post("w", "d", self.TID, "question", "b")
        r = self.storage.get_receipt(1)
        self.assertEqual((r.status, r.attempts), ("pending", 1))
        self.assertTrue(r.last_error)

    def test_oc_raise_back_to_pending_with_accounting(self):
        self.oc.mode = "raise"
        self.svc.post("w", "d", self.TID, "question", "b")
        r = self.storage.get_receipt(1)
        self.assertEqual((r.status, r.attempts), ("pending", 1))
        self.assertIn("oc down", r.last_error)

    def test_inflight_lease_timeout_returns_pending(self):
        self.injector.arm("dispatch", "before")  # inflight 已落、OC 调用前炸
        with self.assertRaises(harness.FaultInjected):
            self.svc.post("w", "d", self.TID, "question", "b")
        r = self.storage.get_receipt(1)
        self.assertEqual(r.status, "inflight")
        self.assertEqual(r.lease_until_ms - self.clock.ms, LEASE_MS)  # 30s lease
        self.clock.advance(LEASE_MS + 1)  # 时钟注入：31s 过期
        self.svc.post("w", "d", self.TID, "note", "n")  # 任一入口惰性扫
        r = self.storage.get_receipt(1)
        self.assertEqual((r.status, r.lease_until_ms), ("pending", None))


class TestPullAlgorithm(_Base):  # 断言3
    def _seed_stuck(self):
        self.svc.post("w", "d", self.TID, "question", "m1")  # OC ok→delivered
        self.injector.arm("dispatch", "before")
        with self.assertRaises(harness.FaultInjected):
            self.svc.post("w", "d", self.TID, "question", "m2")  # 停在 inflight
        self.injector.disarm("dispatch")
        self.svc.post("w", "d", self.TID, "note", "m3")  # digest 缓冲→pending

    def test_stop_at_first_inflight_cursor_advances_no_beyond(self):
        self._seed_stuck()
        got = self.svc.pull("d")
        self.assertEqual([m.id for m in got], [1])  # m1 delivered 不阻断；m2 停
        self.assertEqual(self.storage.get_cursor("d").last_read_ms, 1)  # 不越过
        self.assertEqual(self.svc.pull("d"), [])  # 同 sid 二次 pull 不重复
        self.assertEqual(self.storage.get_receipt(3).status, "pending")
        self.svc.recover()  # m2 inflight→pending
        got = self.svc.pull("d")
        self.assertEqual([m.id for m in got], [2, 3])
        self.assertEqual({self.storage.get_receipt(i).status for i in (2, 3)},
                         {"delivered"})  # 抑制 Outbox 重复注入
        calls = len(self.oc.calls)
        self.assertFalse(self.svc.dispatch(2))  # 已 delivered→不再注入
        self.assertEqual(len(self.oc.calls), calls)
        self.clock.advance(DIGEST_T_MS + 1)  # 缓冲内 m3 已送达→digest 剔除
        self.svc.post("w", "d", self.TID, "note", "m4")
        self.assertEqual([c for c in self.oc.calls if c[0] == "silent"], [])


class TestLineageBackfill(_Base):  # 断言4
    def test_new_sid_pulls_full_history_via_credential_lineage(self):
        self.svc.post("w", "ses-old", self.TID, "question", "b1")  # delivered
        self.svc.post("w", "ses-old", self.TID, "note", "n1")  # pending
        got = self.svc.pull("ses-old")  # 旧 sid 已消费
        self.assertEqual([m.id for m in got], [1, 2])
        self.assertEqual(self.svc.pull("ses-old"), [])
        self.assertEqual(self.svc.pull("ses-other"), [])  # 无血统不可见
        with self.storage.uow():  # credential lineage 模拟换号
            self.storage.put_credential(domain.Credential(
                sid="ses-new", token="t", lineage=("ses-old",)))
        got = self.svc.pull("ses-new")  # 新 sid cursor=0 全量补拉（含 delivered）
        self.assertEqual([m.id for m in got], [1, 2])


class TestDedupKey(_Base):  # 断言5
    def test_same_key_original_id_zero_new_rows(self):
        m1 = self.svc.post("w", "d", self.TID, "question", "a",
                           dedup_key="dk-x")
        calls = len(self.oc.calls)
        m2 = self.svc.post("w", "d", self.TID, "question", "retry",
                           dedup_key="dk-x")
        self.assertEqual(m2.id, m1.id)  # 原 id 返回
        self.assertEqual(len(self.oc.calls), calls)  # 零重投
        self.assertEqual(count(self.storage, "messages"), 1)
        self.assertEqual(count(self.storage, "events"), 1)  # 零新建（event 同伙）
        m3 = self.svc.post("w", "d", self.TID, "question", "b",
                           dedup_key="dk-y")
        self.assertGreater(m3.id, m1.id)  # 不同键正常新建
        self.assertEqual(count(self.storage, "messages"), 2)


class TestNoteDigest(_Base):  # 断言6
    def test_flush_at_count_threshold(self):
        for i in range(DIGEST_N - 1):
            self.svc.post("w", "d", self.TID, "note", f"note-{i}")
        self.assertEqual(self.oc.calls, [])  # 未满不冲；receipt pending 留库
        self.assertEqual(self.storage.get_receipt(1).status, "pending")
        self.svc.post("w", "d", self.TID, "note", f"note-{DIGEST_N - 1}")
        [silent] = [c for c in self.oc.calls if c[0] == "silent"]
        text = silent[2]
        self.assertIn(f"{DIGEST_N} 条过程消息（{self.TID}）", text)
        self.assertIn("ids=1,2,3,4,5", text)
        for i in range(DIGEST_N):
            self.assertIn(f"note-{i}", text)
        self.assertEqual({self.storage.get_receipt(i + 1).status
                          for i in range(DIGEST_N)}, {"delivered"})  # 冲刷即视已投

    def test_age_flush_and_critical_kind_preflush(self):
        self.svc.post("w", "d", self.TID, "note", "n0")
        self.assertEqual(self.oc.calls, [])
        self.clock.advance(DIGEST_T_MS + 1)  # 最旧存龄≥120s
        self.svc.post("w", "d", self.TID, "question", "q1")  # 关键 kind 先冲后发
        self.assertEqual([c[0] for c in self.oc.calls], ["silent", "inject"])
        self.assertIn("1 条过程消息", self.oc.calls[0][2])
        self.assertIn("n0", self.oc.calls[0][2])
        self.assertIn("q1", self.oc.calls[1][2])
        self.assertEqual(self.storage.get_receipt(1).status, "delivered")


class TestAtLeastOnceCrashWindow(_Base):  # 断言7
    def test_crash_after_oc_before_delivered_redeliver_same_id(self):
        self.injector.arm("dispatch", "after")  # OC 成功后、delivered 落账前
        with self.assertRaises(harness.FaultInjected):
            self.svc.post("w", "d", self.TID, "question", "b")
        self.assertEqual(len(self.oc.calls), 1)  # OC 已成功收到
        self.assertEqual(self.storage.get_receipt(1).status, "inflight")
        self.assertIn(("dispatch", "after"),
                      [(n, p) for n, p, _ in self.injector.hits])
        self.svc.recover()  # 启动恢复：inflight→pending
        self.assertEqual(self.storage.get_receipt(1).status, "pending")
        self.injector.disarm("dispatch")
        self.assertTrue(self.svc.dispatch(1))  # 恢复后允许重投
        self.assertEqual(len(self.oc.calls), 2)  # at-least-once：第二次 OC
        self.assertEqual(count(self.storage, "messages"), 1)  # 无第二条业务消息
        self.assertEqual(self.storage.get_receipt(1).status, "delivered")
        self.assertEqual(self.storage.get_message(1).body, b"b")  # id 不变


class TestReferenceLines(_Base):  # 断言8
    def test_export_success_path_exists_readable(self):
        self.service(exporter=_FileExporter(self.home)).post(
            "w", "d", self.TID, "question", "正文内容")
        text = self.oc.calls[0][2]
        self.assertIn("msg=state/messages/00000001.md", text)
        path = self.home.path / "state" / "messages" / "00000001.md"
        self.assertTrue(path.is_file())
        self.assertIn("正文内容", path.read_text(encoding="utf-8"))

    def test_export_failure_omits_msg_line(self):
        self.service(exporter=_FailingExporter()).post(
            "w", "d", self.TID, "question", "正文内容")
        text = self.oc.calls[0][2]
        self.assertNotIn("msg=", text)  # 引用行省略，不悬空
        self.assertIn("home=", text)  # 其余引用行保留
        self.assertIn("正文内容", text)


class TestBlobBody(_Base):  # 断言9
    def test_non_utf8_bytes_roundtrip_via_pull(self):
        blob = bytes(range(256))  # 含非法 UTF-8 序列与 NUL
        self.oc.mode = "false"  # 投递失败→留库
        msg = self.svc.post("w", "d", self.TID, "question", blob)
        self.assertIsInstance(msg.body, bytes)
        self.assertEqual(self.storage.get_message(msg.id).body, blob)
        got = self.svc.pull("d")
        self.assertEqual([m.id for m in got], [msg.id])
        self.assertEqual(got[0].body, blob)  # 逐字节一致


if __name__ == "__main__":
    unittest.main()
