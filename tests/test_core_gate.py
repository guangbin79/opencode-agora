"""T8 core/gate 契约：done 前置六拒/verify 门/隔离集成/崩溃恢复/升级调用点（R3/R9/R11/R12/F13）。

真 Store/State/Router＋假 oc/gitx/health/read_anchor/runner 全注入；
门体 run 同步调用（后台线程包装归 httpd 装配）。
"""
import os
import shlex
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import Gate, _subprocess_runner
from agorad.core.msg import Router
from agorad.core.state import State

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
OTHER_SHA = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5"
R_SHA = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1"  # F32 重放后提交（R）
R2_SHA = "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1b2"  # 崩溃重排再 rebase 的新 sha（R′）


class FakeOc:
    def __init__(self):
        self.injected = []

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class FakeGitx:
    def __init__(self, merge="ok", gate=False, crash=False, fail_remove=False):
        self.merge_result, self.gate, self.crash = merge, gate, crash
        self.fail_remove = fail_remove
        self.merge_calls, self.removed = [], []
        self.head_calls, self.checkouts, self.gate_calls = [], [], []
        self.warn, self.check_calls = "", []  # diff_check 告警注入（gt-83185d R3）
        self.restore_ok = True
        self.merged, self.head_sha = False, "aaa111"
        self.head_shas = {}  # repo_dir→sha 覆盖：双头可区分（F29 拒因实值断言）
        self.integration_builds, self.integration_removed = [], []
        self.clean_calls, self.contains_calls = [], []
        self.contains_result, self.dirty = False, False
        self.contains_results = []  # 逐次覆盖队列：空则回退 contains_result
        self.rebase_result = "conflict"  # F32 缺省冲突：既有 non-ff 用例保持弹回口径
        self.rebase_calls = []
        self.ff_sha_calls, self.ff_sha_result = [], "ok"

    def rebase_onto(self, env_dir, integration):
        self.rebase_calls.append((env_dir, integration))
        return self.rebase_result

    def ff_sha(self, target, sha, branch=None):
        self.ff_sha_calls.append((target, sha, branch))
        return self.ff_sha_result

    def contains(self, repo_dir, commit, branch=None):
        self.contains_calls.append((repo_dir, commit, branch))
        return self.contains_results.pop(0) if self.contains_results else self.contains_result

    def ensure_clean(self, repo_dir):
        self.clean_calls.append(repo_dir)
        if self.dirty:
            raise RuntimeError("目标仓工作区不干净")

    def build_integration(self, target_dir, integration_dir, branch=None):
        self.integration_builds.append((target_dir, integration_dir, branch))
        return integration_dir

    def remove_integration(self, integration_dir, target_dir):
        self.integration_removed.append((integration_dir, target_dir))

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

    def remove_env(self, env_dir):
        if self.fail_remove:  # 回收失败注入：终态后异常须留痕不假账
            raise RuntimeError("rmtree: Directory not empty")
        self.removed.append(env_dir)

    def touches_gate(self, env_dir, patterns, branch=None):
        self.gate_calls.append((env_dir, patterns, branch))
        return self.gate

    def diff_check(self, env_dir, branch=None):
        self.check_calls.append((env_dir, branch))
        return self.warn

    def head(self, repo_dir, branch=None):
        self.head_calls.append((repo_dir, branch))
        return self.head_shas.get(repo_dir, self.head_sha)


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


class TestSubprocessRunner(unittest.TestCase):
    def test_timeout_kills_whole_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "late-write"
            cmd = f"(sleep 0.5; printf survived > {shlex.quote(str(marker))}) & wait"
            rc, out = _subprocess_runner(tmp, cmd, timeout=0.1)
            self.assertEqual(rc, 124)
            self.assertIn("verify 超时", out)
            self.assertIn("AGORA_VERIFY_TIMEOUT", out)
            time.sleep(0.6)  # 若只杀 shell 而漏杀子进程，此处会出现迟到写入
            self.assertFalse(marker.exists())

    def test_runner_strips_service_env_vars(self):  # Req: F32
        """F32 门环境消毒：verify 子进程 env 剥服务自身态三变量；其余照常继承；
        os.environ 本体不被污染（剥除落显式副本）。"""
        strip = ("AGORA_PORT", "AGORA_OC_URL", "AGORA_OC_PASSWORD", "AGORA_HOME")
        saved = {k: os.environ.get(k) for k in strip}
        os.environ["AGORA_PORT"] = "4096"
        os.environ["AGORA_OC_URL"] = "http://127.0.0.1:4096"
        os.environ["AGORA_OC_PASSWORD"] = "sekrit"
        os.environ["AGORA_HOME"] = "/fixture-home"
        snapshot = dict(os.environ)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rc, out = _subprocess_runner(
                    tmp, 'printf "[PORT=%s][URL=%s][PW=%s][HOME=%s]" '
                         '"${AGORA_PORT-}" "${AGORA_OC_URL-}" '
                         '"${AGORA_OC_PASSWORD-}" "${AGORA_HOME-}"')
            self.assertEqual(dict(os.environ), snapshot)  # os.environ 本体不被污染
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)
        # 三枚服务自身态被剥（空）；AGORA_HOME 明示不剥——夹具自设值照常继承
        self.assertEqual(out, "[PORT=][URL=][PW=][HOME=/fixture-home]")


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

    def mk_gate(self, merge="ok", gate=False, crash=False, fail_remove=False,
                on_blocked=None):
        self.gx = FakeGitx(merge=merge, gate=gate, crash=crash,
                           fail_remove=fail_remove)
        # 锚随 .review-loop/ 在位（镜像生产 read_anchor：缺目录即 None）
        anchor = lambda env: (self.anchor
                              if (self.env / ".review-loop").is_dir() else None)
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         anchor, self.health, self.auth,
                         self.store, runner=self.runner, on_blocked=on_blocked)

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

    def test_reject_anchor_without_head_key_no_crash(self):  # Req: F13
        self.seed()
        self.anchor = {"clean": True}  # 契约外形态（缺 head 键）
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)  # 照常拒收，不 KeyError
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


class TestNestedDoneGate(TestCoreGate):
    def add_child(self, tid="child", state="running", cancel_reason=None):
        self.seed(tid, state=state)
        self.state.set_fields(tid, dispatcher_sid="s-w1", worker_sid="s-child")
        if cancel_reason is not None:
            self.state.set_fields(tid, cancel_reason=cancel_reason)

    def test_open_child_rejects_parent_done(self):
        self.seed()
        self.add_child()
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:children", r)
        self.assertIn("child", r)
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_closed_and_intentionally_cancelled_children_accept_parent_done(self):
        self.seed()
        self.add_child("closed-child", state="closed")
        self.add_child("skipped-child", state="cancelled")
        self.add_child("confirmed-child", state="cancelled",
                       cancel_reason="dispatcher_confirmed:dependency_failed:other")
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")

    def test_dependency_failed_cancellation_still_rejects_parent_done(self):
        self.seed()
        self.add_child(state="cancelled", cancel_reason="dependency_failed:other")
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:children", r)
        self.assertEqual(self.state.get("t1")["state"], "running")


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

    def test_conflict_markers_fail_before_verify_no_merge(self):  # Req: gt-83185d R3
        self.in_gate()
        self.gx.warn = "docs/x.md:3: leftover conflict marker.\n<<<<<<< HEAD"
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)  # verify_failed 口径计连败
        self.assertEqual(self.runner.calls, [])  # 先于用户 verify（verify 未跑）
        self.assertEqual(self.gx.merge_calls, [])  # 不合并
        body = self.worker_msgs()[0]["body"]
        self.assertIn("<<<<<<< HEAD", body)  # 标记证据随告警尾部回 worker
        self.assertIn("post(done", body)

    def test_diff_check_clean_merges_as_before(self):  # Req: gt-83185d R3
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 干净零影响照常合并
        self.assertEqual(self.gx.check_calls, [(str(self.env), None)])  # env＋branch 口径

    def test_verify_green_merges_and_closes(self):  # Req: F7
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 尾部置 closed
        self.assertEqual(t["commit"], SHA)  # merged 行含 commit
        self.assertEqual(t["fail_streak"], 0)  # 绿清连败（R12）
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", None)])
        self.assertEqual(self.gx.removed, [str(self.env)])  # 环境即回收
        self.assertEqual(self.runner.calls,  # env verify→detached 集成环境 verify
                         [(str(self.env), "pytest -q"), (integration, "pytest -q")])
        self.assertEqual(self.gx.clean_calls, ["/repo", "/repo"])  # 开始前＋最终推进前复查
        self.assertEqual(self.gx.integration_removed, [(integration, "/repo")])
        self.assertIsNone(t.get("gate_env"))
        self.assertEqual(self.health.calls, [])
        self.assertTrue(self.worker_msgs())  # worker 收 merged 回执
        self.assertFalse(t.get("in_gate"))

    def test_reclaim_failure_keeps_merged_and_notifies(self):  # Req: gt-2e4829
        self.mk_gate(fail_remove=True)
        self.in_gate()
        self.gate.run("t1")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("reclaim_failed", names)  # 终态后异常留痕
        self.assertEqual(self.state.get("t1")["state"], "closed")  # merged 不逆转
        disp = [m for m in self.store.read_messages() if m["to"] == "disp1"]
        self.assertTrue(any("环境回收失败" in m["body"] for m in disp))  # 实时提示派发方
        self.assertTrue(all("环境已回收" not in m["body"]
                            for m in self.store.read_messages()))  # 禁假账文案

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
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        (self.env / ".review-loop").mkdir(parents=True)  # verify 期间 idle 评审环跑绿
        self.anchor = {"head": SHA, "clean": True}
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # merged→closed
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", None)])

    def test_rebase_conflict_reopens_without_merge_or_bump(self):  # Req: F7, F32
        """F32 后拒并 reopened 来源①：rebase 同步冲突——不计连败，环境保留。"""
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")  # 缺省 rebase_result=conflict：同步冲突弹回
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)  # 拒并不计连败
        self.assertEqual(self.gx.rebase_calls,
                         [(str(self.env), f"{self.env}__gate")])  # 先试门内自愈
        self.assertFalse(self.gx.merged)  # 无 merge commit 落地
        self.assertEqual(self.gx.removed, [])  # 环境保留
        self.assertEqual(len(self.runner.calls), 1)  # 不跑集成 verify
        self.assertTrue(self.worker_msgs())  # worker 收续修通知

    def test_rebase_conflict_message_self_evident(self):  # Req: F29, F32
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.head_shas[str(self.env)] = "bbb222"  # 任务支头≠主干头：实值可区分
        self.in_gate()
        self.gate.run("t1")
        body = self.worker_msgs()[0]["body"]
        self.assertIn("同步冲突", body)  # 冲突路径文案（干净同步已门内自愈）
        self.assertIn("main@aaa111", body)  # 拒因内嵌实际比较值（双端各验）
        self.assertIn("branch@bbb222", body)
        self.assertIn("post(done", body)

    def test_non_ff_narrow_race_guard_routes_to_success(self):  # Req: F7
        """拒并 reopened 来源②：试合并窗口内 commit 已被人工推进目标支：守卫补终态成功，绝不重开。"""
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.contains_results = [False, True]  # 门首探针 False、_non_ff 内 True
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 守卫路由 _succeed，未走 reopened
        self.assertEqual(self.gx.removed, [str(self.env)])  # 成功出口回收环境
        self.assertEqual(len(self.runner.calls), 1)  # 恰一次 env verify，未跑集成

    def test_non_ff_auto_rebase_merges_at_r(self):  # Req: F32
        """F32 干净同步门内自愈：merged @R、rebased 成对留痕、崩溃态①同路径覆盖
        （contains(E)=False → 重走门重新 rebase）。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"  # rebase_onto 成功；R 经 integration head 呈现
        self.gx.head_shas[f"{self.env}__gate"] = R_SHA
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], R_SHA)  # 落账 merged sha＝重放后 R
        self.assertEqual(t["rebased_from"], SHA)  # 留痕成对（E）
        self.assertEqual(t["fail_streak"], 0)  # 全绿清连败
        self.assertEqual(self.gx.rebase_calls, [(str(self.env), f"{self.env}__gate")])
        self.assertEqual(self.gx.ff_sha_calls, [("/repo", R_SHA, None)])  # R 级 ff 推进
        self.assertEqual(self.gx.merge_calls,
                         [(str(self.env), f"{self.env}__gate", None)])  # 仅试合并
        self.assertEqual(self.gx.removed, [str(self.env)])
        self.assertEqual(self.runner.calls,  # 集成 verify 跑在 R 上（重测不重审）
                         [(str(self.env), "pytest -q"), (f"{self.env}__gate", "pytest -q")])
        evs = self.store.read_events("t1")
        rebased = next(e for e in evs if e["event"] == "rebased")
        self.assertEqual(rebased["from"], SHA)
        self.assertEqual(rebased["to"], R_SHA)
        merged = next(e for e in evs if e["event"] == "merged")
        self.assertEqual(merged["commit"], R_SHA)
        self.assertEqual(merged["rebased_from"], SHA)  # _succeed 从 state 读（裁定②）

    def test_rebased_integration_fail_reopens_counts_streak(self):  # Req: F32
        """rebase 后集成 verify 败：reopened 计连败，重放内容未过闸目标分支未动。"""
        self.runner = FakeRunner((0, "env pass"), (1, "R broke"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.head_shas[f"{self.env}__gate"] = R_SHA
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)  # 集成败计连败（F7 口径）
        self.assertEqual(t["commit"], R_SHA)  # 重放 sha 已落库（门会再 rebase）
        self.assertEqual(t["rebased_from"], SHA)
        self.assertEqual(self.gx.ff_sha_calls, [])  # 目标分支未动
        self.assertFalse(self.gx.merged)
        body = self.worker_msgs()[0]["body"]
        self.assertIn("R broke", body)
        self.assertIn("rebase 后集成 verify 失败", body)
        self.assertIn("post(done", body)

    def test_rebased_advance_race_guard_routes_to_success(self):  # Req: F32
        """重放推进窗口内 R 被人工推进：contains(R) 守卫补终态，绝不重开。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.head_shas[f"{self.env}__gate"] = R_SHA
        self.gx.ff_sha_result = "non-ff"  # 推进窗口目标支被人工推进至含 R
        self.gx.contains_results = [False, True]  # 门首探针 False、_non_ff 内 True
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 守卫路由 _succeed
        self.assertEqual(self.gx.ff_sha_calls, [("/repo", R_SHA, None)])

    def test_crash_after_set_fields_reruns_rebase_lands_new_r(self):  # Req: F32
        """崩溃态②（落库后推进前崩，commit=R、锚=E）：contains(R)=False → 再 rebase
        落新 sha R′（裁定③：不要求 R′==R）；锚重验用 rebased_from 判过（裁定①）。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.contains_results = [False]  # 门首探针：上轮 R 未推进
        self.gx.rebase_result = "ok"
        self.gx.head_shas[f"{self.env}__gate"] = R2_SHA
        self.seed()
        self.state.transition("t1", "done", commit=R_SHA, rebased_from=SHA)  # 上轮崩溃残态
        self.gate.run("t1")  # 崩溃重排直入门体（不经 accept_done）
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], R2_SHA)  # 新 sha 落地
        self.assertEqual(t["rebased_from"], SHA)
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("review_absent_no_merge", names)  # 锚 E 判过，未死锁

    def test_crash_after_advance_only_repairs_terminal(self):  # Req: F32
        """崩溃态③（推进后 transition 前崩）：contains(R)=True → 补终态，
        merged 审计带 rebased_from（裁定②：从 state 读）。"""
        self.mk_gate()
        self.gx.contains_result = True
        self.seed()
        self.state.transition("t1", "done", commit=R_SHA, rebased_from=SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], R_SHA)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gx.merge_calls, [])
        merged = next(e for e in self.store.read_events("t1")
                      if e["event"] == "merged")
        self.assertEqual(merged["rebased_from"], SHA)

    def test_accept_done_clears_stale_rebased_from(self):  # Req: F32
        """裁定②：受理即清 rebased_from——陈旧留痕不得毒化锚判定或伪造下一轮。"""
        self.seed()
        self.state.set_fields("t1", rebased_from=OTHER_SHA)  # 上轮陈旧留痕
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertFalse(t.get("rebased_from"))  # 受理即清（唯一清点）

    def test_accept_after_rebased_reopen_takes_fresh_commit(self):  # Req: F32
        """rebased 重开续修后 done：锚＝新 env 头，受理按新 commit 判
        （陈旧 rebased_from 不参与受理锚比较）。"""
        self.seed()
        self.state.set_fields("t1", commit=R2_SHA,
                              rebased_from=OTHER_SHA)  # 上轮重放残值
        self.anchor = {"head": R_SHA, "clean": True}
        self.assertEqual(self.gate.accept_done("t1", "s-w1", R_SHA), "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["commit"], R_SHA)  # 新 done 覆写重放残值
        self.assertFalse(t.get("rebased_from"))

    def test_integration_fail_leaves_target_untouched(self):  # Req: F7
        self.runner = FakeRunner((0, "env pass"), (1, "I: broke"))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        integration = f"{self.env}__gate"
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 1)  # 集成败计连败
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None)])
        self.assertEqual(self.gx.removed, [])
        self.assertEqual(self.gx.integration_removed, [(integration, "/repo")])
        body = self.worker_msgs()[0]["body"]
        self.assertIn("I: broke", body)
        self.assertIn("目标分支未改动", body)
        self.assertIn("post(done", body)

    def test_crash_after_final_merge_only_repairs_terminal_state(self):
        """目标已含任务 commit＝上轮推进完成：不得重验、重并或把任务假重开。"""
        self.in_gate(streak=2)
        self.gx.contains_result = True
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["fail_streak"], 0)
        self.assertEqual(self.gx.contains_calls, [("/repo", SHA, None)])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gx.merge_calls, [])
        self.assertEqual(self.gx.clean_calls, [])
        self.assertEqual(self.gx.removed, [str(self.env)])

    def test_dirty_target_reopens_before_verify_or_merge(self):
        self.in_gate(streak=1)
        self.gx.dirty = True
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 1)  # 环境病不计质量连败
        self.assertEqual(self.gx.clean_calls, ["/repo"])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gx.merge_calls, [])
        self.assertEqual(self.gx.integration_builds, [])
        self.assertIn("工作区不干净", self.worker_msgs()[0]["body"])

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
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", None)])  # 照常合并
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


class TestDispatcherResults(TestCoreGate):
    def results(self):
        return [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"]

    def test_closed_result_returns_to_dispatcher(self):
        self.in_gate()
        self.gate.run("t1")
        (m,) = self.results()
        self.assertEqual(m["from"], "agorad")
        self.assertIn("已 closed", m["body"])
        self.assertIn(SHA, m["body"])

    def test_closed_splits_milestone_to_worker_and_result_to_dispatcher(self):
        """双通道契约（F30 跟进）：关单 milestone（人读）归 worker，result（机器读）归 dispatcher，各恰一条。"""
        self.in_gate()
        self.gate.run("t1")
        routed = sorted((m["to"], m["kind"]) for m in self.store.read_messages())
        self.assertEqual(routed, [("disp1", "result"), ("s-w1", "milestone")])
        milestone, = [m["body"] for m in self.store.read_messages()
                      if m["kind"] == "milestone"]
        self.assertIn("merged @", milestone)

    def test_blocked_result_returns_and_triggers_failure_callback(self):
        blocked = []
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate(on_blocked=blocked.append)
        self.in_gate(streak=2)
        self.gate.run("t1")
        self.assertEqual(blocked, ["t1"])
        (m,) = self.results()
        self.assertIn("已 blocked", m["body"])

    def test_gate_tail_cancel_result_returns_to_dispatcher(self):
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        (m,) = self.results()
        self.assertIn("已取消", m["body"])


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
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.integration_builds, [("/repo", integration, "issue/7")])
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", "issue/7")])
        self.assertEqual(self.gx.head_calls, [(integration, None)])  # 试合并前取集成环境头
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("merged", names)
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_target_branch_absent_keeps_default_target(self):  # Req: gt-132747
        self.in_gate()
        self.gate.run("t1")
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", None)])
        self.assertEqual(self.gx.head_calls, [(integration, None)])
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
