"""P0-1/P0-2 门看门狗与孤儿进程组治理（ag-9c4cbb）。

生产 wedge（2026-11-16 13:41-15:20，ag-9e055d）：挂死 verify 的子进程派生
长命孙进程持住 stdout 管道——subprocess 超时只杀直接子进程，communicate()
永不返回，单线程门队列被堵 1.5h（signature：done＋delivery pending＋
attempt=0＋零事件）。本文件验证：

- 集成（真 Gitx/真 runner/真 make_server 队列）：看门狗到点整组收杀＋
  blocked(gate_overrun) 终局＋队列续行＋无孤儿孙进程；
- 单元：SpawnTracker 登记簿语义、runner 让位 124、gate_crashed 留痕。
killpg/start_new_session 的 gitx 面单测归 tests/test_access_gitx.py。
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

from agorad.access.gitx import Gitx  # noqa: F401  # P0-2 契约面归 gitx 测试
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import Gate, SpawnTracker, _subprocess_runner
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.main import assemble
from agorad.transport.httpd import make_server


class FakeOc:
    def inject(self, sid, text, model=None):
        return False

    def messages(self, sid):
        return []


class FakeHealth:
    def escalate(self, kind, tid, raw=None, streak=None):
        pass


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


def _head(repo):
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _wait_until(fn, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


def _proc_alive(pid):
    """/proc 存在且非僵尸＝活（孤儿被收杀后可能短暂僵尸态——僵尸即已死）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


class TestGateWatchdog(unittest.TestCase):
    """集成四联断言：watchdog 触发/blocked 终局/队列续行/无孤儿。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        _git(["init", "-b", "master"], self.proj)
        _git(["config", "user.email", "t@t"], self.proj)
        _git(["config", "user.name", "t"], self.proj)
        _git(["commit", "--allow-empty", "-m", "init"], self.proj)
        self.oc = FakeOc()
        self.app = assemble(self.tmp, oc=self.oc,
                            read_anchor=lambda env: {"head": _head(env),
                                                     "clean": True},
                            verify_timeout=60)
        self.grandchild = None

    def tearDown(self):
        if self.grandchild and _proc_alive(self.grandchild):
            try:  # 断言失败的兜底：不留 300s 孤儿拖垮 CI
                os.kill(self.grandchild, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, tid, cmd, timeout=60):
        """真 env＋空提交（保证 contains=False 走完整门链）→running→done。"""
        env = self.app.gitx.build_env(str(self.proj), tid)
        _git(["commit", "--allow-empty", "-m", f"task {tid}"], env)
        head = _head(env)
        self.app.state.add(tid, {
            "title": tid, "goal": "g", "project_dir": str(self.proj),
            "env_dir": env, "after": None, "target_branch": "master",
            "verify": {"cmd": cmd, "state": "registered",
                       "registered_by": "disp1", "registered_at": 1.0,
                       "timeout": timeout},
            "quick": False, "state": "running", "dispatcher_sid": "disp1",
            "worker_sid": "s-" + tid, "chain_depth": 1, "fail_streak": 0,
            "created": 0.0, "updated": 0.0})
        self.app.state.transition(tid, "done", commit=head, rebased_from=None)
        return env

    def test_watchdog_kills_orphan_group_unblocks_queue(self):
        # t1＝挂死步：直接子进程即刻退出、孙进程 sleep 持 stdout 管道
        # （wedge 形态）；runner 自身预算 60s/硬顶 240s——看门狗 1.0s 先触发。
        env1 = self._seed("t1", "sleep 300 & echo $! > grandchild.pid; exit 0")
        self._seed("t2", "true")  # t2＝健康步：排在 wedged t1 之后
        start = time.time()  # 审计事件 ts＝墙钟，同源比对
        server = make_server(self.app, gate_deadline=1.0)  # 注入秒级硬顶
        try:
            # (c) 队列续行：t2 走完整门（env+集成 verify 绿→merged→closed）
            self.assertTrue(
                _wait_until(lambda: self.app.state.get("t2")["state"]
                            == "closed"),
                "wedged t1 未解卡：t2 从未走完门")
            # (b) t1 终局 blocked＋delivery error=gate_overrun
            t1 = self.app.state.get("t1")
            self.assertEqual(t1["state"], "blocked")
            self.assertEqual(t1["delivery"]["status"], "failed")
            self.assertEqual(t1["delivery"]["error"], "gate_overrun")
            events = [e["event"] for e in self.app.store.read_events("t1")]
            self.assertIn("gate_overrun", events)
            # (a) 看门狗在注入 deadline 量级触发（runner 硬顶 240s 不可达）
            overrun = next(e for e in self.app.store.read_events("t1")
                           if e["event"] == "gate_overrun")
            self.assertLess(overrun["ts"] - start, 15)
            # (d) 孙进程被进程组杀净（孤儿已死，无 300s 残留）
            pidfile = Path(env1) / "grandchild.pid"
            self.assertTrue(_wait_until(pidfile.exists, timeout=10))
            self.grandchild = int(pidfile.read_text().strip())
            self.assertTrue(_wait_until(
                lambda: not _proc_alive(self.grandchild), timeout=10),
                f"孙进程 {self.grandchild} 未被进程组收杀")
            self.assertIn("merged", [e["event"] for e
                                     in self.app.store.read_events("t2")])
        finally:
            server.server_close()

    def test_gate_crashed_audited_and_queue_survives(self):
        """门体崩溃（队列毒丸＝不存在的任务）：gate_crashed 审计＋消费线程存活。"""
        server = make_server(self.app, gate_deadline=1.0)
        try:
            server.RequestHandlerClass.gate_q.put("t-nope")
            self.assertTrue(_wait_until(
                lambda: any(e["event"] == "gate_crashed"
                            for e in self.app.store.read_events("t-nope"))))
            # 消费线程存活：再投一枚毒丸仍被处置
            server.RequestHandlerClass.gate_q.put("t-nope2")
            self.assertTrue(_wait_until(
                lambda: any(e["event"] == "gate_crashed"
                            for e in self.app.store.read_events("t-nope2"))))
        finally:
            server.server_close()


class TestGitPhaseOverrun(unittest.TestCase):
    """R1 blocker 回归（ag-9c4cbb 评审）：门体挂在 gitx 阶段（_git 形态）被
    看门狗收杀——run() 异常路径让位，终局 blocked/gate_overrun，绝不误弹
    reopened 弹回 worker。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.router = Router(self.store, self.state, FakeOc())
        self.tracker = SpawnTracker()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_phase_kill_routes_blocked_not_reopened(self):
        class HangingCleanGitx:
            """ensure_clean 挂在 tracked 子进程组上（_git 阶段 wedge 确定性
            替身）：直接子进程退出、孙进程持管道；看门狗杀组解链后 raise。"""

            def __init__(self, tracker):
                self._tracker = tracker

            def contains(self, repo_dir, commit, branch=None,
                         fetch_timeout=None):
                return False

            def clean_ignore(self, repo_dir, env_value=""):
                return []

            def ensure_clean(self, repo_dir, exempt=()):
                # start_new_session 镜像 _git 真实契约（P0-2）：pid 即 pgid，
                # 看门狗 killpg 才收得到孙进程
                proc = subprocess.Popen(
                    ["bash", "-c", "sleep 300 & exit 0"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)
                self._tracker.add(proc.pid)
                try:
                    proc.communicate()  # 孙进程持管道：直到看门狗杀组
                finally:
                    self._tracker.discard(proc.pid)
                raise RuntimeError("git status: killed by watchdog")

            def remove_integration(self, integration_dir, target_dir):
                pass

        self.state.add("t1", {
            "title": "t1", "goal": "g", "project_dir": "/repo",
            "env_dir": str(self.tmp / "env"), "after": None,
            "target_branch": "master",
            "verify": {"cmd": "true", "state": "registered",
                       "registered_by": "disp1", "registered_at": 1.0,
                       "timeout": 5},
            "quick": False, "state": "running", "dispatcher_sid": "disp1",
            "worker_sid": "s-w1", "chain_depth": 1, "fail_streak": 0,
            "created": 0.0, "updated": 0.0})
        self.state.transition("t1", "done", commit="a" * 40, rebased_from=None)
        gate = Gate(self.state, HangingCleanGitx(self.tracker), self.router,
                    FakeOc(), lambda env: {"head": "a" * 40, "clean": True},
                    FakeHealth(), Auth(self.store), self.store,
                    spawn_tracker=self.tracker)
        gate.run_guarded("t1", deadline=0.5)
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")  # 绝不 reopened 弹回 worker
        self.assertEqual(t["delivery"]["error"], "gate_overrun")
        events = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("gate_overrun", events)
        self.assertNotIn("reopened", events)


class TestSpawnTracker(unittest.TestCase):
    def test_kill_all_marks_and_kills_registered_group(self):
        tracker = SpawnTracker()
        tracker.activate()
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        tracker.add(proc.pid)
        tracker.kill_all()
        self.assertTrue(tracker.killed())
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGKILL)  # 整组 SIGKILL

    def test_foreign_thread_spawn_not_registered(self):
        """非门线程（HTTP 并发 _git）不入册——看门狗不误杀门外进程。"""
        tracker = SpawnTracker()
        tracker.activate()
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        threading.Thread(target=tracker.add, args=(proc.pid,)).start()
        time.sleep(0.2)
        tracker.kill_all()
        time.sleep(0.2)
        self.assertIsNone(proc.poll())  # 门外的 sleep 不被收杀
        proc.kill()
        proc.wait()

    def test_killed_tracker_maps_runner_to_124(self):
        """看门狗已收杀：runner 让位 124 分类（不伪装 rc=0/质量败）。"""
        tracker = SpawnTracker()
        tracker.activate()
        tracker.kill_all()  # 模拟看门狗先于进程退出触发
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = _subprocess_runner(tmp, "printf ok", timeout=5,
                                         ceiling=10, tracker=tracker)
        self.assertEqual(rc, 124)
        self.assertIn("看门狗", out)


if __name__ == "__main__":
    unittest.main()
