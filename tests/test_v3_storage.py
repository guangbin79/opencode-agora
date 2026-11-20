"""Req: F133 — v3 SQLite 存储 core（opencode-agora v3 P2-T6）。

断言清单（任务页 ag-1fe3af，逐条机械判定）：1 schema 初始化（14 表全集
——任务页计数「15」与两处枚举 14 名不符，以枚举名单为准）＋PRAGMA＋
0600；2 UoW 原子性；3 UoW 禁网络/子进程/文件遍历；4 CRUD＋孤儿 timer
FK 拒；5 唯一约束与幂等回读；6 读写并发；7 写锁有界；8 热路径索引无
全表 SCAN；9 版本守卫；10 body BLOB 非 UTF-8 往返。架构守护在
test_architecture.py（storage 分层＋SQL 圈禁）。
"""
import dataclasses
import os
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import unittest

from agorad import domain
from agorad.storage import (SCHEMA_VERSION, TABLES, NoActiveUnitOfWork,
                            SchemaVersionError, Storage, UnitOfWorkViolation)
from tests import v3_harness as harness

FOUR_TABLES = ("tasks", "messages", "delivery_receipts", "events")


def make_storage(home, **kw):
    return Storage(home.state / "agora.db", **kw)


def mk_task(tid="ag-st01", **over):
    base = dict(id=tid, state="running", title="t", goal="g", root_sid="root",
                client_key=None, created_ms=1, updated_ms=2, after=("ag-p1",))
    base.update(over)
    return domain.Task(**base)


def composite_write(storage, tid="ag-st01"):
    """task＋message＋receipt＋event 复合写（断言2 语料）。"""
    storage.upsert_task(mk_task(tid))
    msg = storage.insert_message(ts_ms=100, task_id=tid, kind="note",
                                 from_sid="w", to_sid="disp", body="hello",
                                 dedup_key=f"dk-{tid}")
    storage.upsert_receipt(domain.DeliveryReceipt(message_id=msg.id))
    storage.append_event(domain.Event(seq=0, ts_ms=100, actor="w",
                                      event="note", task=tid))
    return msg


class _HomeTest(unittest.TestCase):
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.storage = make_storage(self.home)
        self.addCleanup(self.storage.close)


class TestSchemaInit(_HomeTest):  # 断言1
    def test_tables_pragmas_permissions(self):
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        names = {r[0] for r in reader.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, set(TABLES))  # 14 表全集（枚举权威）
        self.assertEqual(len(TABLES), 14)
        self.assertEqual(
            reader.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(
            reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(reader.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreater(
            reader.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        mode = stat.S_IMODE(os.stat(self.home.state / "agora.db").st_mode)
        self.assertEqual(mode, 0o600)


class TestUowAtomicity(_HomeTest):  # 断言2
    @staticmethod
    def _counts(conn):
        return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in FOUR_TABLES]

    def test_mid_uow_fault_zero_residue(self):
        injector = harness.FaultInjector().register("uow")
        storage = make_storage(self.home, injector=injector)
        self.addCleanup(storage.close)
        injector.arm("uow", "after")  # 写完、COMMIT 前中途炸
        with self.assertRaises(harness.FaultInjected):
            with storage.uow():
                composite_write(storage)
        reader = storage.open_reader()
        self.addCleanup(reader.close)
        self.assertEqual(self._counts(reader), [0, 0, 0, 0])  # 四表零残留
        injector.disarm("uow")
        with storage.uow():  # 库未坏：回滚后可继续写
            composite_write(storage)
        self.assertEqual(self._counts(storage.open_reader()), [1, 1, 1, 1])

    def test_commit_persists_all(self):
        with self.storage.uow():
            msg = composite_write(self.storage)
        self.storage.close()
        reopened = make_storage(self.home)  # 重开＝落盘证明
        self.addCleanup(reopened.close)
        self.assertEqual(self._counts(reopened.open_reader()), [1, 1, 1, 1])
        self.assertEqual(reopened.get_message(msg.id).body, b"hello")


class TestUowGuard(_HomeTest):  # 断言3
    def test_dangerous_calls_blocked_inside_uow(self):
        with self.storage.uow():
            for danger in (lambda: socket.socket(),
                          lambda: subprocess.run(["true"]),
                          lambda: os.walk(str(self.home.path))):
                with self.assertRaises(UnitOfWorkViolation):
                    danger()
        self.assertIsInstance(socket.socket, type)  # 退出后恢复原入口
        self.assertTrue(callable(os.walk))

    def test_other_thread_not_blocked_during_uow(self):  # 评审环 R1 修4 锚
        results = []

        def other_thread_runs_subprocess():
            results.append(subprocess.run(["true"]).returncode)

        with self.storage.uow():
            thread = threading.Thread(target=other_thread_runs_subprocess)
            thread.start()
            thread.join(10)
        self.assertEqual(results, [0])  # 持锁线程外放行原实现

    def test_writes_require_uow(self):
        with self.assertRaises(NoActiveUnitOfWork):
            self.storage.upsert_task(mk_task())


class TestRepositories(_HomeTest):  # 断言4
    def test_task_roundtrip_full_and_1to1_replace(self):
        full = domain.Task(
            id="ag-full", state="done", title="标题", goal="g", req_id="F1",
            model="m", quick=True, deliverable="d", chain_depth=2,
            root_sid="root", dispatcher_sid="disp", worker_sid="w",
            created_ms=1, updated_ms=2, fail_streak=1, after=("ag-a", "ag-b"),
            waiting_on="ag-x", client_key="ck", last_seen_ms=9,
            project_dir="/repo", target_branch="b", env_dir="/env",
            env_base="e" * 40, commit="c" * 40, rebased_from="r" * 40,
            verify_pending=True, review_cmd="rc",
            verify=domain.VerifySpec(cmd="pytest", state="registered",
                                     registered_by="disp", registered_at_ms=5,
                                     timeout_s=300, timeout_source="task"),
            delivery=domain.DeliveryState(mode="managed", status="pending",
                                          target_branch="b", attempt=1,
                                          local_status="ok"),
            probe=domain.BaseProbe(result="green", cmd="p", base="b",
                                   ran_at_ms=3),
            gate=domain.GateState(
                in_gate=True, gate_env="/g", review_fail_streak=1,
                gate_artifact=domain.Artifact(
                    task_id="ag-full", phase="verify", path="/p", cmd="c",
                    rc=0, duration_s=1.5, bytes=10, started_at_ms=1,
                    ended_at_ms=2),
                review_cache={"cmd": "x", "ok": True}),
            legacy_extra_json='{"future": 1}')
        with self.storage.uow():
            self.storage.upsert_task(full)
        self.assertEqual(self.storage.get_task("ag-full"), full)  # 全字段往返
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        for table in ("task_verify", "task_delivery", "task_gate"):
            self.assertEqual(
                reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                1, table)  # 1:1 子表单行
        replaced = dataclasses.replace(full, state="merged", delivery=None)
        with self.storage.uow():
            self.storage.upsert_task(replaced)
        self.assertEqual(self.storage.get_task("ag-full"), replaced)  # 覆盖旧行
        self.assertEqual(self._sub_counts(reader), (1, 0, 1))  # delivery None 撤行

    def _sub_counts(self, reader):
        return tuple(
            reader.execute(f"SELECT COUNT(*) FROM task_{s}").fetchone()[0]
            for s in ("verify", "delivery", "gate"))

    def test_row_entities_roundtrip(self):
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            msg = self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="question", from_sid="w",
                to_sid="d", body="q")
            receipt = domain.DeliveryReceipt(
                message_id=msg.id, status="inflight", attempts=2,
                last_error="e", email_status="sent", lease_until_ms=99,
                updated_ms=100)
            self.storage.upsert_receipt(receipt)
            approval = domain.Approval(
                id="ap-1", task_id="ag-st01", requester_sid="w",
                backend="native", question="Q?", metadata={"task": "ag-st01"},
                native_request_id="nr-1", deadline_ms=500, created_ms=1)
            self.storage.upsert_approval(approval)
            self.storage.upsert_approval(domain.Approval(
                id="ap-2", requester_sid="root"))  # task_id ""→NULL（可空 FK）
            self.storage.upsert_cursor("ses-a", 1234)
            event = self.storage.append_event(domain.Event(
                seq=0, ts_ms=5, actor="w", event="created", task="ag-st01",
                detail={"k": 1}))
            artifact = domain.Artifact(task_id="ag-st01", phase="verify",
                                       rc=0, duration_s=2.0, bytes=5)
            self.storage.insert_artifact(artifact)
            credential = domain.Credential(
                sid="ses-new", token="tok", task_id="ag-st01",
                lineage=("ses-old", "ses-oldest"), created_ms=7)
            self.storage.put_credential(credential)
        self.assertEqual(self.storage.get_receipt(msg.id), receipt)
        self.assertEqual(self.storage.get_approval("ap-1"), approval)
        self.assertEqual(self.storage.get_approval("ap-2").task_id, "")
        self.assertEqual(self.storage.get_cursor("ses-a").last_read_ms, 1234)
        self.assertEqual(event.seq, 1)  # 全局 seq 派号
        self.assertEqual([e.seq for e in self.storage.events_for_task("ag-st01")],
                         [1])
        self.assertEqual(self.storage.artifacts_for_task("ag-st01"), [artifact])
        self.assertEqual(self.storage.get_credential("ses-new"), credential)
        self.assertEqual(self.storage.get_message(msg.id).body, b"q")
        timer = domain.Timer(id="tm-1", task_id="ag-st01", due_ms=1000)
        with self.storage.uow():
            self.storage.upsert_timer(timer)
        self.assertEqual(self.storage.get_timer("tm-1"), timer)
        for getter, arg in ((self.storage.get_task, "none"),
                            (self.storage.get_timer, "none"),
                            (self.storage.get_approval, "none"),
                            (self.storage.get_receipt, 999),
                            (self.storage.get_cursor, "none"),
                            (self.storage.get_credential, "none"),
                            (self.storage.get_message, 999)):
            self.assertIsNone(getter(arg))

    def test_upsert_task_keeps_cascade_children(self):  # 评审环 R1 修1 锚
        with self.storage.uow():
            composite_write(self.storage)
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)

        def counts():
            return [reader.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in FOUR_TABLES]
        updated = dataclasses.replace(mk_task(), state="done")
        with self.storage.uow():
            self.storage.upsert_task(updated)
        self.assertEqual(self.storage.get_task("ag-st01"), updated)  # 状态已更新
        self.assertEqual(counts(), [1, 1, 1, 1])  # 消息/回执/事件不被级联清空

    def test_upsert_timer_after_its_message(self):  # 评审环 R1 修2 锚（§6 对账）
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            self.storage.upsert_timer(domain.Timer(id="tm-f", task_id="ag-st01"))
            self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="timer", from_sid="sys",
                to_sid="w", body="tick", timer_id="tm-f")
            self.storage.upsert_timer(domain.Timer(  # 消息已引用后仍可补 fired
                id="tm-f", task_id="ag-st01",
                state=domain.TimerState.FIRED, fired_ms=99))
        self.assertIs(self.storage.get_timer("tm-f").state,
                      domain.TimerState.FIRED)

    def test_orphan_timer_rejected_by_fk(self):
        with self.storage.uow():
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.upsert_timer(domain.Timer(id="tm-x",
                                                       task_id="gt-nope"))


class TestUniqueness(_HomeTest):  # 断言5
    def test_client_key_conflict(self):
        with self.storage.uow():
            self.storage.upsert_task(mk_task("ag-a", client_key="k"))
        with self.storage.uow(), self.assertRaises(sqlite3.IntegrityError):
            self.storage.upsert_task(mk_task("ag-b", client_key="k"))

    def test_message_dedup_key_idempotent(self):
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            first = self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="note", from_sid="w",
                to_sid="d", body="b1", dedup_key="dk")
            again = self.storage.insert_message(
                ts_ms=2, task_id="ag-st01", kind="note", from_sid="w",
                to_sid="d", body="b2", dedup_key="dk")
        self.assertEqual((again.id, again.body), (first.id, b"b1"))  # 既有行
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        self.assertEqual(
            reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_message_timer_id_idempotent(self):
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            self.storage.upsert_timer(domain.Timer(id="tm-9", task_id="ag-st01"))
            first = self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="timer", from_sid="sys",
                to_sid="w", body="a", timer_id="tm-9")
            again = self.storage.insert_message(
                ts_ms=2, task_id="ag-st01", kind="timer", from_sid="sys",
                to_sid="w", body="b", timer_id="tm-9")
        self.assertEqual(again.id, first.id)

    def test_both_keys_collision_on_timer_id(self):  # 评审环 R1 修3 锚
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            self.storage.upsert_timer(domain.Timer(id="tm-b", task_id="ag-st01"))
            first = self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="timer", from_sid="sys",
                to_sid="w", body="a", dedup_key="dk-a", timer_id="tm-b")
            again = self.storage.insert_message(  # 撞的是 timer_id 非 dedup_key
                ts_ms=2, task_id="ag-st01", kind="timer", from_sid="sys",
                to_sid="w", body="b", dedup_key="dk-b", timer_id="tm-b")
        self.assertEqual(again.id, first.id)  # 回读既有行而非 None

    def test_native_request_id_rejected(self):
        def approval(aid, native):
            return domain.Approval(id=aid, task_id="ag-st01",
                                   requester_sid="w", native_request_id=native)
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            self.storage.upsert_approval(approval("ap-1", "nr"))
        with self.storage.uow(), self.assertRaises(sqlite3.IntegrityError):
            self.storage.upsert_approval(approval("ap-2", "nr"))


class TestConcurrency(_HomeTest):  # 断言6
    def test_reader_snapshot_then_new_after_commit(self):
        with self.storage.uow():
            composite_write(self.storage)  # 先落一条已提交
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)

        def count():
            return reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        self.assertEqual(count(), 1)
        entered, release = threading.Event(), threading.Event()

        def writer():
            with self.storage.uow():
                self.storage.insert_message(
                    ts_ms=2, task_id="ag-st01", kind="note", from_sid="w",
                    to_sid="disp", body="second")
                entered.set()
                self.assertTrue(release.wait(10))

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(entered.wait(10))
        self.assertEqual(count(), 1)  # 未提交写事务期间＝旧快照
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(count(), 2)  # commit 后读者见新


class TestWriteLockBounded(unittest.TestCase):  # 断言7
    def test_second_writer_bounded_retry(self):
        home = harness.TempAgoraHome()
        self.addCleanup(home.cleanup)
        holder = Storage(home.state / "agora.db")
        self.addCleanup(holder.close)
        contender = Storage(home.state / "agora.db", busy_timeout_ms=300)
        self.addCleanup(contender.close)
        with holder.uow():
            holder.upsert_task(mk_task())
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                with contender.uow():
                    pass
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.2)  # 按 busy_timeout 受限等待
        self.assertLess(waited, 5)  # 有界、不死等
        with contender.uow():  # 锁释放后重试成功，进程不崩
            contender.upsert_task(mk_task("ag-b2"))
        self.assertIsNotNone(contender.get_task("ag-b2"))


class TestHotPathIndexes(_HomeTest):  # 断言8
    def test_hot_paths_hit_indexes(self):
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        plans = (
            ("SELECT id FROM messages WHERE to_sid=? AND id>?", ("d", 0)),
            ("SELECT id FROM timers WHERE status=? AND due_ms<=?",
             ("pending", 999)),
            ("SELECT id FROM approvals WHERE session_id=? AND status=?",
             ("w", "pending")),
            ("SELECT seq FROM events WHERE task_id=? AND seq>?", ("t", 0)),
        )
        for sql, params in plans:
            detail = " | ".join(row[3] for row in reader.execute(
                "EXPLAIN QUERY PLAN " + sql, params))
            self.assertNotIn("SCAN", detail, sql)  # 无全表 SCAN
            self.assertIn("INDEX", detail, sql)  # 命索引


class TestSchemaVersionGuard(unittest.TestCase):  # 断言9
    def test_future_version_rejected(self):
        home = harness.TempAgoraHome()
        self.addCleanup(home.cleanup)
        conn = sqlite3.connect(home.state / "agora.db")
        conn.execute("PRAGMA user_version=9")
        conn.close()
        with self.assertRaises(SchemaVersionError):
            Storage(home.state / "agora.db")


class TestBodyBlob(_HomeTest):  # 断言10
    def test_non_utf8_roundtrip(self):
        raw = b"\xff\xfe\x00bin\x80ary\xc3"
        with self.storage.uow():
            self.storage.upsert_task(mk_task())
            binary = self.storage.insert_message(
                ts_ms=1, task_id="ag-st01", kind="peer", from_sid="w",
                to_sid="d", body=raw)
            text = self.storage.insert_message(
                ts_ms=2, task_id="ag-st01", kind="peer", from_sid="w",
                to_sid="d", body="文本")
        self.assertEqual(self.storage.get_message(binary.id).body, raw)
        self.assertEqual(self.storage.get_message(text.id).body, "文本".encode())


if __name__ == "__main__":
    unittest.main()
