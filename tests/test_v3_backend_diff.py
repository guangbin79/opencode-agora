"""Req: F149 — GitStore Phase 1 后端差分（spec gitstore-phase1-differential）。

断言清单（spec「Required work」逐条机械判定）：
1 make_store 工厂（BACKENDS 单常量；sqlite/git 双选、未知拒）；
2 journal 只读观察器（refs/agora/journal 解析＋首父 commit 链＋对象路径＋
   tx JSON；Git 模式零 SQLite 开库——sqlite3.connect 全局换装探针）；
3 同一非空操作脚本双后端跑公共仓库方法，逐步结果＋终态投影严格差分
   （tasks/messages 含字节体/receipts/cursors/credentials 血统/events/
   错误形状）；差异必须命中显式 allowlist（三条已知有意差异，非虚设）；
4 Git-only 不变量（零 agora.db/-wal/-shm；复合操作恰一 tx＋期望对象版本
   ＝恰一 commit；canonical diff 只增）；
5 harness 自检（空操作清单 INFRA_ERROR 不冒充 PASS；故意分叉假件必红；
   去 allowlist 恰现三条已知差异——证明比较器能红且 allowlist 有据）；
6 知识沉淀（docs/agents/knowledge 记 F147/F148）。
现有 SQLite 测试零改动；生产码零改动（spec：差分暴露具体缺陷才先红测
最小修——本差分未暴露，gitstore/storage 语义面未动）。
"""
import dataclasses
import hashlib
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from agorad import domain
from tests import v3_harness as harness

ROOT = Path(__file__).resolve().parent.parent
TID = "ag-diff"
DISP = "disp-diff"
SID = "ses-diff"
STATUSES = ("pending", "inflight", "delivered")


# ---- 归一化＋叶子级比较器 ----

def norm(value):
    """值归一化：dataclass→dict（递归）、tuple→list、bytes 原样（== 保真）。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: norm(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [norm(v) for v in value]
    return value


def _leaf_diff(a, b, path, out):
    """递归叶子比对；bool/int 严格分流（True==1 不得冒充相等）。"""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append((f"{path}.{k}", a.get(k), b.get(k)))
            else:
                _leaf_diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path + "[len]", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _leaf_diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, bool) != isinstance(b, bool) or a != b:
        out.append((path, a, b))


def _canon(path):
    """索引归零（[3]→[*]）：allowlist 按结构位精确匹配，不模糊通配。"""
    return re.sub(r"\[\d+\]", "[*]", path)


# 同一底层不对称的共享理由（op: 与 state: 两面各一条 allowlist）
CRED_REASON = ("SQLite get_credential 读回 None→''（_null 逆映射不对称），"
               "GitStore 保 None——同义「无签发任务」")
EVENT_REASON = ("SQLite events_all 读回 None→''（r[4] or ''），GitStore 保"
                " None——同义「非任务事件」")

# 已知有意差异（spec：意图差异须显式 allowlist＋理由；此外一律严格相等）。
# 实证来源：F149 差分探针＋去 allowlist 全投影 diff 恰此五条（同一底层
# 不对称会在逐步结果面（op:）与终态投影面（state:）各现一条）。
ALLOWED_DIFFERENCES = {
    "op:credential_put.ok[*].task_id": CRED_REASON,
    "state:credentials[*].task_id": CRED_REASON,
    "op:reads_events_all.ok[*].task": EVENT_REASON,
    "state:events[*].task": EVENT_REASON,
    "op:err_nested_uow.error[*]":
        "同一 StorageError 拒嵌套，文案「单一全局写锁」vs「单一写者」"
        "——语义等价（均拒嵌套 UoW）",
}


# ---- 同一操作脚本（后端无关闭包，双后端逐条同参执行）----

def _uow(st, fn):
    with st.uow():
        return fn(st)


def _full_task(state="running", **over):
    base = dict(
        id=TID, state=state, title="差分任务", goal="g", req_id="F149",
        quick=False, chain_depth=2, root_sid="root", dispatcher_sid="disp",
        worker_sid="w", created_ms=1, updated_ms=2, after=("ag-p0",),
        project_dir="/repo", target_branch="main", env_dir="/envs/ag-diff",
        legacy_extra_json='{"future": [1]}',
        verify=domain.VerifySpec(cmd="true", state="registered",
                                 registered_by="disp", registered_at_ms=5,
                                 timeout_s=300, timeout_source="task"),
        delivery=domain.DeliveryState(status="pending",
                                      conflict_files=["a.py"]),
        gate=domain.GateState(review_cache={"cmd": "true", "ok": True},
                              review_fail_streak=1),
        probe=domain.BaseProbe(result="green", cmd="true", base="e" * 40,
                               ran_at_ms=4))
    base.update(over)
    return domain.Task(**base)


def _nested(st):
    with st.uow():
        with st.uow():
            pass


def scenario_ops():
    """非空操作清单：写路径（task 两版/消息含非 UTF-8 字节体/receipt/
    event 含 detail/游标两版/凭据血统/dedup 幂等重放）＋读路径＋错误形状。"""

    def _composite(st):
        with st.uow():
            m = st.insert_message(ts_ms=101, task_id=TID, kind="question",
                                  from_sid="w", to_sid=DISP,
                                  body=bytes(range(256)))
            rcpt = st.upsert_receipt(domain.DeliveryReceipt(
                message_id=m.id, status="pending", attempts=1, updated_ms=101))
            ev1 = st.append_event(domain.Event(
                seq=0, ts_ms=101, actor="w", event="created", task=TID,
                detail={"k": [1, 2]}))
            ev2 = st.append_event(domain.Event(
                seq=0, ts_ms=102, actor="w", event="tick"))
            return (m, rcpt, ev1, ev2)

    def _cursors(st):
        with st.uow():
            first = st.upsert_cursor(SID, 1)
            second = st.upsert_cursor(SID, 2)
        return (first, second, st.get_cursor(SID))

    return [
        ("upsert_task_full",
         lambda st: _uow(st, lambda s: s.upsert_task(_full_task()))),
        ("get_task_read", lambda st: st.get_task(TID)),
        ("upsert_task_v2",
         lambda st: _uow(st, lambda s: s.upsert_task(
             _full_task(state="done", commit="a" * 40)))),
        ("insert_message_str",
         lambda st: _uow(st, lambda s: s.insert_message(
             ts_ms=100, task_id=TID, kind="note", from_sid="w", to_sid=DISP,
             body="hello", dedup_key="dk-1"))),
        ("composite_tx", _composite),
        ("dedup_replay",
         lambda st: _uow(st, lambda s: s.insert_message(
             ts_ms=103, task_id=TID, kind="note", from_sid="w", to_sid=DISP,
             body="hello-retry", dedup_key="dk-1"))),
        ("cursor_upserts", _cursors),
        ("credential_put",
         lambda st: _uow(st, lambda s: (
             s.put_credential(domain.Credential(
                 sid=SID, token="tk", lineage=("ses-old", "ses-older"),
                 created_ms=7)),
             s.get_credential(SID)))),
        ("reads_tasks_all", lambda st: st.tasks_all()),
        ("reads_messages_for_sid", lambda st: st.messages_for_sid(DISP)),
        ("reads_messages_all", lambda st: st.messages_all()),
        ("reads_receipts",
         lambda st: tuple(st.receipts_by_status(s) for s in STATUSES)),
        ("reads_events_all", lambda st: st.events_all()),
        ("err_write_outside_uow",
         lambda st: st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                      from_sid="w", to_sid=DISP, body="x")),
        ("err_nested_uow", _nested),
        ("reads_missing",
         lambda st: (st.get_task("ag-none"), st.get_message(999),
                     st.get_cursor("ses-none"), st.get_receipt(999),
                     st.get_credential("ses-none"))),
    ]


def run_ops(st, ops):
    """逐条执行并归一化记录；异常也记录（类型＋文案＝错误形状差分面）。"""
    recs = []
    for name, fn in ops:
        try:
            recs.append({"op": name, "ok": norm(fn(st))})
        except Exception as exc:  # noqa: BLE001——错误形状本身是差分对象
            recs.append({"op": name, "error": [type(exc).__name__, str(exc)]})
    return recs


def project_state(st, sids=(SID,)):
    """终态外部投影（公共读方法面；receipts/cursors/credentials 无全量读
    方法——按已知 status/sid 枚举，两侧同参）。"""
    return {
        "tasks": [dataclasses.asdict(t) for t in st.tasks_all()],
        "messages": [dataclasses.asdict(m) for m in st.messages_all()],
        "receipts": [dataclasses.asdict(r) for s in STATUSES
                     for r in st.receipts_by_status(s)],
        "cursors": [None if st.get_cursor(s) is None
                    else dataclasses.asdict(st.get_cursor(s)) for s in sids],
        "credentials": [None if st.get_credential(s) is None
                        else dataclasses.asdict(st.get_credential(s))
                        for s in sids],
        "events": [dataclasses.asdict(e) for e in st.events_all()],
    }


# ---- 差分主入口 ----

def run_backend_diff(ops=None, sids=(SID,), allow=ALLOWED_DIFFERENCES,
                     sides=None):
    """双后端差分：ops 空＝INFRA_ERROR（空场景无从对齐，禁冒充 PASS）。
    返回 {"status","ops","mismatches","suppressed"}；mismatches 非空即红。"""
    ops = scenario_ops() if ops is None else ops
    if not ops:
        return {"status": "INFRA_ERROR", "ops": 0, "mismatches": [],
                "suppressed": [], "detail": "空操作清单——差分须非空场景"}
    homes = []
    try:
        if sides is None:
            sides = {}
            for backend in harness.BACKENDS:
                home = harness.TempAgoraHome()
                homes.append(home)
                sides[backend] = harness.make_store(home, backend=backend)
        raw = []
        recs_a = run_ops(sides["sqlite"], ops)
        recs_b = run_ops(sides["git"], ops)
        if len(recs_a) != len(recs_b):
            raw.append(("op[len]", len(recs_a), len(recs_b)))
        for ra, rb in zip(recs_a, recs_b):
            key = "ok" if "ok" in ra else "error"
            _leaf_diff(ra.get(key), rb.get(key),
                       f"op:{ra['op']}.{key}", raw)
        state_a, state_b = (project_state(sides["sqlite"], sids),
                            project_state(sides["git"], sids))
        for family in sorted(state_a):
            _leaf_diff(state_a[family], state_b[family],
                       f"state:{family}", raw)
        mismatches, suppressed = [], []
        for path, a, b in raw:
            canon = _canon(path)
            entry = {"path": canon, "sqlite": repr(a)[:200],
                     "git": repr(b)[:200]}
            if canon in allow:
                suppressed.append({**entry, "reason": allow[canon]})
            else:
                mismatches.append(entry)
        return {"status": "PASS" if not mismatches else "MISMATCH",
                "ops": len(ops), "mismatches": mismatches,
                "suppressed": suppressed}
    finally:
        for st in (sides or {}).values():
            close = getattr(st, "close", None)
            if close is not None:
                close()
        for home in homes:
            home.cleanup()


class _DivergentStore:
    """故意分叉假件（spec 5）：包真 GitStore，读面单点改写 task title
    ——比较器必须红，防「恒绿」哑比较器。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_task(self, tid):
        t = self._inner.get_task(tid)
        return None if t is None else dataclasses.replace(t, title="DIVERGED")

    def tasks_all(self):
        return [dataclasses.replace(t, title="DIVERGED")
                for t in self._inner.tasks_all()]


def _bucket(key):
    h = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return f"{h[:2]}/{h[2:4]}"


class TestMakeStore(unittest.TestCase):  # 断言1
    def test_selects_both_backends_and_rejects_unknown(self):
        self.assertEqual(harness.BACKENDS, ("sqlite", "git"))  # 单常量枚举
        hs = harness.TempAgoraHome()
        self.addCleanup(hs.cleanup)
        st = harness.make_store(hs, backend="sqlite")
        self.addCleanup(st.close)
        from agorad.storage import Storage
        self.assertIsInstance(st, Storage)
        self.assertTrue((hs.state / "agora.db").exists())
        hg = harness.TempAgoraHome()
        self.addCleanup(hg.cleanup)
        gt = harness.make_store(hg, backend="git")
        from agorad.gitstore import GitStore
        self.assertIsInstance(gt, GitStore)
        self.assertTrue((hg.state / "journal.git").is_dir())
        with self.assertRaises(ValueError):
            harness.make_store(hg, backend="postgres")  # 未知 backend 拒


class TestJournalObserver(unittest.TestCase):  # 断言2
    def _seeded(self):
        home = harness.TempAgoraHome()
        self.addCleanup(home.cleanup)
        st = harness.make_store(home, backend="git")
        with st.uow():
            st.upsert_task(domain.Task(id=TID))
        with st.uow():
            m = st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                  from_sid="w", to_sid=DISP, body=b"x")
            st.upsert_receipt(domain.DeliveryReceipt(message_id=m.id,
                                                     status="pending"))
            st.append_event(domain.Event(seq=0, ts_ms=1, actor="w",
                                         event="e", task=TID))
        return home, st

    def test_observer_resolves_ref_commits_paths_and_txs(self):
        home, st = self._seeded()
        obs = harness.GitJournalObserver(home.state)
        self.assertEqual(obs.head(), st.head)  # ref 解析＝内存 head
        self.assertEqual(len(obs.commits()), 2)  # 每笔 UoW 恰一 commit
        paths = obs.paths()
        self.assertTrue(any(p.startswith("objects/tasks/") for p in paths))
        self.assertTrue(any(p.startswith("objects/receipts/")
                            for p in paths))
        txs = obs.txs()
        self.assertEqual(sorted(txs), [1, 2])
        self.assertEqual(txs[2]["seq"], 2)
        self.assertEqual([op["op"] for op in txs[2]["operations"]],
                         ["insert_message", "upsert_receipt", "append_event"])
        for op in txs[2]["operations"]:
            self.assertIn(op["path"], paths)  # tx 引用对象在 HEAD 树
        self.assertTrue(obs.additions_only())

    def test_git_mode_never_opens_sqlite(self):
        # sqlite3.connect 全局换装探针：Git 模式全程（构造/重放/提交/读面
        # /观察器）开库即炸——零 SQLite 触达的机械证明。
        with mock.patch("sqlite3.connect",
                        side_effect=AssertionError("Git 模式禁开 SQLite")):
            home = harness.TempAgoraHome()
            self.addCleanup(home.cleanup)
            st = harness.make_store(home, backend="git")
            run_ops(st, scenario_ops())
            project_state(st, (SID,))
            self.assertTrue(
                harness.GitJournalObserver(home.state).additions_only())
        self.assertEqual(list(home.state.glob("*.db")), [])


class TestDifferential(unittest.TestCase):  # 断言3
    def test_strict_differential_passes_with_explicit_allowlist(self):
        report = run_backend_diff()
        self.assertEqual(report["status"], "PASS", report["mismatches"])
        self.assertGreaterEqual(report["ops"], 12)  # 操作清单非空且覆盖面足
        self.assertEqual({s["path"] for s in report["suppressed"]},
                         set(ALLOWED_DIFFERENCES))  # 恰三条已知差异

    def test_without_allowlist_exactly_known_differences_surface(self):
        report = run_backend_diff(allow={})
        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual({m["path"] for m in report["mismatches"]},
                         set(ALLOWED_DIFFERENCES))
        self.assertEqual(report["suppressed"], [])


class TestHarnessSelfCheck(unittest.TestCase):  # 断言5
    def test_empty_operation_script_is_infra_error_not_pass(self):
        report = run_backend_diff(ops=[])
        self.assertEqual(report["status"], "INFRA_ERROR")
        self.assertEqual(report["mismatches"], [])  # 不得冒充零失配 PASS

    def test_deliberately_divergent_fake_reported_as_mismatch(self):
        ops = [
            ("upsert_task",
             lambda st: _uow(st, lambda s: s.upsert_task(
                 domain.Task(id=TID, title="v1")))),
            ("get_task", lambda st: st.get_task(TID)),
            ("tasks_all", lambda st: st.tasks_all()),
        ]
        hs = harness.TempAgoraHome()
        self.addCleanup(hs.cleanup)
        hg = harness.TempAgoraHome()
        self.addCleanup(hg.cleanup)
        report = run_backend_diff(
            ops=ops,
            sides={"sqlite": harness.make_store(hs, backend="sqlite"),
                   "git": _DivergentStore(
                       harness.make_store(hg, backend="git"))})
        self.assertEqual(report["status"], "MISMATCH")
        paths = {m["path"] for m in report["mismatches"]}
        self.assertIn("op:get_task.ok.title", paths)  # 逐步结果面红
        self.assertIn("op:tasks_all.ok[*].title", paths)
        self.assertIn("state:tasks[*].title", paths)  # 终态投影面红


class TestGitOnlyInvariants(unittest.TestCase):  # 断言4
    def _git_home(self):
        home = harness.TempAgoraHome()
        self.addCleanup(home.cleanup)
        return home, harness.make_store(home, backend="git")

    def test_git_backend_creates_no_sqlite_files(self):
        home, st = self._git_home()
        run_ops(st, scenario_ops())
        for name in ("agora.db", "agora.db-wal", "agora.db-shm"):
            self.assertFalse((home.state / name).exists(), name)
        self.assertEqual(list(home.state.glob("*.db")), [])

    def test_composite_operation_one_tx_expected_versions_one_commit(self):
        home, st = self._git_home()
        obs = harness.GitJournalObserver(home.state)
        with st.uow():
            st.upsert_task(domain.Task(id=TID))
        n_tx, n_commits = len(obs.txs()), len(obs.commits())
        with st.uow():  # 复合：task v2＋message＋receipt＋event
            st.upsert_task(domain.Task(id=TID, state="running", title="v2"))
            m = st.insert_message(ts_ms=1, task_id=TID, kind="note",
                                  from_sid="w", to_sid=DISP, body=b"x",
                                  dedup_key="dk-inv")
            st.upsert_receipt(domain.DeliveryReceipt(message_id=m.id,
                                                     status="pending"))
            st.append_event(domain.Event(seq=0, ts_ms=1, actor="w",
                                         event="e", task=TID))
        txs = obs.txs()
        self.assertEqual(len(txs), n_tx + 1)  # 恰一条新 tx
        self.assertEqual(len(obs.commits()), n_commits + 1)  # 恰一个新 commit
        tx = txs[2]
        self.assertEqual(tx["seq"], 2)
        by_name = {op["op"]: op for op in tx["operations"]}
        self.assertEqual(sorted(by_name),
                         ["append_event", "insert_message",
                          "upsert_receipt", "upsert_task"])
        # 期望不可变对象版本：task 1→2、receipt 0→1、message id=1、event seq=1
        self.assertEqual((by_name["upsert_task"]["from_version"],
                          by_name["upsert_task"]["to_version"]), (1, 2))
        self.assertEqual((by_name["upsert_receipt"]["from_version"],
                          by_name["upsert_receipt"]["to_version"]), (0, 1))
        self.assertEqual(by_name["insert_message"]["id"], 1)
        self.assertEqual(by_name["append_event"]["id"], 1)
        paths = obs.paths()
        self.assertIn(f"objects/tasks/{_bucket(TID)}/{TID}/{2:020d}.json",
                      paths)
        mdir = f"objects/messages/{_bucket(1)}/{1:020d}"
        self.assertIn(mdir + "/meta.json", paths)
        self.assertIn(mdir + "/body.txt", paths)
        self.assertIn(f"objects/receipts/{_bucket(1)}/{1:020d}/{1:020d}.json",
                      paths)
        self.assertIn(f"objects/events/{_bucket(1)}/{1:020d}.json", paths)
        for op in tx["operations"]:
            self.assertIn(op["path"], paths)  # 全部对象版本同在单 commit 树

    def test_canonical_diff_additions_only_after_full_scenario(self):
        home, st = self._git_home()
        run_ops(st, scenario_ops())
        self.assertTrue(
            harness.GitJournalObserver(home.state).additions_only())


class TestKnowledgeIngest(unittest.TestCase):  # 断言6
    def test_knowledge_log_and_index_record_f147_f148(self):
        log = (ROOT / "docs/agents/knowledge/log.md").read_text("utf-8")
        idx = (ROOT / "docs/agents/knowledge/knowledge-index.md").read_text(
            "utf-8")
        for text in (log, idx):
            self.assertIn("F147", text)
            self.assertIn("F148", text)


if __name__ == "__main__":
    unittest.main()
