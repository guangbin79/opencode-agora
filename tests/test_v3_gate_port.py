"""Req: F136 — v3 verify/review 门执行体（opencode-agora v3 P3-T9a）。

断言清单（任务页 ag-065b7f，逐条机械判定）：1 accept_done 前置（锚三检＋
F111 链根＋vacuous/子任务闸＋六类结构化拒因＋连败口径）；2 base-red 探针
（F107：红放行/绿硬拒弹回计连败/缓存 reopen 不重跑/豁免/超顶·机械败驻留
路由）；3 env verify 分类（质量红→verify_failed 弹 worker 计连败；126/127
→verify_dead 驻留；signal→verify_infra 不计连败；124→overrun 驻留）；
4 双顶（F36/F51：软顶提醒一次不杀；硬顶 124 驻留；预算 task→profile→global
三级＋服务级硬顶旋钮；排队等待不折预算——时钟注入）；5 门审（F114：缓存
cmd+HEAD 双键/独立 review_fail_streak/三轮分歧 blocked/export 前缀注入/
超顶·死同 verify 分类）；6 集成复跑（F123：AGORA_INTEGRATION_BASE export
前缀/集成红质量口径 reopened/同步冲突不计连败/重放落账）；7 artifact
（F122：每轮 output.log＋meta.json 含 rc/phase/duration_s/bytes/extra、
目录 0700 文件 0600、追加式保留、DB 行＋任务行投影＋事件、sink 败不断门）；
8 门段审计（phase_* 逐点；迁移＋审计＋消息同 UoW 崩溃注入零半态）；
9 崩溃窗（done 残留重排恰一次；FaultInjector verify 前后/结算前后触发
→in_gate/pending_cancel/settle 语义与 tasksvc 一致无悬挂）；10 架构守卫
（test_architecture.py 分层锁＋SQL 圈禁自动覆盖；runner 调用点必在 UoW 外）。
gitx/锚/runner 按桩约定注入；消息面接真 MessagingService；streak 面接真
TaskService（bump_fail_streak 单源复用的活证）。
"""
import json
import os
import stat
import unittest

from agorad import domain
from agorad.domain import BaseProbe, Credential, GateState, VerifySpec
from agorad.gateverify import (DiskArtifactSink, DoneReject,
                               GateVerifyService, classify_failure,
                               cmd_dead, with_shell_exports)
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from tests import v3_harness as harness

DISPATCHER, WORKER = "ses-d1", "ses-w1"
BRANCH, TIP, E, C = "v3-runtime", "b" * 40, "e" * 40, "c" * 40


class GitxGateStub:
    """gitx 桩：钉死支 tip/可达集/祖先对可预置；ff/rebase 结果可脚本化。"""

    def __init__(self):
        self.branches = {BRANCH}
        self.shas = {BRANCH: TIP}
        self.contained = set()      # {(commit, branch)}
        self.ancestors = set()      # {(ancestor, descendant)}
        self.merge_ff_result = "ok"
        self.rebase_result = "ok"
        self.rebase_head = None     # rebase 后 integration 头（None＝不动）
        self.fail_base_build = False
        self.calls = []
        self.removed_envs = set()
        self._gate_head = None      # integration 树当前头（build→ff→rebase 推进）

    def contains(self, repo, commit, branch=None):
        self.calls.append(("contains", commit, branch or BRANCH))
        return (commit, branch or BRANCH) in self.contained

    def is_ancestor(self, repo, base, commit):
        return (base, commit) in self.ancestors

    def head(self, repo, branch=None):
        if branch is not None:
            return self.shas.get(branch, TIP)
        if repo.endswith("__gate"):
            return self._gate_head or TIP
        return E

    def symref(self, repo):
        return BRANCH

    def build_integration(self, target, integration, branch=None):
        self.calls.append(("build_integration", integration, branch))
        self._gate_head = self.shas.get(branch or BRANCH, TIP)

    def build_integration_at(self, target, integration, base_sha=None):
        self.calls.append(("build_integration_at", integration, base_sha))
        if self.fail_base_build:
            raise RuntimeError(f"bad base {base_sha[:8]}")

    def merge_ff(self, env_dir, target, branch=None):
        self.calls.append(("merge_ff", env_dir, target))
        return self.merge_ff_result

    def rebase_onto(self, env_dir, integration, branch=None):
        self.calls.append(("rebase_onto", env_dir, integration))
        if self.rebase_result == "ok" and self.rebase_head:
            self._gate_head = self.rebase_head
        return self.rebase_result

    def materialize_checkout(self, tree, source):
        return ""

    def remove_integration(self, integration, target):
        self.calls.append(("remove_integration", integration))

    def remove_env(self, env_dir):
        self.calls.append(("remove_env", env_dir))
        self.removed_envs.add(env_dir)


class RunnerStub:
    """runner 桩：按调用序脚本化 (rc, out)；记录 kwargs（预算/软顶观察）；
    调用点必在 UoW 外（断言10）；before 钩子可注入门内副作用（取消排队）。"""

    def __init__(self, script=(), storage=None, before=None):
        self.script = list(script)
        self.calls = []
        self.storage = storage
        self.before = before

    def __call__(self, env_dir, cmd, *, timeout=None, ceiling=None,
                 on_soft=None):
        if self.storage is not None and self.storage._depth:
            raise AssertionError("runner 调用点身处 UoW 写事务内（§4 禁子进程）")
        if self.before is not None:
            self.before(self)
        self.calls.append({"env": env_dir, "cmd": cmd, "timeout": timeout,
                           "ceiling": ceiling, "on_soft": on_soft})
        return self.script.pop(0) if self.script else (0, "")

    def by_env(self, env_dir):
        return [c for c in self.calls if c["env"] == env_dir]

    def kwargs(self):
        return [(c["timeout"], c["ceiling"]) for c in self.calls]


class OcStub:
    def __init__(self):
        self.injects = []

    def create(self, env, boot, title=None, model=None):
        return WORKER

    def inject(self, sid, text, model=None):
        self.injects.append(("wake", sid, text))
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
        self.gitx = GitxGateStub()
        self.oc = OcStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.tasksvc = TaskService(
            self.storage, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock)
        self.runner = RunnerStub(storage=self.storage)
        self.sink = DiskArtifactSink(self.home.state / "gate-artifacts")
        self.gate = GateVerifyService(
            self.storage, self.gitx, self.runner, messaging=self.messaging,
            tasksvc=self.tasksvc,
            anchors=lambda env: self._anchor(env),
            artifacts=self.sink, home=str(self.home.path),
            clock=self.clock, injector=self.injector, verify_timeout=300)
        self._anchor_rows = {}

    def _anchor(self, env):
        return self._anchor_rows.get(env)

    def set_anchor(self, tid, head=C, base=E, clean=True):
        self._anchor_rows[f"/tmp/envs/{tid}"] = (None if head is None else
                                                 {"head": head, "base": base,
                                                  "clean": clean})

    def seed(self, tid, state="done", **over):
        row = dict(id=tid, state=state, title=tid, goal="g",
                   project_dir="/repo", target_branch=BRANCH,
                   env_dir=f"/tmp/envs/{tid}", env_base=E,
                   dispatcher_sid=DISPATCHER, root_sid=DISPATCHER,
                   worker_sid=WORKER, chain_depth=1, created_ms=1,
                   updated_ms=1, commit=C,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=DISPATCHER,
                                     registered_at_ms=1))
        row.update(over)
        task = domain.Task(**row)
        with self.storage.uow():
            self.storage.upsert_task(task)
        return tid

    def requeue(self, tid, commit=C):
        """reopened/verify_failed→running→accept_done 再入門（worker 续修路）。"""
        self.tasksvc.transition(tid, "running", actor=WORKER)
        self.set_anchor(tid, head=commit)
        self.gitx.contained.discard((commit, BRANCH))
        return self.gate.accept_done(tid, WORKER, commit)

    def events(self, tid):
        return [e.event for e in self.storage.events_for_task(tid)]

    def messages(self, sid):
        return self.storage.messages_for_sid(sid)

    @staticmethod
    def text(m):
        return (m.body.decode("utf-8", "replace")
                if isinstance(m.body, (bytes, bytearray)) else str(m.body))

    def green_script(self, probe_rc=1):
        """全绿一轮：探针红→env 绿→（无门审：锚复验）→集成绿。"""
        self.runner.script = [(probe_rc, "base-out"), (0, "env-ok"),
                              (0, "int-ok")]


class TestAcceptDone(_Base):  # 断言1
    def test_accept_flow_and_double_done(self):
        tid = self.seed("ag-a1", state="running")
        self.set_anchor(tid)
        self.assertEqual(self.gate.accept_done(tid, WORKER, C), "accepted")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit, t.rebased_from),
                         ("done", C, None))
        self.assertIn("done", self.events(tid))
        self.assertIsInstance(self.gate.accept_done(tid, WORKER, C),
                              DoneReject)  # 二次 done：state 拒

    def test_six_reject_codes_and_streak_scope(self):
        cases = []
        tid = self.seed("ag-a2", state="dispatched")
        cases.append((tid, "state", "dispatched"))
        tid = self.seed("ag-a3", state="running",
                        verify=VerifySpec(state="pending"))
        self.assertNotEqual(self.gate.accept_done(tid, WORKER, C).code,
                            "verify-empty")  # 排序护栏：pending 先于空命令
        cases.append((tid, "verify-unregistered", "running"))
        tid = self.seed("ag-a4", state="running",
                        verify=VerifySpec(cmd="", state="registered",
                                          registered_by=DISPATCHER,
                                          registered_at_ms=1))
        cases.append((tid, "verify-empty", "running"))
        tid = self.seed("ag-a5", state="running")
        cases.append((tid, "body-format", "running"))
        tid = self.seed("ag-a6", state="running")
        self.seed("ag-kid", state="dispatched", dispatcher_sid=WORKER)
        r = self.gate.accept_done(tid, WORKER, C)  # 子任务闸单点（先于循环）
        self.assertEqual((r.code, self.storage.get_task(tid).fail_streak),
                         ("children", 0))
        self.assertIn("done_rejected", self.events(tid))
        self.seed("ag-kid", state="closed", dispatcher_sid=WORKER)  # 结案
        tid = self.seed("ag-a7", state="running")  # 无锚
        cases.append((tid, "anchor", "running"))
        for tid, code, orig in cases:
            r = self.gate.accept_done(tid, WORKER, C if code != "body-format"
                                      else "zz")
            self.assertIsInstance(r, DoneReject, (tid, code))
            self.assertEqual(r.code, code, (tid, r.code))
            t = self.storage.get_task(tid)
            expected = 1 if code == "anchor" else 0  # 锚族计连败，余者不计
            self.assertEqual(t.fail_streak, expected, (tid, code))
            self.assertIn("done_rejected", self.events(tid))
            self.assertEqual(t.state, orig)  # 拒收零状态改写

    def test_anchor_triple_and_f111_chain_root(self):
        tid = self.seed("ag-a8", state="running")
        self.set_anchor(tid, head="d" * 40)  # head≠done sha
        self.assertEqual(self.gate.accept_done(tid, WORKER, C).code, "anchor")
        tid = self.seed("ag-a9", state="running")
        self.set_anchor(tid, base="x" * 40)  # F111 链根漂移
        r = self.gate.accept_done(tid, WORKER, C)
        self.assertEqual(r.code, "anchor")
        self.assertIn("任务起点", r.message)
        tid = self.seed("ag-a10", state="running")
        self.set_anchor(tid, clean=False)  # 评审环未绿
        self.assertEqual(self.gate.accept_done(tid, WORKER, C).code, "anchor")

    def test_review_cmd_skips_anchor_face(self):
        tid = self.seed("ag-a11", state="running", review_cmd="ocr-review")
        self.assertEqual(self.gate.accept_done(tid, WORKER, C), "accepted")

    def test_vacuous_done_and_delivered_exemptions(self):
        tid = self.seed("ag-a12", state="running", commit=None)
        self.set_anchor(tid)
        self.gitx.contained.add((C, BRANCH))  # 空集且无在案交付
        r = self.gate.accept_done(tid, WORKER, C)
        self.assertEqual((r.code, self.storage.get_task(tid).fail_streak),
                         ("vacuous-done", 1))
        tid = self.seed("ag-a13", state="running", commit=C)  # (b) prev==C
        self.set_anchor(tid)
        self.assertEqual(self.gate.accept_done(tid, WORKER, C), "accepted")
        tid = self.seed("ag-a14", state="running")
        self.set_anchor(tid)
        self.gitx.contained.add((C, BRANCH))
        self.gitx.ancestors.add((E, C))  # (a) env HEAD 相对起点前进
        self.assertEqual(self.gate.accept_done(tid, WORKER, C), "accepted")
        tid = self.seed("ag-a15", state="running")
        with self.assertRaises(Exception):
            self.gate.accept_done("ag-none", WORKER, C)  # not_found 契约


class TestBaseProbe(_Base):  # 断言2
    def test_red_releases_full_pipeline(self):
        tid = self.seed("ag-p1")
        self.green_script(probe_rc=1)
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        ev = self.events(tid)
        for phase in ("gate_enter", "phase_base_probe", "phase_verify",
                      "phase_integration", "verify_green"):
            self.assertIn(phase, ev)
        self.assertEqual(len(self.runner.by_env(f"/tmp/envs/{tid}__gate")), 2)
        self.assertEqual(self.storage.get_task(tid).probe.result, "red")

    def test_green_hard_reject_counts_streak(self):
        tid = self.seed("ag-p2")
        self.green_script(probe_rc=0)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak, t.probe.result),
                         ("reopened", 1, "green"))
        self.assertTrue(any(m.kind == "process" and "base-red" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_cache_green_no_rerun_on_requeue(self):
        tid = self.seed("ag-p3")
        self.green_script(probe_rc=0)
        self.gate.run(tid)
        probe_calls = len(self.runner.by_env(f"/tmp/envs/{tid}__gate"))
        self.assertEqual(self.requeue(tid), "accepted")
        self.green_script(probe_rc=0)
        self.assertEqual(self.gate.run(tid), "reopened")  # 缓存绿仍硬拒
        self.assertEqual(len(self.runner.by_env(f"/tmp/envs/{tid}__gate")),
                         probe_calls)  # 探针零重跑

    def test_exempt_quick_and_missing_base_skip(self):
        tid = self.seed("ag-p4", verify_base_exempt=True)
        self.runner.script = [(0, "env"), (0, "int")]  # 探针豁免＝不消耗
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        self.assertEqual(self.runner.calls[0]["env"], f"/tmp/envs/{tid}")
        tid = self.seed("ag-p5", quick=True)
        self.runner.script = [(0, "env"), (0, "int")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        tid = self.seed("ag-p6", env_base=None)
        self.runner.script = [(0, "env"), (0, "int")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")  # fail-open

    def test_probe_cmd_dead_counts_red(self):
        # ag-d68faf：base 阶段命令死（126/127）＝判别红放行——真死命令由
        # env 阶段 verify_dead 分类兜底（A册 S1 锚）
        tid = self.seed("ag-p9")
        self.runner.script = [(126, "No such file"), (0, "env"), (0, "int")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        self.assertEqual(self.storage.get_task(tid).probe.result, "red")

    def test_overrun_and_build_failure_park(self):
        tid = self.seed("ag-p7")
        self.runner.script = [(124, "slow")]
        self.assertEqual(self.gate.run(tid), "overrun")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak, t.gate.in_gate),
                         ("done", 0, False))  # 驻留＋结算无悬挂
        self.assertIn("base_probe_overrun", self.events(tid))
        self.assertTrue(any(m.kind == "result" for m in
                            self.messages(DISPATCHER)))
        # Req: F144 门入口 milestone（v2 同款）合法到 worker； overrun 不弹
        # worker 的判据＝零 process 类弹回消息
        self.assertFalse([m for m in self.messages(WORKER)
                          if m.kind == "process"])
        tid = self.seed("ag-p8")
        self.gitx.fail_base_build = True
        self.assertEqual(self.gate.run(tid), "probe_failed")
        self.assertEqual(self.storage.get_task(tid).state, "done")
        self.assertIn("base_probe_failed", self.events(tid))


class TestVerifyClassification(_Base):  # 断言3
    def _park_case(self, tid, rc, outcome, evname):
        self.runner.script = [(1, "red-base"), (rc, "boom")]
        self.assertEqual(self.gate.run(tid), outcome)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("done", 0))
        self.assertIn(evname, self.events(tid))

    def test_quality_red_bounces_and_third_blocks(self):
        tid = self.seed("ag-v1")
        self.runner.script = [(1, ""), (1, "FAIL-tail")]
        self.assertEqual(self.gate.run(tid), "verify_failed")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("verify_failed", 1))
        msgs = self.messages(WORKER)
        self.assertTrue(any(m.kind == "process" and "输出尾部" in self.text(m)
                            and "FAIL-tail" in self.text(m) for m in msgs))
        for _ in range(2):  # 两轮续修再败 → 三连败自动 blocked
            self.assertEqual(self.requeue(tid), "accepted")
            self.runner.script = [(1, ""), (1, "FAIL")]
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("blocked", 3))

    def test_dead_signal_overrun_park(self):
        tid = self.seed("ag-v2")
        self._park_case(tid, 126, "dead", "verify_dead")
        self.assertTrue(any(kind == "notify" and "verify-dead" in text
                            for kind, _, text in self.oc.injects))
        self.assertTrue(any(m.kind == "process" and "命令自身死" in self.text(m)
                            for m in self.messages(WORKER)))
        tid = self.seed("ag-v3")
        self._park_case(tid, -9, "infra", "verify_infra_failed")
        self.assertTrue(any(kind == "notify" and "verify-infra" in text
                            for kind, _, text in self.oc.injects))
        tid = self.seed("ag-v4")
        self._park_case(tid, 124, "overrun", "verify_overrun")
        self.assertTrue(any(m.kind == "result" and "超硬顶" in self.text(m)
                            for m in self.messages(DISPATCHER)))

    def test_runner_exception_is_gate_crash_not_quality(self):
        tid = self.seed("ag-v5")
        def boom(env, cmd, **kw):
            raise RuntimeError("runner blew up")
        self.gate._runner = boom
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak, t.gate.in_gate),
                         ("reopened", 0, False))  # 门内异常不计连败＋零悬挂
        self.assertTrue(any("门内异常" in self.text(m)
                            for m in self.messages(WORKER)))


class TestDualCaps(_Base):  # 断言4
    def test_budget_three_levels_and_ceiling_knob(self):
        tid = self.seed("ag-c1",
                        verify=VerifySpec(cmd="true", state="registered",
                                          registered_by=DISPATCHER,
                                          registered_at_ms=1, timeout_s=111))
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(self.runner.kwargs()[0], (111, 444))  # task 级：4×
        tid = self.seed("ag-c2")
        self.gate._verify_profiles = {"/repo": {"timeout": 222}}
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(self.runner.kwargs()[-1], (222, 888))  # profile 级
        tid = self.seed("ag-c3")
        self.gate._verify_profiles = {}  # 前例画像不外溢
        self.gate._verify_ceiling = 999  # 服务级硬顶旋钮（default 源生效）
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(self.runner.kwargs()[-1], (300, 999))  # global＋旋钮
        tid = self.seed("ag-c4",
                        verify=VerifySpec(cmd="true", state="registered",
                                          registered_by=DISPATCHER,
                                          registered_at_ms=1, timeout_s=50))
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(self.runner.kwargs()[-1], (50, 200))  # task 压旋钮

    def test_soft_notify_once_and_process_continues(self):
        tid = self.seed("ag-c5")
        def slow_runner(env, cmd, *, timeout=None, ceiling=None, on_soft=None):
            if env.endswith("__gate") and "AGORA_INTEGRATION_BASE" not in cmd:
                return 1, "base-red"  # 探针红（先于 env verify）
            on_soft()
            on_soft()  # runner 自身重复触发：门侧 once-guard 只提醒一次
            return 0, "done-after-soft"
        self.gate._runner = slow_runner
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")  # 软顶不杀：流程照走
        notes = [m for m in self.messages(DISPATCHER)
                 if m.kind == "note" and "超软顶" in self.text(m)]
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.events(tid).count("verify_soft_timeout"), 1)

    def test_queue_wait_does_not_consume_budget(self):
        tid = self.seed("ag-c6", state="done")
        self.clock.advance(9_000_000)  # 排队等待 2.5h（时钟注入）
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(self.runner.kwargs()[0], (300, 1200))  # 全额预算


class TestReview(_Base):  # 断言5
    def _review_flow(self, tid, review_rc):
        self.runner.script = [(1, ""), (0, ""), (review_rc, "REVIEW-RED"),
                              (0, "")]
        self.set_anchor(tid)

    def test_green_caches_cmd_head(self):
        tid = self.seed("ag-r1", review_cmd="ocr .")
        self._review_flow(tid, 0)
        self.assertEqual(self.gate.run(tid), "green")
        t = self.storage.get_task(tid)
        self.assertEqual((t.gate.review_cache["cmd"],
                          t.gate.review_cache["commit"],
                          t.gate.review_cache["ok"], t.gate.review_fail_streak),
                         ("ocr .", E, True, 0))
        self.assertIn("gate_reviewed", self.events(tid))
        calls = [c for c in self.runner.calls if "AGORA_REVIEW_BASE" in c["cmd"]]
        self.gate.run(tid)  # 再跑：缓存命中零重审
        self.assertEqual(len([c for c in self.runner.calls
                              if "AGORA_REVIEW_BASE" in c["cmd"]]), len(calls))
        self.assertIn("gate_review_cached", self.events(tid))

    def test_export_prefix_exact_shape(self):
        tid = self.seed("ag-r2", review_cmd="cd x && ocr")
        self._review_flow(tid, 0)
        self.gate.run(tid)
        review_cmds = [c["cmd"] for c in self.runner.calls
                       if "AGORA_REVIEW_BASE" in c["cmd"]]
        self.assertEqual(review_cmds,
                         [f"export AGORA_REVIEW_BASE={E}; cd x && ocr"])

    def test_red_independent_streak_and_blocked_at_three(self):
        tid = self.seed("ag-r3", review_cmd="ocr .")
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
                self.assertTrue(any(
                    e.event == "blocked"
                    and (e.detail or {}).get("note") == "review_disagreement"
                    for e in self.storage.events_for_task(tid)))
                self.assertTrue(any("三轮分歧" in self.text(m)
                                    for m in self.messages(DISPATCHER)))

    def test_overrun_and_dead_park_without_streak(self):
        tid = self.seed("ag-r4", review_cmd="ocr .")
        self._review_flow(tid, 124)
        self.assertEqual(self.gate.run(tid), "overrun")
        self.assertEqual(self.storage.get_task(tid).gate.review_fail_streak, 0)
        tid = self.seed("ag-r5", review_cmd="ocr .")
        self._review_flow(tid, 127)
        self.assertEqual(self.gate.run(tid), "dead")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.gate.review_fail_streak), ("done", 0))
        self.assertTrue(any(kind == "notify" and "verify-dead" in text
                            for kind, _, text in self.oc.injects))

    def test_missing_env_base_passes_with_audit(self):
        tid = self.seed("ag-r6", review_cmd="ocr .", env_base=None)
        self.runner.script = [(0, "env"), (0, "int")]  # 探针跳过（无 base）
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        self.assertIn("review_base_unavailable", self.events(tid))


class TestIntegration(_Base):  # 断言6
    def test_green_export_prefix_and_meta_extra(self):
        tid = self.seed("ag-i1")
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        int_cmds = [c["cmd"] for c in self.runner.by_env(f"/tmp/envs/{tid}__gate")
                    if "AGORA_INTEGRATION_BASE" in c["cmd"]]
        self.assertEqual(int_cmds, [f"export AGORA_INTEGRATION_BASE={TIP}; true"])
        metas = self._meta_by_phase(tid, "integration_verify")
        self.assertEqual(metas[0]["extra"], {"integration_base": TIP})
        self.assertIn(("build_integration", f"/tmp/envs/{tid}__gate", BRANCH),
                      self.gitx.calls)

    def test_red_is_quality_reopened(self):
        tid = self.seed("ag-i2")
        self.runner.script = [(1, ""), (0, ""), (2, "INTEGRATION-FAIL")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("reopened", 1))
        self.assertTrue(any(m.kind == "process"
                            and "集成 verify 失败" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_sync_conflict_bounces_without_streak(self):
        tid = self.seed("ag-i3")
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_result = "conflict"
        self.runner.script = [(1, ""), (0, "")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("reopened", 0))
        self.assertTrue(any("同步冲突" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_rebase_replay_bookkeeping(self):
        tid = self.seed("ag-i4")
        R = "9" * 40
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_head = R  # ff 失败→rebase 重放出新头 R≠before
        self.runner.script = [(1, ""), (0, ""), (1, "REBASE-RED")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.commit, t.rebased_from), (R, C))  # 先落库再验证
        self.assertIn("rebased", self.events(tid))
        self.assertTrue(any(m.kind == "process"
                            and "rebase 后集成 verify 失败" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_overrun_and_dead_park_at_integration(self):
        # A册 S2 锚：集成阶段 124/命令死同 verify 分类——驻留 done、不计
        # 连败、路由派发方（signal 分支经 classify_failure 纯函数单测兜底）
        tid = self.seed("ag-i5")
        self.runner.script = [(1, ""), (0, ""), (124, "slow")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "overrun")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("done", 0))
        self.assertIn("verify_overrun", self.events(tid))
        tid = self.seed("ag-i6")
        self.runner.script = [(1, ""), (0, ""), (127, "dead")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "dead")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("done", 0))
        self.assertIn("verify_dead", self.events(tid))
        self.assertTrue(any(kind == "notify" and "verify-dead" in text
                            for kind, _, text in self.oc.injects))

    def _meta_by_phase(self, tid, phase):
        out = []
        for d in (self.home.state / "gate-artifacts" / tid).iterdir():
            if phase in d.name:
                out.append(json.loads((d / "meta.json").read_text()))
        return out


class TestArtifact(_Base):  # 断言7
    def _full_review_flow(self, tid):
        self.runner.script = [(1, "base"), (0, "env"), (0, "rev"),
                              (0, "int")]
        self.set_anchor(tid)
        return self.gate.run(tid)

    def test_files_meta_permissions_and_retention(self):
        tid = self.seed("ag-t1", review_cmd="ocr .")
        self.assertEqual(self._full_review_flow(tid), "green")
        root = self.home.state / "gate-artifacts" / tid
        phases = sorted(d.name.rsplit("-", 1)[0].split("T")[-1]
                        for d in root.iterdir())
        for d in root.iterdir():
            self.assertEqual(_mode(d), 0o700)
            self.assertEqual(_mode(d / "output.log"), 0o600)
            self.assertEqual(_mode(d / "meta.json"), 0o600)
            meta = json.loads((d / "meta.json").read_text())
            for key in ("rc", "phase", "duration_s", "bytes", "cmd", "extra"):
                self.assertIn(key, meta)
            self.assertEqual(meta["tid"], tid)
        self.assertEqual(len(phases), 4)  # base_probe/verify/review/integration
        rows = self.storage.artifacts_for_task(tid)
        self.assertEqual(len(rows), 4)  # DB 行逐轮落账
        self.assertEqual(self.storage.get_task(tid).gate.gate_artifact.path,
                         rows[0].path)  # 任务行投影＝最新一条
        self.assertIn("gate_artifact_written", self.events(tid))
        n_before = len(list(root.iterdir()))
        self.runner.script = []  # 探针/门审缓存命中：仅 env＋integration 复跑
        self.assertEqual(self.gate.run(tid), "green")  # green 驻留 done 可重排
        self.assertEqual(len(list(root.iterdir())), n_before + 2)  # 追加式保留

    def test_sink_failure_does_not_break_gate(self):
        tid = self.seed("ag-t2")
        class BrokenSink:
            def record(self, *a, **kw):
                raise OSError("disk full")
        self.gate._artifacts = BrokenSink()
        self.runner.script = [(1, ""), (1, "FAIL")]
        self.assertEqual(self.gate.run(tid), "verify_failed")  # 分类照路由
        self.assertIn("gate_artifact_write_failed", self.events(tid))
        self.assertIsNone(self.storage.get_task(tid).gate.gate_artifact)


class TestAuditUow(_Base):  # 断言8
    def test_phase_events_sequence(self):
        tid = self.seed("ag-m1", review_cmd="ocr .")
        self.runner.script = [(1, ""), (0, ""), (0, ""), (0, "")]
        self.set_anchor(tid)
        self.gate.run(tid)
        ev = self.events(tid)
        i = (ev.index("phase_base_probe"), ev.index("phase_verify"),
             ev.index("phase_review"), ev.index("phase_integration"),
             ev.index("verify_green"))
        self.assertEqual(i, tuple(sorted(i)))  # 逐点有序落 events

    def test_transition_audit_message_same_uow_zero_half_state(self):
        tid = self.seed("ag-m2")
        self.runner.script = [(1, ""), (1, "FAIL")]
        self.injector.arm("uow", "after")  # 写完、COMMIT 前
        with self.assertRaises(harness.FaultInjected):
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "done")  # 零半态：迁移回滚
        self.assertNotIn("verify_failed", self.events(tid))
        self.assertEqual([m for m in self.messages(WORKER)
                          if m.kind == "process"], [])
        self.injector.disarm("uow")
        self.assertEqual(self.gate.run(tid), "verify_failed")  # 重放完整成功
        self.assertEqual(self.storage.get_task(tid).fail_streak, 1)


class TestCrashWindow(_Base):  # 断言9
    def test_landed_residue_settles_exactly_once(self):
        tid = self.seed("ag-w1")
        self.gitx.contained.add((C, BRANCH))  # 权威已含交付（崩溃残留形态）
        self.home.write_credential(tid, "tok")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=WORKER, token="tok", task_id=tid))
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")  # merged→closed 一事务收口
        self.assertIsNone(self.storage.get_credential(WORKER))  # 撤牌
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 回收
        n_events = len(self.events(tid))
        self.assertEqual(self.gate.run(tid), "skip")  # 重排恰一次：无重复迁移
        # Req: F144 skip 路径同占道（v2 run_guarded 同位）——唯车道审计三行
        # （wait/acquire/release）新增，迁移/门体事件零重复
        self.assertEqual(len(self.events(tid)), n_events + 3)

    def test_cancel_queued_in_gate_settles_at_tail(self):
        tid = self.seed("ag-w2")
        self.runner = RunnerStub(storage=self.storage, before=lambda r: (
            self.tasksvc.cancel(tid, DISPATCHER, "dispatcher")))
        self.gate._runner = self.runner
        self.runner.script = [(1, ""), (0, ""), (0, "")]  # 全绿但排队取消
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")  # verify 结果不被取消吞
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.cancel_reason), ("cancelled", None))
        self.assertFalse(t.gate.pending_cancel)  # 排队消费零残留
        self.assertIsNone(self.storage.get_credential(WORKER))
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)
        self.assertTrue(any("cancelled at gate tail" in self.text(m)
                            for m in self.messages(DISPATCHER)))

    def test_settle_crash_before_keeps_queue_then_settles(self):
        tid = self.seed("ag-w3")
        self.runner = RunnerStub(storage=self.storage, before=lambda r: (
            self.tasksvc.cancel(tid, DISPATCHER, "dispatcher")))
        self.gate._runner = self.runner
        self.runner.script = [(1, ""), (0, ""), (0, "")]
        self.set_anchor(tid)
        self.injector.arm("settle", "before")
        with self.assertRaises(harness.FaultInjected):
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.gate.in_gate, t.gate.pending_cancel),
                         ("done", True, True))  # 崩溃窗：队列与门闩原样保留
        self.injector.disarm("settle")
        self.runner.script = [(0, ""), (0, "")]  # 探针缓存：直接 env/integration
        self.assertEqual(self.gate.run(tid), "green")
        self.assertEqual(self.storage.get_task(tid).state, "cancelled")

    def test_verify_crash_leaves_no_dangling_latch(self):
        tid = self.seed("ag-w4")
        self.runner.script = [(1, ""), (0, "")]
        self.set_anchor(tid)
        self.injector.arm("verify", "before")
        self.assertEqual(self.gate.run(tid), "reopened")  # 门内崩弹回（捕获）
        t = self.storage.get_task(tid)
        self.assertEqual((t.gate.in_gate, t.gate.gate_env), (False, None))
        self.assertIn("remove_integration",
                      [c[0] for c in self.gitx.calls])  # 回收面已走
        self.injector.disarm("verify")
        self.requeue(tid)
        self.runner.script = [(0, ""), (0, "")]  # 探针缓存命中
        self.assertEqual(self.gate.run(tid), "green")  # 重排完整续跑

    def test_settle_matches_tasksvc_parked_cancel_semantics(self):
        # 等价锚：门尾结算行与 tasksvc 驻留直结同形（撤牌/回收/终态消息）
        tid = self.seed("ag-w5")
        self.home.write_credential(tid, "tok")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=WORKER, token="tok", task_id=tid))
        with self.storage.uow():
            fresh = self.storage.get_task(tid)
            self.storage.upsert_task(domain.dataclasses.replace(
                fresh, gate=GateState(in_gate=True, pending_cancel=True,
                                      pending_cancel_reason="superseded")))
        self.runner.script = [(1, ""), (0, "")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.cancel_reason), ("cancelled",
                                                      "superseded"))
        self.assertFalse(self.home.credentials.joinpath(f"{tid}.token").exists())


class TestPureHelpers(unittest.TestCase):  # 断言3/5/6 分类与注入单源
    def test_classify_failure_taxonomy(self):
        self.assertEqual(classify_failure(0), "green")
        self.assertEqual(classify_failure(124), "overrun")
        self.assertEqual(classify_failure(126), "command_dead")
        self.assertEqual(classify_failure(127), "command_dead")
        self.assertEqual(classify_failure(2, "x: No such file or directory"),
                         "command_dead")
        self.assertEqual(classify_failure(1, "No such file or directory"),
                         "content_failed")  # 文案不独立触发
        self.assertEqual(classify_failure(-9), "signal")
        self.assertEqual(classify_failure(137), "signal")   # SIGKILL
        self.assertEqual(classify_failure(139), "signal")   # SIGSEGV
        self.assertEqual(classify_failure(2, "usage"), "content_failed")
        self.assertEqual(classify_failure(None, "", error=RuntimeError("x")),
                         "runner_exception")
        self.assertTrue(cmd_dead(126))
        self.assertFalse(cmd_dead(1))

    def test_with_shell_exports_prefix_and_validation(self):
        self.assertEqual(with_shell_exports("cd x && pytest", {"K": "v"}),
                         "export K=v; cd x && pytest")  # 非 VAR=x cmd 形态
        self.assertEqual(with_shell_exports("c", {}), "c")
        with self.assertRaises(ValueError):
            with_shell_exports("c", {"1BAD": "v"})
        with self.assertRaises(ValueError):
            with_shell_exports("c", {"K": "a b"})
        with self.assertRaises(ValueError):
            with_shell_exports("c", {"K": "x;y"})


class TestUowOutsideRunner(_Base):  # 断言10（runner 调用点必在事务外）
    def test_runner_never_called_inside_uow(self):
        tid = self.seed("ag-x1")
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "green")  # 桩内逐调用断言 _depth


if __name__ == "__main__":
    unittest.main()
