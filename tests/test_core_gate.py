"""T8 core/gate 契约：done 前置三拒/verify 门/自动合并/回滚/升级调用点（R3/R9/R11/R12）。

真 Store/State/Router＋假 oc/gitx/health/read_anchor/runner 全注入；
门体 run 同步调用（后台线程包装归 httpd 装配）。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.gate import Gate
from agorad.core.msg import Router
from agorad.core.state import State


class FakeOc:
    def __init__(self):
        self.injected = []

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class FakeGitx:
    def __init__(self, merge="ok", gate=False):
        self.merge_result, self.gate = merge, gate
        self.merge_calls, self.rollbacks, self.removed = [], [], []
        self.merged, self.head_sha = False, "aaa111"

    def merge_ff(self, env_dir, target):
        self.merge_calls.append((env_dir, target))
        self.merged = self.merge_result == "ok"  # 拒并不落地（无 merge commit）
        return self.merge_result

    def rollback(self, target, before_sha):
        self.rollbacks.append((target, before_sha))

    def remove_env(self, env_dir):
        self.removed.append(env_dir)

    def touches_gate(self, env_dir, patterns):
        return self.gate

    def head(self, repo_dir):
        return self.head_sha


class FakeHealth:
    def __init__(self):
        self.calls = []

    def escalate(self, kind, tid):
        self.calls.append((kind, tid))


class FakeRunner:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cwd, cmd):
        self.calls.append((cwd, cmd))
        return self.results.pop(0)


class TestCoreGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.health = FakeHealth()
        self.anchor = {"head": "abc123", "clean": True}
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def mk_gate(self, merge="ok", gate=False):
        self.gx = FakeGitx(merge=merge, gate=gate)
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         lambda env: self.anchor, self.health, runner=self.runner)

    def seed(self, tid="t1", state="running", streak=0):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": "/envs/t1", "after": None,
                             "verify": {"cmd": "pytest -q", "state": "registered",
                                        "registered_by": "disp1", "registered_at": 1.0},
                             "state": state, "dispatcher_sid": "disp1",
                             "worker_sid": "s-w1", "chain_depth": 1,
                             "fail_streak": streak, "created": 0.0, "updated": 0.0})
        return tid

    def in_gate(self, streak=0):
        """播种 running→受理 done（进入门临界段）。"""
        self.seed(streak=streak)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", "abc123"), "accepted")

    def worker_msgs(self):
        return [m for m in self.store.read_messages() if m["to"] == "s-w1"]


class TestAcceptDone(TestCoreGate):
    def test_reject_verify_unregistered_with_guidance(self):  # Req: F7
        self.seed()
        self.state.set_fields("t1", verify={"cmd": "", "state": "pending",
                                            "registered_by": None,
                                            "registered_at": None})
        r = self.gate.accept_done("t1", "s-w1", "abc123")
        self.assertIn("rejected:verify-unregistered", r)
        self.assertIn("task_update", r)  # 动作指引：找派发方登记
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用

    def test_reject_anchor_missing_stale_or_dirty(self):  # Req: F7
        self.seed()
        for anchor in (None, {"head": "old", "clean": True},
                       {"head": "abc123", "clean": False}):
            self.anchor = anchor  # 缺失／落后于报告 commit／评审环未绿
            r = self.gate.accept_done("t1", "s-w1", "abc123")
            self.assertIn("rejected:anchor", r)
            self.assertIn("review-loop", r)  # 动作指引
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_empty_verify_cmd(self):  # Req: F7
        self.seed()
        self.state.set_fields("t1", verify={"cmd": "  ", "state": "registered",
                                            "registered_by": "disp1",
                                            "registered_at": 1.0})  # 绕过登记直改快照
        r = self.gate.accept_done("t1", "s-w1", "abc123")
        self.assertIn("rejected:verify-empty", r)
        self.assertIn("task_update", r)  # 动作指引：登记非空命令
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_non_running(self):  # Req: F7
        self.seed(state="dispatched")
        self.assertIn("rejected:state", self.gate.accept_done("t1", "s-w1", "abc123"))
        self.seed("t2", state="done")  # 二次 done 拒（防重入门）
        self.assertIn("rejected:state", self.gate.accept_done("t2", "s-w1", "abc123"))

    def test_accept_transitions_done_with_commit(self):  # Req: F7
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", "abc123"), "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["commit"], "abc123")  # 报告 commit 入行（merged 证据源）


class TestGateRun(TestCoreGate):
    def test_verify_fail_posts_tail_bumps_no_merge(self):  # Req: F7
        self.runner = FakeRunner((1, "y" * 2500))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)
        self.assertEqual(self.gx.merge_calls, [])  # 败不合并
        body = self.worker_msgs()[0]["body"]
        self.assertIn("y" * 2000, body)  # 输出尾部（截 2000）
        self.assertNotIn("y" * 2001, body)
        self.assertIn("post(done", body)  # 动作指引

    def test_verify_green_merges_and_closes(self):  # Req: F7
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 尾部置 closed
        self.assertEqual(t["commit"], "abc123")  # merged 行含 commit
        self.assertEqual(t["fail_streak"], 0)  # 绿清连败（R12）
        self.assertEqual(self.gx.merge_calls, [("/envs/t1", "/repo")])
        self.assertEqual(self.gx.removed, ["/envs/t1"])  # 环境即回收
        self.assertEqual(self.runner.calls,  # env verify→目标仓集成 verify
                         [("/envs/t1", "pytest -q"), ("/repo", "pytest -q")])
        self.assertEqual(self.health.calls, [])
        self.assertTrue(self.worker_msgs())  # worker 收 merged 回执
        self.assertFalse(t.get("in_gate"))

    def test_non_ff_reopens_without_merge_or_bump(self):  # Req: F7
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)  # 拒并不计连败
        self.assertFalse(self.gx.merged)  # 无 merge commit 落地
        self.assertEqual(self.gx.rollbacks, [])
        self.assertEqual(self.gx.removed, [])  # 环境保留
        self.assertEqual(len(self.runner.calls), 1)  # 不跑集成 verify
        self.assertTrue(self.worker_msgs())  # worker 收续修通知

    def test_integration_fail_rolls_back_target(self):  # Req: F7
        self.runner = FakeRunner((0, "env pass"), (1, "I: broke"))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 1)  # 集成败计连败
        self.assertEqual(self.gx.rollbacks,  # 目标分支回退到合并前 sha
                         [("/repo", "aaa111")])
        self.assertEqual(self.gx.removed, [])
        body = self.worker_msgs()[0]["body"]
        self.assertIn("I: broke", body)
        self.assertIn("post(done", body)

    def test_three_failures_block_and_escalate_quality(self):  # Req: F7
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate(streak=2)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "blocked")
        self.assertIn(("quality", "t1"), self.health.calls)

    def test_gatefiles_change_escalates_but_gate_proceeds(self):  # Req: F7
        self.mk_gate(gate=True)
        self.in_gate()
        self.gate.run("t1")
        self.assertIn(("gatefiles", "t1"), self.health.calls)
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 拍人不拦门

    def test_pending_cancel_fires_after_gate_fail(self):  # Req: F7
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)  # T7 门临界段排队
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "cancelled")  # 门走完生效
        self.assertEqual(self.gx.removed, ["/envs/t1"])  # 同 T7 直路径：环境回收

    def test_pending_cancel_spares_merged(self):  # Req: F11
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(self.gx.merge_calls, [("/envs/t1", "/repo")])  # 照常合并
        self.assertEqual(t["state"], "closed")  # merged 不逆转历史，走完置 closed
        self.assertNotEqual(t["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
