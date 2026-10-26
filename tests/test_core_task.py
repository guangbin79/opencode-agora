"""T7 core/task 契约：保险丝/级联中止/verify 登记/重派/任务页。

真 Store/State/Auth/Router＋真 pages（tmp 夹具）；假 oc/gitx 注入；
嵌套链用 State.add 播种（平表级联＝子任务 dispatcher_sid==上级 worker_sid）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.oc import OcUnavailable
from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import Rejected, TaskService


class FakeGitx:
    def __init__(self):
        self.built, self.removed = [], []

    def build_env(self, project_dir, tid):
        env = f"/envs/{tid}"
        self.built.append(env)
        return env

    def remove_env(self, env_dir):
        self.removed.append(env_dir)


class FakeOc:
    def __init__(self):
        self.sessions, self.injected = [], []
        self.fail_create = False

    def create(self, cwd, boot):
        if self.fail_create:
            raise OcUnavailable("stub down")
        sid = f"s{len(self.sessions) + 1}"
        self.sessions.append((sid, cwd, boot))
        return sid

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class TestCoreTask(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.gitx, self.oc = FakeGitx(), FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.svc = TaskService(self.state, self.auth, self.gitx, self.oc,
                               self.store, self.router, write_task_page)
        self.root = self.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid, state="running", disp="disp1", worker=None, depth=1, env=None):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": env,
                             "verify": {"cmd": "", "state": "pending",
                                        "registered_by": None, "registered_at": None},
                             "state": state, "dispatcher_sid": disp,
                             "worker_sid": worker, "chain_depth": depth,
                             "fail_streak": 0, "created": 0.0, "updated": 0.0})
        return tid

    def events(self):
        lines = self.tmp.joinpath("events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line]

    def dispatchable(self, goal="do x", after=None):
        return self.svc.create(goal, "/repo", after, self.root)

    def dispatched_pair(self):
        """建＋派一套（dispatcher 角色 token 持有），返回 (tid, dispatcher_token, worker_token)。"""
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp)
        return tid, disp, self.svc.dispatch(tid, disp)


def cancel_msgs(store, sid):
    return [m for m in store.read_messages()
            if m["kind"] == "cancel" and m["to"] == sid]


class TestFuses(TestCoreTask):
    def test_chain_depth_at_limit_rejected(self):  # Req: F3
        self.seed("t5", state="todo", depth=5, disp="main")
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch("t5", self.root)
        self.assertIn("chain_depth", str(cm.exception))
        self.assertEqual(self.state.get("t5")["state"], "todo")  # 拒绝零副作用

    def test_quota_rejected_then_cancel_releases(self):  # Req: F3
        for i in range(8):
            self.seed(f"b{i}", worker=f"s-w{i}", env=f"/envs/b{i}")
        tid = self.dispatchable()
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(tid, self.root)
        self.assertIn("quota", str(cm.exception))
        self.svc.cancel("b0", self.root)  # 配额按活动任务计数：cancelled 即释放
        self.svc.dispatch(tid, self.root)
        self.assertEqual(self.state.get(tid)["state"], "dispatched")

    def test_can_spawn_rejected_cleans_env_no_side_effects(self):  # Req: F3
        tid = self.dispatchable()
        self.oc.fail_create = True  # oc 名册拒 spawn（can_spawn）
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(tid, self.root)
        self.assertIn("can_spawn", str(cm.exception))
        self.assertEqual(self.state.get(tid)["state"], "todo")
        self.assertEqual(self.gitx.removed, [f"/envs/{tid}"])  # 半途环境即回收
        self.assertEqual(self.oc.sessions, [])  # 无会话无凭据

    def test_b7_after_not_terminal_rejected_then_passes(self):  # Req: F3
        a = self.dispatchable("A")
        b = self.dispatchable("B", after=a)
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(b, self.root)
        self.assertIn("after", str(cm.exception))
        self.svc.cancel(a, self.root)  # cancelled ∈ B7 终态
        self.svc.dispatch(b, self.root)
        self.assertEqual(self.state.get(b)["state"], "dispatched")


class TestCancel(TestCoreTask):
    def test_two_level_cascade_with_notifications(self):  # Req: F11
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", env="/envs/t2")
        self.seed("t3", disp="s-w2", worker="s-w3", env="/envs/t3")
        toks = {w: self.auth.issue(f"s-{w}", "worker") for w in ("w1", "w2", "w3")}
        n = self.svc.cancel("t1", self.root)
        self.assertEqual(n, 3)  # 两级级联
        for tid in ("t1", "t2", "t3"):
            self.assertEqual(self.state.get(tid)["state"], "cancelled")
        for w in ("w1", "w2", "w3"):  # 各级 worker 各收一条终止通知
            self.assertEqual(len(cancel_msgs(self.store, f"s-{w}")), 1)
        for tok in toks.values():
            self.assertIsNone(self.auth.identify(tok))  # 凭据作废
        self.assertEqual(sorted(self.gitx.removed),
                         ["/envs/t1", "/envs/t2", "/envs/t3"])  # 环境即回收
        self.assertTrue(any(e["event"] == "cancelled" for e in self.events()))

    def test_cancel_spares_merged_descendant(self):  # Req: F11
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", state="merged")
        tok2 = self.auth.issue("s-w2", "worker")
        self.svc.cancel("t1", self.root)
        self.assertEqual(self.state.get("t2")["state"], "merged")  # 中止不逆转历史
        self.assertIsNotNone(self.auth.identify(tok2))
        self.assertEqual(cancel_msgs(self.store, "s-w2"), [])
        self.assertEqual(self.gitx.removed, ["/envs/t1"])

    def test_empty_chain_idempotent_with_audit(self):  # Req: F11
        tid = self.dispatchable()
        self.svc.cancel(tid, self.root)  # todo 无链：置 cancelled
        self.assertEqual(self.state.get(tid)["state"], "cancelled")
        n = len(self.events())
        self.assertEqual(self.svc.cancel(tid, self.root), 0)  # 幂等
        self.assertEqual(self.state.get(tid)["state"], "cancelled")
        self.assertEqual(len(self.events()), n + 1)  # 幂等仍留审计行

    def test_worker_token_cancel_rejected_and_audited(self):  # Req: F11
        tid, _, worker = self.dispatched_pair()
        with self.assertRaises(Rejected):
            self.svc.cancel(tid, worker)
        self.assertEqual(self.state.get(tid)["state"], "dispatched")
        self.assertTrue(any(e["event"] == "auth_denied" and e.get("task") == tid
                            for e in self.events()))

    def test_done_task_pends_and_children_still_cascade(self):  # Req: F11
        self.seed("t1", state="done", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", env="/envs/t2")
        n = self.svc.cancel("t1", self.root)
        self.assertEqual(n, 1)  # done 只排队，下级照常级联
        self.assertTrue(self.state.get("t1").get("pending_cancel"))  # T8 消费
        self.assertEqual(self.state.get("t2")["state"], "cancelled")


class TestVerify(TestCoreTask):
    def test_worker_update_verify_rejected_and_audited(self):  # Req: F2
        tid, _, worker = self.dispatched_pair()
        with self.assertRaises(Rejected):
            self.svc.update_verify(tid, worker, "pytest -q")
        self.assertTrue(any(e["event"] == "auth_denied" and e.get("task") == tid
                            for e in self.events()))
        d2 = self.auth.issue("disp2", "dispatcher")
        with self.assertRaises(Rejected):  # R18：非该任务派发方亦拒
            self.svc.update_verify(tid, d2, "pytest -q")

    def test_empty_verify_cmd_rejected(self):  # Req: F2
        tid, disp, _ = self.dispatched_pair()
        with self.assertRaises(Rejected) as cm:
            self.svc.update_verify(tid, disp, "   ")
        self.assertIn("空", str(cm.exception))
        self.assertTrue(any(e["event"] == "dispatch_rejected"
                            and e.get("task") == tid for e in self.events()))
        self.assertEqual(self.state.get(tid)["verify"]["state"], "pending")  # 未被登记

    def test_happy_path_post_then_register_locks(self):  # Req: F2
        tid, disp, worker = self.dispatched_pair()
        wsid, _, _ = self.oc.sessions[-1]
        self.router.post(wsid, ("up", tid), "verify",
                         "verify cmd: pytest tests/ -x")  # worker 勘察回填
        self.svc.update_verify(tid, disp, "pytest tests/ -x")
        v = self.state.get(tid)["verify"]
        self.assertEqual(v["state"], "registered")
        self.assertEqual(v["registered_by"], "disp1")
        self.assertGreater(v["registered_at"], 0)
        self.assertEqual(v["cmd"], "pytest tests/ -x")
        with self.assertRaises(Rejected):  # 登记即锁定：worker 改必拒
            self.svc.update_verify(tid, worker, "rm -rf /")


class TestCreateAndRedispatch(TestCoreTask):
    def test_create_writes_task_page_and_dispatch_rerenders(self):  # Req: F2
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp)
        page = self.tmp / "wiki" / "tasks" / f"{tid}.md"
        self.assertTrue(page.exists())  # create 产出任务页（create→page 接线）
        self.svc.dispatch(tid, disp)
        text = page.read_text(encoding="utf-8")
        self.assertIn("state: dispatched", text)  # 重渲反映当前态
        self.assertIn("disp1", text)  # 归属（dispatcher_sid）渲染

    def test_create_seeds_queryable_row(self):  # Req: F2
        tid = self.dispatchable("my goal", after=None)
        t = self.state.snapshot()["tasks"][tid]
        self.assertEqual(t["state"], "todo")
        self.assertEqual(t["dispatcher_sid"], "main")  # 归属＝创建者会话
        self.assertEqual(t["verify"]["state"], "pending")
        self.assertEqual(t["goal"], "my goal")
        self.assertEqual(t["chain_depth"], 1)

    def test_redispatch_revokes_old_token(self):  # Req: F3
        tid, disp, old = self.dispatched_pair()
        for _ in range(3):
            self.state.fail_streak_bump(tid)  # 连败→blocked（重派唯一合法来态）
        new = self.svc.dispatch(tid, disp)
        self.assertIsNone(self.auth.identify(old))  # 旧牌即废
        self.assertEqual(self.auth.identify(new)[1], "worker")
        self.assertEqual(self.state.get(tid)["state"], "dispatched")
        self.assertEqual(len(self.gitx.built), 1)  # 一任务一环境：重派不重建
        self.assertEqual(self.gitx.removed, [])


if __name__ == "__main__":
    unittest.main()
