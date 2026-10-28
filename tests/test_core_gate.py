"""T8 core/gate 契约：done 前置五拒/verify 门/自动合并/回滚/升级调用点（R3/R9/R11/R12/F13）。

真 Store/State/Router＋假 oc/gitx/health/read_anchor/runner 全注入；
门体 run 同步调用（后台线程包装归 httpd 装配）。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import Gate
from agorad.core.msg import Router
from agorad.core.state import State

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
OTHER_SHA = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5"


class FakeOc:
    def __init__(self):
        self.injected = []

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class FakeGitx:
    def __init__(self, merge="ok", gate=False, crash=False):
        self.merge_result, self.gate, self.crash = merge, gate, crash
        self.merge_calls, self.rollbacks, self.removed = [], [], []
        self.head_calls, self.checkouts, self.gate_calls = [], [], []
        self.restore_ok = True
        self.merged, self.head_sha = False, "aaa111"

    def symref(self, repo_dir):
        return "main"

    def checkout(self, repo_dir, branch):
        self.checkouts.append((repo_dir, branch))
        return self.restore_ok

    def merge_ff(self, env_dir, target, branch=None):
        if self.crash:
            raise RuntimeError("git checkout issue/7: fatal: used by worktree")
        self.merge_calls.append((env_dir, target, branch))
        self.merged = self.merge_result == "ok"  # 拒并不落地（无 merge commit）
        return self.merge_result

    def rollback(self, target, before_sha):
        self.rollbacks.append((target, before_sha))

    def remove_env(self, env_dir):
        self.removed.append(env_dir)

    def touches_gate(self, env_dir, patterns, branch=None):
        self.gate_calls.append((env_dir, patterns, branch))
        return self.gate

    def head(self, repo_dir, branch=None):
        self.head_calls.append((repo_dir, branch))
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
        self.env = self.tmp / "envs" / "t1"
        (self.env / ".review-loop").mkdir(parents=True)  # 评审环在位：绿→照常合并
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.health = FakeHealth()
        self.anchor = {"head": SHA, "clean": True}
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def mk_gate(self, merge="ok", gate=False, crash=False):
        self.gx = FakeGitx(merge=merge, gate=gate, crash=crash)
        # 锚随 .review-loop/ 在位（镜像生产 read_anchor：缺目录即 None）
        anchor = lambda env: (self.anchor
                              if (self.env / ".review-loop").is_dir() else None)
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         anchor, self.health, self.auth,
                         self.store, runner=self.runner)

    def seed(self, tid="t1", state="running", streak=0, tb=None):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": str(self.env), "after": None,
                             "target_branch": tb,
                             "verify": {"cmd": "pytest -q", "state": "registered",
                                        "registered_by": "disp1", "registered_at": 1.0},
                             "state": state, "dispatcher_sid": "disp1",
                             "worker_sid": "s-w1", "chain_depth": 1,
                             "fail_streak": streak, "created": 0.0, "updated": 0.0})
        return tid

    def in_gate(self, streak=0):
        """播种 running→受理 done（进入门临界段）。"""
        self.seed(streak=streak)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")

    def worker_msgs(self):
        return [m for m in self.store.read_messages() if m["to"] == "s-w1"]


class TestAcceptDone(TestCoreGate):
    def test_reject_verify_unregistered_with_guidance(self):  # Req: F7
        self.seed()
        self.state.set_fields("t1", verify={"cmd": "", "state": "pending",
                                            "registered_by": None,
                                            "registered_at": None})
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:verify-unregistered", r)
        self.assertIn("task_update", r)  # 动作指引：找派发方登记
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用

    def test_reject_body_format_with_received_prefix(self):  # Req: F13
        self.seed()
        for bad in ("bfe2bfc", "结论已修复", f"commit={SHA}", SHA[:39]):
            r = self.gate.accept_done("t1", "s-w1", bad)
            self.assertIn("rejected:body-format", r)
            self.assertIn("40 位", r)  # 契约文案
            self.assertIn(repr(bad[:20]), r)  # 收到内容前缀
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用

    def test_accept_uppercase_sha_normalized_to_lowercase(self):  # Req: F13
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA.upper()), "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["commit"], SHA)  # 落库归一小写（锚比较同口径）
    def test_body_format_rejection_audited_done_rejected(self):  # Req: F13
        self.seed()
        self.gate.accept_done("t1", "s-w1", "bfe2bfc")
        evs = [e for e in self.store.read_events("t1")
               if e["event"] == "done_rejected"]
        self.assertEqual([e["note"] for e in evs], ["body-format"])

    def test_reject_anchor_missing_guides_review_loop(self):  # Req: F13
        self.seed()
        self.anchor = None  # 无锚或锚落后
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)
        self.assertIn("review-loop", r)  # 动作指引
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_anchor_head_mismatch_names_both_shas(self):  # Req: F13
        self.seed()
        self.anchor = {"head": OTHER_SHA, "clean": True}  # 锚指向别的 commit
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)
        self.assertIn(SHA, r)  # 报告 sha 与
        self.assertIn(OTHER_SHA, r)  # 分支 HEAD sha 双双可见
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_anchor_dirty_guides_fix(self):  # Req: F13
        self.seed()
        self.anchor = {"head": SHA, "clean": False}  # 评审环未绿
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)
        self.assertIn("未绿", r)
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_empty_verify_cmd(self):  # Req: F7
        self.seed()
        self.state.set_fields("t1", verify={"cmd": "  ", "state": "registered",
                                            "registered_by": "disp1",
                                            "registered_at": 1.0})  # 绕过登记直改快照
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:verify-empty", r)
        self.assertIn("task_update", r)  # 动作指引：登记非空命令
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_reject_non_running(self):  # Req: F7
        self.seed(state="dispatched")
        self.assertIn("rejected:state", self.gate.accept_done("t1", "s-w1", SHA))
        self.seed("t2", state="done")  # 二次 done 拒（防重入门）
        self.assertIn("rejected:state", self.gate.accept_done("t2", "s-w1", SHA))

    def test_accept_transitions_done_with_commit(self):  # Req: F7
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["commit"], SHA)  # 报告 commit 入行（merged 证据源）


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
        self.assertEqual(t["commit"], SHA)  # merged 行含 commit
        self.assertEqual(t["fail_streak"], 0)  # 绿清连败（R12）
        self.assertEqual(self.gx.merge_calls, [(str(self.env), "/repo", None)])
        self.assertEqual(self.gx.removed, [str(self.env)])  # 环境即回收
        self.assertEqual(self.runner.calls,  # env verify→目标仓集成 verify
                         [(str(self.env), "pytest -q"), ("/repo", "pytest -q")])
        self.assertEqual(self.health.calls, [])
        self.assertTrue(self.worker_msgs())  # worker 收 merged 回执
        self.assertFalse(t.get("in_gate"))

    def test_review_absent_keeps_done_no_merge(self):  # Req: F7
        """评审环缺失：verify 绿亦不自动合并——驻留 done＋审计＋回 worker。"""
        shutil.rmtree(self.env / ".review-loop")
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 不 merged／closed
        self.assertEqual(t["fail_streak"], 0)  # 非质量败：不计连败
        self.assertEqual(self.gx.merge_calls, [])
        self.assertEqual(self.gx.removed, [])  # 环境保留（补基础设施后重入）
        self.assertEqual(len(self.runner.calls), 1)  # 不跑集成 verify
        self.assertFalse(t.get("in_gate"))  # 门闩照常落
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("review_absent_no_merge", names)
        self.assertIn("review-loop", self.worker_msgs()[0]["body"])

    def test_review_loop_absent_accepts_done_then_stays_done(self):  # Req: gt-8c8fe8
        """评审环缺失降级：受理不再锚拒，done 放行；门体绿亦驻留 done 不合并。"""
        shutil.rmtree(self.env / ".review-loop")
        self.anchor = None  # 无锚：评审环缺失即不查锚
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留 done：人工审合并
        self.assertEqual(t["fail_streak"], 0)  # 非质量败：不计连败
        self.assertEqual(self.gx.merge_calls, [])  # 无自动合并
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("review_absent_no_merge", names)
        self.assertIn("review-loop", self.worker_msgs()[0]["body"])  # 补设施指引

    def test_merge_time_anchor_missing_degrades(self):  # Req: gt-3e7301
        """合并前锚重验：受理后锚不在/未绿（accept→run 窗口 TOCTOU）→驻留 done。"""
        self.in_gate()  # 正常受理：目录在＋锚绿
        self.anchor = None  # 合并时锚不在/未绿
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留 done：人工审合并
        self.assertEqual(t["fail_streak"], 0)  # 非质量败：不计连败
        self.assertEqual(self.gx.merge_calls, [])  # 零合并
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("review_absent_no_merge", names)

    def test_review_green_during_verify_merges(self):  # Req: gt-3e7301
        """降级受理后 verify 期间评审环跑绿：锚绿于 commit 即照常合并（正路径）。"""
        shutil.rmtree(self.env / ".review-loop")
        self.anchor = None  # 降级受理（评审环缺失）
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", "abc123"), "accepted")
        (self.env / ".review-loop").mkdir(parents=True)  # verify 期间 idle 评审环跑绿
        self.anchor = {"head": "abc123", "clean": True}
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # merged→closed
        self.assertEqual(self.gx.merge_calls, [(str(self.env), "/repo", None)])

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
        self.assertEqual(self.gx.removed, [str(self.env)])  # 同 T7 直路径：环境回收

    def test_pending_cancel_spares_merged(self):  # Req: F11
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(self.gx.merge_calls, [(str(self.env), "/repo", None)])  # 照常合并
        self.assertEqual(t["state"], "closed")  # merged 不逆转历史，走完置 closed
        self.assertNotEqual(t["state"], "cancelled")

    def test_cancel_during_gate_consumed_no_hang(self):  # Req: F11
        """F-3 封闭性：cancel 在门内到达，门走完 pending 必被消费（不悬挂）。"""
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)  # cancel×gate-tail 到达
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # merged 二择：closed
        self.assertNotIn("pending_cancel", t)  # 不许悬挂


class TestGateAudit(TestCoreGate):
    """F-2：门段迁移逐点落 events.jsonl（F8 完整链：受理→门→合并→尾）。"""

    def test_target_branch_restores_default_branch_position(self):  # Req: gt-132747
        self.seed(tb="issue/7")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.gx.checkouts, [("/repo", "main")])  # 门毕还原，防缺省任务漂移
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_restore_failure_audited(self):  # Req: gt-132747
        self.seed(tb="issue/7")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gx.restore_ok = False
        self.gate.run("t1")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("restore_failed", names)  # 漂移防线失效留痕

    def test_gate_files_touched_passes_target_branch(self):  # Req: gt-132747
        self.seed(tb="issue/7")
        self.gate.gate_files_touched("t1")
        self.assertEqual(self.gx.gate_calls[-1][2], "issue/7")  # diff 基点＝目标支

    def test_gate_crash_reopens_and_notifies(self):  # Req: gt-132747
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(crash=True)
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")  # 不静默卡 done
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("reopened", names)
        msgs = [m for m in self.store.read_messages() if m["to"] == "s-w1"]
        self.assertTrue(any("门内异常" in m.get("body", "") for m in msgs))

    def test_target_branch_merges_into_branch(self):  # Req: gt-132747
        self.seed(tb="issue/7")
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        self.assertEqual(self.gx.merge_calls, [(str(self.env), "/repo", "issue/7")])
        self.assertEqual(self.gx.head_calls, [("/repo", "issue/7")])  # 回滚点取目标支
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("merged", names)
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_target_branch_absent_keeps_default_target(self):  # Req: gt-132747
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.gx.merge_calls, [(str(self.env), "/repo", None)])
        self.assertEqual(self.gx.head_calls, [("/repo", None)])  # 缺省＝仓默认支
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_green_path_audits_done_merged_closed_with_commit(self):  # Req: F8
        self.in_gate()
        self.gate.run("t1")
        evs = self.store.read_events("t1")
        names = [e["event"] for e in evs]
        for name in ("done", "merged", "closed"):
            self.assertIn(name, names)
        merged = next(e for e in evs if e["event"] == "merged")
        self.assertEqual(merged["commit"], SHA)  # merged 带 commit 证据

    def test_fail_path_audits_verify_failed_and_blocked(self):  # Req: F8
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate(streak=2)
        self.gate.run("t1")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_failed", names)
        self.assertIn("blocked", names)  # 3 连败自动 blocked 亦审计

    def test_non_ff_reopen_audited(self):  # Req: F8
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.in_gate()
        self.gate.run("t1")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("done", names)
        self.assertIn("reopened", names)

    def test_gate_tail_cancel_audited(self):  # Req: F8
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_failed", names)
        self.assertIn("cancelled", names)


if __name__ == "__main__":
    unittest.main()
