"""Req: F135 — v3 task 应用服务（opencode-agora v3 P3-T8）。

断言清单（任务页 ag-0abfcf，逐条机械判定）：1 create 三形态（managed 缺省
钉支＋delivery 初始行/out_of_band None/external_branch 形状）；2 worker
子单继承/拒绝矩阵（F37，含叶子层与探索父）；3 verify 双路（F23：原子登记/
探索路后补登/未登记拒派/worker 改 verify 拒）；4 client_key 幂等（F95）；
5 dispatch（钉死基线建 env/凭据 secret 文件＋DB/重派清 fail_streak）；
6 状态机接线（合法链全通/非法拒且零写入/连败三次自动 blocked）；7 cancel
族（叶子直取/级联/门内 queue/驻留 done 直结/merged 不回滚/after 链
dependency_failed/二次确认）；8 task_wait（F124：仅 running/成环报路径/
终态立即 resolved 双消息/CAS 清除）；9 reverify＋task_deliver 三 outcome；
10 迁移联动消息同 UoW（崩溃注入零半态）；11 last_seen 批量窗（不逐调用
独立写事务，PRAGMA data_version 计证）；12 架构守卫在 test_architecture.py
（tasksvc 分层锁＋SQL 圈禁自动覆盖）。gitx/oc 按桩约定注入；FaultInjector/
临时 home 复用 v3_harness；消息面接真 MessagingService（Notifier Protocol
结构化满足的活证）。
"""
import contextlib
import dataclasses
import os
import stat
import unittest

from agorad import domain
from agorad.contracts import ContractError
from agorad.domain import Credential, DeliveryState, GateState, VerifySpec
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from tests import v3_harness as harness

DISPATCHER, ROOT, WORKER = "ses-d1", "ses-root", "ses-w1"


def make_task_spec(goal="g", execution=None, verify=None, delivery=None,
                   title=None):
    """契约形建单 spec（_Base.spec 的模块级共享面——Req: F152 Git 端口/
    差分测试复用；_Base.spec 保持原样不动）。"""
    nested = {"goal": goal, "title": title,
              "execution": {"model": None, "after": None, "req_id": None,
                            "project_dir": "/repo", "client_key": None,
                            "quick": None},
              "verify": {"cmd": None, "review_cmd": None,
                         "verify_pending": None,
                         "verify_base_exempt": None, "timeout": None},
              "delivery": {"deliverable": None, "target_branch": None}}
    for group, over in (("execution", execution), ("verify", verify),
                        ("delivery", delivery)):
        nested[group].update(over or {})
    return nested


@contextlib.contextmanager
def flip_state_on_second_read(store, tid, state="running"):
    """制造 dispatch 卫兵冲突窗（Req: F152）：对 tid 的第二次 get_task 读
    改写 state——读-判-写竞态的确定性注入（首读＝入口判定、次读＝UoW 内
    复审），离开上下文即还原。"""
    orig = store.get_task
    seen = []

    def _flaky(tid_):
        t = orig(tid_)
        if t is not None and tid_ == tid:
            seen.append(1)
            if len(seen) == 2:
                return dataclasses.replace(t, state=state)
        return t

    store.get_task = _flaky
    try:
        yield
    finally:
        del store.get_task  # 去实例属性＝还原绑定方法（Storage/GitStore 同面）


class GitxStub:
    """gitx 桩：记录调用；钉死支 sha/祖先对可预置；env 生命周期可断言。"""

    def __init__(self):
        self.symref_branch = "v3-runtime"
        self.fallback_branch = "main"
        self.branches = {"v3-runtime", "main"}
        self.shas = {"v3-runtime": "b" * 40, "main": "a" * 40}
        self.ancestors = set()  # {(ancestor, descendant)}
        self.calls = []
        self.built, self.removed = set(), set()

    def symref(self, project_dir):
        self.calls.append(("symref", project_dir))
        return self.symref_branch

    def default_branch(self, project_dir):
        return self.fallback_branch

    def check_branch(self, project_dir, branch):
        if branch not in self.branches:
            raise RuntimeError(f"unknown branch {branch!r}")

    def local_branch_sha(self, project_dir, branch):
        self.calls.append(("local_branch_sha", project_dir, branch))
        if branch not in self.branches:
            raise RuntimeError(f"no such branch {branch!r}")
        return self.shas[branch]

    def build_env(self, project_dir, tid, branch=None):
        self.calls.append(("build_env", tid, branch, None))
        env = f"/tmp/envs/{tid}"
        self.built.add(env)
        return env

    def build_env_at(self, project_dir, tid, branch=None, base_sha=None):
        self.calls.append(("build_env_at", tid, branch, base_sha))
        env = f"/tmp/envs/{tid}"
        self.built.add(env)
        return env

    def materialize_checkout(self, tree_dir, source_dir):
        self.calls.append(("materialize_checkout", tree_dir))
        return "materialized"

    def head(self, env):
        return "e" * 40

    def env_on_disk(self, env):
        return bool(env) and env in self.built and env not in self.removed

    def remove_env(self, env):
        self.calls.append(("remove_env", env))
        self.removed.add(env)

    def is_ancestor(self, project_dir, ancestor, descendant):
        return (ancestor, descendant) in self.ancestors


class OcStub:
    """oc 桩：会话创建（fail 熔断面）＋三注入通道（messaging 投递面）。"""

    def __init__(self):
        self.created = []
        self.injects = []
        self.fail = False

    def create(self, env, boot, title=None, model=None):
        if self.fail:
            raise RuntimeError("oc down")
        sid = f"ses-w{len(self.created) + 1}"
        self.created.append((sid, env, title, model))
        return sid

    def inject(self, sid, text, model=None):
        self.injects.append((sid, text))
        return True

    def inject_silent(self, sid, text):
        self.injects.append(("silent", sid, text))
        return True

    def inject_notify(self, sid, text):
        self.injects.append(("notify", sid, text))
        return True


class FakeClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = (harness.FaultInjector()
                         .register("uow").register("dispatch"))
        self.storage = Storage(self.home.state / "agora.db",
                               injector=self.injector)
        self.addCleanup(self.storage.close)
        self.clock = FakeClock()
        self.gitx = GitxStub()
        self.oc = OcStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.svc = TaskService(
            self.storage, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock)

    def spec(self, goal="g", execution=None, verify=None, delivery=None,
             title=None):
        nested = {"goal": goal, "title": title,
                  "execution": {"model": None, "after": None, "req_id": None,
                                "project_dir": "/repo", "client_key": None,
                                "quick": None},
                  "verify": {"cmd": None, "review_cmd": None,
                             "verify_pending": None,
                             "verify_base_exempt": None, "timeout": None},
                  "delivery": {"deliverable": None, "target_branch": None}}
        for group, over in (("execution", execution), ("verify", verify),
                            ("delivery", delivery)):
            nested[group].update(over or {})
        return nested

    def mk(self, sid=DISPATCHER, role="dispatcher", **kw):
        return self.svc.create(sid, role, self.spec(**kw))

    def seed(self, tid, **over):
        row = dict(id=tid, state="todo", title=tid, goal="g",
                   project_dir="/repo", target_branch="v3-runtime",
                   dispatcher_sid=DISPATCHER, root_sid=DISPATCHER,
                   chain_depth=1, created_ms=1, updated_ms=1,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=DISPATCHER,
                                     registered_at_ms=1))
        row.update(over)
        task = domain.Task(**row)
        with self.storage.uow():
            self.storage.upsert_task(task)
        return tid

    def events(self, tid):
        return [e.event for e in self.storage.events_for_task(tid)]

    @staticmethod
    def text(m):
        return (m.body.decode("utf-8", "replace")
                if isinstance(m.body, (bytes, bytearray)) else str(m.body))


class TestCreateForms(_Base):  # 断言1
    def test_managed_default_pins_symref_and_delivery_row(self):
        tid = self.mk()
        t = self.storage.get_task(tid)
        self.assertEqual(t.target_branch, "v3-runtime")  # symref 实名钉死
        self.assertIn(("symref", "/repo"), self.gitx.calls)
        self.assertEqual((t.delivery.mode, t.delivery.status,
                          t.delivery.attempt, t.delivery.local_status,
                          t.delivery.target_branch),
                         ("managed", "pending", 0, "pending", "v3-runtime"))
        self.assertEqual(t.state, "todo")
        self.assertIn("task_created", self.events(tid))

    def test_out_of_band_keeps_delivery_none(self):
        t = self.storage.get_task(
            self.mk(delivery={"deliverable": "out_of_band"}))
        self.assertIsNone(t.delivery)

    def test_external_branch_shape(self):
        t = self.storage.get_task(
            self.mk(delivery={"deliverable": "external_branch"}))
        self.assertEqual((t.delivery.mode, t.delivery.status,
                          t.delivery.attempt, t.delivery.local_status),
                         ("external_branch", "pending", 0, "pending"))

    def test_explicit_branch_validated_and_fallback_audited(self):
        with self.assertRaises(ContractError) as cm:
            self.mk(delivery={"target_branch": "nope"})
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertEqual(self.storage.tasks_all(), [])  # 零行
        self.gitx.symref_branch = None  # detached/解析失败 → 回退默认支＋审计
        tid = self.mk()
        t = self.storage.get_task(tid)
        self.assertEqual(t.target_branch, "main")
        self.assertIn("target_branch_fallback", self.events(tid))

    def test_absent_project_dir_skips_branch_pinning(self):
        # Req: F159 缺 project_dir＝跳过钉支/验支（真实 gitx 对 None 路径报
        # ENOENT 天书）：照建 todo、支空、零 gitx 调用
        tid = self.mk(execution={"project_dir": None})
        t = self.storage.get_task(tid)
        self.assertEqual((t.project_dir, t.target_branch, t.state),
                         (None, None, "todo"))
        self.assertEqual(self.gitx.calls, [])  # 钉支面零调用

    def test_absent_project_dir_explicit_branch_unvalidated(self):
        # Req: F159 缺 project_dir 显式支照存不验（"nope" 不在 branches，
        # 若误发 check_branch 即拒建——本测证明零验证零钉支）
        tid = self.mk(execution={"project_dir": None},
                      delivery={"target_branch": "nope"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.target_branch, t.delivery.target_branch),
                         ("nope", "nope"))
        self.assertEqual(self.gitx.calls, [])


class TestWorkerMatrix(_Base):  # 断言2
    def setUp(self):
        super().setUp()
        self.parent = self.seed("ag-p1", state="dispatched", worker_sid=WORKER,
                                model="prov/m")

    def test_inherits_parent_scope(self):
        tid = self.svc.create(WORKER, "worker", self.spec())
        t = self.storage.get_task(tid)
        self.assertEqual((t.project_dir, t.target_branch, t.model),
                         ("/repo", "v3-runtime", "prov/m"))
        self.assertEqual(t.verify,
                         self.storage.get_task("ag-p1").verify)  # 整体继承
        self.assertEqual((t.dispatcher_sid, t.root_sid, t.chain_depth),
                         (WORKER, DISPATCHER, 2))

    def test_explicit_other_repo_rejected(self):
        with self.assertRaises(ContractError) as cm:
            self.svc.create(WORKER, "worker",
                            self.spec(execution={"project_dir": "/other"}))
        self.assertEqual(cm.exception.code, "forbidden")
        self.assertIn("强制继承", cm.exception.message)
        self.assertEqual({t.id for t in self.storage.tasks_all()}, {"ag-p1"})

    def test_write_faces_rejected(self):
        faces = ({"verify": {"cmd": "x"}}, {"verify": {"timeout": 5}},
                 {"verify": {"verify_pending": True}},
                 {"verify": {"verify_base_exempt": True}},
                 {"verify": {"verify_base_exempt": False}},  # 显式 False 亦拒
                 {"verify": {"review_cmd": "r"}},
                 {"delivery": {"target_branch": "main"}},
                 {"execution": {"model": "p/m"}},
                 {"delivery": {"deliverable": "out_of_band"}})
        for over in faces:
            with self.assertRaises(ContractError) as cm:
                self.svc.create(WORKER, "worker", self.spec(**over))
            self.assertEqual(cm.exception.code, "forbidden", over)
        self.assertEqual({t.id for t in self.storage.tasks_all()}, {"ag-p1"})

    def test_leaf_depth_and_exploring_parent(self):
        self.seed("ag-p2", state="dispatched", worker_sid=WORKER,
                  chain_depth=2, created_ms=5)
        with self.assertRaises(ContractError) as cm:
            self.svc.create(WORKER, "worker", self.spec())
        self.assertIn("叶子层", cm.exception.message)
        self.seed("ag-p3", state="dispatched", worker_sid=WORKER, created_ms=10,
                  verify=VerifySpec())  # pending＝探索父
        with self.assertRaises(ContractError) as cm:
            self.svc.create(WORKER, "worker", self.spec())
        self.assertIn("探索中", cm.exception.message)


class TestVerifyDualPath(_Base):  # 断言3
    def test_create_atomic_registration(self):
        tid = self.mk(verify={"cmd": "python3 -m pytest -q"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.verify.state, t.verify.registered_by, t.verify.cmd),
                         ("registered", DISPATCHER, "python3 -m pytest -q"))
        self.assertIn("verify_registered", self.events(tid))

    def test_exploration_path_dispatchable_then_late_registration(self):
        tid = self.mk(verify={"verify_pending": True})
        t = self.storage.get_task(tid)
        self.assertEqual((t.verify.state, t.verify_pending),
                         ("pending", True))
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")  # 探索路可派发
        self.assertEqual(self.storage.get_task(tid).state, "dispatched")
        self.svc.update_verify(tid, DISPATCHER, "dispatcher", "make check")
        t = self.storage.get_task(tid)
        self.assertEqual((t.verify.state, t.verify.cmd),
                         ("registered", "make check"))

    def test_dispatch_rejects_unregistered_zero_write(self):
        tid = self.mk()
        with self.assertRaises(ContractError) as cm:
            self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("verify 未登记", cm.exception.message)
        self.assertEqual(self.storage.get_task(tid).state, "todo")  # 零写入
        self.assertIn("dispatch_rejected_verify_missing", self.events(tid))
        self.assertEqual(self.oc.created, [])  # 零会话零 env

    def test_worker_and_foreign_dispatcher_cannot_register(self):
        tid = self.mk(verify={"verify_pending": True})
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        with self.assertRaises(ContractError) as cm:
            self.svc.update_verify(tid, WORKER, "worker", "cmd")
        self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(ContractError):
            self.svc.update_verify(tid, "ses-d2", "dispatcher", "cmd")
        self.assertEqual(self.storage.get_task(tid).verify.state,
                         "pending")  # 登记面锁定未动

    def test_quick_history_resolution(self):
        registered = dict(state="registered", registered_by="x",
                          registered_at_ms=1)
        self.seed("ag-h1", state="merged", commit="c" * 40,
                  verify=VerifySpec(cmd="pytest -q", **registered))
        self.seed("ag-h2", state="merged", commit="d" * 40,
                  verify=VerifySpec(cmd="pytest -q", **registered))
        tid = self.mk(execution={"quick": True})
        t = self.storage.get_task(tid)
        self.assertEqual((t.quick, t.verify.cmd, t.verify.state),
                         (True, "pytest -q", "registered"))


class TestClientKey(_Base):  # 断言4
    def test_same_key_same_payload_idempotent(self):
        kw = dict(goal="same", execution={"client_key": "k1"},
                  verify={"cmd": "true"})
        t1 = self.mk(**kw)
        self.svc.dispatch(t1, DISPATCHER, "dispatcher")
        builds = [c for c in self.gitx.calls if c[0].startswith("build_env")]
        n_events = len(self.events(t1))
        t2 = self.mk(**kw)
        self.assertEqual(t2, t1)  # 原 tid 返回
        self.assertEqual(len(self.storage.tasks_all()), 1)  # 零新行
        self.assertEqual(
            [c for c in self.gitx.calls if c[0].startswith("build_env")],
            builds)  # 零新 env
        self.assertEqual(len(self.events(t1)), n_events)  # 零新事件

    def test_same_key_different_payload_conflict(self):
        self.mk(goal="same", execution={"client_key": "k1"})
        with self.assertRaises(ContractError) as cm:
            self.mk(goal="different", execution={"client_key": "k1"})
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("载荷不同", cm.exception.message)
        self.assertEqual(len(self.storage.tasks_all()), 1)

    def test_other_pool_same_key_rejected(self):
        self.mk(goal="same", execution={"client_key": "k1"})
        with self.assertRaises(ContractError) as cm:
            self.svc.create("ses-d2", "dispatcher",
                            self.spec(goal="same",
                                      execution={"client_key": "k1"}))
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("key conflict", cm.exception.message)


class TestDispatch(_Base):  # 断言5
    def test_full_flow_env_base_and_credentials(self):
        tid = self.mk(verify={"cmd": "true"})
        res = self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "dispatched")
        self.assertEqual(t.worker_sid, "ses-w1")
        self.assertEqual(t.env_dir, f"/tmp/envs/{tid}")
        self.assertEqual(t.env_base, "e" * 40)
        self.assertIn(("local_branch_sha", "/repo", "v3-runtime"),
                      self.gitx.calls)
        self.assertIn(("build_env_at", tid, "v3-runtime", "b" * 40),
                      self.gitx.calls)  # 按钉死基线建 env（权威定基）
        self.assertIn(("materialize_checkout", f"/tmp/envs/{tid}"),
                      self.gitx.calls)  # ag-0d2daa 检出即物化
        self.assertIn("checkout_materialize", self.events(tid))
        cred = self.storage.get_credential("ses-w1")  # DB 面
        self.assertEqual((cred.token, cred.task_id), (res["token"], tid))
        p = self.home.credentials / f"{tid}.token"  # secret 文件面 0600
        self.assertTrue(p.is_file())
        self.assertEqual(_mode(p), 0o600)
        self.assertIn(res["token"], p.read_text(encoding="utf-8"))
        self.assertIn("dispatched", self.events(tid))
        self.assertTrue(any(sid == "ses-w1"
                            for sid, *_ in self.oc.injects))  # 凭据消息已投

    def test_redispatch_resets_fail_streak_and_failed_delivery(self):
        tid = self.seed(
            "ag-r1", state="blocked", fail_streak=3,
            delivery=DeliveryState(mode="managed", status="failed",
                                   error="x", attempt=2, commit="c" * 40))
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("dispatched", 0))
        self.assertEqual((t.delivery.status, t.delivery.error,
                          t.delivery.attempt, t.delivery.commit),
                         ("pending", None, 0, None))

    def test_can_spawn_failure_recycles_new_env(self):
        tid = self.mk(verify={"cmd": "true"})
        self.oc.fail = True
        with self.assertRaises(ContractError) as cm:
            self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        self.assertIn("can_spawn", cm.exception.message)
        self.assertEqual(self.storage.get_task(tid).state, "todo")
        self.assertIn(("remove_env", f"/tmp/envs/{tid}"), self.gitx.calls)


class TestStateMachine(_Base):  # 断言6
    def test_legal_chain_full(self):
        tid = self.seed("ag-s1")
        for to in ("dispatched", "running", "done", "merged", "closed"):
            self.svc.transition(tid, to, actor="gate")
        self.assertEqual(self.storage.get_task(tid).state, "closed")

    def test_illegal_transition_rejected_zero_write(self):
        tid = self.seed("ag-s2")
        before = self.storage.get_task(tid)
        with self.assertRaises(ContractError) as cm:
            self.svc.transition(tid, "running")
        self.assertEqual(cm.exception.code, "conflict")
        after = self.storage.get_task(tid)
        self.assertEqual((after.state, after.updated_ms),
                         (before.state, before.updated_ms))  # 零写入

    def test_done_blocked_requires_delivery_failure(self):
        tid = self.seed("ag-s3", state="done")
        with self.assertRaises(ContractError):
            self.svc.transition(tid, "blocked")
        self.svc.transition(tid, "blocked", delivery_failure=True)
        self.assertEqual(self.storage.get_task(tid).state, "blocked")

    def test_fail_streak_third_bump_auto_blocks_persisted(self):
        tid = self.seed("ag-s4", state="verify_failed")
        self.assertEqual(self.svc.bump_fail_streak(tid)["state"],
                         "verify_failed")
        self.assertEqual(self.svc.bump_fail_streak(tid)["state"],
                         "verify_failed")
        r = self.svc.bump_fail_streak(tid)
        self.assertEqual((r["state"], r["fail_streak"]), ("blocked", 3))
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("blocked", 3))  # 落库
        self.assertIn("blocked", self.events(tid))
        self.assertTrue(any(m.kind == "result"
                            for m in self.messaging.pull(DISPATCHER)))


class TestCancelFamily(_Base):  # 断言7
    def test_leaf_direct_cancel_with_message_and_revocation(self):
        tid = self.seed("ag-c1", state="dispatched", worker_sid="ses-w9")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid="ses-w9", token="t", task_id=tid))
        self.assertEqual(self.svc.cancel(tid, DISPATCHER, "dispatcher"),
                         {"cancelled": 1})
        self.assertEqual(self.storage.get_task(tid).state, "cancelled")
        self.assertIsNone(self.storage.get_credential("ses-w9"))  # 撤牌
        msgs = self.messaging.pull(DISPATCHER)
        self.assertTrue(any(m.kind == "result" and "已取消" in self.text(m)
                            for m in msgs))

    def test_cascade_nested_all_states(self):
        self.seed("ag-top", state="dispatched", worker_sid="ses-w1")
        self.seed("ag-mid", state="running", worker_sid="ses-w2",
                  dispatcher_sid="ses-w1", chain_depth=2)
        self.seed("ag-leaf", state="todo", dispatcher_sid="ses-w2",
                  chain_depth=3)
        res = self.svc.cancel("ag-top", DISPATCHER, "dispatcher")
        self.assertEqual(res["cancelled"], 3)
        for tid in ("ag-top", "ag-mid", "ag-leaf"):
            self.assertEqual(self.storage.get_task(tid).state, "cancelled")

    def test_done_in_gate_queues(self):
        tid = self.seed("ag-q1", state="done", gate=GateState(in_gate=True))
        res = self.svc.cancel(tid, DISPATCHER, "dispatcher")
        self.assertEqual(res["cancelled"], 0)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.gate.pending_cancel),
                         ("done", True))  # queue 语义＋in_gate 标记保持

    def test_parked_done_settles_directly(self):
        tid = self.seed("ag-q2", state="done")  # 门已走完（in_gate=False）
        self.assertEqual(self.svc.cancel(tid, DISPATCHER, "dispatcher"),
                         {"cancelled": 1})
        self.assertEqual(self.storage.get_task(tid).state, "cancelled")

    def test_merged_not_touched_but_kids_cascade(self):
        self.seed("ag-m1", state="merged", worker_sid="ses-w1")
        self.seed("ag-m2", state="todo", dispatcher_sid="ses-w1")
        res = self.svc.cancel("ag-m1", DISPATCHER, "dispatcher")
        self.assertEqual(res["cancelled"], 1)
        self.assertEqual(self.storage.get_task("ag-m1").state, "merged")
        self.assertEqual(self.storage.get_task("ag-m2").state, "cancelled")

    def test_after_chain_dependency_failed(self):
        self.seed("ag-a1", state="done")
        self.seed("ag-a2", state="todo", after=("ag-a1",))
        self.assertEqual(self.svc.stop_after_failed("ag-a1"), ["ag-a2"])
        t = self.storage.get_task("ag-a2")
        self.assertEqual((t.state, t.cancel_reason),
                         ("cancelled", "dependency_failed:ag-a1"))
        self.assertIn("dependency_stopped", self.events("ag-a2"))

    def test_dependency_skip_confirm_semantics(self):
        tid = self.seed("ag-skip", state="cancelled",
                        cancel_reason="dependency_failed:ag-x")
        self.assertEqual(
            self.svc.cancel(tid, DISPATCHER, "dispatcher"),
            {"cancelled": 0, "reason": "dependency_skip_confirmed"})
        self.assertEqual(self.storage.get_task(tid).cancel_reason,
                         "dispatcher_confirmed:dependency_failed:ag-x")
        self.assertEqual(self.svc.cancel(tid, DISPATCHER, "dispatcher"),
                         {"cancelled": 0})  # 已确认：幂等空过


class TestTaskWait(_Base):  # 断言8
    def test_only_running_and_role_gates(self):
        todo = self.seed("ag-w0", state="todo")
        dep = self.seed("ag-w1", state="running")
        with self.assertRaises(ContractError) as cm:
            self.svc.wait(todo, DISPATCHER, "dispatcher", dep)
        self.assertIn("仅 running", cm.exception.message)
        running = self.seed("ag-w2", state="running")
        with self.assertRaises(ContractError) as cm:
            self.svc.wait(running, WORKER, "worker", dep)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_self_and_missing_rejected(self):
        tid = self.seed("ag-w3", state="running")
        with self.assertRaises(ContractError):
            self.svc.wait(tid, DISPATCHER, "dispatcher", tid)
        with self.assertRaises(ContractError) as cm:
            self.svc.wait(tid, DISPATCHER, "dispatcher", "ag-none")
        self.assertEqual(cm.exception.code, "not_found")

    def test_cycle_detected_with_path(self):
        t1 = self.seed("ag-c1", state="running")
        t2 = self.seed("ag-c2", state="running", waiting_on="ag-c1")
        with self.assertRaises(ContractError) as cm:
            self.svc.wait(t1, DISPATCHER, "dispatcher", t2)  # t1→t2→t1
        self.assertIn("成环", cm.exception.message)
        self.assertIn("ag-c2", cm.exception.message)  # 报路径

    def test_resolved_immediately_with_double_message(self):
        tid = self.seed("ag-r0", state="running", worker_sid="ses-w5")
        dep = self.seed("ag-r1", state="merged")
        self.assertEqual(
            self.svc.wait(tid, DISPATCHER, "dispatcher", dep),
            {"ok": True, "resolved": True, "dependency_state": "merged"})
        self.assertIn("wait_resolved", self.events(tid))
        worker_msgs = self.messaging.pull("ses-w5")
        self.assertTrue(any(m.kind == "process"
                            and "受控等待解除" in self.text(m)
                            for m in worker_msgs))
        disp_msgs = self.messaging.pull(DISPATCHER)
        self.assertTrue(any(m.kind == "result"
                            and "受控等待解除" in self.text(m)
                            for m in disp_msgs))  # 双消息

    def test_register_clear_and_cas(self):
        t1 = self.seed("ag-t1", state="running")
        a = self.seed("ag-ta", state="running")
        b = self.seed("ag-tb", state="dispatched")
        self.assertEqual(self.svc.wait(t1, DISPATCHER, "dispatcher", a),
                         {"ok": True, "waiting_on": a})
        self.assertEqual(self.storage.get_task(t1).waiting_on, a)
        self.svc.wait(t1, DISPATCHER, "dispatcher", b)  # 换登记
        self.assertFalse(self.svc.clear_wait_if(t1, a))  # CAS：旧值不抹
        self.assertEqual(self.storage.get_task(t1).waiting_on, b)
        self.assertTrue(self.svc.clear_wait_if(t1, b))
        self.assertIsNone(self.storage.get_task(t1).waiting_on)


class TestReverifyDeliver(_Base):  # 断言9
    def test_reverify_only_done(self):
        running = self.seed("ag-v1", state="running")
        with self.assertRaises(ContractError):
            self.svc.reverify(running, DISPATCHER, "dispatcher")
        done = self.seed("ag-v2", state="done")
        self.assertEqual(self.svc.reverify(done, DISPATCHER, "dispatcher"),
                         {"ok": True, "requeued": True})
        self.assertIn("reverify", self.events(done))
        ext = self.seed("ag-v3", state="done",
                        delivery=DeliveryState(
                            mode="external_branch",
                            status="awaiting_external", commit="c" * 40))
        with self.assertRaises(ContractError) as cm:
            self.svc.reverify(ext, DISPATCHER, "dispatcher")
        self.assertIn("awaiting_external", cm.exception.message)

    def _ext(self, tid, **dl):
        return self.seed(tid, state="done",
                         delivery=DeliveryState(
                             mode="external_branch",
                             status="awaiting_external", commit="c" * 40,
                             **dl))

    def test_deliver_delivered_full_chain(self):
        tid = self._ext("ag-x1")
        res = self.svc.deliver(tid, DISPATCHER, "dispatcher", "delivered",
                               "c" * 40)
        self.assertEqual(res, {"ok": True, "outcome": "delivered"})
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")  # done→merged→closed 全链
        self.assertEqual((t.delivery.status, t.delivery.local_status,
                          t.commit),
                         ("delivered", "not_required", "c" * 40))
        self.assertIn("merged", self.events(tid))
        self.assertIn("closed", self.events(tid))

    def test_deliver_failed_blocks_with_error(self):
        tid = self._ext("ag-x1")
        self.assertEqual(
            self.svc.deliver(tid, DISPATCHER, "dispatcher", "failed",
                             "c" * 40),
            {"ok": True, "outcome": "failed",
             "error": "external_delivery_failed"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status, t.delivery.error),
                         ("blocked", "failed", "external_delivery_failed"))
        self.assertIn("delivery_failed", self.events(tid))

    def test_deliver_non_ff_diverged_and_contradiction(self):
        tid = self._ext("ag-x1")
        res = self.svc.deliver(tid, DISPATCHER, "dispatcher", "non_ff",
                               "c" * 40, base="d" * 40)
        self.assertEqual((res["outcome"], res["error"]),
                         ("failed", "diverged"))  # 非祖先＝结构性 diverged
        self.assertEqual(self.storage.get_task(tid).state, "blocked")
        self.gitx.ancestors.add(("d" * 40, "c" * 40))
        tid2 = self._ext("ag-x2")
        with self.assertRaises(ContractError) as cm:
            self.svc.deliver(tid2, DISPATCHER, "dispatcher", "non_ff",
                             "c" * 40, base="d" * 40)
        self.assertEqual(cm.exception.code, "conflict")  # 协议自相矛盾
        self.assertEqual(self.storage.get_task(tid2).state, "done")  # 零改写

    def test_deliver_stale_commit_and_mode_guards(self):
        tid = self._ext("ag-x1")
        with self.assertRaises(ContractError) as cm:
            self.svc.deliver(tid, DISPATCHER, "dispatcher", "delivered",
                             "f" * 40)
        self.assertIn("过期 commit", cm.exception.message)
        self.assertEqual(self.storage.get_task(tid).state, "done")
        managed = self.seed("ag-m9", state="done",
                            delivery=DeliveryState(mode="managed"))
        with self.assertRaises(ContractError):
            self.svc.deliver(managed, DISPATCHER, "dispatcher", "delivered",
                             "c" * 40)


class TestAtomicMessages(_Base):  # 断言10
    def test_cancel_crash_zero_half_state(self):
        tid = self.seed("ag-k1", state="dispatched", worker_sid="ses-w9")
        self.injector.arm("uow", "after")  # 写完、COMMIT 前
        with self.assertRaises(harness.FaultInjected):
            self.svc.cancel(tid, DISPATCHER, "dispatcher")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "dispatched")  # 零半态：任务未动
        self.assertEqual(self.storage.events_for_task(tid), [])  # 零事件
        self.assertEqual(self.storage.messages_for_sid(DISPATCHER), [])
        self.injector.disarm("uow")
        self.assertEqual(self.svc.cancel(tid, DISPATCHER, "dispatcher"),
                         {"cancelled": 1})
        self.assertEqual(self.storage.get_task(tid).state, "cancelled")
        self.assertIn("cancelled", self.events(tid))
        self.assertTrue(self.storage.messages_for_sid(DISPATCHER))


class TestLastSeenThrottle(_Base):  # 断言11
    def test_batched_window_no_per_call_txn(self):
        tid = self.seed("ag-l1", state="running")
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)

        def version():
            return reader.execute("PRAGMA data_version").fetchone()[0]

        v0 = version()
        for _ in range(100):  # 逐调用不独立写事务
            self.svc.touch_seen(tid)
        self.assertLessEqual(version() - v0, 1)  # 至多一次窗口持久
        self.assertEqual(self.storage.get_task(tid).last_seen_ms,
                         self.clock.ms)
        self.clock.advance(6000)  # 超窗后下一次调用落一次
        before = version()
        self.svc.touch_seen(tid)
        self.assertEqual(version(), before + 1)


class TestReviewLoopFixes(_Base):
    """评审环 R1 修复的机械锚（四源 findings 去重后逐项验证）。"""

    def test_settle_cancel_in_gate_race_inlines_queue_no_nested_uow(self):
        # B1/OCR：_settle_cancel UoW 内遇刚进门任务须内联排队不嵌套 uow
        tid = self.seed("ag-f1", state="done", gate=GateState(in_gate=True))
        self.assertFalse(self.svc._settle_cancel(tid, DISPATCHER, None))
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.gate.pending_cancel), ("done", True))

    def test_redispatch_revokes_previous_worker_credential(self):
        # C1/B3：blocked 重派撤旧牌防双绑
        tid = self.mk(verify={"cmd": "true"})
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        self.assertIsNotNone(self.storage.get_credential("ses-w1"))
        for _ in range(3):
            self.svc.bump_fail_streak(tid)  # dispatched 三连败→blocked
        self.assertEqual(self.storage.get_task(tid).state, "blocked")
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        self.assertIsNone(self.storage.get_credential("ses-w1"))  # 旧牌已撤
        self.assertIsNotNone(self.storage.get_credential("ses-w2"))

    def test_cancel_removes_credential_secret_file(self):
        # B5/C3：终局撤牌双删（DB 行＋secret 文件）
        tid = self.seed("ag-f2", state="dispatched", worker_sid="ses-w9")
        p = self.home.write_credential(tid, "tok")
        self.assertTrue(p.is_file())
        self.svc.cancel(tid, DISPATCHER, "dispatcher")
        self.assertFalse(p.is_file())

    def test_transition_blocked_notify_not_swallowed_across_rounds(self):
        # B2：dedup 键掺迁移时点——blocked 循环第二轮通知不被吞（两轮
        # 迁移时点不同＝不同键；键形与 v2 ag-acae3b (tid,to,updated) 同构）
        tid = self.seed("ag-f3", state="done")
        self.svc.transition(tid, "blocked", actor="gate",
                            delivery_failure=True)
        for to in ("dispatched", "running", "done"):
            self.clock.advance(5)  # 真实时钟下迁移时点必异
            self.svc.transition(tid, to, actor="gate")
        self.clock.advance(5)
        self.svc.transition(tid, "blocked", actor="gate",
                            delivery_failure=True)
        pulled = self.messaging.pull(DISPATCHER)
        self.assertEqual(sum(1 for m in pulled
                             if m.kind == "result"
                             and "blocked" in self.text(m)), 2)  # 两轮各达

    def test_transition_missing_task_not_found(self):
        # OCR：not_found 契约一致（防 None 入 domain）
        with self.assertRaises(ContractError) as cm:
            self.svc.transition("ag-none", "running")
        self.assertEqual(cm.exception.code, "not_found")
        with self.assertRaises(ContractError) as cm:
            self.svc.bump_fail_streak("ag-none")
        self.assertEqual(cm.exception.code, "not_found")

    def test_blank_review_cmd_rejected(self):
        # OCR：review_cmd 空值 fail-fast（v2 F114 语义）
        with self.assertRaises(ContractError) as cm:
            self.mk(verify={"review_cmd": "   "})
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertEqual(self.storage.tasks_all(), [])

    def test_external_branch_without_pinned_branch_rejected_at_dispatch(self):
        # A3：external_branch 无钉死支拒派（v2 local_base 硬拒）
        tid = self.seed("ag-f4", delivery=DeliveryState(
            mode="external_branch"), target_branch=None)
        self.assertIsNone(self.storage.get_task(tid).target_branch)
        with self.assertRaises(ContractError) as cm:
            self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        self.assertIn("local_base", cm.exception.message)

    def test_client_key_idempotent_with_absent_project_dir(self):
        # OCR：project_dir=None 幂等重试不炸（_repo_key None 防御）
        kw = dict(execution={"client_key": "k9", "project_dir": None})
        t1 = self.mk(**kw)
        self.assertEqual(self.mk(**kw), t1)

    def test_touch_seen_binding_rejects_foreign_worker(self):
        # C4：worker_sid 绑定校验——非本任务在役 worker 拒刷
        tid = self.seed("ag-f5", state="running", worker_sid="ses-w9")
        self.assertFalse(self.svc.touch_seen(tid, worker_sid="ses-w1"))
        self.assertNotIn(tid, self.svc._seen)  # 拒刷零内存态
        self.assertTrue(self.svc.touch_seen(tid, worker_sid="ses-w9"))
        self.assertIn(tid, self.svc._seen)

    def test_credential_file_created_atomically_0600(self):
        # C2：原子建 0600（os.open 带模式，无先建后 chmod 窗）
        tid = self.mk(verify={"cmd": "true"})
        self.svc.dispatch(tid, DISPATCHER, "dispatcher")
        p = self.home.credentials / f"{tid}.token"
        self.assertEqual(_mode(p), 0o600)


if __name__ == "__main__":
    unittest.main()
