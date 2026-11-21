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
Req: F153 — Phase 2 artifact/event 仓库面＋GateVerify 移植（spec
gitstore-phase2-artifacts-gate-verify）：TestArtifactRepository（events_
for_task 归一化/seq 序、insert/artifacts_for_task newest-first＋overlay、
弃单 id 不复用＋重启重放）、TestArtifactCorruption（损毁/重号/错型/缺引用
fail-closed）、TestGateVerifyOnGitPort（受理拒因/分类路由/门审缓存与独立
连败/集成全绿产物投影/复合门事务不变量/崩窗结算撤牌回收——服务零改动）。

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
import dataclasses
import hashlib
import json
from collections import Counter
import os
import shutil
import socket
import subprocess
import unittest

from agorad.contracts import ContractError
from agorad.domain import (Artifact, Credential, DeliveryReceipt, DeliveryState,
                           Event, Message, Task, Timer, TimerState, VerifySpec,
                           dispatch_secret_key, timer_transition)
from agorad.gatedelivery import GateDeliveryService
from agorad.gateverify import DiskArtifactSink, DoneReject, GateVerifyService
from agorad.gitstore import (ALLOWED_VERBS, CommitConflict, CredentialSecretError,
                             ForeignLedgerError, GitStore,
                             JournalCorruptionError, check_verb)
from agorad.messaging import MessagingService
from agorad.storage import NoActiveUnitOfWork, StorageError, UnitOfWorkViolation
from agorad.tasksvc import TaskService
from agorad.timersvc import TimerService
from tests import v3_harness as harness
from tests.test_v3_gate_port import (BRANCH as GATE_BRANCH,
                                     C as GATE_C, E as GATE_E,
                                     TIP as GATE_TIP,
                                     DISPATCHER as GATE_DISP,
                                     WORKER as GATE_WORKER,
                                     GitxGateStub as GateGitxStub,
                                     OcStub as GateOcStub)
from tests.test_v3_task_service import (GitxStub as TaskGitxStub,
                                        OcStub as TaskOcStub,
                                        flip_state_on_second_read,
                                        make_task_spec)
from tests.test_v3_gate_delivery import (BRANCH as D_BRANCH, C as D_C,
                                        DISPATCHER as D_DISP, E as D_E,
                                        R as D_R, TIP as D_TIP,
                                        TIP2 as D_TIP2, WORKER as D_WORKER,
                                        GitxDeliveryStub)

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

    def test_home_relocation_replays_from_relative_secret_path(self):
        # 评审环 OCR R1：canonical secret_path 须 home 相对——整树搬迁后
        # 绝对路径即陈旧（重放 fail-closed 砖死），相对键位照常水合
        st = self.store()
        self._put(st)
        moved = self.home.path.with_name(self.home.path.name + "-moved")
        os.rename(self.home.path, moved)
        try:
            fresh = GitStore(os.path.join(moved, "state"))
            self.assertEqual(fresh.get_credential(self.SID).token, self.TOK)
        finally:
            shutil.rmtree(moved, ignore_errors=True)


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
        obj["data"]["created_ms"] = 5
        obj["data"]["secret_path"] = "/etc/passwd"  # 绝对路径重放拒（join 逃逸）
        _mutate_head(self.home.state, {v1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()
        # 评审环 C 册 R1：../ 穿越与任意键位同拒（canonical 只认派生键位）
        for bad in ("../../escape.token", "credentials/../escape.token",
                    "elsewhere/x.token"):
            obj["data"]["secret_path"] = bad
            _mutate_head(self.home.state, {v1: json.dumps(obj).encode()})
            with self.assertRaises(JournalCorruptionError):
                self.store()

    def test_secret_file_symlink_rejected_at_read(self):
        # 评审环 C 册 R1：读面 O_NOFOLLOW——符号链接换牌文件 fail-closed
        self._write("real", self.TOK)
        (self.home.credentials / f"{self.SID}.token").symlink_to(
            self.home.credentials / "real.token")
        st = self.store()
        with self.assertRaises(CredentialSecretError):
            with st.uow():
                st.put_credential(Credential(sid=self.SID, token=self.TOK))

    def test_task_id_path_traversal_rejected_at_put(self):
        # 评审环 OCR R1：secret 键位派生自 task_id——同 sid 白名单纵深
        st = self.store()
        with self.assertRaises(StorageError):
            with st.uow():
                st.put_credential(Credential(sid="ses-x", token="tk",
                                             task_id="../evil"))
        self.assertIsNone(st.head)  # 零提交


class TestTaskServiceOnGitPort(unittest.TestCase):
    """Req: F152——TaskService 契约移植 Git 后端（spec gitstore-phase2-
    task-service）：生命周期代表路径（create/update_verify/dispatch/
    transition/wait/bump_fail_streak/deliver/touch_seen）＋F151 redispatch
    外部 secret 风险封堵——per-dispatch 唯一 secret 键位：旧活牌文件在
    新 canonical 落账（tombstone 旧＋install 新同一 UoW）前绝不销毁，
    失败重派只清未用新 secret，本尊键位（插件 pin 面）提交后回填。"""

    D = "ses-d1"

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS + ("dispatch",):
            self.injector.register(fp)
        self.clock = FakeClock()
        self.store = GitStore(self.home.state, injector=self.injector)
        self.gitx = TaskGitxStub()
        self.oc = TaskOcStub()
        self.messaging = MessagingService(
            self.store, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.timers = TimerService(self.store, self.messaging,
                                   clock=self.clock)
        self.svc = TaskService(
            self.store, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock, timers=self.timers)

    def _mk(self, **kw):
        kw.setdefault("verify", {"cmd": "true"})
        return self.svc.create(self.D, "dispatcher", make_task_spec(**kw))

    def _staging(self, tid, token):
        d = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return self.home.credentials / f"{tid}.{d}.token"

    def _tx_with(self, obs, *ops):
        return next(t for t in obs.txs().values()
                    if set(ops) <= {op["op"] for op in t["operations"]})

    def _cred_files(self):
        return sorted(p.name for p in self.home.credentials.glob("*.token"))

    def _block(self, tid):
        for _ in range(3):
            self.svc.bump_fail_streak(tid)
        self.assertEqual(self.store.get_task(tid).state, "blocked")

    def _seed_awaiting(self, tid, commit):
        self.svc.transition(tid, "running", actor="gate")
        self.svc.transition(tid, "done", actor="gate")
        with self.store.uow():  # awaiting_external 驻留形归 T9 门体——直种子
            t = self.store.get_task(tid)
            self.store.upsert_task(dataclasses.replace(
                t, delivery=dataclasses.replace(
                    t.delivery, status="awaiting_external", commit=commit)))

    def test_create_forms_verify_registration_and_update_on_git(self):
        managed = self._mk()
        ob = self._mk(delivery={"deliverable": "out_of_band"})
        ext = self._mk(delivery={"deliverable": "external_branch",
                                 "target_branch": "v3-runtime"})
        for tid, mode in ((managed, "managed"), (ob, None),
                          (ext, "external_branch")):
            t = self.store.get_task(tid)
            self.assertEqual(t.state, "todo")
            self.assertEqual(t.delivery.mode if t.delivery else None, mode)
            self.assertEqual(t.verify.state, "registered")
        evs = [e.event for e in self.store.events_all()]
        self.assertEqual(evs.count("task_created"), 3)
        self.assertEqual(evs.count("verify_registered"), 3)
        pend = self._mk(verify={"verify_pending": True})  # 探索路补登
        self.assertTrue(self.store.get_task(pend).verify_pending)
        self.assertEqual(self.svc.update_verify(pend, self.D, "dispatcher",
                                                "pytest -q"), {"ok": True})
        self.assertEqual(self.store.get_task(pend).verify.cmd, "pytest -q")
        with self.assertRaises(ContractError) as cm:  # worker 永拒登记
            self.svc.update_verify(pend, "ses-w9", "worker", "x")
        self.assertEqual(cm.exception.code, "forbidden")
        fresh = GitStore(self.home.state)  # 重启重放等价
        self.assertEqual(fresh.get_task(managed).delivery.mode, "managed")
        self.assertEqual(fresh.get_task(pend).verify.cmd, "pytest -q")

    def test_first_dispatch_composite_tx_pointer_and_restart_hydration(self):
        tid = self._mk()
        obs = harness.GitJournalObserver(self.home.state)
        n0 = len(obs.txs())
        res = self.svc.dispatch(tid, self.D, "dispatcher")
        t = self.store.get_task(tid)
        self.assertEqual((t.state, t.worker_sid, t.env_dir),
                         ("dispatched", "ses-w1", f"/tmp/envs/{tid}"))
        self.assertEqual(t.env_base, "e" * 40)
        self.assertGreater(max(obs.txs()), n0)  # dispatch 产生新 tx
        self.assertEqual(len(obs.commits()), len(obs.txs()))  # 每 tx 恰一 commit
        dtx = self._tx_with(obs, "put_credential")  # 派发复合 tx
        self.assertEqual({op["op"] for op in dtx["operations"]},
                         {"upsert_task", "put_credential", "append_event",
                          "insert_message", "upsert_receipt"})
        cred = self.store.get_credential("ses-w1")
        self.assertEqual((cred.token, cred.task_id), (res["token"], tid))
        conv = self.home.credentials / f"{tid}.token"  # 指针本尊（插件 pin）
        self.assertTrue(conv.is_file())
        self.assertEqual(json.loads(
            conv.read_text("utf-8"))["token"], res["token"])
        self.assertEqual(os.stat(conv).st_mode & 0o777, 0o600)
        self.assertTrue(self._staging(tid, res["token"]).is_file())
        [cpath] = [p for p in _head_files(self.home.state)
                   if p.startswith("objects/credentials/")]
        reg = json.loads(_blob(self.home.state,
                               _head_files(self.home.state)[cpath]))["data"]
        # canonical 只记 per-dispatch 唯一键（不裸记本尊键位）
        self.assertEqual(reg["secret_path"],
                         f"credentials/{tid}."
                         f"{hashlib.sha256(res['token'].encode()).hexdigest()[:16]}"
                         ".token")
        fresh = GitStore(self.home.state)  # 重启水合＝活牌在案
        self.assertEqual(fresh.get_credential("ses-w1").token, res["token"])
        body = self.messaging.pull("ses-w1")[0].body.decode("utf-8")
        self.assertIn(f"file: {conv}", body)  # BOOT 凭据指针形态不动

    def test_redispatch_replaces_credential_in_same_uow_and_cleans_old(self):
        tid = self._mk()
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self._block(tid)
        stg1 = self._staging(tid, r1["token"])
        obs = harness.GitJournalObserver(self.home.state)
        tx0 = max(obs.txs())
        r2 = self.svc.dispatch(tid, self.D, "dispatcher")
        self.assertEqual(self.store.get_task(tid).worker_sid, "ses-w2")
        rtx = self._tx_with(obs, "delete_credential")  # 重派复合 tx（单 UoW）
        self.assertEqual(rtx["seq"], tx0 + 1)
        self.assertEqual({op["op"] for op in rtx["operations"]},
                         {"upsert_task", "put_credential", "delete_credential",
                          "append_event", "insert_message", "upsert_receipt"})
        self.assertIsNone(self.store.get_credential("ses-w1"))  # 旧牌 tombstone
        self.assertEqual(self.store.get_credential("ses-w2").token,
                         r2["token"])
        self.assertFalse(stg1.exists())  # 旧 secret（既录键位）已清
        self.assertTrue(self._staging(tid, r2["token"]).is_file())
        conv = json.loads((self.home.credentials / f"{tid}.token").read_text(
            "utf-8"))["token"]
        self.assertEqual(conv, r2["token"])  # 本尊指针随新牌
        fresh = GitStore(self.home.state)  # 重启重放：新键位水合、旧牌缺席
        self.assertEqual(fresh.get_credential("ses-w2").token, r2["token"])
        self.assertIsNone(fresh.get_credential("ses-w1"))

    def test_redispatch_guard_conflict_preserves_old_credential_restart_safe(self):
        tid = self._mk()
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self._block(tid)
        with flip_state_on_second_read(self.store, tid, "running"):
            with self.assertRaises(ContractError) as cm:
                self.svc.dispatch(tid, self.D, "dispatcher")
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("session_replaced", cm.exception.message)
        # 旧活牌原封不动：canonical 未动＋本尊文件仍旧牌
        self.assertEqual(self.store.get_credential("ses-w1").token,
                         r1["token"])
        self.assertEqual(json.loads(
            (self.home.credentials / f"{tid}.token").read_text("utf-8")
        )["token"], r1["token"])
        # 只清未用新 secret：凭据目录不留孤儿 per-dispatch 文件
        self.assertEqual(self._cred_files(),
                         [self._staging(tid, r1["token"]).name, f"{tid}.token"])
        self.assertIn("dispatch_rejected",
                      [e.event for e in self.store.events_all()])
        fresh = GitStore(self.home.state)  # 重启不砖（F151 风险封堵锚）
        self.assertEqual(fresh.get_credential("ses-w1").token, r1["token"])

    def test_dispatch_exception_window_cleans_only_new_secret(self):
        tid = self._mk()
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self._block(tid)
        self.injector.arm("write-object", "before")  # UoW 提交前崩
        with self.assertRaises(harness.FaultInjected):
            self.svc.dispatch(tid, self.D, "dispatcher")
        self.injector.disarm("write-object")
        self.assertEqual(self.store.get_credential("ses-w1").token,
                         r1["token"])
        self.assertEqual(json.loads(
            (self.home.credentials / f"{tid}.token").read_text("utf-8")
        )["token"], r1["token"])
        self.assertEqual(self._cred_files(),
                         [self._staging(tid, r1["token"]).name, f"{tid}.token"])
        fresh = GitStore(self.home.state)  # 重启不砖：旧活牌照常水合
        self.assertEqual(fresh.get_credential("ses-w1").token, r1["token"])
        self.assertEqual(fresh.get_task(tid).state, "blocked")  # 零状态改写

    def test_cancel_terminal_cleanup_tombstones_credential_timers_files(self):
        tid = self._mk()
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self.timers.create(self.D, "dispatcher",
                           {"task": tid, "target": "worker",
                            "after_seconds": 60})
        obs = harness.GitJournalObserver(self.home.state)
        tx0 = max(obs.txs())
        self.assertEqual(self.svc.cancel(tid, self.D, "dispatcher",
                                         reason="testing"), {"cancelled": 1})
        ctx = self._tx_with(obs, "delete_credential")  # 结算单 UoW
        self.assertEqual(ctx["seq"], tx0 + 1)
        ops = {op["op"] for op in ctx["operations"]}
        self.assertEqual(ops, {"upsert_task", "append_event",
                               "delete_credential", "upsert_timer",
                               "insert_message",
                               "upsert_receipt"})  # F145 timer 同 UoW 取消
        self.assertEqual(self.store.get_task(tid).state, "cancelled")
        self.assertIsNone(self.store.get_credential("ses-w1"))
        self.assertEqual([t.state for t in self.store.timers_all()],
                         [TimerState.CANCELLED])
        self.assertFalse((self.home.credentials / f"{tid}.token").exists())
        self.assertFalse(self._staging(tid, r1["token"]).exists())
        self.assertEqual(self._cred_files(), [])
        fresh = GitStore(self.home.state)  # 无悬挂 secret 引用：重启不砖
        self.assertIsNone(fresh.get_credential("ses-w1"))
        self.assertEqual(fresh.get_task(tid).state, "cancelled")
        self.assertEqual(len([m for m in fresh.messages_all()
                              if m.kind == "result"]), 1)  # 终态通知恰一条

    def test_task_wait_resolution_and_terminal_notify_dedup_on_git(self):
        dep = self._mk()
        self.svc.cancel(dep, self.D, "dispatcher")  # 前任务终态
        tid = self._mk()
        self.svc.dispatch(tid, self.D, "dispatcher")
        self.svc.transition(tid, "running", actor="gate")
        out = self.svc.wait(tid, self.D, "dispatcher", dep)  # 立即 resolved
        self.assertEqual((out["resolved"], out["dependency_state"]),
                         (True, "cancelled"))
        pairs = sorted(
            (m.kind, m.to_sid) for m in self.store.messages_all()
            if "受控等待解除".encode() in m.body)  # 双消息：worker＋dispatcher
        self.assertEqual(pairs, [("process", "ses-w1"), ("result", self.D)])
        self.svc.transition(tid, "done", actor="gate")
        self.svc.transition(tid, "merged", actor="gate")
        n = len([m for m in self.store.messages_all() if m.kind == "result"])
        with self.assertRaises(ContractError):
            self.svc.transition(tid, "merged", actor="gate")  # merged→merged
        self.assertEqual(len([m for m in self.store.messages_all()
                              if m.kind == "result"]), n)  # 零新通知
        self.assertEqual(self.store.get_task(tid).state, "merged")

    def test_touch_seen_throttled_flush_and_restart_visibility(self):
        tid = self._mk()
        self.svc.dispatch(tid, self.D, "dispatcher")
        self.svc.transition(tid, "running", actor="gate")
        obs = harness.GitJournalObserver(self.home.state)
        tx0 = max(obs.txs())
        self.assertTrue(self.svc.touch_seen(tid, worker_sid="ses-w1"))  # 首刷
        self.assertEqual(max(obs.txs()), tx0 + 1)
        self.assertFalse(self.svc.touch_seen(tid, worker_sid="ses-w9"))
        self.clock.advance(1_000)
        self.assertTrue(self.svc.touch_seen(tid, worker_sid="ses-w1"))
        self.assertEqual(max(obs.txs()), tx0 + 1)  # 窗内纯内存零提交
        self.clock.advance(5_000)
        self.assertTrue(self.svc.touch_seen(tid, worker_sid="ses-w1"))
        self.assertEqual(max(obs.txs()), tx0 + 2)  # 过窗恰一次持久
        t = self.store.get_task(tid)
        self.assertIsNotNone(t.last_seen_ms)
        self.assertEqual(GitStore(self.home.state).get_task(tid).last_seen_ms,
                         t.last_seen_ms)  # 重启重放可见

    def test_deliver_external_lifecycle_on_git(self):
        tid = self._mk(delivery={"deliverable": "external_branch",
                                 "target_branch": "v3-runtime"})
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self._seed_awaiting(tid, "c" * 40)
        with self.assertRaises(ContractError) as cm:  # stale handoff 拒
            self.svc.deliver(tid, self.D, "dispatcher", "delivered", "d" * 40)
        self.assertEqual(cm.exception.code, "conflict")
        obs = harness.GitJournalObserver(self.home.state)
        tx0 = max(obs.txs())
        out = self.svc.deliver(tid, self.D, "dispatcher", "delivered",
                               "c" * 40)
        self.assertEqual(out["outcome"], "delivered")
        t = self.store.get_task(tid)
        self.assertEqual((t.state, t.delivery.status, t.commit),
                         ("closed", "delivered", "c" * 40))
        dtx = self._tx_with(obs, "delete_credential")  # 全链同一 UoW
        self.assertEqual(dtx["seq"], tx0 + 1)
        self.assertEqual({op["op"] for op in dtx["operations"]},
                         {"upsert_task", "append_event", "delete_credential",
                          "insert_message", "upsert_receipt"})
        self.assertIsNone(self.store.get_credential("ses-w1"))
        self.assertEqual(self._cred_files(), [])  # 本尊＋staging 全清
        self.assertFalse(self._staging(tid, r1["token"]).exists())
        fresh = GitStore(self.home.state)  # 重启不砖：终态零悬挂
        self.assertEqual(fresh.get_task(tid).state, "closed")
        self.assertIsNone(fresh.get_credential("ses-w1"))

    def test_deliver_failed_blocks_and_keeps_credential_on_git(self):
        tid = self._mk(delivery={"deliverable": "external_branch",
                                 "target_branch": "v3-runtime"})
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        self._seed_awaiting(tid, "d" * 40)
        out = self.svc.deliver(tid, self.D, "dispatcher", "failed", "d" * 40,
                               reason="push rejected")
        self.assertEqual((out["outcome"], out["error"]),
                         ("failed", "push rejected"))
        t = self.store.get_task(tid)
        self.assertEqual((t.state, t.delivery.status, t.delivery.error),
                         ("blocked", "failed", "push rejected"))
        self.assertEqual(self.store.get_credential("ses-w1").token,
                         r1["token"])  # 交付失败非撤牌面（blocked 重派路）
        self.assertTrue((self.home.credentials / f"{tid}.token").is_file())
        self.assertTrue(self._staging(tid, r1["token"]).is_file())
        self.assertEqual(GitStore(self.home.state)
                         .get_credential("ses-w1").token, r1["token"])


    def test_authsvc_sweep_keeps_live_staging_and_cleans_orphans(self):
        # Req: F152——AuthService.sweep 保活/回收口径对 per-dispatch 键位
        # 成立：活牌 staging 留、死牌 staging 孤儿（派发崩窗残件）清、
        # 本尊指针不动——清扫后重启不砖（设计「preserve AuthService sweep」
        # 的机械锚）。
        from agorad.authsvc import AuthService
        tid = self._mk()
        r1 = self.svc.dispatch(tid, self.D, "dispatcher")
        orphan = self.home.write_credential(
            dispatch_secret_key(tid, "tok-dead")[:-len(".token")], "tok-dead")
        self.assertTrue(orphan.is_file())
        self.assertEqual(AuthService(self.store, home=str(self.home.path))
                         .sweep(), 1)  # 恰清死牌 staging 孤儿
        self.assertFalse(orphan.exists())
        self.assertTrue(self._staging(tid, r1["token"]).is_file())  # 活牌留
        self.assertTrue((self.home.credentials / f"{tid}.token").is_file())
        fresh = GitStore(self.home.state)  # 清扫后重启照常水合
        self.assertEqual(fresh.get_credential("ses-w1").token, r1["token"])


class TestArtifactRepository(_Base):  # Req: F153——artifact/event 仓库面
    OTHER = "ag-o1"

    def _seed_task(self, st, tid=TID):
        with st.uow():
            st.upsert_task(Task(id=tid, state="running"))

    def _ev(self, ts, name, task=TID):
        return Event(seq=0, ts_ms=ts, actor="a", event=name, task=task)

    def test_events_for_task_ordering_and_normalization(self):
        st = self.store()
        self._seed_task(st)
        self._seed_task(st, self.OTHER)
        with st.uow():
            for ev in (self._ev(1, "e1"), self._ev(2, "e2", task=None),
                       self._ev(3, "e3", task=self.OTHER), self._ev(4, "e4"),
                       self._ev(5, "e5", task="")):
                st.append_event(ev)
        self.assertEqual([e.event for e in st.events_for_task(TID)],
                         ["e1", "e4"])  # 全局 seq 升序，跨任务事件不混入
        self.assertEqual([e.event for e in st.events_for_task(self.OTHER)],
                         ["e3"])
        # SQLite 同语义：_null("")→NULL 且 SQL NULL 不等任何参数——空/None
        # tid 恒空集；空 task 事件（""/None）不可按任务命中
        self.assertEqual(st.events_for_task(""), [])
        self.assertEqual(st.events_for_task(None), [])
        self.assertEqual(st.events_for_task("ag-none"), [])

    def test_insert_requires_uow_newest_first_and_overlay(self):
        st = self.store()
        self._seed_task(st)
        self._seed_task(st, self.OTHER)
        with self.assertRaises(NoActiveUnitOfWork):
            st.insert_artifact(Artifact(task_id=TID, phase="verify"))
        with st.uow():
            st.insert_artifact(Artifact(task_id=TID, phase="verify", rc=0,
                                        bytes=10))
            st.insert_artifact(Artifact(task_id=TID, phase="integration",
                                        rc=1, bytes=20, duration_s=1.5))
            st.insert_artifact(Artifact(task_id=self.OTHER, phase="verify"))
            self.assertEqual(  # overlay 读己之写：未提交即见 newest-first
                [a.phase for a in st.artifacts_for_task(TID)],
                ["integration", "verify"])
        self.assertEqual([a.phase for a in st.artifacts_for_task(TID)],
                         ["integration", "verify"])  # 提交后口径不变
        self.assertEqual([a.phase for a in st.artifacts_for_task(self.OTHER)],
                         ["verify"])
        self.assertEqual(st.artifacts_for_task("ag-none"), [])

    def test_ids_monotonic_after_failed_uow_and_replay(self):
        st = self.store()
        self._seed_task(st)
        self.injector.arm("write-object", "before")
        with self.assertRaises(harness.FaultInjected):
            with st.uow():
                st.insert_artifact(Artifact(task_id=TID, phase="a1"))
        self.injector.disarm("write-object")
        with st.uow():
            st.insert_artifact(Artifact(task_id=TID, phase="a2"))
            st.append_event(self._ev(1, "e"))
        paths = [p for p in _head_files(self.home.state)
                 if p.startswith("objects/artifacts/")]
        self.assertEqual(len(paths), 1)  # 弃单对象不入树
        self.assertTrue(paths[0].endswith("%020d.json" % 2))  # 弃单 id 不复用
        fresh = GitStore(self.home.state)  # 重启重放等价
        self.assertEqual([a.phase for a in fresh.artifacts_for_task(TID)],
                         ["a2"])
        self.assertEqual([e.event for e in fresh.events_for_task(TID)], ["e"])
        with fresh.uow():  # 重启后派号接续（高水位只升不降）
            fresh.insert_artifact(Artifact(task_id=TID, phase="a3"))
        self.assertEqual([a.phase for a in fresh.artifacts_for_task(TID)],
                         ["a3", "a2"])


class TestArtifactCorruption(_Base):  # Req: F153——artifact canonical fail-closed
    def _seed(self):
        st = self.store()
        with st.uow():
            st.upsert_task(Task(id=TID))
            st.insert_artifact(Artifact(task_id=TID, phase="verify", rc=0))
            st.insert_artifact(Artifact(task_id=TID, phase="review", rc=1))
        return st

    def _paths(self):
        return sorted(p for p in _head_files(self.home.state)
                      if p.startswith("objects/artifacts/"))

    def test_malformed_entity_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        p1 = self._paths()[0]
        obj = json.loads(_blob(self.home.state, files[p1]))
        obj["data"] = {"bad": 1}  # 实体字段缺形
        _mutate_head(self.home.state, {p1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_id_path_mismatch_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        p1 = self._paths()[0]
        obj = json.loads(_blob(self.home.state, files[p1]))
        obj["id"] = 99  # 内容 id 与路径 id 不符
        _mutate_head(self.home.state, {p1: json.dumps(obj).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_duplicate_id_wrong_bucket_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        blob = _blob(self.home.state, files[self._paths()[0]])
        _mutate_head(self.home.state, {
            "objects/artifacts/de/ad/%020d.json" % 1: blob})  # 冒位重号
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_scalar_type_mismatch_fails_closed(self):
        self._seed()
        files = _head_files(self.home.state)
        p1, p2 = self._paths()
        o1 = json.loads(_blob(self.home.state, files[p1]))
        o1["data"]["bytes"] = "10"  # 错型标量（字符串）
        o2 = json.loads(_blob(self.home.state, files[p2]))
        o2["data"]["bytes"] = None  # 错型标量（null）
        _mutate_head(self.home.state, {
            p1: json.dumps(o1).encode(), p2: json.dumps(o2).encode()})
        with self.assertRaises(JournalCorruptionError):
            self.store()

    def test_missing_referenced_object_fails_closed(self):
        self._seed()
        _mutate_head(self.home.state, {self._paths()[0]: None})  # 留 tx 引用
        with self.assertRaises(JournalCorruptionError):
            self.store()


class GateRunnerStub:
    """runner 桩（Git 口径，SQLite 桩同约）：调用点必在 UoW 外——断言走
    GitStore overlay 面（SQLite 侧 _depth 探针在 Git 后备无对应）。"""

    def __init__(self, store, script=(), before=None):
        self.store, self.script, self.before = store, list(script), before
        self.calls = []

    def __call__(self, env_dir, cmd, *, timeout=None, ceiling=None,
                 on_soft=None):
        if self.store is not None and self.store._in_uow():
            raise AssertionError("runner 调用点身处 UoW 写事务内（§4 禁子进程）")
        if self.before is not None:
            self.before(self)
        self.calls.append({"env": env_dir, "cmd": cmd, "timeout": timeout,
                           "ceiling": ceiling, "on_soft": on_soft})
        return self.script.pop(0) if self.script else (0, "")

    def by_env(self, env_dir):
        return [c for c in self.calls if c["env"] == env_dir]


class TestGateVerifyOnGitPort(unittest.TestCase):
    """Req: F153——GateVerifyService 契约移植 Git 后端（服务零改动；不复制
    SQLite 全套——择受理/分类/门审/集成/产物/崩窗结算代表性路径＋复合门
    事务不变量；gitx/锚/runner 沿用 SQLite 桩约）。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS + ("uow", "dispatch", "verify", "settle"):
            self.injector.register(fp)  # Git 提交面 failpoint 须先注册
        self.storage = GitStore(self.home.state, injector=self.injector)
        self.clock = FakeClock()
        self.gitx = GateGitxStub()
        self.oc = GateOcStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.tasksvc = TaskService(
            self.storage, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock)
        self.runner = GateRunnerStub(self.storage)
        self.sink = DiskArtifactSink(self.home.state / "gate-artifacts")
        self.gate = GateVerifyService(
            self.storage, self.gitx, self.runner, messaging=self.messaging,
            tasksvc=self.tasksvc,
            anchors=lambda env: self._anchor_rows.get(env),
            artifacts=self.sink, home=str(self.home.path),
            clock=self.clock, injector=self.injector, verify_timeout=300)
        self._anchor_rows = {}

    def set_anchor(self, tid, head=GATE_C, base=GATE_E, clean=True):
        self._anchor_rows[f"/tmp/envs/{tid}"] = (None if head is None else
                                                 {"head": head, "base": base,
                                                  "clean": clean})

    def seed(self, tid, state="done", **over):
        row = dict(id=tid, state=state, title=tid, goal="g",
                   project_dir="/repo", target_branch=GATE_BRANCH,
                   env_dir=f"/tmp/envs/{tid}", env_base=GATE_E,
                   dispatcher_sid=GATE_DISP, root_sid=GATE_DISP,
                   worker_sid=GATE_WORKER, chain_depth=1, created_ms=1,
                   updated_ms=1, commit=GATE_C,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=GATE_DISP,
                                     registered_at_ms=1))
        row.update(over)
        with self.storage.uow():
            self.storage.upsert_task(Task(**row))
        return tid

    def requeue(self, tid, commit=GATE_C):
        self.tasksvc.transition(tid, "running", actor=GATE_WORKER)
        self.set_anchor(tid, head=commit)
        self.gitx.contained.discard((commit, GATE_BRANCH))
        return self.gate.accept_done(tid, GATE_WORKER, commit)

    def events(self, tid):
        return [e.event for e in self.storage.events_for_task(tid)]

    def messages(self, sid):
        return self.storage.messages_for_sid(sid)

    @staticmethod
    def text(m):
        return (m.body.decode("utf-8", "replace")
                if isinstance(m.body, (bytes, bytearray)) else str(m.body))

    def test_accept_done_flow_double_done_and_reject_codes(self):
        tid = self.seed("ag-g1", state="running")
        self.set_anchor(tid)
        self.assertEqual(self.gate.accept_done(tid, GATE_WORKER, GATE_C),
                         "accepted")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit, t.rebased_from),
                         ("done", GATE_C, None))
        self.assertIn("done", self.events(tid))
        self.assertIsInstance(self.gate.accept_done(tid, GATE_WORKER, GATE_C),
                              DoneReject)  # 二次 done：state 拒
        cases = [("ag-g2", "dispatched", {}, GATE_C, "state"),
                 ("ag-g3", "running",
                  {"verify": VerifySpec(state="pending")}, GATE_C,
                  "verify-unregistered"),
                 ("ag-g4", "running",
                  {"verify": VerifySpec(cmd="", state="registered",
                                        registered_by=GATE_DISP,
                                        registered_at_ms=1)}, GATE_C,
                  "verify-empty"),
                 ("ag-g5", "running", {}, "zz", "body-format"),
                 ("ag-g6", "running", {}, GATE_C, "anchor")]
        for tid, state, over, commit, code in cases:
            tid = self.seed(tid, state=state, **over)
            r = self.gate.accept_done(tid, GATE_WORKER, commit)
            self.assertIsInstance(r, DoneReject, (tid, code))
            self.assertEqual(r.code, code, (tid, r.code))
            t = self.storage.get_task(tid)
            self.assertEqual(t.fail_streak,
                             1 if code == "anchor" else 0, (tid, code))
            self.assertIn("done_rejected", self.events(tid))
            self.assertEqual(t.state, state)  # 拒收零状态改写
        tid = self.seed("ag-g7", state="running")  # 子任务闸：先于锚单点
        self.seed("ag-kid", state="dispatched", dispatcher_sid=GATE_WORKER)
        r = self.gate.accept_done(tid, GATE_WORKER, GATE_C)
        self.assertEqual((r.code, self.storage.get_task(tid).fail_streak),
                         ("children", 0))
        self.seed("ag-kid", state="closed", dispatcher_sid=GATE_WORKER)  # 结案
        tid = self.seed("ag-g8", state="running", commit=None)  # 空集且无在案交付
        self.set_anchor(tid)
        self.gitx.contained.add((GATE_C, GATE_BRANCH))
        r = self.gate.accept_done(tid, GATE_WORKER, GATE_C)
        self.assertEqual((r.code, self.storage.get_task(tid).fail_streak),
                         ("vacuous-done", 1))

    def test_verify_classification_and_streak_blocked(self):
        tid = self.seed("ag-v1")
        self.runner.script = [(1, ""), (1, "FAIL-tail")]
        self.assertEqual(self.gate.run(tid), "verify_failed")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("verify_failed", 1))
        self.assertTrue(any(m.kind == "process" and "FAIL-tail" in self.text(m)
                            for m in self.messages(GATE_WORKER)))
        for _ in range(2):  # 两轮续修再败 → 三连败自动 blocked
            self.assertEqual(self.requeue(tid), "accepted")
            self.runner.script = [(1, ""), (1, "FAIL")]
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("blocked", 3))
        for tid, rc, outcome, evname in (  # 驻留分类：dead/infra/overrun
                ("ag-v2", 126, "dead", "verify_dead"),
                ("ag-v3", -9, "infra", "verify_infra_failed"),
                ("ag-v4", 124, "overrun", "verify_overrun")):
            self.runner.script = [(1, "red-base"), (rc, "boom")]
            self.assertEqual(self.gate.run(self.seed(tid)), outcome)
            t = self.storage.get_task(tid)
            self.assertEqual((t.state, t.fail_streak), ("done", 0))
            self.assertIn(evname, self.events(tid))

    def test_review_green_caches_cmd_head(self):
        tid = self.seed("ag-r1", review_cmd="ocr .")
        self.runner.script = [(1, ""), (0, ""), (0, "REVIEW"), (0, "")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        t = self.storage.get_task(tid)
        self.assertEqual((t.gate.review_cache["cmd"],
                          t.gate.review_cache["commit"],
                          t.gate.review_cache["ok"],
                          t.gate.review_fail_streak),
                         ("ocr .", GATE_E, True, 0))
        calls = [c for c in self.runner.calls
                 if "AGORA_REVIEW_BASE" in c["cmd"]]
        self.gate.run(tid)  # 再跑：缓存命中零重审
        self.assertEqual(len([c for c in self.runner.calls
                              if "AGORA_REVIEW_BASE" in c["cmd"]]), len(calls))
        self.assertIn("gate_review_cached", self.events(tid))

    def test_review_red_independent_streak_and_blocked_at_three(self):
        tid = self.seed("ag-r2", review_cmd="ocr .")
        for i, expect in enumerate((1, 2, 3)):
            # 首轮探针实跑；reopen 重排队后探针缓存命中＝脚本少一发
            self.runner.script = ([(1, "")] if i == 0 else []) + \
                [(0, ""), (1, "REVIEW-RED")]
            self.set_anchor(tid)
            outcome = self.gate.run(tid)
            t = self.storage.get_task(tid)
            if expect < 3:
                self.assertEqual(outcome, "reopened")
                self.assertEqual((t.gate.review_fail_streak, t.fail_streak),
                                 (expect, 0))  # 独立计数：不烧质量连败
                self.requeue(tid)
            else:
                self.assertEqual(outcome, "blocked")
                self.assertEqual(
                    (t.state, t.gate.review_fail_streak, t.fail_streak),
                    ("blocked", 3, 0))
                self.assertTrue(any("三轮分歧" in self.text(m)
                                    for m in self.messages(GATE_DISP)))

    def test_integration_green_pipeline_artifacts_and_projection(self):
        # 全绿一轮（探针红→env 绿→门审绿→集成绿）：export 前缀、artifact
        # 行逐轮落账 newest-first、任务行投影最新一条
        tid = self.seed("ag-i1", review_cmd="ocr .")
        self.runner.script = [(1, "base"), (0, "env"), (0, "rev"), (0, "int")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        int_cmds = [c["cmd"] for c in
                    self.runner.by_env(f"/tmp/envs/{tid}__gate")
                    if "AGORA_INTEGRATION_BASE" in c["cmd"]]
        self.assertEqual(
            int_cmds, [f"export AGORA_INTEGRATION_BASE={GATE_TIP}; true"])
        rows = self.storage.artifacts_for_task(tid)
        self.assertEqual([r.phase for r in rows],
                         ["integration_verify", "review", "verify",
                          "base_probe"])
        self.assertEqual(
            self.storage.get_task(tid).gate.gate_artifact.path, rows[0].path)
        self.assertIn("gate_artifact_written", self.events(tid))
        ev = self.events(tid)
        i = (ev.index("phase_base_probe"), ev.index("phase_verify"),
             ev.index("phase_review"), ev.index("phase_integration"),
             ev.index("verify_green"))
        self.assertEqual(i, tuple(sorted(i)))  # 逐点有序落 events

    def test_artifact_tx_one_commit_all_changed_objects(self):
        # 产物复合事务不变量：insert_artifact＋任务行投影＋事件恰一 tx
        # ＝恰一 commit 含全部变更对象版本；整账本 canonical 只增
        tid = self.seed("ag-q1")
        self.runner.script = [(1, ""), (1, "FAIL")]
        self.assertEqual(self.gate.run(tid), "verify_failed")
        obs = harness.GitJournalObserver(self.home.state)
        txs = obs.txs()
        self.assertEqual(len(obs.commits()), len(txs))  # 每 tx 恰一 commit
        self.assertTrue(obs.additions_only())  # canonical 只增
        atx = next(t for t in txs.values()
                   if any(op["op"] == "insert_artifact"
                          for op in t["operations"]))
        self.assertEqual({op["op"] for op in atx["operations"]},
                         {"insert_artifact", "upsert_task", "append_event"})
        files = obs.paths()
        for op in atx["operations"]:  # tx 引用对象俱在单 commit 树
            self.assertIn(op["path"], files)

    def test_cancel_queued_in_gate_settles_at_tail(self):
        # 崩窗结算：门内排队取消在门尾消费——迁移＋事件＋消息同 UoW、
        # 撤牌（get/delete_credential Git 面）＋env 回收
        tid = self.seed("ag-w1")
        self.runner = GateRunnerStub(self.storage, before=lambda r: (
            self.tasksvc.cancel(tid, GATE_DISP, "dispatcher")))
        self.gate._runner = self.runner
        self.runner.script = [(1, ""), (0, ""), (0, "")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")  # verify 结果不被取消吞
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.gate.pending_cancel), ("cancelled", False))
        self.assertIsNone(self.storage.get_credential(GATE_WORKER))
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)
        self.assertTrue(any("cancelled at gate tail" in self.text(m)
                            for m in self.messages(GATE_DISP)))

    def test_landed_residue_settles_merged_closed_and_revokes(self):
        tid = self.seed("ag-w2")
        self.gitx.contained.add((GATE_C, GATE_BRANCH))  # 权威已含交付
        self.home.write_credential(tid, "tok")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=GATE_WORKER, token="tok", task_id=tid))
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")  # merged→closed 一事务收口
        self.assertIsNone(self.storage.get_credential(GATE_WORKER))  # 撤牌
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 回收
        n_events = len(self.events(tid))
        self.assertEqual(self.gate.run(tid), "skip")  # 重排恰一次零重复迁移
        self.assertEqual(len(self.events(tid)), n_events + 3)  # 唯车道审计



class GitxDeliveryGitStub(GitxDeliveryStub):
    """gitx 交付桩（Git 口径，桩约同 SQLite 版）：UoW 外断言走 GitStore
    overlay 面（SQLite 侧 _depth 探针在 Git 后备无对应）。"""

    def _ok(self):
        if self.storage is not None and self.storage._in_uow():
            raise AssertionError("gitx 调用点身处 UoW 写事务内（§4 禁子进程）")


class TestGateDeliveryOnGitPort(unittest.TestCase):
    """Req: F154 — GateDeliveryService 契约移植 Git 后端（服务零改动；不
    复制 SQLite 全套——managed 绿收口/CAS 重试与耗尽/dirty·busy 终局/幻影
    双守卫/假冲突自愈、external awaiting→确认三面、out_of_band 直收口、
    崩窗重启重放恰一次、交付收口复合单事务不变量；gitx/锚/runner 桩约沿
    tests/test_v3_gate_delivery.py）。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector()
        for fp in GIT_FAILPOINTS + ("uow", "dispatch", "verify", "settle"):
            self.injector.register(fp)  # Git 提交面 failpoint 须先注册
        self.storage = GitStore(self.home.state, injector=self.injector)
        self.clock = FakeClock()
        self.gitx = GitxDeliveryGitStub(storage=self.storage)
        self.oc = GateOcStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.tasksvc = TaskService(
            self.storage, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock)
        self.timers = TimerService(self.storage, self.messaging,
                                   clock=self.clock)
        self.runner = GateRunnerStub(self.storage)
        self.gate = GateDeliveryService(
            self.storage, self.gitx, self.runner, messaging=self.messaging,
            tasksvc=self.tasksvc, timers=self.timers,
            anchors=lambda env: self._anchor_rows.get(env),
            artifacts=None, home=str(self.home.path),
            clock=self.clock, injector=self.injector, verify_timeout=300,
            lock_wait_s=0)
        self._anchor_rows = {}

    # ---- 桩约助手（同 test_v3_gate_delivery._Base 形状） ----

    def set_anchor(self, tid, head=D_C, base=D_E, clean=True):
        if head is not None:
            self._anchor_rows[f"/tmp/envs/{tid}"] = {"head": head,
                                                     "base": base,
                                                     "clean": clean}

    def seed(self, tid, state="done", delivery="managed", deliverable=None,
             delivery_extra=None, **over):
        dl = {"managed": DeliveryState(mode="managed",
                                       target_branch=D_BRANCH),
              "external": DeliveryState(mode="external_branch",
                                        target_branch=D_BRANCH),
              "none": None}[delivery]
        if dl is not None and delivery_extra:
            dl = dataclasses.replace(dl, **delivery_extra)
        row = dict(id=tid, state=state, title=tid, goal="g",
                   project_dir="/repo", target_branch=D_BRANCH,
                   env_dir=f"/tmp/envs/{tid}", env_base=D_E,
                   dispatcher_sid=D_DISP, root_sid=D_DISP,
                   worker_sid=D_WORKER, chain_depth=1, created_ms=1,
                   updated_ms=1, commit=D_C,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=D_DISP,
                                     registered_at_ms=1),
                   deliverable=deliverable, delivery=dl)
        row.update(over)
        with self.storage.uow():
            self.storage.upsert_task(Task(**row))
        return tid

    def green_script(self, probe_rc=1, n=1):
        """全绿交付一轮：探针红→env 绿→交付循环集成绿×n 次。"""
        self.runner.script = ([(probe_rc, "base-out")] if probe_rc is not None
                              else []) + [(0, "env-ok")] + [(0, "int-ok")] * n

    def events(self, tid):
        return [e.event for e in self.storage.events_for_task(tid)]

    def messages(self, sid):
        return self.storage.messages_for_sid(sid)

    @staticmethod
    def text(m):
        return (m.body.decode("utf-8", "replace")
                if isinstance(m.body, (bytes, bytearray)) else str(m.body))

    def put_credential(self, tid):
        self.home.write_credential(tid, "tok")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=D_WORKER, token="tok", task_id=tid))

    def advance_args(self):
        return [(c[3], c[4]) for c in self.gitx.calls
                if c[0] == "advance_local_target"]

    def integration_cmds(self):
        return [c for c in self.runner.calls
                if "AGORA_INTEGRATION_BASE" in c["cmd"]]

    # ---- managed（断言1：三形态在 Git 后备保持 SQLite 服务语义） ----

    def test_managed_green_settles_revokes_and_autocancels_timer(self):
        tid = self.seed("ag-dm1")
        self.put_credential(tid)
        with self.storage.uow():  # Req: F145——终态自动取消 pending timer
            self.storage.upsert_timer(Timer(
                id="tm-dm1", task_id=tid, target_role="worker",
                due_ms=self.clock.ms + 60_000, state=TimerState.PENDING,
                created_ms=self.clock.ms, body="b", created_by=D_DISP))
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit, t.fail_streak),
                         ("closed", D_C, 0))
        d = t.delivery
        self.assertEqual((d.status, d.local_status, d.commit, d.base,
                          d.attempt, d.error, d.conflict_files),
                         ("delivered", "synced", D_C, D_TIP, 1, None, None))
        # 推进前复跑集成 verify（恰一次）＋CAS expected-old 前置
        self.assertEqual(len(self.integration_cmds()), 1)
        self.assertEqual(self.advance_args(), [(D_C, D_TIP)])
        self.assertIn("merged", self.events(tid))
        self.assertIn("closed", self.events(tid))
        self.assertIsNone(self.storage.get_credential(D_WORKER))  # 撤牌
        self.assertFalse(
            self.home.credentials.joinpath(f"{tid}.token").exists())
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 回收
        self.assertEqual(self.storage.get_timer("tm-dm1").state,
                         TimerState.CANCELLED)  # 终态自动取消（Git 后备）
        self.assertIn("timer_autocancelled", self.events(tid))
        self.assertTrue(any("local=synced" in self.text(m)
                            for m in self.messages(D_DISP)))
        # Git 不变量：每 tx 恰一 commit＋canonical 只增＋交付收口单事务
        # （task/delivery/event/message/timer 迁移同一 tx 同一 commit）
        obs = harness.GitJournalObserver(self.home.state)
        txs = obs.txs()
        self.assertEqual(len(obs.commits()), len(txs))
        self.assertTrue(obs.additions_only())
        settle = next(t for t in txs.values()
                      if {"upsert_task", "insert_message", "upsert_timer"}
                      <= {op["op"] for op in t["operations"]})
        # task1＋timer1＋message3（含 receipt3＋posted 事件3）＋merged/
        # closed/autocancel 事件——全部变更对象版本同一 tx 同一 commit
        self.assertEqual(Counter(op["op"] for op in settle["operations"]),
                         {"upsert_task": 1, "upsert_timer": 1,
                          "insert_message": 3, "upsert_receipt": 3,
                          "append_event": 6})
        files = obs.paths()
        for op in settle["operations"]:  # 全部变更对象版本同 commit 树
            self.assertIn(op["path"], files)

    def test_managed_non_ff_retries_then_exhausts(self):
        tid = self.seed("ag-dm2")
        self.green_script(n=3)
        self.set_anchor(tid)
        self.gitx.advance_script = ["non-ff", "non-ff", "non-ff"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((t.state, d.status, d.error, d.attempt,
                          t.fail_streak),
                         ("blocked", "failed", "retry_exhausted", 3, 0))
        ev = self.events(tid)
        self.assertEqual(ev.count("delivery_nonff"), 3)
        self.assertEqual(ev.count("delivery_failed"), 1)
        self.assertEqual(len(self.integration_cmds()), 3)  # 每次重验
        self.assertEqual(self.advance_args(), [(D_C, D_TIP)] * 3)

    def test_managed_dirty_and_busy_terminal(self):
        tid = self.seed("ag-dm3")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error, t.fail_streak),
                         ("blocked", "local_target_dirty", 0))
        self.assertNotIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 留现场
        tid = self.seed("ag-dm4")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["failed"]
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_target_busy")

    def test_rebase_conflict_same_base_and_false_heal(self):
        # 同基内容冲突：终局 rebase_conflict＋conflict_files（禁盲试恰一次）
        tid = self.seed("ag-dc1")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_script = ["conflict"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((d.status, d.error, d.conflict_files, d.attempt),
                         ("failed", "rebase_conflict", ["src/x.py"], 1))
        self.assertIn("rebase_conflict_same_base", self.events(tid))
        self.assertEqual(len([c for c in self.gitx.calls
                              if c[0] == "rebase_onto"]), 1)
        self.assertTrue(any("冲突文件: src/x.py" in self.text(m)
                            for m in self.messages(D_DISP)))
        # F105 假冲突自愈：基前进→最新基重建再 rebase 恰一次（attempt 不变）
        tid = self.seed("ag-dc2")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_script = ["conflict", "ok"]
        self.gitx.rebase_head = D_R
        self.gitx.on_rebase = lambda: setattr(self.gitx, "tip", D_TIP2)
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit, t.rebased_from),
                         ("closed", D_R, D_C))  # 重放先落账
        d = t.delivery
        self.assertEqual((d.status, d.attempt, d.base, d.commit),
                         ("delivered", 1, D_TIP2, D_R))  # attempt 不变
        self.assertIn("rebase_conflict_retry", self.events(tid))
        self.assertEqual(self.advance_args(), [(D_R, D_TIP2)])

    def test_phantom_guards_bounce_without_merge(self):
        # G1 空集 rebase：零交付弹回（不 advance）
        tid = self.seed("ag-dp1")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_script = ["ok"]
        self.gitx.rebase_head = None
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak, t.delivery.status),
                         ("reopened", 1, "pending"))
        self.assertIn("empty_rebase_noop", self.events(tid))
        self.assertEqual(self.advance_args(), [])
        # G2 推进后终检不含：已 advance 但绝不 merged
        tid = self.seed("ag-dp2")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "ok"  # 复位 G1 残面：本例走 ff 直通
        self.gitx.sync_marks_contained = False
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status),
                         ("reopened", "pending"))
        self.assertIn("phantom_merge_blocked", self.events(tid))
        self.assertEqual(len(self.advance_args()), 1)  # advance 已发不重复

    # ---- external_branch（断言2：驻留＋确认面） ----

    def test_external_awaiting_then_delivered_and_failed_confirm(self):
        tid = self.seed("ag-de1", delivery="external")
        self.put_credential(tid)
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "awaiting_external")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((t.state, d.status, d.commit, d.base, d.attempt,
                          d.local_status), ("done", "awaiting_external", D_C,
                                            D_TIP, 1, "not_required"))
        self.assertEqual(self.advance_args(), [])  # 零目标支动作
        self.assertNotIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)
        self.assertIsNotNone(self.storage.get_credential(D_WORKER))  # 保留
        res = self.tasksvc.deliver(tid, D_DISP, "dispatcher", "delivered",
                                   D_C)
        self.assertEqual(res, {"ok": True, "outcome": "delivered"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status,
                          t.delivery.local_status, t.commit),
                         ("closed", "delivered", "not_required", D_C))
        self.assertIsNone(self.storage.get_credential(D_WORKER))  # 回收
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)
        # failed 确认桶：经真 handoff 建立 awaiting 后 blocked（残值清）
        tid = self.seed("ag-de2", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        res = self.tasksvc.deliver(tid, D_DISP, "dispatcher", "failed", D_C)
        self.assertEqual((res["outcome"], res["error"]),
                         ("failed", "external_delivery_failed"))
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status,
                          t.delivery.conflict_files),
                         ("blocked", "failed", None))
        # non_ff 确认面：祖先预判失败＝结构性 diverged（F110，零改写）
        tid = self.seed("ag-de3", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        res = self.tasksvc.deliver(tid, D_DISP, "dispatcher", "non_ff", D_C,
                                   base="d" * 40)
        self.assertEqual((res["outcome"], res["error"]),
                         ("failed", "diverged"))
        self.assertEqual(self.storage.get_task(tid).state, "blocked")

    # ---- out_of_band（断言3：零 Git 交付动作直收口） ----

    def test_out_of_band_direct_close_without_git_delivery(self):
        tid = self.seed("ag-do1", state="running", delivery="none",
                        deliverable="out_of_band", commit=None)
        self.set_anchor(tid, head=None)  # 无锚要求
        self.assertEqual(self.gate.accept_done(tid, D_WORKER,
                                               "docs/report.md #结论"),
                         "accepted")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit), ("done", None))  # 零提交
        self.runner.script = [(1, "base"), (0, "env")]
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")
        self.assertIn("merged", self.events(tid))
        detail = [e.detail for e in self.storage.events_for_task(tid)
                  if e.event == "done"][0]
        self.assertIn("docs/report.md", detail["note"])  # 引用留痕
        ops = self.gitx.ops()
        self.assertNotIn("advance_local_target", ops)  # 零 Git 交付动作
        self.assertNotIn("merge_ff", ops)
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 回收

    # ---- 崩窗＋重启重放（断言4：恰一次、零半态、零重复推进） ----

    def test_publish_crash_after_advance_restart_replay_settles_once(self):
        tid = self.seed("ag-dw1")
        self.put_credential(tid)
        self.green_script()
        self.set_anchor(tid)
        self.injector.arm("publish", "after")  # advance 已落地、收口前崩溃
        self.assertEqual(self.gate.run(tid), "reopened")  # 门内崩弹回
        self.assertEqual(self.storage.get_task(tid).state, "reopened")
        self.assertEqual(len(self.advance_args()), 1)
        self.injector.disarm("publish")
        # 重启重放：fresh GitStore＋新服务栈——重受理（vacuous 放行：在案
        # commit==C）→landed 探针直收口，绝不重复推进
        storage2 = GitStore(self.home.state)
        self.assertEqual(storage2.get_task(tid).state, "reopened")  # 重放
        clock2 = FakeClock()
        gitx2 = GitxDeliveryGitStub(storage=storage2)
        oc2 = GateOcStub()
        msg2 = MessagingService(storage2, oc2, home=str(self.home.path),
                                clock=clock2)
        tsvc2 = TaskService(storage2, gitx2, oc2, messaging=msg2,
                            home=str(self.home.path), clock=clock2)
        gate2 = GateDeliveryService(
            storage2, gitx2, GateRunnerStub(storage2), messaging=msg2,
            tasksvc=tsvc2, anchors=lambda env: self._anchor_rows.get(env),
            artifacts=None, home=str(self.home.path), clock=clock2,
            verify_timeout=300, lock_wait_s=0)
        tsvc2.transition(tid, "running", actor=D_WORKER)
        gitx2.contained.add((D_C, D_BRANCH))  # 首轮 advance 已落地（真仓）
        self.assertEqual(gate2.accept_done(tid, D_WORKER, D_C), "accepted")
        self.assertEqual(gate2.run(tid), "merged")
        t = storage2.get_task(tid)
        self.assertEqual((t.state, t.delivery.status, t.delivery.local_status,
                          t.delivery.commit),
                         ("closed", "delivered", "synced", D_C))
        self.assertEqual([c for c in gitx2.calls
                          if c[0] == "advance_local_target"],
                         [])  # 绝不重复推进

    def test_publish_crash_before_advance_then_full_cycle(self):
        # 评审环 A1 闭包：before-publication 崩窗直接点名（镜像 SQLite
        # ag-w9）——推进前崩：目标支未动；重排队后续跑完整交付恰一次
        tid = self.seed("ag-dw5")
        self.green_script()
        self.set_anchor(tid)
        self.injector.arm("publish", "before")
        self.assertEqual(self.gate.run(tid), "reopened")  # 门内崩弹回
        self.assertEqual(self.storage.get_task(tid).state, "reopened")
        self.assertEqual(self.advance_args(), [])  # 推进前崩：目标支未动
        self.injector.disarm("publish")
        self.tasksvc.transition(tid, "running", actor=D_WORKER)
        self.set_anchor(tid, head=D_C)
        self.gitx.contained.discard((D_C, D_BRANCH))
        self.assertEqual(self.gate.accept_done(tid, D_WORKER, D_C), "accepted")
        self.runner.script = [(0, ""), (0, "int")]  # 探针缓存命中
        self.assertEqual(self.gate.run(tid), "merged")  # 完整续跑一轮交付
        self.assertEqual(len(self.advance_args()), 1)

    def test_settle_commit_crash_zero_half_state_then_replay(self):
        tid = self.seed("ag-dw2")
        self.green_script()
        self.set_anchor(tid)
        orig = self.gitx.contains

        def arm_at_final_probe(repo, commit, branch=None):
            hit = orig(repo, commit, branch)
            if hit and commit == D_C:  # G2 终检：advance 后、收口 tx 前
                self.injector.arm("update-ref", "before")
            return hit

        self.gitx.contains = arm_at_final_probe
        # 收口 tx 在 update-ref 崩（GitStore 提交窗）→弹回链路各 UoW 同点
        # 再崩——FaultInjected 逃出门体，收口零半态（overlay 弃、ref 未动）
        with self.assertRaises(harness.FaultInjected):
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status),
                         ("done", "pending"))  # 零半态：终局未落账
        self.assertNotIn("merged", self.events(tid))
        self.assertEqual(len(self.advance_args()), 1)  # advance 已落地
        self.injector.disarm("update-ref")
        self.gitx.contains = orig
        self.runner.script = []
        self.assertEqual(self.gate.run(tid), "merged")  # landed 直收：零重验
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status,
                          t.delivery.local_status), ("closed", "delivered",
                                                     "synced"))
        self.assertEqual(len(self.advance_args()), 1)  # 恰一次推进
        fresh = GitStore(self.home.state)  # 重启重放：终态投影等价
        ft = fresh.get_task(tid)
        self.assertEqual((ft.state, ft.delivery.status),
                         ("closed", "delivered"))

    def test_recover_pending_done_landed_closes_without_advance(self):
        tid = self.seed("ag-dw4",
                        delivery_extra=dict(status="pending", attempt=2,
                                            base=D_TIP))
        self.put_credential(tid)
        self.gitx.contained.add((D_C, D_BRANCH))  # 目标支已含（崩窗残留）
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status, t.delivery.attempt,
                          t.delivery.base),
                         ("closed", "delivered", 2, D_TIP))  # 在案基线保真
        self.assertIn("delivery_recovered", self.events(tid))
        self.assertNotIn("advance_local_target", self.gitx.ops())  # 零推进
        self.assertIsNone(self.storage.get_credential(D_WORKER))  # 撤牌
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 回收

    def test_failed_done_residue_patches_blocked_never_retries(self):
        tid = self.seed("ag-dw3",
                        delivery_extra=dict(status="failed", attempt=2,
                                            error="rebase_conflict"))
        self.put_credential(tid)
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status), ("blocked", "failed"))
        self.assertEqual(self.gitx.ops() & {"advance_local_target",
                                            "merge_ff"}, set())  # 绝不重试
        self.assertEqual(self.runner.calls, [])  # 不烧 verify
        self.assertIn("delivery_failed_recovery", self.events(tid))
        n = len(self.events(tid))
        self.gate.recover()  # 幂等：终局行零接触
        self.assertEqual(len(self.events(tid)), n)

if __name__ == "__main__":
    unittest.main()
