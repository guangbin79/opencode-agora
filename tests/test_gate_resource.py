"""Req: F116 跨进程 gate 资源闸：repo 互斥锁＋AGORA_HOME 全局槽闸＋可配并行数.

模块级：真 flock/槽文件、真子进程（kill -9 验内核自动回收）；
门级：真 State/Router/Gate（resources 构造注入）——waiting 投影、
gate_resource_wait/acquire/release 事件、排队不计 fail_streak、
看门狗预算自获锁后起算、同仓跨对象恒串行。
"""
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agorad.access.gate_resources import GateResources
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import Gate
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.main import gate_max_concurrent_from_env
from tests.test_core_gate import SHA, FakeGitx, FakeHealth, FakeOc


def _wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


def _acquire_async(resources, repo):
    """后台线程取锁（跨对象并发扮多 gate 进程）；box["handle"]＝成功。"""
    box = {}

    def run():
        try:
            box["handle"] = resources.acquire(repo)
        except BaseException as e:  # noqa: BLE001——失败也是观测面
            box["error"] = e
        box["done"] = True

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return box, th


class TestSlotSemantics(unittest.TestCase):
    """槽闸模块级语义（真文件真锁；max/序/崩溃回收）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo_a = self.tmp / "repoA"
        self.repo_a.mkdir()
        (self.tmp / "repoB").mkdir()
        (self.tmp / "repoC").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_limit1_cross_repo_serializes(self):  # Req: F116 a
        res = GateResources(self.tmp, 1)
        h1 = res.acquire(self.repo_a)
        box, th = _acquire_async(res, self.tmp / "repoB")
        self.assertFalse(_wait_until(lambda: box.get("done"), 0.5),
                         "limit=1 时异仓第二路不得先行")
        h1.release()
        th.join(5)
        self.assertIn("handle", box, f"释放后第二路必须取到锁（{box}）")
        box["handle"].release()

    def test_limit2_two_parallel_third_waits(self):  # Req: F116 b
        res = GateResources(self.tmp, 2)
        h1 = res.acquire(self.repo_a)
        h2 = res.acquire(self.tmp / "repoB")
        self.assertIsNotNone(h1.slot)
        self.assertIsNotNone(h2.slot)
        self.assertNotEqual(h1.slot, h2.slot, "并行两路须占不同槽")
        box, th = _acquire_async(res, self.tmp / "repoC")
        self.assertFalse(_wait_until(lambda: box.get("done"), 0.5),
                         "limit=2 时第三路必须等待")
        h1.release()
        th.join(5)
        self.assertIn("handle", box)
        box["handle"].release()
        h2.release()

    def test_same_repo_serial_across_objects_even_limit_high(self):  # Req: F116 c
        r1, r2 = GateResources(self.tmp, 3), GateResources(self.tmp, 3)
        h1 = r1.acquire(self.repo_a)
        box, th = _acquire_async(r2, self.repo_a)
        self.assertFalse(_wait_until(lambda: box.get("done"), 0.5),
                         "同仓恒串行——全局 limit 再高也不可并行")
        h1.release()
        th.join(5)
        self.assertIn("handle", box)
        box["handle"].release()

    def test_slot_reusable_after_holder_process_killed(self):  # Req: F116 d
        slot = self.tmp / ".agora" / "gate" / "slot-0.lock"
        slot.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            ["python3", "-c",
             "import fcntl, time\n"
             f"fh = open({str(slot)!r}, 'a+b')\n"
             "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
             "print('locked', flush=True)\n"
             "time.sleep(300)"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            res = GateResources(self.tmp, 1)
            box, th = _acquire_async(res, self.tmp / "repoB")
            self.assertFalse(_wait_until(lambda: box.get("done"), 0.5),
                             "持有进程活着时槽不得复用")
            holder.kill()
            holder.wait(5)
            th.join(5)
            self.assertIn("handle", box,
                          "持有进程被杀后槽必须立即可复用（内核回收 flock）")
            box["handle"].release()
        finally:
            if holder.poll() is None:
                holder.kill()

    def test_repo_lock_in_git_common_dir(self):  # Req: F116（真 git 仓锚定）
        repo = self.tmp / "gitrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        res = GateResources(self.tmp, 1)
        h = res.acquire(repo)
        try:
            self.assertTrue((repo / ".git" / "agora-gate.lock").is_file(),
                            "repo 锁须落 git common dir（跨实例同仓共享）")
        finally:
            h.release()

    def test_bad_limit_rejected(self):  # Req: F116（模块构造防线）
        for bad in (0, -1, "2", 1.5, True):
            with self.assertRaises(ValueError):
                GateResources(self.tmp, bad)


class TestGateMaxEnv(unittest.TestCase):
    """AGORA_GATE_MAX_CONCURRENT 解析：缺省 1、合法生效、坏值 fail-fast。"""

    def test_default_is_1(self):  # Req: F116 e
        with mock.patch.dict(os.environ):
            os.environ.pop("AGORA_GATE_MAX_CONCURRENT", None)
            self.assertEqual(gate_max_concurrent_from_env(), 1)

    def test_valid_value(self):  # Req: F116 e
        with mock.patch.dict(os.environ, {"AGORA_GATE_MAX_CONCURRENT": "3"}):
            self.assertEqual(gate_max_concurrent_from_env(), 3)

    def test_bad_values_fail_fast_with_name(self):  # Req: F116 e
        for bad in ("0", "-2", "abc", "2.5", ""):
            with mock.patch.dict(os.environ,
                                 {"AGORA_GATE_MAX_CONCURRENT": bad}):
                with self.assertRaises(ValueError) as cm:
                    gate_max_concurrent_from_env()
                self.assertIn("AGORA_GATE_MAX_CONCURRENT", str(cm.exception))
                self.assertIn(bad, str(cm.exception),
                              "错误信息须带原值（合法域可判读）")


class GateResourceTest(unittest.TestCase):
    """门级夹具：真 State/Router/Gate＋GateResources 注入（fake gitx/oc）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.router = Router(self.store, self.state, FakeOc())
        self.runner_calls = []
        self.gate_release = threading.Event()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, block_envs):
        release, calls = self.gate_release, self.runner_calls

        def runner(cwd, cmd, timeout=None, ceiling=None):
            calls.append((cwd, time.monotonic()))
            if any(str(cwd).startswith(str(b)) for b in block_envs):
                release.wait(10)  # 指定 env 门内挂起
            return 0, "pass"

        return runner

    def _gate(self, max_concurrent, runner):
        return Gate(self.state, FakeGitx(), self.router, FakeOc(),
                    lambda env: {"head": SHA, "base": "aaa111", "clean": True},
                    FakeHealth(), Auth(self.store), self.store,
                    runner=runner, verify_timeout=60,
                    resources=GateResources(self.tmp, max_concurrent))

    def _seed_done(self, tid, repo):
        env = self.tmp / "envs" / tid
        env.mkdir(parents=True)
        self.state.add(tid, {
            "title": tid, "goal": "g", "project_dir": repo,
            "env_dir": str(env), "after": None, "target_branch": None,
            "verify": {"cmd": "true", "state": "registered",
                       "registered_by": "disp1", "registered_at": 1.0,
                       "timeout": None},
            "quick": False, "state": "running", "dispatcher_sid": "disp1",
            "worker_sid": f"w-{tid}", "chain_depth": 1, "fail_streak": 0,
            "created": 0.0, "updated": 0.0})
        self.assertEqual(self.state.get(tid)["state"], "running")

    def _run_async(self, gate, tid, deadline=None):
        box = {}

        def run():
            try:
                gate.run_guarded(tid, deadline=deadline)
                box["ok"] = True
            except BaseException as e:  # noqa: BLE001
                box["error"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        return box, th

    def test_waiting_observable_and_fail_streak_frozen(self):  # Req: F116 a/f
        self._seed_done("t1", "/repoA-not-exist")
        self._seed_done("t2", "/repoB-not-exist")
        gate = self._gate(1, self._runner([self.tmp / "envs" / "t1"]))
        gate.accept_done("t1", "w-t1", SHA)
        gate.accept_done("t2", "w-t2", SHA)
        env1 = str(self.tmp / "envs" / "t1")
        box1, th1 = self._run_async(gate, "t1")
        self.assertTrue(_wait_until(
            lambda: any(c[0] == env1 for c in self.runner_calls)),
            "t1 应先进门跑 verify")
        box2, th2 = self._run_async(gate, "t2")
        self.assertTrue(_wait_until(lambda: self.state.get("t2").get(
            "gate_resource", {}).get("waiting")),
            "limit=1 排队中任务行必须呈现 gate_resource.waiting")
        self.assertEqual(self.state.get("t2")["state"], "done",
                         "排队任务状态仍为 done（门队列语义）")
        self.assertEqual(self.state.get("t2")["fail_streak"], 0,
                         "排队期间不计 fail_streak")
        self.assertEqual(len([c for c in self.runner_calls
                              if c[0] != env1]), 0,
                         "t2 不得在获锁前跑 verify")
        self.gate_release.set()
        th1.join(10)
        th2.join(10)
        self.assertNotIn("error", box1, box1)
        self.assertNotIn("error", box2, box2)
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.state.get("t2")["state"], "closed")
        proj = self.state.get("t2")["gate_resource"]
        self.assertFalse(proj["waiting"])
        self.assertIn("acquired_at", proj)

    def test_watchdog_budget_starts_after_acquire(self):  # Req: F116 f
        self._seed_done("t1", "/repoA-not-exist")
        self._seed_done("t2", "/repoB-not-exist")
        gate = self._gate(1, self._runner([self.tmp / "envs" / "t1"]))
        gate.accept_done("t1", "w-t1", SHA)
        gate.accept_done("t2", "w-t2", SHA)
        box1, th1 = self._run_async(gate, "t1")
        _wait_until(lambda: self.runner_calls)
        # deadline=0.5s 但 t2 需排队 >0.5s——排队不占预算则不得 overrun
        box2, th2 = self._run_async(gate, "t2", deadline=0.5)
        time.sleep(0.8)
        self.assertTrue(self.state.get("t2").get("gate_resource", {})
                        .get("waiting"), "t2 应仍在排队")
        self.gate_release.set()
        th1.join(10)
        th2.join(10)
        self.assertNotIn("error", box2, box2)
        t2 = self.state.get("t2")
        self.assertEqual(t2["state"], "closed",
                         "排队时间不得计入看门狗（预算获锁后起算）")
        self.assertNotEqual((t2.get("delivery") or {}).get("error"),
                            "gate_overrun")

    def test_same_repo_serial_at_gate_level(self):  # Req: F116 c
        self._seed_done("t1", "/repoA-not-exist")
        self._seed_done("t2", "/repoA-not-exist")
        gate = self._gate(3, self._runner([self.tmp / "envs" / "t1"]))
        gate.accept_done("t1", "w-t1", SHA)
        gate.accept_done("t2", "w-t2", SHA)
        box1, th1 = self._run_async(gate, "t1")
        _wait_until(lambda: self.runner_calls)
        box2, th2 = self._run_async(gate, "t2")
        self.assertTrue(_wait_until(lambda: self.state.get("t2").get(
            "gate_resource", {}).get("waiting")),
            "同仓第二路必须等 repo 锁（呈现 waiting）")
        self.assertEqual(len(self.runner_calls), 1, "同仓不得并行进门")
        self.gate_release.set()
        th1.join(10)
        th2.join(10)
        self.assertNotIn("error", box1, box1)
        self.assertNotIn("error", box2, box2)
        self.assertEqual(self.state.get("t2")["state"], "closed")

    def test_events_recorded_with_fields(self):  # Req: F116 可观测性
        self._seed_done("t1", "/repoA-not-exist")
        gate = self._gate(1, self._runner([]))
        gate.accept_done("t1", "w-t1", SHA)
        box, th = self._run_async(gate, "t1")
        th.join(10)
        self.assertNotIn("error", box, box)
        names = [e["event"] for e in self.store.read_events("t1")]
        for expected in ("gate_resource_wait", "gate_resource_acquire",
                         "gate_resource_release"):
            self.assertIn(expected, names)
        by_name = {e["event"]: e for e in self.store.read_events("t1")
                   if e["event"].startswith("gate_resource")}
        acq = by_name["gate_resource_acquire"]
        self.assertEqual(acq.get("task"), "t1")
        self.assertEqual(acq.get("repo"), "/repoA-not-exist")
        self.assertEqual(acq.get("limit"), 1)
        self.assertIn("duration", acq)
        self.assertIn("duration", by_name["gate_resource_release"])

    # ---- 评审轮 1 修复锚（Req: F116）----

    def _audit_boom(self, event_name):
        """append_event 包装：仅指定事件名抛 OSError（账本 I/O 病注入）。"""
        orig = self.store.append_event

        def boom(ev):
            if ev.get("event") == event_name:
                raise OSError("ledger io")
            return orig(ev)

        return boom

    def test_wait_audit_failure_flips_waiting(self):  # Req: F116 评审轮1
        self._seed_done("t1", "/repoA-not-exist")
        gate = self._gate(1, self._runner([]))
        gate.accept_done("t1", "w-t1", SHA)
        with mock.patch.object(self.store, "append_event",
                               self._audit_boom("gate_resource_wait")):
            with self.assertRaises(OSError):
                gate.run_guarded("t1")
        self.assertFalse(
            self.state.get("t1")["gate_resource"]["waiting"],
            "wait 审计败也必须翻 waiting=False——不留幽灵排队投影")

    def test_release_audit_failure_does_not_mask_success(self):  # Req: F116 评审轮1
        self._seed_done("t1", "/repoA-not-exist")
        gate = self._gate(1, self._runner([]))
        gate.accept_done("t1", "w-t1", SHA)
        with mock.patch.object(self.store, "append_event",
                               self._audit_boom("gate_resource_release")):
            gate.run_guarded("t1")  # 释放面观测尽力而为：不得上抛掩门结果
        self.assertEqual(self.state.get("t1")["state"], "closed")

    def test_projection_never_persisted(self):  # Req: F116 评审轮1
        from agorad.access.store import Store as _Store
        self._seed_done("t1", "/repoA-not-exist")
        self.state.set_gate_resource("t1", waiting=True, resource="/r",
                                     limit=1)
        self.state.set_fields("t1", updated=1.0)  # 触发任一 _persist 面
        persisted = _Store(self.tmp).load_state()
        self.assertNotIn("gate_resource", persisted["tasks"]["t1"],
                         "瞬态投影不得随快照落账本")
        # 渗漏旧行防御：账本已含 gate_resource 的行，加载即剔除
        leaked = {"version": 1, "mail_read": {},
                  "tasks": {"t2": {"state": "running",
                                   "gate_resource": {"waiting": True}}}}
        _Store(self.tmp).append_state(leaked)
        self.assertNotIn("gate_resource",
                         State(_Store(self.tmp)).get("t2"))

    def test_stale_candidates_exempt_resource_waiting(self):  # Req: F116 评审轮1
        from agorad.core import report
        snap = {"tasks": {
            "twait": {"state": "done", "in_gate": False, "created": 0.0,
                      "gate_resource": {"waiting": True, "limit": 1}},
            "tidle": {"state": "running", "created": 0.0},
        }}
        now = 99999.0  # 两行基线均远超 _STALE_AFTER
        cands = [c[0] for c in report.stale_candidates(snap, now)]
        self.assertNotIn("twait", cands,
                         "F116 锁前等待相视同门排队——免判僵（ag-b49329 口径）")
        self.assertIn("tidle", cands)
