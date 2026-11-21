"""Req: F147 — v3 GitStore Phase 0/1：Git 记账垂直切片测试。

断言清单（wiki/spec/gitstore-phase0-1.md「Tests required」逐条机械判定）：
1 init/config/head/ref＋零 SQLite 件；2 外来账本（agora.db/tasks.jsonl）拒建
且不留 journal.git；3 git 谓词白名单精确（plumbing 收/网络与业务动词拒）；
4 UoW 读己之写/外部读者不见 overlay/嵌套拒/事务外写拒；5 复合提交＝单 tx
＋期望对象版本＋父 HEAD＋重开重放等价；6 相邻提交 diff-tree 只增（canonical
不可变）；7 失败 UoW 与 CAS 冲突后标识符单调不复用；8 dedup_key/timer_id
幂等回原消息；9 MessagingService 消息垂直跑在 GitStore 上（OC 面复用 outbox
桩约）；10 canonical 损毁/重复 seq/缺引用对象 fail-closed。断言11（git
子进程圈禁＋SQL 圈禁）在 tests/test_architecture.py。

路径面裁定（F147）：spec canonical 路径清单未覆盖 get_credential（lineage
pull 必须可持久化 credential）与 append_event——按同款 bucket 方案扩两个
对象族 objects/credentials/<bucket>/<sid>/<v>.json、objects/events/<bucket>/
<seq>.json，保持「单 tx＋全部变更对象版本」提交不变量统一。
"""
import json
import os
import socket
import subprocess
import unittest

from agorad.domain import (Credential, DeliveryReceipt, Event, Message, Task)
from agorad.gitstore import (ALLOWED_VERBS, CommitConflict, ForeignLedgerError,
                             GitStore, JournalCorruptionError, check_verb)
from agorad.messaging import MessagingService
from agorad.storage import NoActiveUnitOfWork, StorageError, UnitOfWorkViolation
from tests import v3_harness as harness

TID = "ag-f147"
GIT_FAILPOINTS = ("write-object", "write-tree", "commit-tree", "update-ref",
                  "memory-publish")


# ---- 测试侧裸 git（断言/损毁语料用；生产圈禁只在 agorad/）----

def _plumb(state, *args, index=None, input_bytes=None):
    env = dict(os.environ, GIT_DIR=str(state / "journal.git"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.invalid")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(["git", *args], env=env, input=input_bytes,
                          capture_output=True)


def _head_files(state):
    """HEAD 树全路径→blob sha（测试侧读树面）。"""
    r = _plumb(state, "ls-tree", "-r", "HEAD")
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.decode().splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def _blob(state, sha):
    return _plumb(state, "cat-file", "blob", sha).stdout


def _mutate_head(state, changes):
    """在 HEAD 之上以 plumbing 落一笔改写提交（{path: bytes|None}）——
    损毁语料制造器（None＝删路径）。"""
    index = state / "mutate-index"
    index.unlink(missing_ok=True)
    _plumb(state, "read-tree", "HEAD", index=index)
    for path, data in changes.items():
        if data is None:
            _plumb(state, "update-index", "--force-remove", path, index=index)
        else:
            sha = _plumb(state, "hash-object", "-w", "--stdin",
                         input_bytes=data, index=index).stdout.decode().strip()
            _plumb(state, "update-index", "--add", "--cacheinfo",
                   f"100644,{sha},{path}", index=index)
    tree = _plumb(state, "write-tree", index=index).stdout.decode().strip()
    head = _plumb(state, "rev-parse", "HEAD").stdout.decode().strip()
    commit = _plumb(state, "commit-tree", "-p", head, "-m", "mutate",
                    tree).stdout.decode().strip()
    r = _plumb(state, "update-ref", "refs/agora/journal", commit)
    assert r.returncode == 0, r.stderr


def _bucket_of(key):
    import hashlib
    h = hashlib.sha256(str(key).encode()).hexdigest()
    return "%s/%s" % (h[:2], h[2:4])


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS:
            self.injector.register(fp)

    def store(self, **over):
        kw = dict(injector=self.injector)
        kw.update(over)
        return GitStore(self.home.state, **kw)


class TestInitConfig(_Base):  # 断言1
    CONFIG = {"core.fsync": "object,reference",
              "core.fsyncMethod": "fsync",
              "core.logAllRefUpdates": "false",
              "core.autocrlf": "false",
              "gc.auto": "0",
              "gc.autoDetach": "false"}

    def test_bare_layout_config_head_ref_and_no_db_file(self):
        st = self.store()
        j = self.home.state / "journal.git"
        self.assertTrue(j.is_dir())
        for d in ("objects", "refs"):
            self.assertTrue((j / d).is_dir())
        self.assertEqual(
            _plumb(self.home.state, "symbolic-ref", "HEAD").stdout.decode().strip(),
            "refs/agora/journal")  # HEAD 符号指向 journal ref
        cfg = j / "config"
        for key, want in self.CONFIG.items():
            r = _plumb(self.home.state, "config", "--file", str(cfg),
                       "--get", key)
            self.assertEqual(r.stdout.decode().strip(), want, key)
        r = _plumb(self.home.state, "show-ref", "refs/agora/journal")
        self.assertNotEqual(r.returncode, 0)  # 空历史：ref 未生
        with st.uow():
            st.upsert_task(Task(id=TID))
        r = _plumb(self.home.state, "show-ref", "refs/agora/journal")
        self.assertEqual(r.returncode, 0)
        head = r.stdout.decode().split()[0]
        self.assertEqual(st.head, head)  # ref＝内存 head
        self.assertEqual(list(self.home.state.glob("*.db")), [])  # 零 SQLite 件


class TestForeignLedger(_Base):  # 断言2
    def test_agora_db_home_refused_before_init(self):
        (self.home.state / "agora.db").write_bytes(b"legacy")
        with self.assertRaises(ForeignLedgerError) as cm:
            self.store()
        self.assertIsInstance(cm.exception, StorageError)  # 存储层专用错
        self.assertFalse((self.home.state / "journal.git").exists())
        # 拒绝＝零动土：不生 ref/对象/config/骨架（仅剩 harness 既有骨架件）
        self.assertEqual(
            sorted(p.name for p in self.home.state.iterdir()
                   if p.name != "agora.db"),
            ["messages"])

    def test_tasks_jsonl_home_refused_before_init(self):
        (self.home.state / "tasks.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ForeignLedgerError):
            self.store()
        self.assertFalse((self.home.state / "journal.git").exists())

    def test_existing_journal_wins_over_stale_foreign_files(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
        (self.home.state / "agora.db").write_bytes(b"stale")  # journal 在＝不拒
        self.assertIsNotNone(self.store().get_task(TID))


class TestVerbAllowlist(unittest.TestCase):  # 断言3
    def test_exact_allowlist_accepts_plumbing_rejects_network_and_business(self):
        self.assertEqual(ALLOWED_VERBS, frozenset({
            "hash-object", "mktree", "read-tree", "update-index", "write-tree",
            "commit-tree", "update-ref", "ls-tree", "cat-file", "rev-parse",
            "config", "symbolic-ref", "show-ref"}))  # 精确＝不多不少
        for verb in ALLOWED_VERBS:
            check_verb(verb)  # plumbing 全收
        for verb in ("fetch", "push", "clone", "remote", "add", "commit",
                     "merge", "rebase", "pull"):
            with self.assertRaises(StorageError, msg=verb):
                check_verb(verb)  # 网络/业务动词一律拒


class TestUowSemantics(_Base):  # 断言4
    def test_read_your_writes_and_outside_reader_sees_head_only(self):
        st = self.store()
        t = Task(id=TID, state="running", title="v1")
        reader = self.store()
        with st.uow():
            st.upsert_task(t)
            self.assertEqual(st.get_task(TID), t)  # overlay 先见
            self.assertIsNone(reader.get_task(TID))  # 外部读者只见 HEAD
        self.assertEqual(st.get_task(TID), t)  # 提交后 base 见
        self.assertEqual(self.store().get_task(TID), t)  # 重开重放见

    def test_nested_uow_rejected(self):
        st = self.store()
        with st.uow():
            with self.assertRaises(StorageError):
                with st.uow():
                    pass

    def test_write_outside_uow_rejected(self):
        st = self.store()
        for call in (lambda: st.upsert_task(Task(id=TID)),
                     lambda: st.insert_message(
                         ts_ms=1, task_id=TID, kind="note",
                         from_sid="w", to_sid="d", body="b"),
                     lambda: st.append_event(Event(seq=0, ts_ms=1, actor="a",
                                                   event="e"))):
            with self.assertRaises(NoActiveUnitOfWork):
                call()

    def test_uow_guard_blocks_network_and_business_subprocess(self):
        st = self.store()
        with st.uow():
            with self.assertRaises(UnitOfWorkViolation):
                socket.socket()  # UoW 守卫禁网络（§4 同 storage 面）
            with self.assertRaises(UnitOfWorkViolation):
                subprocess.run(["git", "status"])  # 业务 git 同禁
        s = socket.socket()  # UoW 结束恢复
        s.close()


class TestCompositeCommit(_Base):  # 断言5
    def test_one_tx_expected_versions_parent_and_replay_equality(self):
        st = self.store()
        t1 = Task(id=TID, state="running", after=("dep",))
        with st.uow():
            st.upsert_task(t1)
        base_head = st.head
        t2 = Task(id=TID, state="done", after=("dep",), commit="a" * 40)
        blob = bytes(range(256))  # 非 UTF-8 字节体
        with st.uow():
            st.upsert_task(t2)
            msg = st.insert_message(ts_ms=123, task_id=TID, kind="question",
                                    from_sid="w", to_sid="d", body=blob,
                                    dedup_key="dk-c")
            rc = DeliveryReceipt(message_id=msg.id, status="pending",
                                 attempts=0, updated_ms=123)
            st.upsert_receipt(rc)
            ev = st.append_event(Event(seq=0, ts_ms=123, actor="w",
                                       event="message_posted", task=TID,
                                       detail={"k": 1}))
        # 单 tx：新提交恰增一条 tx 记录（seq=2）
        files = _head_files(self.home.state)
        tx_files = sorted(p for p in files if p.startswith("tx/"))
        self.assertEqual(len(tx_files), 2)
        self.assertIn("tx/%s/%020d.json" % (_bucket_of(2), 2), tx_files)
        # 期望对象版本：task v2／消息 meta+body／receipt v1／event seq1
        self.assertIn("objects/tasks/%s/%s/%020d.json"
                      % (_bucket_of(TID), TID, 2), files)
        mdir = "objects/messages/%s/%020d" % (_bucket_of(str(msg.id)), msg.id)
        self.assertIn(mdir + "/meta.json", files)
        self.assertIn(mdir + "/body.txt", files)
        self.assertIn("objects/receipts/%s/%020d/%020d.json"
                      % (_bucket_of(str(msg.id)), msg.id, 1), files)
        self.assertIn("objects/events/%s/%020d.json"
                      % (_bucket_of("1"), 1), files)
        # 父 HEAD：新提交的 parent＝提交前 head
        commit = _plumb(self.home.state, "cat-file", "commit",
                        st.head).stdout.decode()
        self.assertIn("parent %s" % base_head, commit)
        # 重开重放＝等价（全家族含非 UTF-8 字节体逐字节）
        fresh = self.store()
        self.assertEqual(fresh.get_task(TID), t2)
        self.assertEqual(fresh.messages_all(), [msg])
        self.assertEqual(fresh.get_message(msg.id).body, blob)
        self.assertEqual(fresh.get_receipt(msg.id), rc)
        self.assertEqual(fresh.events_all(), [ev])
        # tx JSON 结构面：spec 字段齐＋操作引用路径在树
        tx = json.loads(_blob(self.home.state, files[tx_files[-1]]))
        self.assertEqual(tx["schema_version"], 1)
        self.assertEqual(tx["epoch"], 1)
        self.assertEqual(tx["seq"], 2)
        self.assertEqual(tx["base_head"], base_head)
        self.assertTrue(tx["operations"])
        for op in tx["operations"]:
            self.assertIn(op["path"], files)
            self.assertIn(op["op"], ("upsert_task", "insert_message",
                                     "upsert_receipt", "append_event"))


class TestImmutability(_Base):  # 断言6
    def test_diff_tree_between_commits_shows_additions_only(self):
        st = self.store()
        heads = []
        with st.uow():
            st.upsert_task(Task(id=TID, title="v1"))
        heads.append(st.head)
        with st.uow():
            st.upsert_task(Task(id=TID, title="v2"))
            st.insert_message(ts_ms=1, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="m1")
        heads.append(st.head)
        with st.uow():
            st.upsert_receipt(DeliveryReceipt(message_id=1, status="delivered"))
            st.upsert_cursor("ses-a", 1)
        heads.append(st.head)
        for old, new in zip(heads, heads[1:]):
            r = _plumb(self.home.state, "diff-tree", "-r", "--name-status",
                       old, new)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = [l for l in r.stdout.decode().splitlines() if l.strip()]
            self.assertTrue(lines)
            for line in lines:
                self.assertTrue(line.startswith("A\t"), line)  # 只增不改不删


class TestIdentifierMonotonicity(_Base):  # 断言7
    def test_failed_uow_never_reuses_ids_or_seqs(self):
        st = self.store()
        self.injector.arm("write-object", "before")
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="x")
                st.append_event(Event(seq=0, ts_ms=1, actor="a", event="e"))
        self.assertIsNone(st.head)  # 未落任何提交
        self.injector.disarm("write-object")
        with st.uow():
            m = st.insert_message(ts_ms=2, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="y")
            ev = st.append_event(Event(seq=0, ts_ms=2, actor="a", event="e2"))
        self.assertEqual((m.id, ev.seq), (2, 2))  # 失败窗占用 1 后不复用
        self.assertEqual([x.id for x in st.messages_all()], [2])
        self.assertEqual([e.seq for e in st.events_all()], [2])

    def test_cas_conflict_discards_overlay_and_retries_without_reuse(self):
        g1 = self.store()
        with g1.uow():
            g1.insert_message(ts_ms=1, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="m1")
        g2 = self.store()
        with self.assertRaises(CommitConflict):
            with g2.uow():  # 基于旧 head 的 overlay
                g2.insert_message(ts_ms=2, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="m2-stale")
                with g1.uow():  # 竞态：g1 先落新提交，ref 前移
                    g1.insert_message(ts_ms=3, task_id=TID, kind="note",
                                      from_sid="w", to_sid="d", body="m2-wins")
                self.assertIsNotNone(g2.get_message(2))  # overlay 自见
        # 冲突后：g2 重载基线、重试分配更高 id（不复用竞态双方占用过的号）
        self.assertIsNotNone(g2.get_message(2))  # 重载后见 g1 竞态提交
        with g2.uow():
            m = g2.insert_message(ts_ms=7, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="d")
        self.assertEqual(m.id, 3)
        # 标识符序与提交序一致：重开重放按 id 升序全量可见
        fresh = self.store()
        self.assertEqual([x.id for x in fresh.messages_all()], [1, 2, 3])


class TestDedupIdempotent(_Base):  # 断言8
    def test_same_dedup_key_or_timer_id_returns_original_no_new_objects(self):
        st = self.store()
        with st.uow():
            m1 = st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                   from_sid="w", to_sid="d", body="orig",
                                   dedup_key="dk-1")
        head, files = st.head, _head_files(self.home.state)
        with st.uow():
            m2 = st.insert_message(ts_ms=2, task_id=TID, kind="note",
                                   from_sid="w", to_sid="d", body="retry",
                                   dedup_key="dk-1")
        self.assertEqual(m2.id, m1.id)  # 原消息返回
        self.assertEqual(m2.body, b"orig")
        self.assertEqual(st.head, head)  # 零新 tx 零新提交
        self.assertEqual(_head_files(self.home.state), files)
        with st.uow():
            m3 = st.insert_message(ts_ms=3, task_id=TID, kind="timer",
                                   from_sid="w", to_sid="d", body="t1",
                                   timer_id="tm-1")
        with st.uow():
            m4 = st.insert_message(ts_ms=4, task_id=TID, kind="timer",
                                   from_sid="w", to_sid="d", body="t2",
                                   timer_id="tm-1")
        self.assertEqual(m4.id, m3.id)  # timer 幂等同口径
        self.assertEqual(m4.body, b"t1")
        with st.uow():
            m5 = st.insert_message(ts_ms=5, task_id=TID, kind="note",
                                   from_sid="w", to_sid="d", body="n")
        self.assertEqual({m1.id, m3.id, m5.id}, {1, 2, 3})  # 异键正常新建


class OcStub:  # outbox 桩约单测内联形态（GitStore 无 SQL 读者面，seen 恒缺）
    def __init__(self, mode=None):
        self.calls = []
        self.mode = mode

    def _record(self, channel, sid, text):
        self.calls.append((channel, sid, text))
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


class TestMessagingVertical(unittest.TestCase):  # 断言9
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS + ("dispatch",):
            self.injector.register(fp)
        self.clock = FakeClock()
        self.store = GitStore(self.home.state, injector=self.injector)
        with self.store.uow():
            self.store.upsert_task(Task(id=TID, state="running"))
        self.oc = OcStub()
        self.svc = MessagingService(
            self.store, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)

    def test_receipt_lifecycle_and_cursor_advancement(self):
        self.oc.mode = "false"
        m1 = self.svc.post("w", "d", TID, "question", "b1")
        r = self.store.get_receipt(m1.id)  # OC 败→pending 记账
        self.assertEqual((r.status, r.attempts), ("pending", 1))
        self.oc.mode = None
        m2 = self.svc.post("w", "d", TID, "question", "b2")
        self.assertEqual(self.store.get_receipt(m2.id).status, "delivered")
        got = self.svc.pull("d")
        self.assertEqual([m.id for m in got], [m1.id, m2.id])
        cur = self.store.get_cursor("d")
        self.assertEqual(cur.last_read_ms, m2.id)  # cursor＝last-read id
        self.assertEqual({self.store.get_receipt(i).status
                          for i in (m1.id, m2.id)}, {"delivered"})  # 抑制重投
        self.assertEqual(self.svc.pull("d"), [])  # 二次 pull 不重复

    def test_inflight_crash_then_dispatch_retry_same_id(self):
        self.injector.arm("dispatch", "after")  # OC 成功后、delivered 落账前
        with self.assertRaises(harness.FaultInjected):
            self.svc.post("w", "d", TID, "question", "b")
        self.assertEqual(len(self.oc.calls), 1)
        self.assertEqual(self.store.get_receipt(1).status, "inflight")
        self.svc.recover()  # 启动恢复 inflight→pending
        self.assertEqual(self.store.get_receipt(1).status, "pending")
        self.injector.disarm("dispatch")
        self.assertTrue(self.svc.dispatch(1))  # at-least-once 同 id 重投
        self.assertEqual(len(self.oc.calls), 2)
        self.assertEqual(self.oc.calls[0][2], self.oc.calls[1][2])  # 同文本
        self.assertEqual([m.id for m in self.store.messages_all()], [1])
        self.assertEqual(self.store.get_receipt(1).status, "delivered")
        fresh = GitStore(self.home.state)  # 重放面：单消息单对象
        self.assertEqual([m.id for m in fresh.messages_all()], [1])
        self.assertEqual(fresh.get_receipt(1).status, "delivered")

    def test_lineage_pull_full_backfill_for_new_sid(self):
        self.svc.post("w", "ses-old", TID, "question", "b1")
        self.svc.post("w", "ses-old", TID, "note", "n1")
        self.assertEqual([m.id for m in self.svc.pull("ses-old")], [1, 2])
        self.assertEqual(self.svc.pull("ses-old"), [])
        self.assertEqual(self.svc.pull("ses-other"), [])  # 无血统不可见
        with self.store.uow():
            self.store.put_credential(Credential(
                sid="ses-new", token="t", lineage=("ses-old",)))
        got = self.svc.pull("ses-new")  # 新 sid cursor=0 全量血统补拉
        self.assertEqual([m.id for m in got], [1, 2])
        self.assertEqual(self.store.get_cursor("ses-new").last_read_ms, 2)
        fresh = GitStore(self.home.state)  # credential 持久化重放
        self.assertEqual(fresh.get_credential("ses-new").lineage, ("ses-old",))

    def test_non_utf8_body_byte_preserving_across_replay(self):
        blob = bytes(range(256))
        self.oc.mode = "false"
        msg = self.svc.post("w", "d", TID, "question", blob)
        self.assertEqual(self.store.get_message(msg.id).body, blob)
        got = self.svc.pull("d")
        self.assertEqual(got[0].body, blob)
        self.assertEqual(GitStore(self.home.state).get_message(1).body, blob)


class TestFaultInjection(_Base):  # spec §Fault injection：update-ref 两相位
    def test_before_update_ref_no_ref_change_orphan_commit_allowed(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
        head = st.head
        loose_before = self._loose_objects()
        self.injector.arm("update-ref", "before")
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="orphan")
        self.assertEqual(st.head, head)  # ref 未动
        self.assertGreater(self._loose_objects(), loose_before)  # 孤儿提交允许
        self.assertIsNone(GitStore(self.home.state).get_message(1))  # 重开不见
        self.injector.disarm("update-ref")
        with st.uow():  # 原实例续写正常（内存基线未损；弃单号不复用）
            st.insert_message(ts_ms=2, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="after")
        self.assertEqual([m.id for m in st.messages_all()], [2])
        self.assertEqual([m.id for m in GitStore(self.home.state).messages_all()],
                         [2])

    def test_after_update_ref_reconciled_exactly_once_on_reopen(self):
        st = self.store()
        self.injector.arm("update-ref", "after")  # ref 已动、内存未发布
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="m1")
        fresh = GitStore(self.home.state)  # 重开对账恰一次：恰一条消息
        self.assertEqual([m.id for m in fresh.messages_all()], [1])
        with fresh.uow():
            m = fresh.insert_message(ts_ms=2, task_id=TID, kind="note",
                                     from_sid="w", to_sid="d", body="m2")
        self.assertEqual(m.id, 2)  # 无重复 seq/消息：续号不复用
        self.assertEqual([x.id for x in GitStore(self.home.state).messages_all()],
                         [1, 2])

    def _loose_objects(self):
        r = _plumb(self.home.state, "count-objects", "-v")
        for line in r.stdout.decode().splitlines():
            if line.startswith("count:"):
                return int(line.split()[1])
        return 0


class TestCorruptionFailClosed(_Base):  # 断言10
    def _seed(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            st.insert_message(ts_ms=1, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="m")
        return st

    def test_canonical_json_garbled_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        [task_path] = [p for p in files if p.startswith("objects/tasks/")]
        _mutate_head(self.home.state, {task_path: b"<<<not json"})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_duplicate_tx_seq_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        [tx1] = [p for p in files if p.endswith("tx/%s/%020d.json"
                                                % (_bucket_of(1), 1))]
        tx = json.loads(_blob(self.home.state, files[tx1]))
        tx["seq"] = 1  # 冒称既有 seq（落位路径 seq=2）
        _mutate_head(self.home.state, {
            "tx/%s/%020d.json" % (_bucket_of(2), 2):
                json.dumps(tx).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_missing_referenced_object_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        [body_path] = [p for p in files if p.endswith("body.txt")]
        _mutate_head(self.home.state, {body_path: None})  # 删消息字节体
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_object_path_id_mismatch_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        [task_path] = [p for p in files if p.startswith("objects/tasks/")]
        obj = json.loads(_blob(self.home.state, files[task_path]))
        obj["id"] = "ag-other"  # 内容 id 与路径 id 不符
        _mutate_head(self.home.state,
                     {task_path: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()


if __name__ == "__main__":
    unittest.main()
