"""T8 core/gate 契约：done 前置六拒/verify 门/隔离集成/崩溃恢复/升级调用点（R3/R9/R11/R12/F13）。

真 Store/State/Router＋假 oc/gitx/health/read_anchor/runner 全注入；
门体 run 同步调用（后台线程包装归 httpd 装配）。
"""
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agorad.core.gate as gate_mod
from agorad.access.gitx import Gitx
from agorad.access.oc import OcUnavailable
from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import Gate, _cmd_dead, _subprocess_runner
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import TaskService

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
OTHER_SHA = "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5"
R_SHA = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1"  # F32 重放后提交（R）
R2_SHA = "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1b2"  # 崩溃重排再 rebase 的新 sha（R′）


class FakeOc:
    def __init__(self):
        self.injected = []
        self.msgs = []  # F57 用量采集脚本面（缺省空＝降级缺席不设 usage）

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True

    def messages(self, sid):
        return self.msgs


class FakeDeliveryLock:  # Req: F98
    def __init__(self):
        self.busy = False
        self.acquires, self.releases = 0, 0

    def acquire(self, blocking=False):
        self.acquires += 1
        return not self.busy

    def release(self):
        self.releases += 1


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
        self.exempt_calls = []  # ag-f5aa4d ensure_clean 豁免面逐调用记录
        self.clean_ignore_calls, self.clean_ignore_result = [], []
        self.roster_calls = []  # F35 apply_ignore_roster 调用（repo_dir, patterns）
        self.events = []  # F35 公共事件序：apply/ensure_clean 各推一格
        self.contains_result, self.dirty = False, False
        self.contains_raise = False  # F69 谓词病注入（fail-open 放行面）
        self.contains_results = []  # 逐次覆盖队列：空则回退 contains_result
        self.ancestor_calls, self.ancestor_result = [], False  # ag-1ae0f1 空集判据 (a)
        self.rebase_result = "conflict"  # F32 缺省冲突：既有 non-ff 用例保持弹回口径
        self.rebase_calls = []
        self.rebase_done = None  # ag-54bd98：重放翻页——integration 头在 rebase 前后取不同值
        self.rebase_new_head = None  # 重放后 R；None＝空集 no-op（头==onto）
        self.phantom_ff = False  # ag-54bd98 形态①注入：幂等 ff 零移动，交付从未落库
        self.ff_sha_calls, self.ff_sha_result = [], "ok"
        self.push_calls, self.push_result = [], "ok"  # Req: ag-0d2daa
        self.push_tail = ""  # push 失败输出尾（审计/告警外带）
        self.delete_calls, self.delete_result = [], "ok"  # Req: ag-0f60fd
        self.delete_tail = ""  # 删除失败输出尾
        self.behind_result = False  # ag-49312d strictly_behind 注入（push 前守卫）
        self.materialize_calls = []  # F: 检出后物化调用记录
        self.materialize_result = ""  # 缺省零依赖物＝空报告
        self.diff_stats_result = (0, 0, "")  # F65 quick 包络读数注入
        self.diff_stats_calls = []
        # Req: F98 managed origin-first 注入面
        self.has_origin_result = False
        self.fetch_calls, self.fetch_result = [], "d" * 40
        self.fetch_results, self.fetch_raise = [], False
        self.build_at_calls = []
        self.push_commit_calls, self.push_commit_results = [], []
        self.remote_contains_calls, self.remote_contains_result = [], False
        self.remote_contains_results = []
        self.sync_calls, self.sync_result = [], "pending"
        self.lock_calls, self.lock = [], FakeDeliveryLock()

    def rebase_onto(self, env_dir, integration):
        self.rebase_calls.append((env_dir, integration))
        if self.rebase_result != "conflict":
            self.rebase_done = integration  # 重放成功：integration 头翻页（时序）
        return self.rebase_result

    def ff_sha(self, target, sha, branch=None):
        self.ff_sha_calls.append((target, sha, branch))
        if branch is not None and self.ff_sha_result == "ok":
            self.contains_result = True  # ag-54bd98：R 级推进落地＝目标支含 R
        return self.ff_sha_result

    def contains(self, repo_dir, commit, branch=None, fetch_timeout=None):
        self.contains_calls.append((repo_dir, commit, branch))
        if self.contains_raise:
            raise RuntimeError("git merge-base --is-ancestor: fatal: bad object")
        if self.contains_results:
            hit = self.contains_results.pop(0)
            if hit:
                self.contains_result = True  # True 粘滞：人工推进后目标支持续含之
            return hit
        return self.contains_result

    def is_ancestor(self, repo_dir, base, commit):  # ag-1ae0f1 空集判据 (a)
        self.ancestor_calls.append((repo_dir, base, commit))
        return self.ancestor_result

    def ensure_clean(self, repo_dir, exempt=()):
        self.clean_calls.append(repo_dir)
        self.exempt_calls.append(list(exempt))
        self.events.append("clean")
        if self.dirty:
            raise RuntimeError("目标仓工作区不干净")

    def clean_ignore(self, repo_dir, env_value=""):  # Req: ag-f5aa4d
        self.clean_ignore_calls.append((repo_dir, env_value))
        return self.clean_ignore_result

    def apply_ignore_roster(self, repo_dir, patterns):  # Req: F35
        self.roster_calls.append((repo_dir, list(patterns)))
        self.events.append("apply")

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
        if branch is not None and self.merged and not self.phantom_ff:
            # ag-54bd98：最终推进落地（试合并 branch=None）＝目标支已含交付；
            # phantom_ff＝形态①注入——幂等 ff 零移动，交付从未落库
            self.contains_result = True
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
        if self.rebase_done == repo_dir:  # ag-54bd98：重放后 R（None＝空集 no-op==onto）
            return self.rebase_new_head or self.head_shas.get(repo_dir, self.head_sha)
        return self.head_shas.get(repo_dir, self.head_sha)

    def push(self, repo_dir, branch):  # Req: ag-0d2daa
        self.push_calls.append((repo_dir, branch))
        return self.push_result, self.push_tail

    def delete_branch(self, repo_dir, branch, target_branch):  # Req: ag-0f60fd
        self.delete_calls.append((repo_dir, branch, target_branch))
        return self.delete_result, self.delete_tail

    def strictly_behind(self, repo_dir, branch):  # Req: ag-49312d
        return self.behind_result

    def materialize_checkout(self, tree_dir, source_dir):
        self.materialize_calls.append((tree_dir, source_dir))
        return self.materialize_result

    def diff_stats(self, env_dir, branch=None):  # Req: F65
        self.diff_stats_calls.append((env_dir, branch))
        return self.diff_stats_result

    def has_origin(self, repo_dir):  # Req: F98
        return self.has_origin_result

    def fetch_origin_branch(self, repo_dir, branch, timeout=None):  # Req: F98
        self.fetch_calls.append((repo_dir, branch))
        if self.fetch_raise:
            raise RuntimeError(f"git fetch origin {branch}: network down")
        if self.fetch_results:
            r = self.fetch_results.pop(0)
            if isinstance(r, Exception):  # 按次注入网络病（锁内重读抛等）
                raise r
            return r
        return self.fetch_result

    def build_integration_at(self, target_dir, integration_dir, base_sha=None):  # Req: F98
        self.build_at_calls.append((target_dir, integration_dir, base_sha))
        self.rebase_done = None  # 新 detached 基建：integration 头翻页复位（重试轮新基）
        return integration_dir

    def push_commit(self, repo_dir, sha, branch):  # Req: F98
        self.push_commit_calls.append((repo_dir, sha, branch))
        if self.push_commit_results:
            return self.push_commit_results.pop(0)
        return "ok", ""

    def remote_contains(self, repo_dir, branch, commit):  # Req: F98
        self.remote_contains_calls.append((repo_dir, branch, commit))
        if self.remote_contains_results:
            return self.remote_contains_results.pop(0)
        return self.remote_contains_result

    def sync_local_target(self, repo_dir, branch, sha, expected_old):  # Req: F98
        self.sync_calls.append((repo_dir, branch, sha, expected_old))
        return self.sync_result

    def delivery_lock(self, repo_dir):  # Req: F96
        self.lock_calls.append(repo_dir)
        return self.lock


class FakeHealth:
    def __init__(self):
        self.calls = []
        self.raws = []

    def escalate(self, kind, tid, raw=None, streak=None):  # raw/streak：F80/退避面签名对齐
        self.calls.append((kind, tid))
        self.raws.append(raw)


class FakeRunner:
    def __init__(self, *results):
        self.results, self.calls = list(results), []
        self.budgets = []  # F51 生效预算观察：(timeout, ceiling) 逐调用

    def __call__(self, cwd, cmd, timeout=None, ceiling=None):
        self.calls.append((cwd, cmd))
        self.budgets.append((timeout, ceiling))
        return self.results.pop(0)


class TestSubprocessRunner(unittest.TestCase):
    def test_hard_ceiling_kills_whole_process_group(self):  # Req: F36
        """硬顶到点 SIGKILL 全组 rc=124：迟到写入不存在（F32 杀净语义不变）。"""
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "late-write"
            cmd = f"(sleep 0.5; printf survived > {shlex.quote(str(marker))}) & wait"
            rc, out = _subprocess_runner(tmp, cmd, timeout=0.1, ceiling=0.3)
            self.assertEqual(rc, 124)
            self.assertIn("verify 超硬顶", out)
            self.assertIn("AGORA_VERIFY_CEILING", out)
            time.sleep(0.6)  # 若只杀 shell 而漏杀子进程，此处会出现迟到写入
            self.assertFalse(marker.exists())

    def test_soft_timeout_notifies_without_kill(self):  # Req: F36
        """软顶到点 on_soft 恰一次、不杀：进程跑完 rc=0 全输出在手。"""
        with tempfile.TemporaryDirectory() as tmp:
            fired = []
            rc, out = _subprocess_runner(tmp, "sleep 0.3; printf done",
                                         timeout=0.1, ceiling=5,
                                         on_soft=lambda: fired.append(1))
            self.assertEqual(rc, 0)
            self.assertEqual(fired, [1])  # 恰一次
            self.assertIn("done", out)

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

    def test_runner_replaces_non_utf8_output(self):  # Req: ag-830b1b
        """非 UTF-8 输出（ag-fa17e4 实证 GBK 0xc8）：errors=replace 降级 U+FFFD
        不抛门——rc 真实、输出尾照常捕获（原 text=True 严格解码即门崩）。"""
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = _subprocess_runner(tmp, "printf '\\310\\316\\n'; exit 7")
        self.assertEqual(rc, 7)  # verify 判定回归真实退出码
        self.assertIn("\ufffd", out)  # 非法字节降级替换符（自带定位线索）


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
                on_blocked=None, roster_path=None,
                quick_max_files=3, quick_max_lines=30, clean_ignore_env=""):
        self.gx = FakeGitx(merge=merge, gate=gate, crash=crash,
                           fail_remove=fail_remove)
        # 锚随 .review-loop/ 在位（镜像生产 read_anchor：缺目录即 None）
        anchor = lambda env: (self.anchor
                              if (self.env / ".review-loop").is_dir() else None)
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         anchor, self.health, self.auth,
                         self.store, runner=self.runner, on_blocked=on_blocked,
                         roster_path=roster_path,
                         quick_max_files=quick_max_files,
                         quick_max_lines=quick_max_lines,
                         clean_ignore_env=clean_ignore_env)

    def seed(self, tid="t1", state="running", streak=0, tb=None,
             cmd="pytest -q", timeout=None, quick=False):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": str(self.env), "after": None,
                             "target_branch": tb,
                             "verify": {"cmd": cmd, "state": "registered",
                                        "registered_by": "disp1", "registered_at": 1.0,
                                        "timeout": timeout},
                             "quick": quick,
                             "state": state, "dispatcher_sid": "disp1",
                             "worker_sid": "s-w1", "chain_depth": 1,
                             "fail_streak": streak, "created": 0.0, "updated": 0.0})
        return tid

    def in_gate(self, streak=0, timeout=None, quick=False):
        """播种 running→受理 done（进入门临界段）。"""
        self.seed(streak=streak, timeout=timeout, quick=quick)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")

    def worker_msgs(self):
        return [m for m in self.store.read_messages() if m["to"] == "s-w1"]


class TestAcceptDone(TestCoreGate):
    def test_reject_zero_commit_done_with_noop_guidance(self):  # Req: F69
        """vacuous-done 拒收：env HEAD 已被目标支基线包含（零新增提交）→
        按锚拒收同款机制弹回 worker，拒因指 no-op question 申报出口，状态零副作用。"""
        self.seed(tb="main")
        self.gx.contains_result = True  # 目标基线已含 HEAD＝零新增
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:vacuous-done", r)
        self.assertIn("零提交 done 拒收", r)
        self.assertIn("no-op", r)
        self.assertIn("post(kind=question", r)  # Req: F80
        self.assertNotIn("post(kind=note)", r)  # Req: F80
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用
        self.assertEqual(self.gx.contains_calls, [("/repo", SHA, "main")])

    def test_accept_done_with_new_commit_passes_zero_commit_gate(self):  # Req: F69
        """≥1 新增提交的 worker 不受影响：基线不含 HEAD 照常受理，判拒谓词已在受理点执行。"""
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.gx.contains_calls, [("/repo", SHA, None)])

    def test_accept_delivery_landed_on_target_branch(self):  # Req: ag-1ae0f1
        """空集放行判据 (a)：交付提交挂目标支自身（hotfix 直推落库）＝空集但
        env HEAD 相对起点 base 有前进——有效交付受理，不再 vacuous 误拒。"""
        self.seed(tb="main")
        self.state.set_fields("t1", env_base=OTHER_SHA)
        self.gx.contains_result = True  # 目标基线已含 HEAD＝空集
        self.gx.ancestor_result = True  # base 为 HEAD 祖先＝env 有前进
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.gx.ancestor_calls, [("/repo", OTHER_SHA, SHA)])
        self.assertEqual(self.state.get("t1")["state"], "done")

    def test_reject_true_zero_commit_with_base_recorded(self):  # Req: ag-1ae0f1
        """真零提交仍拒：env HEAD==起点 base（worker 未动）＝判据 (a)(b) 皆不
        成立，vacuous-done 照弹回（F69 护栏不破）。"""
        self.seed(tb="main")
        self.state.set_fields("t1", env_base=SHA)
        self.gx.contains_result = True
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:vacuous-done", r)
        self.assertIn("零提交 done 拒收", r)
        self.assertEqual(self.gx.ancestor_calls, [])  # ==base 前置短路，零谓词调用
        self.assertEqual(self.state.get("t1")["state"], "running")

    def test_accept_redone_when_prev_commit_landed(self):  # Req: ag-1ae0f1
        """空集放行判据 (b) 等价短路：reopen 后重报同一 commit 且已在目标支——
        既有 commit 字段留痕与 done body 相等即放行，零补探（与 run() 崩溃重排
        直通探针同语义）。"""
        self.seed(tb="main")
        self.state.set_fields("t1", commit=SHA)  # 前次受理留痕（reopen 不清）
        self.gx.contains_result = True
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.gx.contains_calls, [("/repo", SHA, "main")])
        self.assertEqual(self.gx.ancestor_calls, [])

    def test_accept_redone_prev_commit_probe(self):  # Req: ag-1ae0f1
        """空集放行判据 (b) 补探：重做 done 换新 sha、env_base 缺席（升级前
        在飞任务）——前次 commit 经 contains 在目标支可达即放行。"""
        self.seed(tb="main")
        self.state.set_fields("t1", commit=OTHER_SHA)
        self.gx.contains_results = [True, True]  # 主探针空集＋(b) 补探可达
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.gx.contains_calls,
                         [("/repo", SHA, "main"), ("/repo", OTHER_SHA, "main")])

    def test_probe_illness_fails_open_with_audit(self):  # Req: F69
        """判拒谓词病不拦受理：contains 抛错→带痕放行，门体 run() 异常面兜底 reopened。"""
        self.seed()
        self.gx.contains_raise = True
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        evs = [e for e in self.store.read_events("t1")
               if e["event"] == "zero_commit_probe_failed"]
        self.assertEqual(len(evs), 1)

    def test_anchor_missing_rejection_precedes_zero_commit(self):  # Req: F69
        """锚缺失拒收意图不变：锚先行，零提交判拒仅在锚==HEAD 后执行。"""
        (self.env / ".review-loop").rmdir()
        self.seed()
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("锚缺失/落后", r)
        self.assertEqual(self.gx.contains_calls, [])

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

    def test_done_acceptance_records_usage_sum(self):  # Req: F57
        self.oc.msgs = [
            {"type": "user"},
            {"type": "assistant", "tokens": {"input": 10, "output": 5, "reasoning": 2,
                                             "cache": {"read": 7, "write": 3}},
             "cost": 0.5},
            {"type": "assistant", "tokens": {"input": 1, "output": 2, "reasoning": 0,
                                             "cache": {"read": 1, "write": 0}},
             "cost": 0.25},
        ]
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.state.get("t1")["usage"],
                         {"input": 11, "output": 7, "reasoning": 2,
                          "cache_read": 8, "cache_write": 3,
                          "cost": 0.75, "msgs": 3})  # assistant 求和＋全消息计数

    def test_done_acceptance_survives_oc_down(self):  # Req: F57
        class DownOc(FakeOc):
            def messages(self, sid):
                raise OcUnavailable("down")
        self.oc = DownOc()
        self.mk_gate()
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertNotIn("usage", self.state.get("t1"))  # 经济性数据不阻塞验收门

    def test_done_usage_scalar_drift_degrades_to_zero(self):  # Req: F57
        self.oc.msgs = [{"type": "assistant", "tokens": {"input": "123", "output": 5},
                         "cost": "0.5"}]  # 值类型漂移（F56 同族形状漂移）
        self.seed()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.assertEqual(self.state.get("t1")["usage"],
                         {"input": 0, "output": 5, "reasoning": 0, "cache_read": 0,
                          "cache_write": 0, "cost": 0.0, "msgs": 1})  # 漂移字段按 0 计不炸

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

    def test_review_loop_absent_rejects_done_with_guidance(self):  # Req: ag-084a22
        """锚门无条件（A）：.review-loop 缺失＝锚缺失→done 拒收——原降级受理
        （gt-8c8fe8）放无锚任务入门内驻留 done，死锁态由此封死。"""
        shutil.rmtree(self.env / ".review-loop")
        self.anchor = None
        self.seed()
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)
        self.assertIn("合并前置失败", r)
        self.assertIn("review-loop", r)  # 动作指引：重建锚
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用
        evs = [e for e in self.store.read_events("t1")
               if e["event"] == "done_rejected"]
        self.assertEqual([e["note"] for e in evs], ["anchor"])

    def test_anchor_rejections_count_and_block_at_three(self):  # Req: ag-084a22
        """防抖（C）：锚拒收计入 fail_streak——3 连败 blocked＋health 升级＋拍派发方。"""
        blocked = []
        self.mk_gate(on_blocked=blocked.append)
        self.anchor = None
        self.seed()
        for _ in range(2):
            self.assertIn("rejected:anchor",
                          self.gate.accept_done("t1", "s-w1", SHA))
            self.assertEqual(self.state.get("t1")["state"], "running")  # 未到阈值只拒收
        self.assertIn("rejected:anchor", self.gate.accept_done("t1", "s-w1", SHA))
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(t["fail_streak"], 3)
        self.assertEqual(self.health.calls, [("quality", "t1")])
        self.assertEqual(blocked, ["t1"])
        self.assertTrue(any("已 blocked" in m["body"]
                            for m in self.store.read_messages()
                            if m["to"] == "disp1" and m["kind"] == "result"))
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertEqual(names.count("done_rejected"), 3)
        self.assertIn("blocked", names)

    def test_blocked_escalation_survives_notify_failure(self):  # Req: ag-084a22
        """拍人尾尽力而为（F54 同口径）：升级/通知面败不丢 worker 拒因与审计。"""
        class BoomRouter:
            def notify_dispatcher(self, tid, kind, body):
                raise OSError("disk full")

            def notify_transition(self, tid, to, detail=""):
                raise OSError("disk full")

            def post(self, *a, **k):
                return 1

        self.gate._router = BoomRouter()
        self.anchor = None
        self.seed(streak=2)
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:anchor", r)  # worker 照常拿到拒因
        self.assertEqual(self.state.get("t1")["state"], "blocked")
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("done_rejected", names)  # 拒收审计未丢
        self.assertIn("blocked", names)

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


class TestOutOfBandDone(TestCoreGate):
    """ag-af96bb② review-only（deliverable=out_of_band）done 通道：
    零提交＋交付物在案豁免——sha 形状/锚/F69 三检跳过，children 归属闸保留；
    verify 仍跑；未标记任务 F69 语义一字不动。"""

    def seed_oob(self, **kw):
        tid = self.seed(**kw)
        self.state.set_fields(tid, deliverable="out_of_band")
        return tid

    def in_gate_oob(self, streak=0):
        self.seed_oob(streak=streak)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", "docs/review.md 在案"),
                         "accepted")

    def test_accept_zero_commit_ref_skips_anchor_and_probe(self):  # ag-af96bb②
        self.seed_oob()
        self.anchor = None  # 锚缺失不拒——review-only 结构性无锚（禁改仓）
        r = self.gate.accept_done("t1", "s-w1", "docs/review.md: 结论在案")
        self.assertEqual(r, "accepted")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertNotIn("commit", t)  # 零提交：无 sha 入行
        self.assertEqual(self.gx.contains_calls, [])  # F69 谓词不探（无 sha 可探）
        ev = [e for e in self.store.read_events("t1") if e["event"] == "done"]
        self.assertEqual(ev[0]["note"], "docs/review.md: 结论在案")  # 交付物在案留痕

    def test_empty_ref_rejected(self):  # ag-af96bb②
        self.seed_oob()
        r = self.gate.accept_done("t1", "s-w1", "   ")
        self.assertIn("rejected:body-format", r)
        self.assertIn("交付物", r)  # 动作指引：引用随 body 在案
        self.assertEqual(self.state.get("t1")["state"], "running")  # 拒收零副作用

    def test_open_children_still_rejected(self):  # ag-af96bb② 归属闸不豁免
        self.seed_oob()
        self.seed("child")
        self.state.set_fields("child", dispatcher_sid="s-w1")
        self.assertIn("rejected:children",
                      self.gate.accept_done("t1", "s-w1", "ref"))

    def test_unmarked_zero_commit_still_rejected(self):  # ag-af96bb② F69 不破
        self.seed()  # 无 deliverable 标记
        self.gx.contains_result = True
        r = self.gate.accept_done("t1", "s-w1", SHA)
        self.assertIn("rejected:vacuous-done", r)

    def test_green_verifies_only_closes_without_merge_push(self):  # ag-af96bb②
        self.in_gate_oob()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 门尾 merged→closed 既有链结算
        self.assertEqual(t["fail_streak"], 0)  # 绿清连败（R12 同口径）
        self.assertNotIn("commit", t)  # 零 sha 落账
        self.assertEqual(self.runner.calls, [(str(self.env), "pytest -q")])  # 仅 env verify
        self.assertEqual(self.gx.merge_calls, [])  # 零合入
        self.assertEqual(self.gx.push_calls, [])  # 零 push
        self.assertEqual(self.gx.integration_builds, [])  # 无集成环境
        self.assertEqual(self.gx.removed, [str(self.env)])  # 环境照常回收
        self.assertTrue(self.worker_msgs())  # worker 收交付回执

    def test_green_removes_credential_file(self):  # ag-af96bb② F43 同序
        cred = self.tmp / "credentials" / "t1.token"
        cred.parent.mkdir(parents=True)
        cred.write_text('{"token": "tok"}')
        self.in_gate_oob()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertFalse(cred.exists())

    def test_verify_fail_reopens_no_merge(self):  # ag-af96bb② verify 仍跑
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate_oob()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)
        self.assertEqual(self.gx.merge_calls, [])
        self.assertIn("post(done", self.worker_msgs()[-1]["body"])


class TestGateRun(TestCoreGate):
    def test_gate_entry_milestone_both_sides(self):  # Req: F54
        self.in_gate()
        self.gate.run("t1")
        first = self.worker_msgs()[0]  # worker 侧启动拍
        self.assertIn("已入验证门", first["body"])
        disp = [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"]
        self.assertTrue(disp)
        self.assertIn("已入验证门", disp[0]["body"])  # dispatcher 侧 result 直投

    def test_gate_entry_beat_best_effort_no_chain_break(self):  # Req: F54
        """启动拍通知面异常不断裂门链（评审环 R0：残闩卡死面）——门照常走完。"""
        self.in_gate()

        def boom(tid, body):
            raise OSError("disk full")

        self.gate._post = boom
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertTrue(self.runner.calls)  # verify 未被启动拍跳过
        self.assertTrue([m for m in self.store.read_messages()  # R1：另一侧不受牵连
                         if m["to"] == "disp1" and m["kind"] == "result"])

    def test_verify_fail_posts_tail_bumps_no_merge(self):  # Req: F7
        self.runner = FakeRunner((1, "y" * 2500))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)
        self.assertEqual(self.gx.merge_calls, [])  # 败不合并
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("y" * 2000, body)  # 输出尾部（截 2000）
        self.assertNotIn("y" * 2001, body)
        self.assertIn("post(done", body)  # 动作指引

    def test_blocked_with_after_cycle_stops_chain_without_fake_reclaim(self):  # Req: F38
        """W-2 端到端：blocked 触发成环停链（真实 TaskService.stop_after_failed）——
        visited 兜底后不抛 RecursionError，后继取消落账，无 reclaim_failed 假审计。"""
        svc = TaskService(self.state, self.auth, self.gx, self.oc, self.store,
                          self.router, write_task_page)
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate(on_blocked=svc.stop_after_failed)
        self.in_gate(streak=2)  # t1 进门临界段（本测试的停链起点）
        self.seed("t2", state="todo")
        self.state.set_fields("t1", after="t2")  # 环：t1→t2→t1（create 不校验的既有形态）
        self.state.set_fields("t2", after="t1")
        self.gate.run("t1")  # verify 败→3 连败 blocked→on_blocked 停链（曾在此递归引爆）
        self.assertEqual(self.state.get("t1")["state"], "blocked")
        self.assertEqual(self.state.get("t2")["state"], "cancelled")
        self.assertEqual(self.state.get("t2")["cancel_reason"],
                         "dependency_failed:t1")
        events = self.store.read_events()
        self.assertEqual([e["task"] for e in events if e["event"] == "dependency_stopped"],
                         ["t2"])
        self.assertEqual([e for e in events if e["event"] == "reclaim_failed"], [])

    def test_conflict_markers_fail_before_verify_no_merge(self):  # Req: gt-83185d R3
        self.in_gate()
        self.gx.warn = "docs/x.md:3: leftover conflict marker.\n<<<<<<< HEAD"
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)  # verify_failed 口径计连败
        self.assertEqual(self.runner.calls, [])  # 先于用户 verify（verify 未跑）
        self.assertEqual(self.gx.merge_calls, [])  # 不合并
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("<<<<<<< HEAD", body)  # 标记证据随告警尾部回 worker
        self.assertIn("post(done", body)

    def test_diff_check_clean_merges_as_before(self):  # Req: gt-83185d R3
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 干净零影响照常合并
        self.assertEqual(self.gx.check_calls, [(str(self.env), "main")])  # env＋branch 口径（缺省支实名解析）

    def test_merge_recycle_removes_credential_file(self):  # Req: F43
        self.in_gate()
        cred = self.tmp / "credentials" / "t1.token"
        cred.parent.mkdir(parents=True)
        cred.write_text('{"token": "tok"}')
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertFalse(cred.exists())  # closed 环境回收处删牌文件

    def test_reclaim_failure_still_removes_credential_file(self):  # Req: F43
        self.in_gate()
        cred = self.tmp / "credentials" / "t1.token"
        cred.parent.mkdir(parents=True)
        cred.write_text('{"token": "tok"}')
        self.mk_gate(fail_remove=True)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # merged 不逆转
        self.assertFalse(cred.exists())  # env 回收败不得拦断删牌（merged 路径牌无其他作废点）

    def test_verify_green_merges_and_closes(self):  # Req: F7
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 尾部置 closed
        self.assertEqual(t["commit"], SHA)  # merged 行含 commit
        self.assertEqual(t["fail_streak"], 0)  # 绿清连败（R12）
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", "main")])
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

    def test_merge_time_anchor_failure_reopens(self):  # Req: ag-084a22
        """合并前锚重验失败（accept→run 窗口 TOCTOU）：自动 reopened 回 worker
        自愈（B）——无出边的驻留 done 态封死；锚败计连败（C）。"""
        self.in_gate()  # 正常受理：目录在＋锚绿
        self.anchor = None  # 合并时锚不在/未绿
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 1)
        self.assertEqual(self.gx.merge_calls, [])  # 零合并
        self.assertEqual(self.gx.removed, [])  # 环境保留：重建锚后 re-done
        self.assertEqual(len(self.runner.calls), 1)  # 不跑集成 verify
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("review_absent_no_merge", names)
        self.assertIn("reopened", names)
        self.assertIn("合并前置失败", self.worker_msgs()[-1]["body"])
        self.assertIn("重建锚", self.worker_msgs()[-1]["body"])

    def test_merge_time_anchor_streak_carries_to_blocked(self):  # Req: ag-084a22
        """C 通路 B 侧：门内锚败与前序连败累计——3 连败 blocked（与受理拒同尾）。"""
        self.in_gate(streak=2)  # 前序两连败
        self.anchor = None
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(t["fail_streak"], 3)
        self.assertEqual(self.health.calls, [("quality", "t1")])
        self.assertTrue(any("已 blocked" in m["body"]
                            for m in self.store.read_messages()
                            if m["to"] == "disp1" and m["kind"] == "result"))

    def disp_results(self):
        return [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"]

    def test_anchor_missing_reopen_notifies_dispatcher_realtime(self):  # Req: ag-084a22
        """reopened 同拍 dispatcher 收实时拒因摘要（ag-acae3b 通道）。"""
        self.in_gate()
        self.anchor = None  # 合并时锚缺失（accept→run 窗口 TOCTOU 同款注入）
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")
        body = self.disp_results()[-1]["body"]
        self.assertIn("t1", body)
        self.assertIn("reopened", body)
        self.assertIn("锚缺失/落后/未绿", body)

    def test_anchor_unclean_reopen_notifies_dispatcher_realtime(self):  # Req: ag-084a22
        self.in_gate()
        self.anchor = {"head": SHA, "clean": False}  # 合并时评审环未绿
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")
        self.assertIn("未绿", self.worker_msgs()[-1]["body"])  # 拒因原文归 worker
        self.assertIn("锚缺失/落后/未绿",
                      self.disp_results()[-1]["body"])  # 摘要单行归 dispatcher

    def test_reopen_survives_notify_failure(self):  # Req: F54
        """通知面尽力而为（F54 同口径）：注入 router 断链时迁移照落、连败照计，
        不落 reclaim_failed 误导留痕。"""
        class BoomRouter:
            def notify_dispatcher(self, tid, kind, body):
                raise OSError("disk full")

            def notify_transition(self, tid, to, detail=""):
                raise OSError("disk full")

            def post(self, *a, **k):
                return 1

        self.gate._router = BoomRouter()
        self.in_gate()
        self.anchor = None
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")  # 门结果不受通知败影响
        self.assertEqual(self.state.get("t1")["fail_streak"], 1)  # 防抖计数不丢
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("reopened", names)
        self.assertNotIn("reclaim_failed", names)

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
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("同步冲突", body)  # 冲突路径文案（干净同步已门内自愈）
        self.assertIn("main@aaa111", body)  # 拒因内嵌实际比较值（双端各验）
        self.assertIn("branch@bbb222", body)
        self.assertIn("post(done", body)

    def test_non_ff_narrow_race_guard_routes_to_success(self):  # Req: F7
        """拒并 reopened 来源②：试合并窗口内 commit 已被人工推进目标支：守卫补终态成功，绝不重开。"""
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.contains_results = [False, False, True]  # 受理探针 False、门首探针 False、_non_ff 内 True
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
        self.gx.rebase_result = "ok"  # rebase_onto 成功；R 经重放翻页呈现
        self.gx.rebase_new_head = R_SHA
        self.in_gate(streak=2)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], R_SHA)  # 落账 merged sha＝重放后 R
        self.assertEqual(t["rebased_from"], SHA)  # 留痕成对（E）
        self.assertEqual(t["fail_streak"], 0)  # 全绿清连败
        self.assertEqual(self.gx.rebase_calls, [(str(self.env), f"{self.env}__gate")])
        self.assertEqual(self.gx.ff_sha_calls, [("/repo", R_SHA, "main")])  # R 级 ff 推进（实时分支 ref）
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

    def test_empty_rebase_noop_phantom_blocked_with_audit(self):  # Req: ag-54bd98 G1
        """幻影形态②（ag-c3de0f/ag-e75d89）：交付落在自切分支、env boot 分支停在
        起点——空集重放 R==onto 且原锚不在目标支：audit empty_rebase_noop＋
        reopened，禁 set_fields(commit=R) 伪装推进。"""
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"  # 空集重放成功：integration 头不动（R==onto）
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)  # 弹回计连败（3 连败拍人）
        self.assertEqual(t["commit"], SHA)  # 落库保持受理值，未被 R 伪装推进
        self.assertIsNone(t.get("rebased_from"))
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("empty_rebase_noop", names)
        self.assertNotIn("rebased", names)
        self.assertNotIn("merged", names)
        self.assertEqual(self.gx.ff_sha_calls, [])  # 目标分支零移动
        self.assertEqual(self.gx.removed, [])  # 环境保留：修正后 re-done
        self.assertEqual(len(self.runner.calls), 1)  # 集成 verify 未跑
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("env 分支相对目标无新增提交", body)
        self.assertIn("boot 分支", body)
        self.assertIn("post(done", body)

    def test_ff_idempotent_phantom_blocked_before_merged(self):  # Req: ag-54bd98 G2
        """幻影形态①（ag-40a1fa）：env boot 分支==目标 tip——merge --ff-only 幂等
        ok 但零移动，落账 commit 从未进目标支：_succeed 前 contains 假 → audit
        phantom_merge_blocked＋reopened，merged 不落账。"""
        self.in_gate(streak=1)
        self.gx.phantom_ff = True  # 两道 ff 幂等假成功（目标零移动）
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("phantom_merge_blocked", names)
        self.assertNotIn("merged", names)
        self.assertEqual(self.gx.removed, [])  # 环境保留：修正后 re-done
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("env 分支相对目标无新增提交", body)
        self.assertIn("boot 分支", body)

    def test_empty_rebase_with_anchor_contained_still_succeeds(self):  # Req: ag-54bd98 G1
        """崩溃窗口不误伤：空集重放但原锚已 contained 于目标支（人工推进/崩溃
        补终态窗口）——保持现行为：rebased 成对留痕＋ff_sha 推进 R＋merged。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.contains_results = [False, False, True]  # 受理/门首 False，G1 探针 True
        self.in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], "aaa111")  # R==onto 照常落账（现行为）
        self.assertEqual(t["rebased_from"], SHA)
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("empty_rebase_noop", names)
        rebased = next(e for e in self.store.read_events("t1")
                       if e["event"] == "rebased")
        self.assertEqual((rebased["from"], rebased["to"]), (SHA, "aaa111"))
        self.assertEqual(self.gx.ff_sha_calls, [("/repo", "aaa111", "main")])
        self.assertIn("merged", names)

    def test_rebased_integration_fail_reopens_counts_streak(self):  # Req: F32
        """rebase 后集成 verify 败：reopened 计连败，重放内容未过闸目标分支未动。"""
        self.runner = FakeRunner((0, "env pass"), (1, "R broke"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.rebase_new_head = R_SHA
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 2)  # 集成败计连败（F7 口径）
        self.assertEqual(t["commit"], R_SHA)  # 重放 sha 已落库（门会再 rebase）
        self.assertEqual(t["rebased_from"], SHA)
        self.assertEqual(self.gx.ff_sha_calls, [])  # 目标分支未动
        self.assertFalse(self.gx.merged)
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("R broke", body)
        self.assertIn("rebase 后集成 verify 失败", body)
        self.assertIn("post(done", body)

    def test_rebased_advance_race_guard_routes_to_success(self):  # Req: F32
        """重放推进窗口内 R 被人工推进：contains(R) 守卫补终态，绝不重开。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.rebase_new_head = R_SHA
        self.gx.ff_sha_result = "non-ff"  # 推进窗口目标支被人工推进至含 R
        self.gx.contains_results = [False, False, True]  # 受理探针 False、门首探针 False、守卫 True
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 守卫路由 _succeed
        self.assertEqual(self.gx.ff_sha_calls, [("/repo", R_SHA, "main")])

    def test_crash_after_set_fields_reruns_rebase_lands_new_r(self):  # Req: F32
        """崩溃态②（落库后推进前崩，commit=R、锚=E）：contains(R)=False → 再 rebase
        落新 sha R′（裁定③：不要求 R′==R）；锚重验用 rebased_from 判过（裁定①）。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.contains_results = [False]  # 门首探针：上轮 R 未推进
        self.gx.rebase_result = "ok"
        self.gx.rebase_new_head = R2_SHA
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

    def test_crash_residual_r_ff_pass_through_aligns_ledger(self):  # Req: F32
        """崩溃态②残值（commit=R、rebased_from=E）遇直通（目标支回退使 E 可 ff）：
        合入的是 env 头 E——落账对齐 E 并清留痕，防 merged 账面与验证内容脱节。"""
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="ok")  # 目标支已回退：试合并直通，无 rebase
        self.gx.head_shas[str(self.env)] = SHA  # env 分支头＝E
        self.seed()
        self.state.transition("t1", "done", commit=R_SHA, rebased_from=SHA)  # 上轮残值
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], SHA)  # 落账＝实际合入的 env 头，非陈旧 R
        self.assertIsNone(t.get("rebased_from"))  # 残留留痕同步清
        merged = next(e for e in self.store.read_events("t1")
                      if e["event"] == "merged")
        self.assertEqual(merged["commit"], SHA)
        self.assertNotIn("rebased_from", merged)  # 无伪造留痕
        self.assertNotIn("rebased", [e["event"] for e in self.store.read_events("t1")])

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
        body = self.worker_msgs()[-1]["body"]
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
        self.assertEqual(self.gx.contains_calls, [("/repo", SHA, None),
                                                  ("/repo", SHA, None),
                                                  ("/repo", SHA, None)])  # 受理＋门首＋merged 前锚终检各一探（ag-54bd98 G2）
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
        self.assertIn("工作区不干净", self.worker_msgs()[-1]["body"])

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
                                               (str(self.env), "/repo", "main")])  # 照常合并
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


class TestGateSupply(TestCoreGate):
    """ag-0d2daa gate env 供给：集成树依赖物化＋merged 后服务侧 origin push。"""

    def test_gate_materializes_integration_before_verify(self):  # Req: ag-0d2daa
        self.gx.materialize_result = "submodule seeded libs/sub@0123456789ab"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.gx.materialize_calls,
                         [(f"{self.env}__gate", "/repo")])  # 推进后按任务 pin 物化
        ev = [e for e in self.store.read_events("t1")
              if e["event"] == "checkout_materialize"]
        self.assertEqual([e["note"] for e in ev],
                         ["submodule seeded libs/sub@0123456789ab"])
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 供给不碍门结果

    def test_gate_materialize_silent_when_zero_dependencies(self):  # Req: ag-0d2daa
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.gx.materialize_calls, [(f"{self.env}__gate", "/repo")])
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"] == "checkout_materialize"], [])  # 零依赖物无审计行

    def test_merged_pushes_default_branch(self):  # Req: ag-0d2daa
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.gx.push_calls, [("/repo", "main")])  # 服务侧 push 缺省支

    def test_merged_pushes_named_target_branch(self):  # Req: ag-0d2daa
        self.seed(tb="issue/7-fix")
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        self.assertEqual(self.gx.push_calls, [("/repo", "issue/7-fix")])

    def test_push_nonff_warns_dispatcher_keeps_merged(self):  # Req: ag-0d2daa
        self.gx.push_result = "non-ff"
        self.gx.push_tail = "! [rejected] main -> main (non-fast-forward)"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 终态不受 push 拒影响
        ev = [e for e in self.store.read_events("t1") if e["event"] == "push_rejected"]
        self.assertEqual([e["note"] for e in ev],
                         ["! [rejected] main -> main (non-fast-forward)"])  # 尾随审计
        notes = [m["body"] for m in self.store.read_messages()
                 if m["to"] == "disp1" and "non-ff" in m["body"]]
        self.assertEqual(len(notes), 1)  # 告警恰一次，不 force 文案＋输出尾在场
        self.assertIn("不 force", notes[0])
        self.assertIn("non-fast-forward", notes[0])

    def test_push_failed_notifies_keeps_merged(self):  # Req: ag-0d2daa
        self.gx.push_result = "failed"
        self.gx.push_tail = "fatal: Could not read from remote repository"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        ev = [e for e in self.store.read_events("t1") if e["event"] == "push_failed"]
        self.assertEqual([e["note"] for e in ev],
                         ["fatal: Could not read from remote repository"])
        notes = [m["body"] for m in self.store.read_messages()
                 if m["to"] == "disp1" and "push 失败" in m["body"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("Could not read from remote repository", notes[0])  # 因随告警

    def test_push_none_silent(self):  # Req: ag-0d2daa
        self.gx.push_result = "none"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"].startswith("push_")], [])
        self.assertEqual([m for m in self.store.read_messages()
                          if m["to"] == "disp1" and "push" in m["body"]], [])

    def test_push_exception_never_breaks_merged(self):  # Req: ag-0d2daa
        def boom(repo_dir, branch):
            raise RuntimeError("network unreachable")
        self.gx.push = boom
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 全兜底：异常不废门结果
        ev = [e for e in self.store.read_events("t1") if e["event"] == "push_failed"]
        self.assertEqual([e["note"] for e in ev], ["network unreachable"])

    def test_push_skipped_when_local_strictly_behind_origin(self):  # Req: ag-49312d
        """基线直通场景：本地支严格落后 origin（已含任务提交）→ 零 push 零假警报。"""
        self.gx.behind_result = True
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.gx.push_calls, [])  # 推陈旧支必 non-ff：不推
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"].startswith("push_")], [])  # 零审计噪音
        self.assertEqual([m for m in self.store.read_messages()
                          if m["to"] == "disp1" and "push" in m["body"]], [])

    # ---- ag-0f60fd merged 终局删远端任务支 ----

    def test_merged_deletes_remote_task_branch(self):  # Req: ag-0f60fd
        """merged 后删除调用：支名＝env_dir basename，目标支经任务 target_branch
        （缺省＝symref 实名解析），成功审计 task_branch_deleted。"""
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.gx.delete_calls, [("/repo", "t1", "main")])
        ev = [e for e in self.store.read_events("t1")
              if e["event"] == "task_branch_deleted"]
        self.assertEqual([e["branch"] for e in ev], ["t1"])
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_delete_named_target_branch_passed_through(self):  # Req: ag-0f60fd
        self.seed(tb="issue/7-fix")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.gx.delete_calls, [("/repo", "t1", "issue/7-fix")])

    def test_delete_guard_rejection_silent(self):  # Req: ag-0f60fd
        """skip（tip 未含于目标支/本地不可达）＝守卫拒删：静默零审计零告警。"""
        self.gx.delete_result = "skip"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"].startswith("task_branch_delete")], [])
        self.assertEqual([m for m in self.store.read_messages()
                          if m["to"] == "disp1" and "任务支删除失败" in m["body"]],
                         [])

    def test_delete_absent_silent(self):  # Req: ag-0f60fd
        """远端无此支＝静默幂等：零审计零告警，终态不受影响。"""
        self.gx.delete_result = "absent"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"].startswith("task_branch_delete")], [])
        self.assertEqual([m for m in self.store.read_messages()
                          if m["to"] == "disp1" and "任务支删除失败" in m["body"]],
                         [])

    def test_delete_skipped_when_push_not_landed(self):  # Req: ag-0f60fd
        """评审 R1：目标支 push 被拒/失败＝origin 未含已并提交——跳删任务支
        （留作唯一远端恢复源）＋audit 留痕；终态不受影响。"""
        for tid, st in (("t1", "non-ff"), ("t2", "failed")):
            self.seed(tid=tid)
            self.gx.push_result = st
            # 双任务各耗 2 份 runner 结果（env+集成），缺额即 IndexError 假 reopened
            self.runner.results = [(0, "pass")] * 4
            # contains_result 在 t1 门内被 ff_sha 粘滞置 True（ag-54bd98），复位防 t2 误 vacuous
            self.gx.contains_result = False
            self.assertEqual(self.gate.accept_done(tid, "s-w1", SHA), "accepted")
            self.gate.run(tid)
            self.assertEqual(self.state.get(tid)["state"], "closed")
            self.assertEqual(self.gx.delete_calls, [])  # 跳删：保恢复源
            ev = [e for e in self.store.read_events(tid)
                  if e["event"] == "task_branch_delete_skipped_push_failed"]
            self.assertEqual([e["note"] for e in ev], [st])

    def test_delete_failed_notifies_keeps_merged(self):  # Req: ag-0f60fd
        self.gx.delete_result = "failed"
        self.gx.delete_tail = "remote: error: refusing to delete protected branch"
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 终态不受影响
        ev = [e for e in self.store.read_events("t1")
              if e["event"] == "task_branch_delete_failed"]
        self.assertEqual([e["note"] for e in ev],
                         ["remote: error: refusing to delete protected branch"])
        notes = [m["body"] for m in self.store.read_messages()
                 if m["to"] == "disp1" and "任务支删除失败" in m["body"]]
        self.assertEqual(len(notes), 1)  # 告警恰一次
        self.assertIn("refs/heads/t1", notes[0])
        self.assertIn("protected branch", notes[0])  # 因随告警

    def test_delete_exception_never_breaks_merged(self):  # Req: ag-0f60fd
        def boom(repo_dir, branch, target_branch):
            raise RuntimeError("network unreachable")
        self.gx.delete_branch = boom
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 全兜底：异常不废门结果
        ev = [e for e in self.store.read_events("t1")
              if e["event"] == "task_branch_delete_failed"]
        self.assertEqual([e["note"] for e in ev], ["network unreachable"])

    def test_verify_fail_never_touches_remote_task_branch(self):  # Req: ag-0f60fd
        """仅 merged 路径调用：verify_failed（cancelled/blocked 同理不入 _succeed）
        不碰远端任务支。"""
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "verify_failed")
        self.assertEqual(self.gx.delete_calls, [])


class TestGateVerifyOverrun(TestCoreGate):
    """F36 rc=124 分类路由：不 transition、不弹 worker、不计连败，驻留 done 到派发方。"""

    def results(self):
        return [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"]

    def test_env_verify_overrun_routes_to_dispatcher(self):  # Req: F36
        self.runner = FakeRunner((124, "x" * 3000))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留：不 transition
        self.assertEqual(t["fail_streak"], 1)  # 不 bump
        m = self.results()[-1]  # F54 启动拍在前，取本路径末拍
        self.assertIn("超硬顶", m["body"])
        self.assertIn("x" * 100, m["body"])  # 输出尾在手（截断内）
        (wm,) = self.worker_msgs()  # 仅 F54 门启动拍；无「重新 post(done, commit)」
        self.assertIn("已入验证门", wm["body"])
        events = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_overrun", events)

    def test_integration_verify_overrun_routes_to_dispatcher(self):  # Req: F36
        self.runner = FakeRunner((0, "pass"), (124, "itail"))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "done")
        self.assertEqual(self.state.get("t1")["fail_streak"], 0)
        m = self.results()[-1]  # F54 启动拍在前，取本路径末拍
        self.assertIn("超硬顶", m["body"])
        self.assertIn("目标分支未改动", m["body"])
        self.assertIn("itail", m["body"])
        (wm,) = self.worker_msgs()  # 仅 F54 门启动拍；无「重新 post(done, commit)」
        self.assertIn("已入验证门", wm["body"])

    def test_overrun_parked_done_settles_queued_cancel(self):  # Req: F36
        """评审环 R1：驻留 done＋排队取消（软顶文案邀请的挂死 cancel）门尾合法结算——
        矩阵 done 无 cancelled 出口，gate_tail 直结防 InvalidTransition 吞取消。"""
        self.runner = FakeRunner((124, "hang"))
        self.mk_gate()
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "cancelled")
        self.assertEqual(self.gx.removed, [str(self.env)])  # 取消副作用照常：环境回收

    def test_default_runner_soft_notifies_once(self):  # Req: F36
        """未注入 runner：run() 按 tid 构造默认 runner，软顶提醒派发方恰一次。"""
        self.seed(cmd="sleep 0.3; echo ok")
        gate = Gate(self.state, self.gx, self.router, self.oc,
                    lambda env: self.anchor, self.health, self.auth, self.store,
                    runner=None, verify_timeout=0.05, verify_ceiling=5)
        self.assertEqual(gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.anchor = None  # 合并时锚缺失→reopened（ag-084a22 B）：仅 env verify 一跑
        gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")
        soft = [m for m in self.store.read_messages()
                if m["to"] == "disp1" and "仍在跑" in m["body"]]
        self.assertEqual(len(soft), 1)
        self.assertIn("t1", soft[0]["body"])
        self.assertIn("无需操作", soft[0]["body"])


class TestGateVerifyDead(TestCoreGate):
    """Req: F81 verify 命令死分类：命令死≠活差——死类驻留 done、不计连败、
    不弹回 worker 自愈，escalate 通知通道唤醒派发方；断言不中照旧 verify_failed。"""

    def test_cmd_dead_parks_done_escalates_dispatcher(self):  # Req: F81
        """命令不存在（rc=127）＝命令自身死：非活不达标——worker 无从自愈，
        F36 超顶同款驻留语义（不 transition、不 bump），escalate 拍派发方，
        worker 只留最小等待说明（失败尾部不下发）。"""
        self.runner = FakeRunner((127, "noise"))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留：不 transition
        self.assertEqual(t["fail_streak"], 1)  # 不 bump
        self.assertIn(("verify-dead", "t1"), self.health.calls)  # escalate 通道
        events = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_dead", events)
        self.assertNotIn("verify_failed", events)
        bodies = [m["body"] for m in self.worker_msgs()]
        self.assertFalse(any("重新 post(done" in b for b in bodies))  # 不弹回自愈
        self.assertFalse(any("noise" in b for b in bodies))  # 失败尾部不下发
        self.assertTrue(any("等待" in b for b in bodies))  # 最小等待说明

    def test_integration_cmd_dead_parks_done(self):  # Req: F81
        """集成阶段命令死（env 绿后 cwd 换 integration 仍 126/127）：同款分类
        路由，目标分支未改动。"""
        self.runner = FakeRunner((0, "pass"), (126, "itail"))
        self.mk_gate()
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "done")
        self.assertEqual(self.state.get("t1")["fail_streak"], 0)
        self.assertIn(("verify-dead", "t1"), self.health.calls)

    def test_assertion_miss_still_bounces_verify_failed(self):  # Req: F81
        """断言不中（grep 无匹配 exit 1）＝活不达标：照旧 verify_failed 弹回
        worker＋连败计数，零变化。"""
        self.runner = FakeRunner((1, "no match"))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 2)  # bump 照旧
        self.assertNotIn(("verify-dead", "t1"), self.health.calls)
        self.assertTrue(any("重新 post(done" in m["body"]
                            for m in self.worker_msgs()))  # 弹回自愈照旧

    def test_dead_rc_source_mechanical(self):  # Req: F81
        """判定源实证：命令不存在/引用路径不存在→127、在盘非 executable→126
        ——shell 自身退出码语义，不匹配输出文案（管道混合退出码歧义不在本口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            rc_missing, _ = _subprocess_runner(tmp, "no-such-cmd-ag-f79")
            rc_nofile, _ = _subprocess_runner(tmp, "./no-such-script.sh")
            script = Path(tmp) / "x.sh"
            script.write_text("true\n", encoding="utf-8")
            script.chmod(0o644)  # 在盘但非 executable
            rc_noexec, _ = _subprocess_runner(tmp, "./x.sh")
        self.assertEqual(rc_missing, 127)
        self.assertEqual(rc_nofile, 127)
        self.assertEqual(rc_noexec, 126)


class TestGateVerifyDeadRc2(TestCoreGate):
    """Req: F82 死分类补 rc=2＋ENOENT：ag-c49b21 原始形态——命令在、文件参数
    不存在（grep 缺文件 exit 2）照入死类；文案仅作 rc==2 加严条件不独立触发。"""

    def test_rc2_enoent_parks_done_escalates_dispatcher(self):  # Req: F82
        """rc=2＋ENOENT 文案（ag-c49b21 原始形态）＝命令自身死：驻留 done＋
        不 bump＋escalate verify-dead＋不弹回（F80 死类同款处置）。"""
        self.runner = FakeRunner(
            (2, "grep: no-such-fixture.txt: No such file or directory"))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留：不 transition
        self.assertEqual(t["fail_streak"], 1)  # 不 bump
        self.assertIn(("verify-dead", "t1"), self.health.calls)  # escalate 通道
        events = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_dead", events)
        self.assertNotIn("verify_failed", events)
        bodies = [m["body"] for m in self.worker_msgs()]
        self.assertFalse(any("重新 post(done" in b for b in bodies))  # 不弹回自愈
        self.assertFalse(any("no-such-fixture" in b for b in bodies))  # 尾部不下发

    def test_rc2_without_enoent_still_bounces_verify_failed(self):  # Req: F82
        """rc=2 但输出无该文案（如用法错误）＝活不达标防误报控制组：照旧弹回。"""
        self.runner = FakeRunner((2, "usage: grep [-abcDEF] pattern [file]"))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 2)  # bump 照旧
        self.assertNotIn(("verify-dead", "t1"), self.health.calls)
        self.assertTrue(any("重新 post(done" in m["body"]
                            for m in self.worker_msgs()))  # 弹回自愈照旧

    def test_cmd_dead_raw_carries_output_tail(self):  # Req: F82
        """命令死升级携带输出尾：health 头截 500，前置长噪声不得吃掉尾部诊断行。"""
        tail = "bash: ./missing-check.sh: No such file or directory"
        self.runner = FakeRunner((127, "lead-noise-" + "x" * 900 + "\n" + tail))
        self.mk_gate()
        self.in_gate(streak=1)
        self.gate.run("t1")
        raw = self.health.raws[-1]
        self.assertIn("exit=127", raw)
        self.assertIn(tail, raw)
        self.assertTrue(raw.endswith(tail))
        self.assertNotIn("lead-noise", raw)

    def test_cmd_dead_judgement_scope(self):  # Req: F82
        """判定矩阵：126/127 死（F80 回归）；rc=2 仅含 ENOENT 文案才死；文案不独立触发（rc=1 带同文案不入死类）。"""
        self.assertTrue(_cmd_dead(126, ""))
        self.assertTrue(_cmd_dead(127, ""))
        self.assertTrue(_cmd_dead(2, "grep: x: No such file or directory"))
        self.assertFalse(_cmd_dead(2, "usage: grep"))
        self.assertFalse(_cmd_dead(2, ""))
        self.assertFalse(_cmd_dead(1, "No such file or directory"))


class TestGateTaskBudget(TestCoreGate):
    """F51 任务级 verify 预算：gate 取值两级（任务 timeout→服务缺省），硬顶固定 4×生效值。"""

    def test_task_timeout_resolved_with_fixed_4x_ceiling(self):  # Req: F51
        self.mk_gate()
        self.in_gate(timeout=100)  # 服务缺省 300：任务预算优先
        self.gate.run("t1")
        self.assertEqual(self.runner.budgets, [(100, 400), (100, 400)])  # env＋集成同预算

    def test_no_task_timeout_falls_back_to_service_default(self):  # Req: F51
        self.seed()
        self.mk_gate()  # Gate 缺省 verify_timeout=300、ceiling=None→4×软顶
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.runner.budgets, [(300, 1200), (300, 1200)])
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 行为不变

    def test_service_ceiling_kept_without_task_timeout(self):  # Req: F36/F51
        """无任务预算：服务 AGORA_VERIFY_CEILING 旋钮语义原样（F36 不动）。"""
        self.seed()
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         lambda env: self.anchor, self.health, self.auth,
                         self.store, runner=self.runner,
                         verify_timeout=300, verify_ceiling=999)
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.runner.budgets[0], (300, 999))

    def test_overrun_message_contains_effective_budget(self):  # Req: F51
        self.runner = FakeRunner((124, "x" * 3000))
        self.mk_gate()
        self.in_gate(timeout=100)
        self.gate.run("t1")
        m = [m for m in self.store.read_messages()
             if m["to"] == "disp1" and m["kind"] == "result"][-1]  # F54 启动拍在前
        self.assertIn("生效预算 100s", m["body"])
        self.assertIn("硬顶 400s", m["body"])

    def test_integration_overrun_message_contains_effective_budget(self):  # Req: F51
        self.runner = FakeRunner((0, "pass"), (124, "itail"))
        self.mk_gate()
        self.in_gate(timeout=100)
        self.gate.run("t1")
        m = [m for m in self.store.read_messages()
             if m["to"] == "disp1" and m["kind"] == "result"][-1]  # F54 启动拍在前
        self.assertIn("生效预算 100s", m["body"])

    def test_soft_reminder_contains_effective_budget(self):  # Req: F51
        """默认 runner：软顶文案报任务级生效预算（非服务缺省）。"""
        self.seed(cmd="sleep 0.3; echo ok", timeout=0.1)
        gate = Gate(self.state, self.gx, self.router, self.oc,
                    lambda env: self.anchor, self.health, self.auth, self.store,
                    runner=None, verify_timeout=0.05, verify_ceiling=5)
        self.assertEqual(gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.anchor = None  # 合并时锚缺失→reopened（ag-084a22 B）：仅 env verify 一跑
        gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")
        soft = [m for m in self.store.read_messages()
                if m["to"] == "disp1" and "仍在跑" in m["body"]]
        self.assertEqual(len(soft), 1)
        self.assertIn("0.1s", soft[0]["body"])  # 任务级生效预算数值在案


class TestGateIgnoreRoster(TestCoreGate):
    """F35 门干净检查未跟踪名册：apply 先于首道干净检查；名册并入闸体检测面。"""

    def roster(self, text):
        p = self.tmp / "ignore.roster"
        p.write_text(text)
        return str(p)

    def test_roster_applied_before_clean_and_patterns_extended(self):  # Req: F35
        self.mk_gate(roster_path=self.roster("# 注释\n.req-gate/\nbook/\n"))
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 照常 merged
        self.assertEqual(self.gx.roster_calls,
                         [("/repo", [".req-gate/", "book/"])])  # 解析后名册（注释丢）
        self.assertEqual(self.gx.events,
                         ["apply", "clean", "clean"])  # apply 先于首道干净检查
        patterns = self.gx.gate_calls[-1][1]
        self.assertIn(".req-gate", patterns)  # 目录形展开「本体＋子树」两条
        self.assertIn(".req-gate/*", patterns)
        self.assertIn("book", patterns)
        self.assertIn("book/*", patterns)

    def test_roster_absent_zero_apply_merges_as_usual(self):  # Req: F35
        self.mk_gate(roster_path=str(self.tmp / "absent.roster"))
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.gx.roster_calls, [])  # 缺文件＝空名册＝零 apply

    def test_roster_decode_error_degrades_to_empty(self):  # Req: F35
        """F35 round-1 评审修复：名册非 UTF-8 解码败＝不可读＝空名册＝零 apply
        （不得穿到 run() 宽 except 致 reopened 循环）。"""
        p = self.tmp / "gbk.roster"
        p.write_bytes(b"# \xd6\xd0\xce\xc4\n.req-gate/\n")  # GBK 注释字节
        self.mk_gate(roster_path=str(p))
        self.in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.gx.roster_calls, [])  # 空名册＝现状全净


class TestGateCleanIgnore(TestCoreGate):
    """ag-f5aa4d 数据层豁免：声明面（仓根 .agora/clean-ignore ∪
    AGORA_CLEAN_IGNORE）在首道干净检查前解析，同一份豁免传入两处 ensure_clean。"""

    def test_file_and_env_union_reaches_both_clean_checks(self):  # Req: ag-f5aa4d
        target = self.tmp / "target"
        (target / ".agora").mkdir(parents=True)
        (target / ".agora" / "clean-ignore").write_text(
            "# 注释\n.agent-experiences/\n.dup/\n")
        self.mk_gate(clean_ignore_env=".dup/, envonly/")
        union = [".agent-experiences/", ".dup/", "envonly/"]  # 并集计算归 access 层测
        self.gx.clean_ignore_result = union
        self.seed()
        self.state.set_fields("t1", project_dir=str(target))
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 豁免不拦正常合并
        self.assertEqual(self.gx.clean_ignore_calls,
                         [(str(target), ".dup/, envonly/")])  # env 值透传解析
        self.assertEqual(self.gx.exempt_calls, [union, union])  # 首道＋终前复查同面

    def test_declaration_file_missing_env_still_applies(self):  # Req: ag-f5aa4d
        target = self.tmp / "target"
        target.mkdir()
        self.mk_gate(clean_ignore_env="data/")
        self.gx.clean_ignore_result = ["data/"]
        self.seed()
        self.state.set_fields("t1", project_dir=str(target))
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.gx.exempt_calls, [["data/"], ["data/"]])  # 缺文件＝仅 env


class TestDispatcherResults(TestCoreGate):
    def results(self):
        return [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"]

    def test_closed_result_returns_to_dispatcher(self):
        self.in_gate()
        self.gate.run("t1")
        closed = self.results()[-1]  # merged＋closed 两拍，closed 收官
        self.assertEqual(closed["from"], "agorad")
        self.assertIn("已 closed", closed["body"])
        self.assertIn(SHA, closed["body"])

    def test_merged_result_returns_to_dispatcher(self):  # Req: ag-acae3b / F60
        """merged 迁移实时直达 dispatcher：单行紧凑（id＋终态＋短 sha＋沉淀提示），
        先于 closed 收官拍；worker milestone 通道不变。"""
        self.in_gate()
        self.gate.run("t1")
        merged, closed = self.results()[-2:]  # F54 启动拍在前，merged/closed 收官
        self.assertEqual(merged["from"], "agorad")
        self.assertEqual(merged["kind"], "result")
        self.assertEqual(
            merged["body"],
            f"任务 t1 merged @{SHA[:7]}"
            "——沉淀: 聚本任务 note/task page 走 knowledge ingest")  # Req: F60
        self.assertLess(merged["id"], closed["id"])

    def test_closed_splits_milestone_to_worker_and_result_to_dispatcher(self):
        """双通道契约（F30 跟进＋ag-acae3b）：关单 milestone（人读）归 worker，
        result（机器读）归 dispatcher（merged＋closed 两拍），各就各位。"""
        self.in_gate()
        self.gate.run("t1")
        routed = sorted((m["to"], m["kind"]) for m in self.store.read_messages())
        self.assertEqual(routed, [("disp1", "result"), ("disp1", "result"),
                                  ("disp1", "result"),  # 首拍＝F54 门启动
                                  ("s-w1", "milestone"), ("s-w1", "milestone")])
        milestone = [m["body"] for m in self.store.read_messages()
                     if m["kind"] == "milestone"][-1]
        self.assertIn("merged @", milestone)

    def test_blocked_result_returns_and_triggers_failure_callback(self):
        blocked = []
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate(on_blocked=blocked.append)
        self.in_gate(streak=2)
        self.gate.run("t1")
        self.assertEqual(blocked, ["t1"])
        m = self.results()[-1]  # F54 启动拍在前，取本路径末拍
        self.assertIn("已 blocked", m["body"])

    def test_gate_tail_cancel_result_returns_to_dispatcher(self):
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.in_gate()
        self.state.set_fields("t1", pending_cancel=True)
        self.gate.run("t1")
        m = self.results()[-1]  # F54 启动拍在前，取本路径末拍
        self.assertIn("已取消", m["body"])


class TestReopenDispatcherNotice(TestCoreGate):
    """ag-acae3b：reopened 迁移向 dispatcher 推拒因摘要单行（kind=result，
    from=agorad）；拒因原文与输出尾仍只归 worker，摘要不外带长文。"""

    def notices(self):
        return [m for m in self.store.read_messages()
                if m["to"] == "disp1" and m["kind"] == "result"
                and "reopened" in m["body"]]

    def test_rebase_conflict_notice_summary(self):  # Req: ag-acae3b
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(merge="non-ff")  # 缺省 rebase_result=conflict：同步冲突弹回
        self.in_gate()
        self.gate.run("t1")
        (m,) = self.notices()
        self.assertEqual(m["from"], "agorad")
        self.assertIn("任务 t1 reopened 同步冲突", m["body"])
        self.assertNotIn("main@aaa111", m["body"])  # 分支位长文只给 worker
        self.assertEqual(len(m["body"].splitlines()), 1)  # 单行紧凑

    def test_non_ff_reject_notice_summary(self):  # Req: ag-acae3b
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.rebase_new_head = R_SHA
        self.gx.ff_sha_result = "non-ff"
        self.gx.contains_results = [False, False, False]  # 受理/门首/守卫均未含
        self.in_gate()
        self.gate.run("t1")
        (m,) = self.notices()
        self.assertIn("任务 t1 reopened non-ff 拒并", m["body"])

    def test_integration_fail_notice_summary(self):  # Req: ag-acae3b
        self.runner = FakeRunner((0, "env pass"), (1, "R broke"))
        self.mk_gate(merge="non-ff")
        self.gx.rebase_result = "ok"
        self.gx.rebase_new_head = R_SHA
        self.in_gate()
        self.gate.run("t1")
        (m,) = self.notices()
        self.assertIn("任务 t1 reopened rebase 后集成 verify 失败", m["body"])
        self.assertNotIn("R broke", m["body"])  # 输出尾只给 worker

    def test_gate_crash_notice_summary(self):  # Req: ag-acae3b
        self.runner = FakeRunner((0, "pass"))
        self.mk_gate(crash=True)
        self.in_gate()
        self.gate.run("t1")
        (m,) = self.notices()
        self.assertIn("任务 t1 reopened 门内异常", m["body"])


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

    def test_default_branch_resolved_to_live_ref(self):  # Req: ag-bf61e7
        """缺省支实名解析（ag-bf61e7）：target_branch 缺省时门入口解析实时 symref，
        基线/试合并/最终推进全走 refs/heads/<支>——检出 HEAD 不再是事实源。"""
        self.in_gate()
        self.gate.run("t1")
        integration = f"{self.env}__gate"
        self.assertEqual(self.gx.integration_builds, [("/repo", integration, "main")])
        self.assertEqual(self.gx.merge_calls, [(str(self.env), integration, None),
                                               (str(self.env), "/repo", "main")])
        self.assertEqual(self.gx.head_calls, [(integration, None)])
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_detached_default_checkout_refused_before_verify(self):  # Req: ag-bf61e7
        """detached 检出拒判（ag-bf61e7）：symref=HEAD 时无法定位缺省目标支 ref——
        HEAD 基线会以陈旧检出位误判 non-ff，detached 推进只挪 HEAD 不动分支 ref
        （静默丢合并）——verify 零消耗弹回，恢复分支检出后重跑。"""
        self.in_gate()
        self.gx.symref = lambda repo_dir: "HEAD"
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")
        self.assertEqual(self.runner.calls, [])  # 拒判先于门体：verify 零消耗
        self.assertEqual(self.gx.merge_calls, [])  # 无试合并/无推进——分支 ref 零风险
        self.assertTrue(any("detached" in m["body"] for m in self.worker_msgs()))

    def test_detached_kill_window_probe_precedes_refusal(self):  # Req: ag-bf61e7
        """拒判次序（终闸 B 册）：contains 补终态探针先于 detached 拒判——kill
        窗口×detached 组合下已合法合并的任务照常补终态，绝不假 reopened。"""
        self.in_gate()
        self.gx.symref = lambda repo_dir: "HEAD"
        self.gx.contains_result = True
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 补终态优先
        self.assertEqual(self.runner.calls, [])  # 零 verify 消耗
        self.assertEqual(self.gx.merge_calls, [])  # 零重复合并

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


class TestGateStaleBaseline(TestCoreGate):
    """门级端到端（真 Gitx，ag-49312d）：主检出本地支陈旧而任务提交已落 origin——
    contains 认出已落库提交直通终态，零 verify 零合并；旧全链本地基线会重验＋rebase。"""

    def _git(self, args, cwd=None, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r

    def test_stale_local_shortcut_merges_without_rerun(self):  # Req: ag-49312d
        proj = self.tmp / "proj"
        proj.mkdir()
        self._git(["init", "-b", "master"], cwd=proj)
        self._git(["config", "user.email", "t@t"], cwd=proj)
        self._git(["config", "user.name", "t"], cwd=proj)
        self._git(["commit", "--allow-empty", "-m", "init"], cwd=proj)
        origin = self.tmp / "origin.git"
        self._git(["init", "--bare", "-b", "master", str(origin)])
        self._git(["remote", "add", "origin", str(origin)], cwd=proj)
        self._git(["push", "-q", "origin", "master"], cwd=proj)
        gx = Gitx()
        env = gx.build_env(str(proj), "t1")
        self._git(["commit", "--allow-empty", "-m", "task"], cwd=env)
        task = self._git(["rev-parse", "HEAD"], cwd=env).stdout.strip()
        stale = self._git(["rev-parse", "master"], cwd=proj).stdout.strip()
        self._git(["push", "-q", "origin", f"{Path(env).name}:master"],
                  cwd=proj)  # 模拟网页合并落库：origin=init→task，本地 master 仍停在 init
        self.seed(tb="master")
        self.state.set_fields("t1", project_dir=str(proj), env_dir=env)
        self.anchor = {"head": task, "clean": True}
        self.gate = Gate(self.state, gx, self.router, self.oc,
                         lambda e: self.anchor, self.health, self.auth,
                         self.store, runner=self.runner)
        # Req: F69 后受理点必拒「已落库＝零新增」的 done（活已被上游完成→post(kind=question) 申报 no-op）；
        # 本测守护门体直通路径，仿崩溃恢复直入 run，不经受理门
        self.state.transition("t1", "done", commit=task, rebased_from=None)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.runner.calls, [])  # 已落库提交零重验（contains 直通）
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("push_rejected", names)  # 直通不推陈旧支：零假 non-ff 警报
        self.assertEqual(
            self._git(["rev-parse", "master"], cwd=proj).stdout.strip(), stale)  # 主检出未动

    def test_detached_checkout_refused_not_silent_loss(self):  # Req: ag-bf61e7
        """事故形态端到端（真 Gitx）：主检出 detached 而分支 ref 独立——旧码以检出
        HEAD 判 ff 并把 --ff-only 推进落在 detached HEAD 上（refs/heads/master 不动，
        merged 落账但内容静默丢失）；新码门入口实名解析遇 detached 拒判，verify
        零消耗、分支 ref 零风险，恢复检出后重跑即正常落库。"""
        proj = self.tmp / "proj2"
        proj.mkdir()
        self._git(["init", "-b", "master"], cwd=proj)
        self._git(["config", "user.email", "t@t"], cwd=proj)
        self._git(["config", "user.name", "t"], cwd=proj)
        self._git(["commit", "--allow-empty", "-m", "init"], cwd=proj)
        origin = self.tmp / "origin2.git"
        self._git(["init", "--bare", "-b", "master", str(origin)])
        self._git(["remote", "add", "origin", str(origin)], cwd=proj)
        self._git(["push", "-q", "origin", "master"], cwd=proj)
        gx = Gitx()
        env = gx.build_env(str(proj), "t1")
        self._git(["commit", "--allow-empty", "-m", "task"], cwd=env)
        task = self._git(["rev-parse", "HEAD"], cwd=env).stdout.strip()
        self._git(["checkout", "-q", "--detach"], cwd=proj)  # 回滚残留：检出脱离分支
        self.seed()  # tb=None＝缺省支任务
        self.state.set_fields("t1", project_dir=str(proj), env_dir=env)
        self.anchor = {"head": task, "clean": True}
        self.gate = Gate(self.state, gx, self.router, self.oc,
                         lambda e: self.anchor, self.health, self.auth,
                         self.store, runner=self.runner)
        self.state.transition("t1", "done", commit=task, rebased_from=None)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")  # 拒判弹回非假合并
        self.assertEqual(self.runner.calls, [])  # verify 零消耗
        names = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("merged", names)  # 分支 ref 零风险：无静默丢合并
        stale = self._git(["rev-parse", "master"], cwd=proj).stdout.strip()
        self.state.transition("t1", "running")  # 恢复分支检出（派发方处置）
        self._git(["checkout", "-q", "master"], cwd=proj)
        self.assertEqual(
            self._git(["rev-parse", "master"], cwd=proj).stdout.strip(), stale)  # ref 未动
        self.state.transition("t1", "done", commit=task, rebased_from=None)
        self.gate.run("t1")  # 恢复后重跑：实名解析 refs/heads/master 正常落库
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(
            self._git(["rev-parse", "master"], cwd=proj).stdout.strip(), task)  # 内容落库


class TestQuickEnvelope(TestCoreGate):
    """F65 quick 包络门：verify 前机械量累计 diff，超顶按 verify 红弹回。"""

    def test_envelope_exceeded_red_like_verify_fail(self):  # Req: F65
        self.in_gate(quick=True)
        self.gx.diff_stats_result = (5, 30, "2\t20\ta.py\n3\t10\tb.py")
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)  # verify 红同口径计连败
        self.assertEqual(self.runner.calls, [])  # 先于 verify：verify 未跑
        self.assertEqual(self.gx.merge_calls, [])  # 不合并
        body = self.worker_msgs()[-1]["body"]
        self.assertIn("quick envelope exceeded: 5 files (max 3) / 30 lines (max 30)",
                      body)
        self.assertIn("b.py", body)  # numstat 明细随尾部回 worker
        self.assertIn("post(done", body)  # 动作指引

    def test_within_envelope_verify_runs_and_merges(self):  # Req: F65
        self.in_gate(quick=True)
        self.gx.diff_stats_result = (2, 10, "1\t5\ta.py\n1\t5\tb.py")
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 全链照常
        self.assertEqual(self.runner.calls,
                         [(str(self.env), "pytest -q"),
                          (self.gx.integration_builds[-1][1], "pytest -q")])

    def test_knobs_override_tighten_envelope(self):  # Req: F65
        self.mk_gate(quick_max_files=1, quick_max_lines=1)
        self.in_gate(quick=True)
        self.gx.diff_stats_result = (1, 2, "1\t2\ta.py")  # 行数超 1：红
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "verify_failed")
        self.assertIn("1 files (max 1) / 2 lines (max 1)",
                      self.worker_msgs()[-1]["body"])
        self.mk_gate(quick_max_files=1, quick_max_lines=2)  # 包络内：verify 照跑
        self.in_gate(quick=True)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")


BASE_SHA = "a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"  # Req: F98 远端基线
TIP_SHA = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0"  # Req: F98 他写者推进后远端 tip
MANAGED_DELIVERY = {"mode": "managed", "status": "pending", "commit": None,
                    "base": None, "target_branch": "main", "attempt": 0,
                    "local_status": "pending", "error": None}


class TestManagedDelivery(TestCoreGate):
    """Req: F98 managed origin-first 交付：远端基集成/锁内 ff 推送/3 次总预算/
    交付终局契约（legacy delivery=None 与 out_of_band 走原门，另有既有用例回归）。"""

    def setUp(self):
        super().setUp()
        self.integration = str(self.env) + "__gate"

    def seed_managed(self, streak=0):
        self.seed(tb="main", streak=streak)
        self.state.set_fields("t1", delivery=dict(MANAGED_DELIVERY))
        self.gx.has_origin_result = True

    def in_gate_managed(self, streak=0):
        self.seed_managed(streak=streak)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")

    def delivery(self):
        return self.state.get("t1")["delivery"]

    def test_managed_ff_direct_delivery(self):  # Req: F98
        self.in_gate_managed()
        self.gx.fetch_result = BASE_SHA
        self.gx.head_shas[self.integration] = SHA  # ff 直通：integration 头＝env 头
        self.gx.remote_contains_results = [False, True]  # 入口探针未落远端；推送后复核含
        self.gx.sync_result = "synced"
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        d = self.delivery()
        self.assertEqual((d["status"], d["commit"], d["base"], d["attempt"],
                          d["local_status"], d["error"]),
                         ("delivered", SHA, BASE_SHA, 1, "synced", None))
        self.assertEqual(self.gx.build_at_calls,
                         [("/repo", self.integration, BASE_SHA)])
        self.assertEqual(self.gx.push_commit_calls, [("/repo", SHA, "main")])
        self.assertEqual(self.gx.sync_calls, [("/repo", "main", SHA, "aaa111")])
        self.assertEqual(self.gx.removed, [str(self.env)])  # env 回收
        self.assertEqual(self.gx.clean_calls, [])  # 不碰主检出干净面
        self.assertEqual(self.gx.checkouts, [])  # 不做换支还原
        self.assertEqual([c[0] for c in self.runner.calls],
                         [str(self.env), self.integration])  # 双 verify 不减

    def test_managed_remote_race_rebases_and_reverifies(self):  # Req: F98
        self.in_gate_managed()
        self.gx.merge_result = "non-ff"
        self.gx.rebase_result = "sha"
        self.gx.rebase_new_head = R_SHA
        self.gx.fetch_results = [BASE_SHA, TIP_SHA,  # ①基线＋锁内重读（他写者推进）
                                 TIP_SHA, TIP_SHA]   # ②重取基线＋锁内复核
        self.gx.remote_contains_results = [False, True]  # 入口探针未落；推送后复核含
        self.runner.results = [(0, "p")] * 3  # env verify＋两轮集成 verify
        self.gx.sync_result = "synced"
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["commit"], R_SHA)  # 账面 commit＝重排后 sha
        d = self.delivery()
        self.assertEqual((d["status"], d["commit"], d["base"], d["attempt"]),
                         ("delivered", R_SHA, TIP_SHA, 2))
        self.assertEqual([c[2] for c in self.gx.build_at_calls],
                         [BASE_SHA, TIP_SHA])  # 每次尝试整链重建（基＝当轮远端 tip）
        self.assertEqual(self.gx.push_commit_calls, [("/repo", R_SHA, "main")])
        self.assertEqual(len(self.runner.calls), 3)  # 集成 verify 重跑在重排结果上
        self.assertIn("rebased",
                      [e["event"] for e in self.store.read_events("t1")])

    def test_managed_rebase_conflict_blocks_without_reopen(self):  # Req: F98
        self.in_gate_managed(streak=2)  # 连败 2 在案：交付失败不得推到 3
        self.gx.merge_result = "non-ff"
        self.gx.rebase_result = "conflict"
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(t["fail_streak"], 2)  # 非质量败不计连败
        d = self.delivery()
        self.assertEqual((d["status"], d["error"]),
                         ("failed", "rebase_conflict"))
        self.assertEqual(self.gx.push_commit_calls, [])  # 远端零推送
        self.assertEqual(self.gx.removed, [])  # blocked 留 env 现场
        self.assertEqual(self.gx.sync_calls, [])
        evs = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("reopened", evs)  # 不弹回 worker
        self.assertIn("delivery_failed", evs)
        self.assertEqual(  # worker 零续修指引（门入口知会除外）
            [m for m in self.worker_msgs() if "重新 post(done" in m["body"]], [])

    def test_managed_nonff_exhausts_three_attempts(self):  # Req: F98
        self.in_gate_managed()
        self.gx.push_commit_results = [("non-ff", "fetch first")] * 3
        self.runner.results = [(0, "p")] * 4  # env＋三轮集成 verify
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        d = self.delivery()
        self.assertEqual((d["status"], d["error"], d["attempt"]),
                         ("failed", "retry_exhausted", 3))
        self.assertEqual(len(self.gx.push_commit_calls), 3)  # 恰 3 次总尝试

    def test_managed_rebased_verify_fail_reopens_not_push_failed(self):  # Req: F98
        self.in_gate_managed()
        self.gx.merge_result = "non-ff"
        self.gx.rebase_result = "sha"
        self.gx.rebase_new_head = R_SHA
        self.runner.results = [(0, "p"), (1, "boom")]
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "reopened")
        self.assertEqual(t["fail_streak"], 1)  # 质量败照计连败
        self.assertEqual((self.delivery()["status"], self.delivery()["error"]),
                         ("pending", None))  # 非交付终局
        self.assertEqual(self.gx.push_commit_calls, [])  # 未验证不推送
        self.assertTrue(any("rebase 后集成 verify 失败" in m["body"]
                            for m in self.worker_msgs()))

    def test_managed_push_timeout_reconciles_as_delivered(self):  # Req: F98
        self.in_gate_managed()
        self.gx.head_shas[self.integration] = SHA
        self.gx.push_commit_results = [("failed", "push 超时（>300s）")]
        self.gx.remote_contains_results = [False, True]  # 入口探针未落；对账已含
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        d = self.delivery()
        self.assertEqual((d["status"], d["commit"]), ("delivered", SHA))
        self.assertEqual(len(self.gx.push_commit_calls), 1)  # 对账归零不重推

    def test_managed_delivered_dirty_local_pending(self):  # Req: F98
        self.in_gate_managed()
        self.gx.head_shas[self.integration] = SHA
        self.gx.remote_contains_results = [False, True]  # 入口探针未落；推送后复核含
        self.gx.sync_result = "pending"  # 本地脏/分叉：同步守卫不过
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        d = self.delivery()
        self.assertEqual((d["status"], d["local_status"]),
                         ("delivered", "pending"))
        self.assertEqual(self.gx.clean_calls, [])  # 绝不清洗本地 WIP
        self.assertEqual(self.gx.sync_calls, [("/repo", "main", SHA, "aaa111")])

    def test_managed_lock_wait_bounded_times_out(self):  # Req: F98
        self.in_gate_managed()
        self.gx.lock.busy = True
        self.runner.results = [(0, "p")] * 4
        started = time.monotonic()
        with mock.patch.object(gate_mod, "_LOCK_WAIT", 0.15):
            self.gate.run("t1")
        self.assertLess(time.monotonic() - started, 30)  # 有界等待不挂死
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        d = self.delivery()
        self.assertEqual((d["status"], d["error"]),
                         ("failed", "retry_exhausted"))
        self.assertEqual(self.gx.push_commit_calls, [])  # 未得锁零推送
        self.assertEqual(len(self.gx.build_at_calls), 3)  # 每次尝试整链重走
        self.assertGreaterEqual(self.gx.lock.acquires, 3)

    def test_managed_crash_window_remote_contains_finishes(self):  # Req: F98
        self.in_gate_managed()
        self.gx.remote_contains_result = True  # 崩溃窗口：推送已落远端
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        d = self.delivery()
        self.assertEqual((d["status"], d["commit"]), ("delivered", SHA))
        self.assertEqual(self.gx.push_commit_calls, [])  # 不重推
        self.assertEqual(self.gx.build_at_calls, [])  # 不重验
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.gx.removed, [str(self.env)])  # 补终态照回收

    def test_managed_fetch_failure_blocks_delivery(self):  # Req: F98
        self.in_gate_managed()
        self.gx.fetch_raise = True
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(self.delivery()["error"], "fetch_failed")
        self.assertEqual(self.gx.push_commit_calls, [])

    def test_managed_lock_fetch_failure_is_delivery_terminal(self):  # Req: F98
        """评审 D2：锁内重读远端 fetch 败＝交付终局 fetch_failed（循环顶同口径
        blocked），绝不走门内异常 reopened 弹回 worker；锁随 finally 释放。"""
        self.in_gate_managed()
        self.gx.fetch_results = [
            BASE_SHA, RuntimeError("git fetch origin main: could not read"
                                   " from remote repository")]
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(self.delivery()["error"], "fetch_failed")
        self.assertEqual(self.gx.push_commit_calls, [])  # 未推送
        evs = [e["event"] for e in self.store.read_events("t1")]
        self.assertNotIn("reopened", evs)  # 不弹回 worker
        self.assertEqual(self.gx.lock.releases, self.gx.lock.acquires)  # 锁必释放


if __name__ == "__main__":
    unittest.main()
