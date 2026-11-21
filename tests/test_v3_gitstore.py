"""Req: F147 — v3 GitStore Phase 0/1：Git 记账垂直切片测试。
Req: F150 — Phase 2 timer 仓库面（spec gitstore-phase2-timers）：九仓库
方法逐面（含 due 键集游标/None 键 SQL 语义）、objects/timers/<bucket>/
<id>/<v>.json 不可变版本＋tx from/to_version、取消是状态非删除、重放恢复
最新版本、弃单版本不复用、timer canonical 损毁 fail-closed、TimerService
契约移植（create/cancel/fire/catch-up＋崩窗对账）。
Req: F151 — Phase 2 凭据生命周期（spec gitstore-phase2-credentials）：
TestCredentialLifecycle（put/read/credentials_all/delete tombstone/幂等/
re-put 更晚版本/overlay 读己之写/重启外部文件水合/换号继用既录 secret）＋
TestCredentialSecretSafety（canonical 只含摘要＋secret_path、全 Git 对象
零明文牌、put 先验文件缺/不符 fail-closed 零提交、重开缺/错文件专用错、
存储不写不删外部文件、坏摘要/错型 fail-closed）。

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

from agorad.domain import (Credential, DeliveryReceipt, Event, Message, Task,
                           Timer, TimerState, timer_transition)
from agorad.gitstore import (ALLOWED_VERBS, CommitConflict, CredentialSecretError,
                             ForeignLedgerError, GitStore,
                             JournalCorruptionError, check_verb)
from agorad.messaging import MessagingService
from agorad.storage import NoActiveUnitOfWork, StorageError, UnitOfWorkViolation
from agorad.timersvc import TimerService
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
        with self.store.uow():  # Req: F151——put 前先落外部 secret（服务铁则）
            self.home.write_credential("ses-new", "t")
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

    def test_ref_deleted_reopen_resets_baseline_and_writes(self):  # R2 锚
        st = self.store()
        with st.uow():
            st.insert_message(ts_ms=1, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="old")
        r = _plumb(self.home.state, "update-ref", "-d",
                   "refs/agora/journal")  # ref 外部消失（CAS 重入同路径）
        self.assertEqual(r.returncode, 0, r.stderr)
        fresh = GitStore(self.home.state)
        self.assertIsNone(fresh.head)  # 基线清空不滞留陈旧 head
        with fresh.uow():
            fresh.insert_message(ts_ms=2, task_id=TID, kind="note",
                                 from_sid="w", to_sid="d", body="new")
        self.assertEqual([m.body for m in fresh.messages_all()], [b"new"])
        self.assertEqual(
            [m.body for m in GitStore(self.home.state).messages_all()],
            [b"new"])

    def _loose_objects(self):
        r = _plumb(self.home.state, "count-objects", "-v")
        for line in r.stdout.decode().splitlines():
            if line.startswith("count:"):
                return int(line.split()[1])
        return 0


class TestWriteBoundary(_Base):  # R1 锚：canonical 路径段白字符集
    def test_quote_and_non_ascii_rejected_legal_set_passes(self):
        st = self.store()
        with self.assertRaises(StorageError):
            with st.uow():
                st.upsert_task(Task(id='ag-q"x'))  # 引号＝ls-tree 转义面拒于写入
        with self.assertRaises(StorageError):
            with st.uow():
                st.upsert_cursor("ses-é", 1)  # 非 ASCII 同拒
        with st.uow():  # 合法字符集（字母数字._:@+-）放行且可重开
            st.upsert_cursor("ses_ok.1", 1)
        self.assertEqual(self.store().get_cursor("ses_ok.1").last_read_ms, 1)


class TestBootstrapCas(_Base):  # R1 锚：空历史首笔也 CAS
    def test_concurrent_first_commit_conflicts_no_silent_loss(self):
        s1 = self.store()
        s2 = self.store()  # 都在 ref 未生前重放（bootstrap 竞态窗）
        with s1.uow():
            s1.insert_message(ts_ms=1, task_id=TID, kind="note",
                              from_sid="w", to_sid="d", body="first")
        with self.assertRaises(CommitConflict):
            with s2.uow():
                s2.insert_message(ts_ms=2, task_id=TID, kind="note",
                                  from_sid="w", to_sid="d", body="second")
        fresh = self.store()
        self.assertEqual([m.body for m in fresh.messages_all()],
                         [b"first"])  # 后写者不得静默覆盖先写者


class TestHalfInitHealing(_Base):  # R1 锚：初始化崩溃窗骨架自愈
    def test_journal_dir_without_head_heals_and_writes(self):
        j = self.home.state / "journal.git"
        (j / "objects").mkdir(parents=True)
        (j / "refs").mkdir()  # 模拟 objects/refs 已建、HEAD 未写的崩溃窗
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
        self.assertIsNotNone(st.head)
        self.assertIsNotNone(self.store().get_task(TID))


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

    def test_orphan_body_without_meta_fails_closed(self):  # R1 锚
        self._seed()
        files = _head_files(self.home.state)
        [meta] = [p for p in files if p.endswith("meta.json")]
        _mutate_head(self.home.state, {meta: None})  # 删 meta 留字节体
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_non_dict_object_fails_closed_dedicated_error(self):  # R1
        self._seed()
        files = _head_files(self.home.state)
        [task_path] = [p for p in files if p.startswith("objects/tasks/")]
        _mutate_head(self.home.state, {task_path: b"5"})  # 合法 JSON 非对象
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_non_list_operations_fails_closed_dedicated_error(self):  # R1
        self._seed()
        files = _head_files(self.home.state)
        [tx_path] = [p for p in files if p.startswith("tx/")]
        tx = json.loads(_blob(self.home.state, files[tx_path]))
        tx["operations"] = {"a": 1}  # operations 非列表
        _mutate_head(self.home.state,
                     {tx_path: json.dumps(tx).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_unhashable_op_path_fails_closed_dedicated_error(self):  # R2
        self._seed()
        files = _head_files(self.home.state)
        [tx_path] = [p for p in files if p.startswith("tx/")]
        tx = json.loads(_blob(self.home.state, files[tx_path]))
        tx["operations"][0]["path"] = ["objects/tasks/x"]  # 不可哈希路径
        _mutate_head(self.home.state,
                     {tx_path: json.dumps(tx).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_bool_seq_fails_closed(self):  # R2 锚：bool 冒充 int seq
        self._seed()
        files = _head_files(self.home.state)
        [task_path] = [p for p in files if p.startswith("objects/tasks/")]
        obj = json.loads(_blob(self.home.state, files[task_path]))
        obj["seq"] = True  # True == 1 不得溜过形状闸
        _mutate_head(self.home.state,
                     {task_path: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_tx_wrong_bucket_fails_closed(self):  # R1 锚：tx 落位 bucket 校验
        self._seed()
        files = _head_files(self.home.state)
        [tx1] = [p for p in files if p.endswith("tx/%s/%020d.json"
                                               % (_bucket_of(1), 1))]
        tx = json.loads(_blob(self.home.state, files[tx1]))
        tx["seq"] = 3  # 路径序号自洽但落错 bucket
        _mutate_head(self.home.state, {
            "tx/de/ad/%020d.json" % 3: json.dumps(tx).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()


class TestTimerRepository(_Base):  # Req: F150 断言1——九仓库方法逐面
    @staticmethod
    def _mk(tid="tm-a1", task=TID, due=900, **over):
        base = dict(id=tid, task_id=task, target_role="worker", due_ms=due,
                    state=TimerState.PENDING, created_ms=10, fired_ms=None,
                    body="醒", created_by="ses-w1", client_key=None)
        base.update(over)
        return Timer(**base)

    def test_every_repository_method_roundtrip(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            a = st.upsert_timer(self._mk("tm-a1", due=500, client_key="k1"))
            st.upsert_timer(self._mk("tm-b2", task="ag-orphan", due=300,
                                     created_ms=5))
            st.upsert_timer(self._mk("tm-c3", due=700,
                                     target_role="dispatcher"))
        self.assertEqual(st.get_timer("tm-a1"), a)
        self.assertIsNone(st.get_timer("tm-none"))
        self.assertEqual(st.timer_for_key(TID, "k1").id, "tm-a1")  # 命中
        self.assertIsNone(st.timer_for_key(TID, "k2"))  # 未命中
        self.assertIsNone(st.timer_for_key(TID, None))  # 同 SQL ＝NULL 不等
        self.assertEqual([x.id for x in st.timers_for_task(TID)],
                         ["tm-a1", "tm-c3"])  # created_ms,id 序
        self.assertEqual([x.id for x in st.timers_for_task("ag-orphan")],
                         ["tm-b2"])
        self.assertEqual([x.id for x in st.timers_all()],
                         ["tm-a1", "tm-b2", "tm-c3"])  # id 序
        self.assertEqual([x.id for x in st.timers_by_status("pending")],
                         ["tm-a1", "tm-b2", "tm-c3"])
        self.assertEqual(st.timers_by_status("fired"), [])
        self.assertEqual([x.id for x in st.timers_due(600)],
                         ["tm-b2", "tm-a1"])  # due 升序、未到期不出
        self.assertEqual([x.id for x in st.timers_due(10_000, 2)],
                         ["tm-b2", "tm-a1"])  # limit
        self.assertEqual(st.next_due_ms(), 300)  # 最早 pending
        self.assertIsNone(st.message_for_timer("tm-a1"))

    def test_timers_due_keyset_pagination(self):
        st = self.store()
        with st.uow():
            st.upsert_timer(self._mk("tm-b", due=100))
            st.upsert_timer(self._mk("tm-a", due=100))
            st.upsert_timer(self._mk("tm-c", due=200))
            st.upsert_timer(self._mk("tm-d", due=300))
        page1 = st.timers_due(400, 2)
        self.assertEqual([x.id for x in page1], ["tm-a", "tm-b"])
        page2 = st.timers_due(400, 2, after_due=page1[-1].due_ms,
                              after_id=page1[-1].id)  # 键集游标翻页
        self.assertEqual([x.id for x in page2], ["tm-c", "tm-d"])
        self.assertEqual(
            st.timers_due(400, 2, after_due=300, after_id="tm-d"), [])
        self.assertEqual([x.id for x in st.timers_due(150)],
                         ["tm-a", "tm-b"])
        # 半游标（after_id 缺省）：同 SQL ＝NULL 语义——仅 due 严格大于
        self.assertEqual([x.id for x in st.timers_due(400, 5, after_due=100)],
                         ["tm-c", "tm-d"])

    def test_overlay_read_your_writes(self):
        st = self.store()
        reader = self.store()
        with st.uow():
            st.upsert_timer(self._mk("tm-ov", due=800))
            self.assertEqual(st.get_timer("tm-ov").due_ms, 800)  # overlay 先见
            self.assertEqual(st.next_due_ms(), 800)
            self.assertIsNone(reader.get_timer("tm-ov"))  # 外部读者只见 HEAD
        self.assertIsNotNone(st.get_timer("tm-ov"))  # 提交后 base 见
        self.assertIsNotNone(self.store().get_timer("tm-ov"))  # 重开重放见

    def test_transition_versions_monotonic_paths_and_tx_versions(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
        with st.uow():
            st.upsert_timer(self._mk("tm-v"))
        with st.uow():
            st.upsert_timer(timer_transition(
                st.get_timer("tm-v"), TimerState.FIRING))
        b = _bucket_of("tm-v")
        files = _head_files(self.home.state)
        self.assertIn("objects/timers/%s/tm-v/%020d.json" % (b, 1), files)
        self.assertIn("objects/timers/%s/tm-v/%020d.json" % (b, 2), files)
        # 第三笔 UoW＝v2 迁移（task v1→tx1、timer v1→tx2、v2→tx3）
        [tx_path] = [p for p in files
                     if p.endswith("tx/%s/%020d.json" % (_bucket_of(3), 3))]
        tx = json.loads(_blob(self.home.state, files[tx_path]))
        [op] = [o for o in tx["operations"] if o["op"] == "upsert_timer"]
        self.assertEqual((op["from_version"], op["to_version"]), (1, 2))

    def test_cancel_is_state_not_deletion(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            st.upsert_timer(self._mk("tm-cl", due=100))
        with st.uow():
            st.upsert_timer(timer_transition(
                st.get_timer("tm-cl"), TimerState.CANCELLED, now_ms=99))
        tm = st.get_timer("tm-cl")
        self.assertEqual(tm.state, TimerState.CANCELLED)
        self.assertEqual(len(st.timers_all()), 1)  # 行仍在（不物理删除）
        self.assertIsNone(st.next_due_ms())  # cancelled 非 pending
        self.assertEqual(
            self.store().get_timer("tm-cl").state, TimerState.CANCELLED)
        b = _bucket_of("tm-cl")
        v1 = "objects/timers/%s/tm-cl/%020d.json" % (b, 1)
        self.assertIn(v1, _head_files(self.home.state))  # 旧版本仍在树

    def test_replay_restores_latest_and_aborted_version_not_reused(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            st.upsert_timer(self._mk("tm-r"))
        with st.uow():
            st.upsert_timer(timer_transition(
                st.get_timer("tm-r"), TimerState.FIRING))
        fresh = self.store()  # 重启重放：恢复最新版本（v2 firing）
        self.assertEqual(fresh.get_timer("tm-r").state, TimerState.FIRING)
        self.assertIsNone(fresh.next_due_ms())
        self.injector.arm("write-object", "before")  # 弃单窗：v3 分配后失败
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.upsert_timer(timer_transition(
                    st.get_timer("tm-r"), TimerState.FIRED, now_ms=99))
        self.injector.disarm("write-object")
        with st.uow():  # 重试取 v4——弃单版本不复用
            st.upsert_timer(timer_transition(
                st.get_timer("tm-r"), TimerState.FIRED, now_ms=99))
        b = _bucket_of("tm-r")
        files = _head_files(self.home.state)
        self.assertNotIn("objects/timers/%s/tm-r/%020d.json" % (b, 3), files)
        self.assertIn("objects/timers/%s/tm-r/%020d.json" % (b, 4), files)

    def test_message_for_timer_overlay_replay_and_idempotent(self):
        st = self.store()
        with st.uow():
            m = st.insert_message(ts_ms=1, task_id=TID, kind="timer",
                                  from_sid="a", to_sid="b", body="t1",
                                  timer_id="tm-m")
            self.assertEqual(st.message_for_timer("tm-m").id, m.id)  # overlay
        self.assertEqual(st.message_for_timer("tm-m").id, m.id)
        self.assertIsNone(st.message_for_timer("tm-none"))
        with st.uow():  # 重复 timer_id fire：回原消息零新建
            m2 = st.insert_message(ts_ms=2, task_id=TID, kind="timer",
                                   from_sid="a", to_sid="b", body="t2",
                                   timer_id="tm-m")
        self.assertEqual((m2.id, m2.body), (m.id, b"t1"))
        self.assertEqual(
            self.store().message_for_timer("tm-m").id, m.id)  # 重放后


class TestTimerCorruption(_Base):  # Req: F150——timer canonical fail-closed
    def _seed(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            st.upsert_timer(TestTimerRepository._mk("tm-x"))
        with st.uow():
            st.upsert_timer(timer_transition(
                st.get_timer("tm-x"), TimerState.FIRING))
        return st

    def _timer_paths(self):
        return [p for p in _head_files(self.home.state)
                if p.startswith("objects/timers/")]

    def test_malformed_timer_entity_fails_closed(self):
        self._seed()
        [v2] = [p for p in self._timer_paths() if p.endswith("%020d.json" % 2)]
        obj = json.loads(_blob(self.home.state, _head_files(self.home.state)[v2]))
        obj["data"] = {"bad": 1}  # 实体字段缺形
        _mutate_head(self.home.state,
                     {v2: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_timer_id_path_mismatch_fails_closed(self):
        self._seed()
        [v1] = [p for p in self._timer_paths() if p.endswith("%020d.json" % 1)]
        files = _head_files(self.home.state)
        obj = json.loads(_blob(self.home.state, files[v1]))
        obj["id"] = "tm-other"  # 内容 id 与路径 id 不符
        _mutate_head(self.home.state, {v1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_duplicate_id_version_wrong_bucket_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        [v1] = [p for p in self._timer_paths() if p.endswith("%020d.json" % 1)]
        blob = _blob(self.home.state, files[v1])
        _mutate_head(self.home.state, {
            "objects/timers/de/ad/tm-x/%020d.json" % 1: blob})  # 冒位重版
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_scalar_type_mismatch_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        v1 = next(p for p in self._timer_paths()
                  if p.endswith("%020d.json" % 1))
        v2 = next(p for p in self._timer_paths()
                  if p.endswith("%020d.json" % 2))
        o1 = json.loads(_blob(self.home.state, files[v1]))
        o1["data"]["due_ms"] = "100"  # 错型标量（字符串）
        o2 = json.loads(_blob(self.home.state, files[v2]))
        o2["data"]["due_ms"] = None  # 错型标量（null）
        _mutate_head(self.home.state, {
            v1: json.dumps(o1).encode(), v2: json.dumps(o2).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()  # 构造期即拒——不延迟到扫描面 TypeError

    def test_missing_referenced_timer_object_fails_closed(self):
        self._seed()
        [v2] = [p for p in self._timer_paths() if p.endswith("%020d.json" % 2)]
        _mutate_head(self.home.state, {v2: None})  # tx 引用对象缺席
        with self.assertRaises(JournalCorruptionError):
            self.store()


class TestTimerServiceOnGitPort(unittest.TestCase):
    """Req: F150——TimerService 契约移植 Git 后端（服务零改动；不复制
    SQLite 套件——择 create/cancel/fire/catch-up 代表性路径＋崩窗对账）。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS + ("dispatch", "timer_fire",
                                    "timer_firing"):
            self.injector.register(fp)
        self.clock = FakeClock()
        self.oc = OcStub()
        self.store = GitStore(self.home.state, injector=self.injector)
        with self.store.uow():
            self.store.upsert_task(Task(
                id=TID, state="running", dispatcher_sid="ses-d1",
                worker_sid="ses-w1", created_ms=1, updated_ms=2))
        self.msg = MessagingService(
            self.store, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.svc = TimerService(self.store, self.msg, clock=self.clock,
                                injector=self.injector)

    def _create(self, after=60, key=None, target="worker"):
        params = {"task": TID, "target": target, "after_seconds": after}
        if key is not None:
            params["client_key"] = key
        return self.svc.create("ses-d1", "dispatcher", params)

    def test_create_cancel_fire_catchup_and_restart_replay(self):
        a = self._create(after=60, key="k1")
        again = self._create(after=60, key="k1")  # 同键同载荷幂等
        self.assertEqual(a["timer"], again["timer"])
        self.assertEqual(len(self.store.timers_all()), 1)
        out = self.svc.cancel("ses-d1", "dispatcher", a["timer"])
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(self.store.get_timer(a["timer"]).state,
                         TimerState.CANCELLED)  # 取消是状态非删除
        live = [self._create(after=i)["timer"] for i in (1, 2, 3)]
        live.append(self._create(after=1, target="dispatcher")["timer"])
        self.clock.advance(120_000)
        self.assertEqual(self.svc.catch_up(), 4)  # overdue 启动补发
        self.assertEqual(self.svc.catch_up(), 0)  # 收敛即止
        states = {t.id: t.state for t in self.store.timers_all()}
        self.assertTrue(all(states[i] is TimerState.FIRED for i in live))
        self.assertEqual(states[a["timer"]], TimerState.CANCELLED)
        msgs = self.store.messages_all()
        self.assertEqual(len(msgs), 4)
        for m in msgs:  # dedup_key＝timer id：索引回查恰回本消息
            self.assertEqual(self.store.message_for_timer(m.dedup_key).id,
                             m.id)
        self.assertEqual(self.svc.next_wait_seconds(), 30.0)  # 无 pending
        self.svc.scan()  # fired 幂等：不重发
        self.assertEqual(len(self.store.messages_all()), 4)
        fresh = GitStore(self.home.state)  # 重启重放：最新状态等价
        self.assertEqual({t.id: t.state for t in fresh.timers_all()}, states)
        self.assertEqual(fresh.message_for_timer(
            msgs[0].dedup_key).id, msgs[0].id)
        self.assertIsNone(fresh.next_due_ms())

    def test_fire_single_tx_transition_message_receipt_event(self):
        self._create(after=10)
        obs = harness.GitJournalObserver(self.home.state)
        before = len(obs.txs())
        self.clock.advance(20_000)
        self.svc.scan()
        txs = obs.txs()
        self.assertGreater(len(txs), before)
        fire_tx = next(t for t in txs.values()  # 恰一 tx 含四类对象变更
                       if any(op["op"] == "insert_message"
                              for op in t["operations"]))
        self.assertEqual({op["op"] for op in fire_tx["operations"]},
                         {"upsert_timer", "insert_message", "upsert_receipt",
                          "append_event"})
        [top] = [op for op in fire_tx["operations"]
                 if op["op"] == "upsert_timer"]
        self.assertEqual((top["from_version"], top["to_version"]), (1, 2))
        self.assertIn(top["path"], obs.paths())

    def test_crash_window_reconcile_no_second_message(self):
        r = self._create(after=10)
        self.clock.advance(20_000)
        self.injector.arm("timer_firing", "after")  # 崩窗2：消息已提交
        with self.assertRaises(harness.FaultInjected):
            self.svc.scan()
        self.assertEqual(self.store.get_timer(r["timer"]).state,
                         TimerState.FIRING)
        self.assertEqual(len(self.store.messages_all()), 1)
        fresh = GitStore(self.home.state)  # 重启重放：firing＋消息俱在
        self.assertEqual(fresh.get_timer(r["timer"]).state,
                         TimerState.FIRING)
        self.assertEqual(len(fresh.messages_all()), 1)
        self.injector.disarm("timer_firing")
        svc2 = TimerService(fresh, MessagingService(
            fresh, OcStub(), home=str(self.home.path), clock=self.clock),
            clock=self.clock)
        self.assertEqual(svc2.scan(), 0)  # 对账轮零新触发
        self.assertEqual(fresh.get_timer(r["timer"]).state,
                         TimerState.FIRED)  # 按 timer_id 补记 fired
        self.assertEqual(len(fresh.messages_all()), 1)  # 不产第二消息
        self.assertIn("timer_reconciled",
                      [e.event for e in fresh.events_all()])


class TestCredentialLifecycle(_Base):  # Req: F151——仓库面生命周期
    SID, TOK = "ses-c1", "tok-c1"

    def _put(self, st, sid=SID, token=TOK, task_id=None, lineage=(),
             created_ms=5):
        # 服务铁则镜像：secret 文件先落盘（键位＝task_id or sid）后记账
        self.home.write_credential(task_id or sid, token)
        cred = Credential(sid=sid, token=token, task_id=task_id,
                          lineage=lineage, created_ms=created_ms)
        with st.uow():
            return st.put_credential(cred)

    def _cred_files(self, sid=SID):
        b = _bucket_of(sid)
        return (f"objects/credentials/{b}/{sid}/{v:020d}.json"
                for v in range(1, 10))

    def test_put_read_roundtrip_and_restart_hydration(self):
        st = self.store()
        self._put(st, lineage=("ses-old",))
        got = st.get_credential(self.SID)
        self.assertEqual((got.token, got.task_id, got.lineage, got.created_ms),
                         (self.TOK, None, ("ses-old",), 5))
        self.assertEqual([c.sid for c in st.credentials_all()], [self.SID])
        fresh = self.store()  # 重启重放：外部 secret 文件水合＋验摘要
        self.assertEqual(fresh.get_credential(self.SID), got)
        self.assertEqual([c.token for c in fresh.credentials_all()], [self.TOK])

    def test_worker_and_root_secret_key_forms(self):
        st = self.store()
        self._put(st, sid="ses-wx", token="tok-w", task_id="ag-cw")  # worker 键
        p = self.home.agora / "root.token"  # root 键＝.agora/root.token
        p.write_text(json.dumps({"token": "tok-r"}) + "\n", encoding="utf-8")
        os.chmod(p, 0o600)
        with st.uow():
            st.put_credential(Credential(sid="root", token="tok-r"))
        self.assertEqual(st.get_credential("ses-wx").token, "tok-w")
        self.assertEqual(st.get_credential("root").token, "tok-r")
        fresh = self.store()
        self.assertEqual({c.sid: c.token for c in fresh.credentials_all()},
                         {"ses-wx": "tok-w", "root": "tok-r"})

    def test_delete_tombstone_idempotent_then_reput_later_version(self):
        st = self.store()
        self._put(st)                                    # v1 live
        with st.uow():
            self.assertTrue(st.delete_credential(self.SID))  # v2 tombstone
        self.assertIsNone(st.get_credential(self.SID))
        self.assertEqual(st.credentials_all(), [])
        with st.uow():
            self.assertFalse(st.delete_credential(self.SID))  # 重删 False
            self.assertFalse(st.delete_credential("ses-none"))
        files = _head_files(self.home.state)
        v1, v2 = list(self._cred_files())[:2]
        self.assertIn(v1, files)   # 历史版本仍在树（可审计不抹除）
        self.assertIn(v2, files)
        [tx2] = [p for p in files if p.startswith("tx/")
                 and p.endswith("%s/%020d.json" % (_bucket_of(2), 2))]
        tx = json.loads(_blob(self.home.state, files[tx2]))
        [op] = [o for o in tx["operations"] if o["op"] == "delete_credential"]
        self.assertEqual((op["from_version"], op["to_version"]), (1, 2))
        fresh = self.store()  # tombstone 重放＝缺席
        self.assertIsNone(fresh.get_credential(self.SID))
        self.assertEqual(fresh.credentials_all(), [])
        self._put(fresh, token="tok-c1b")                 # re-put＝v3
        self.assertEqual(fresh.get_credential(self.SID).token, "tok-c1b")
        files = _head_files(self.home.state)
        v3 = list(self._cred_files())[2]
        self.assertIn(v3, files)
        again = self.store()  # v3 活牌重放（文件已被 re-put 前覆写为新牌）
        self.assertEqual(again.get_credential(self.SID).token, "tok-c1b")

    def test_overlay_read_your_writes_for_put_delete_reput(self):
        st = self.store()
        reader = self.store()
        self.home.write_credential(self.SID, self.TOK)
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
            self.assertIsNotNone(st.get_credential(self.SID))  # overlay 先见
            self.assertIsNone(reader.get_credential(self.SID))  # 外部只见 HEAD
            self.assertTrue(st.delete_credential(self.SID))
            self.assertIsNone(st.get_credential(self.SID))  # overlay tombstone
            self.assertEqual(st.credentials_all(), [])
            self.assertFalse(st.delete_credential(self.SID))  # 重删 False
            st.put_credential(Credential(sid=self.SID, token=self.TOK,
                                         created_ms=9))  # re-put 同 UoW
            self.assertEqual(st.get_credential(self.SID).created_ms, 9)
        fresh = self.store()  # 提交后重开重放：v3 活牌在案
        self.assertEqual(fresh.get_credential(self.SID).created_ms, 9)
        self.assertEqual([c.created_ms for c in fresh.credentials_all()], [9])

    def test_aborted_put_version_not_reused(self):
        st = self.store()
        self._put(st)                                     # v1
        self.home.write_credential(self.SID, "tok-x")     # 新牌文件先落
        self.injector.arm("write-object", "before")
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.put_credential(Credential(sid=self.SID, token="tok-x"))
        self.injector.disarm("write-object")
        with st.uow():  # 重试取 v3——弃单版本不复用
            st.put_credential(Credential(sid=self.SID, token="tok-x"))
        files = _head_files(self.home.state)
        v2, v3 = list(self._cred_files())[1:3]
        self.assertNotIn(v2, files)
        self.assertIn(v3, files)

    def test_rebind_reuses_prior_secret_path_without_new_file(self):
        st = self.store()
        self._put(st, sid="ses-d1", token="tok-d")  # 文件＝credentials/ses-d1
        with st.uow():  # authsvc.rebind 形态：同牌换 sid 不写新文件
            st.put_credential(Credential(sid="ses-d2", token="tok-d",
                                         lineage=("ses-d1",)))
            st.delete_credential("ses-d1")
        self.assertEqual(st.get_credential("ses-d2").lineage, ("ses-d1",))
        self.assertIsNone(st.get_credential("ses-d1"))
        self.assertFalse((self.home.credentials / "ses-d2.token").exists())
        fresh = self.store()  # 重启：按既录 secret_path 水合（无 ses-d2 文件）
        self.assertEqual(fresh.get_credential("ses-d2").token, "tok-d")
        self.assertIsNone(fresh.get_credential("ses-d1"))


class TestCredentialSecretSafety(_Base):  # Req: F151——secret 外置安全性
    SID, TOK = "ses-s1", "tok-s1"

    def _put_two(self):
        st = self.store()
        self._write(self.SID, self.TOK)
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
        self._write("ag-cw2", "tok-s2")  # worker 键位
        with st.uow():
            st.put_credential(Credential(sid="ses-w2", token="tok-s2",
                                         task_id="ag-cw2"))
        with st.uow():
            st.delete_credential(self.SID)  # tombstone 也进对象树
        return st

    def _write(self, key, token):
        self.home.write_credential(key, token)

    def _all_objects(self):
        r = _plumb(self.home.state, "cat-file", "--batch-all-objects",
                   "--batch-check")
        assert r.returncode == 0, r.stderr
        return [line.split()[:2] for line in r.stdout.decode().splitlines()
                if line.strip()]

    def test_canonical_carry_digest_and_path_no_token_anywhere(self):
        self._put_two()
        files = _head_files(self.home.state)
        cred_paths = [p for p in files if p.startswith("objects/credentials/")]
        self.assertEqual(len(cred_paths), 3)  # v1+tombstone＋worker v1
        for path in cred_paths:
            data = json.loads(_blob(self.home.state, files[path]))["data"]
            if data.get("deleted") is True:
                continue
            self.assertNotIn("token", data)  # 明文牌不入 canonical
            self.assertRegex(data["token_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(data["secret_path"].endswith(".token"))
        for sha, typ in self._all_objects():  # 全对象扫描（blob/tx/commit）
            content = _plumb(self.home.state, "cat-file", typ, sha).stdout
            for token in (self.TOK, "tok-s2"):
                self.assertNotIn(token.encode(), content, (typ, sha))

    def test_put_verifies_external_secret_fail_closed_zero_commit(self):
        st = self.store()
        with self.assertRaises(CredentialSecretError):  # 无文件
            with st.uow():
                st.put_credential(Credential(sid=self.SID, token=self.TOK))
        self.assertIsNone(st.head)  # 零提交零 tx
        self._write(self.SID, "other-token")  # 文件在而牌值不符
        with self.assertRaises(CredentialSecretError):
            with st.uow():
                st.put_credential(Credential(sid=self.SID, token=self.TOK))
        self.assertIsNone(st.head)
        self.assertIsInstance(CredentialSecretError("x"), StorageError)
        self._write(self.SID, self.TOK)  # 匹配 → 放行
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
        self.assertIsNotNone(st.head)

    def test_missing_or_mismatched_secret_fails_closed_on_restart(self):
        st = self.store()
        self._write(self.SID, self.TOK)
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
        path = self.home.credentials / f"{self.SID}.token"
        path.unlink()  # 文件丢失：不静默空牌/不凭据消失
        with self.assertRaises(CredentialSecretError):
            self.store()
        self._write(self.SID, "tampered")  # 牌值被换：摘要不符
        with self.assertRaises(CredentialSecretError):
            self.store()
        self._write(self.SID, self.TOK)  # 复原即启
        self.assertIsNotNone(self.store().get_credential(self.SID))
        st2 = self.store()
        with st2.uow():  # tombstone 后不再依赖文件
            st2.delete_credential(self.SID)
        path.unlink()
        st3 = self.store()  # 最新版＝tombstone：缺席而非炸启动
        self.assertIsNone(st3.get_credential(self.SID))

    def test_storage_never_writes_or_deletes_secret_files(self):
        st = self.store()
        self._write(self.SID, self.TOK)
        p = self.home.credentials / f"{self.SID}.token"
        raw = p.read_bytes()
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
        with st.uow():
            st.delete_credential(self.SID)  # 删除不清理文件（清理权归 sweep）
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), raw)  # 内容未被存储改写
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_bad_digest_format_and_scalar_type_fail_closed(self):
        st = self.store()
        self._write(self.SID, self.TOK)
        with st.uow():
            st.put_credential(Credential(sid=self.SID, token=self.TOK))
        files = _head_files(self.home.state)
        [v1] = [p for p in files if p.startswith("objects/credentials/")
                and p.endswith("%020d.json" % 1)]
        obj = json.loads(_blob(self.home.state, files[v1]))
        obj["data"]["token_sha256"] = "zz" * 32  # 坏摘要形态
        _mutate_head(self.home.state, {v1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()
        obj["data"]["token_sha256"] = "a" * 64  # 摘要复原、标量错型
        obj["data"]["created_ms"] = "5"
        _mutate_head(self.home.state, {v1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()


if __name__ == "__main__":
    unittest.main()
