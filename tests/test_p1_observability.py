"""P1b 可观测性（ag-b49329 排队假僵＋ag-9e055d 门楔死＋merged 未出版盲区）。

①stalled 分型：done＋非门内＋他任务占门＝queued 不拍人；done＋门内以阶段
审计为活性基线，N 分钟（缺省 20，可注入）无阶段进展＝门内僵拍人带当前门
阶段；fresh/busy_round 判读零变化。
②门阶段审计：verify/base_probe/integration/publish 事件随门运行发出
（managed 交付 attempt 随行）。
③origin 出版投影：refs-only target_ref/origin_ref/ahead＋as of last fetch。
④BOOT git identity 提示行在位。
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.gitx import Gitx
from agorad.access.store import Store
from agorad.core import report
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.health import KIND_STALLED, Health
from agorad.core.msg import Router
from agorad.core.state import State
from tests.test_core_gate import (LOCAL_BASE, MANAGED_DELIVERY, OTHER_SHA,
                                  SHA, FakeRunner, TestCoreGate)
from tests.test_core_health import FakeOc, FakeRecover
from tests.test_core_task import TestCoreTask


class TestStalledTyping(unittest.TestCase):
    """P1b stalled 分型：queued（排队）与 in-gate（门内僵）两新口径。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.recover = FakeRecover()
        self.app = App(store=self.store, state=self.state,
                       auth=Auth(self.store), router=self.router, task=None,
                       peer=None, gate=None, health=None, recover=self.recover,
                       report=None, oc=self.oc, gitx=None, main_sid="main")
        self.mk_health()

    def mk_health(self, **kw):
        self.health = Health(self.state, self.oc, self.router,
                             self.recover, self.app, store=self.store,
                             stale_candidates=report.stale_candidates, **kw)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, tid, state="running", worker="s-w1"):  # 镜像 TestCoreHealth.seed
        env = self.tmp / "envs" / tid
        env.mkdir(parents=True, exist_ok=True)
        os.utime(env, (0.0, 0.0))  # 在盘且 mtime 钉旧：不虚增活性（F15）
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": str(env),
                             "verify": {"cmd": "pytest -q", "state": "registered",
                                        "registered_by": "main",
                                        "registered_at": 1.0},
                             "state": state, "dispatcher_sid": "main",
                             "worker_sid": worker, "chain_depth": 1,
                             "fail_streak": 0, "last_seen": None,
                             "created": time.time(), "updated": 0.0})
        return tid

    def main_msgs(self):
        return [m for m in self.store.read_messages() if m["to"] == "main"]

    def phase_event(self, tid, name, age=0.0):
        self.store.append_event({"ts": time.time() - age, "actor": "gate",
                                 "event": f"phase_{name}", "task": tid})

    def test_done_queued_behind_busy_gate_not_stale(self):  # P1b ag-b49329
        """done＋非门内＋他任务占门＝串行门排队：旧口径 20min 必假拍，新口径零拍。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", created=0.0)  # 基线超阈：旧口径 stalled 候选
        self.seed("t2", state="done", worker="s-w2")
        self.state.set_fields("t2", in_gate=True)
        self.phase_event("t2", "verify")  # 占门任务阶段进展新鲜
        self.oc.liveness["s-w1"] = True
        self.oc.liveness["s-w2"] = True
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])  # queued≠僵

    def test_in_gate_no_phase_progress_escalates_with_phase(self):  # P1b ag-9e055d
        """done＋门内＋阶段审计超阈无进展＝门内僵：拍人正文带当前门阶段。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True, created=0.0)  # 入门久于事件龄（R1 起基线取新）
        self.phase_event("t1", "verify", age=1300)  # 超 20min 无阶段迁移
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        bodies = [m["body"] for m in self.main_msgs()]
        self.assertEqual(sum("失控[stalled]" in b for b in bodies), 1)
        self.assertIn("门阶段 verify", bodies[0])
        self.assertEqual(self.oc.inject_calls, [])  # 门侧楔死：不注 worker 自检

    def test_requeued_gate_entry_window_not_false_alarmed(self):  # R1 ocr#2
        """重排再入门窗：旧轮 phase 事件在案、新轮首事件未发（last_seen 新鲜
        ——刚 re-done）→不以旧轮事件误判门内僵。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True, created=0.0,
                              last_seen=time.time() - 60)
        self.phase_event("t1", "publish", age=1300)  # 上一轮门的末次阶段事件
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])

    def test_in_gate_recent_phase_progress_quiet(self):
        """门内但阶段事件新鲜（verify 还在合理窗口内跑）＝正常，零拍。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True)
        self.phase_event("t1", "verify")
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])

    def test_gate_stale_after_injectable(self):
        """门内无进展阈值可注入：60s 阈下 70s 旧事件即拍（缺省 1200 不拍）。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True, created=0.0)
        self.phase_event("t1", "integration", age=70)
        self.oc.liveness["s-w1"] = True
        self.mk_health(gate_stale_after=60)
        self.health.tick()
        self.assertIn("门阶段 integration",
                      next(m["body"] for m in self.main_msgs()))

    def test_in_gate_without_phase_events_falls_back_to_baseline(self):
        """无 phase 事件（升级前在飞/崩窗残留）：活性基线回落 last_seen/created。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True, created=time.time())
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])  # 基线新鲜：静默
        self.state.set_fields("t1", created=0.0)  # 基线超阈：拍（含门挂死残窗）
        self.health._stale_seen.clear()  # 前轮未入集：清边沿记忆重判
        self.health.tick()
        self.assertEqual(sum("失控[stalled]" in m["body"]
                             for m in self.main_msgs()), 1)

    def test_stale_candidates_excludes_queued_done_only(self):
        """口径单点：全量快照下 done＋门忙＝排除；门闲＝照候选（真滞留）。"""
        now = time.time()
        t1 = {"state": "done", "created": 0.0, "last_seen": None,
              "env_dir": None}
        t2 = {"state": "done", "in_gate": True, "created": 0.0,
              "last_seen": None, "env_dir": None}
        snap = {"tasks": {"t1": t1, "t2": dict(t2)}}
        self.assertEqual(report.stale_candidates(snap, now), [])  # t2 占门：t1 排队排除
        snap["tasks"]["t2"]["in_gate"] = False  # 门闲：两行 done 滞留均为候选
        self.assertEqual([c[0] for c in report.stale_candidates(snap, now)],
                         ["t1", "t2"])

    def test_detail_classifies_queued_consistent_with_health(self):  # R1 ocr#3
        """detail 排队判读与 health/doctor 同口径：全量 snap 判门占，标 'queued'
        ——不出现 detail 报 stale_candidate 而 health 不拍的自相矛盾。"""
        self.seed("t1", state="done")
        self.state.set_fields("t1", created=0.0)  # 基线超阈（旧口径必 stale）
        self.seed("t2", state="done", worker="s-w2")
        self.state.set_fields("t2", in_gate=True)
        out = report.detail(self.state.snapshot(), self.store, "t1")
        self.assertEqual(out["classification"], "queued")

    def test_classify_fresh_busy_round_unchanged(self):
        """fresh/busy_round 判读零变化（P1b 只动 done 排队/门内两口）。"""
        now = time.time()
        fresh = {"state": "running", "worker_sid": "w",
                 "last_seen": now - 60}
        busy = {"state": "running", "worker_sid": "w", "last_seen": None}
        term = {"state": "merged", "worker_sid": "w", "last_seen": None}
        self.assertEqual(report.classify("t", fresh, set(), now), "fresh")
        self.assertEqual(report.classify("t", busy, set(), now), "busy_round")
        self.assertIsNone(report.classify("t", term, set(), now))


class TestPhaseAudits(TestCoreGate):
    """P1b 门阶段审计：phase_* 事件随门运行发出（ag-9e055d 免人工 forensics）。"""

    def phase_events(self):
        return [e["event"] for e in self.store.read_events("t1")
                if e["event"].startswith("phase_")]

    def phase(self, name):
        return next(e for e in self.store.read_events("t1")
                    if e["event"] == f"phase_{name}")

    def test_legacy_run_emits_four_phase_events_in_order(self):
        self.seed(tb="main")
        self.state.set_fields("t1", env_base=OTHER_SHA)
        self.runner = FakeRunner((1, "base red"), (0, "pass"), (0, "pass"))
        self.mk_gate()
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.phase_events(),
                         ["phase_base_probe", "phase_verify",
                          "phase_integration", "phase_publish"])

    def test_managed_delivery_phase_events_carry_attempt(self):
        self.integration = str(self.env) + "__gate"
        self.gx.local_sha_result = LOCAL_BASE
        self.seed(tb="main")
        self.state.set_fields("t1", delivery=dict(MANAGED_DELIVERY))
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")
        self.gx.head_shas[self.integration] = SHA  # ff 直通：integration 头＝env 头
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertEqual(self.phase_events(),
                         ["phase_verify", "phase_integration", "phase_publish"])
        self.assertEqual(self.phase("integration")["attempt"], 1)
        self.assertEqual(self.phase("publish")["attempt"], 1)


class TestPublication(unittest.TestCase):
    """P1b origin 出版投影：refs-only 零网络（merged≠已出版 origin 的观测面）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

        def git(*args):
            subprocess.run(["git", *args], cwd=self.repo, check=True,
                           capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (self.repo / "f").write_text("1")
        git("add", "f")
        git("commit", "-qm", "c1")
        self.base_sha = self.rev("HEAD")
        git("update-ref", "refs/remotes/origin/main", self.base_sha)  # 假 origin 跟踪 ref
        (self.repo / "f").write_text("2")
        git("add", "f")
        git("commit", "-qm", "c2")
        self.head_sha = self.rev("HEAD")
        self.gitx = Gitx()

    def rev(self, ref):
        return subprocess.run(["git", "rev-parse", ref], cwd=self.repo,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publication_fields_with_fake_origin_ref(self):
        pub = self.gitx.publication(str(self.repo))  # branch 缺省＝symref 解析
        self.assertEqual(pub["target_ref"], self.head_sha)
        self.assertEqual(pub["origin_ref"], self.base_sha)
        self.assertEqual(pub["ahead"], 1)  # rev-list origin..local
        self.assertEqual(pub["note"], "as of last fetch")

    def test_publication_without_tracking_ref(self):
        subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"],
                       cwd=self.repo, check=True)
        pub = self.gitx.publication(str(self.repo), "main")
        self.assertEqual(pub["target_ref"], self.head_sha)
        self.assertIsNone(pub["origin_ref"])
        self.assertEqual(pub["ahead"], 0)

    def test_publication_missing_local_branch_raises(self):  # R1 ocr#1
        """本地缺支投影无意义：如实抛（detail except 省略字段，不出 ahead=0 假象）。"""
        with self.assertRaises(RuntimeError):
            self.gitx.publication(str(self.repo), "nope")

    def test_detail_carries_publication_for_merged_only(self):
        home = self.tmp / "home"
        store = Store(home)
        state = State(store)
        row = {"title": "t", "goal": "g", "project_dir": str(self.repo),
               "env_dir": None, "verify": {"cmd": "true",
                                           "state": "registered"},
               "dispatcher_sid": "main", "worker_sid": "w", "chain_depth": 1,
               "fail_streak": 0, "created": 0.0, "updated": 0.0}
        state.add("t1", dict(row, state="merged", target_branch="main"))
        out = report.detail(state.snapshot(), store, "t1",
                            publication=self.gitx.publication)
        self.assertEqual(out["publication"]["ahead"], 1)
        state.add("t2", dict(row, state="running", target_branch="main"))
        out2 = report.detail(state.snapshot(), store, "t2",
                             publication=self.gitx.publication)
        self.assertNotIn("publication", out2)  # 非终态行不带投影


class TestBootGitHint(TestCoreTask):
    """P1b BOOT git identity 提示：mcp-shell 拒 git config/-c，指路 plumbing。"""

    def test_boot_carries_git_plumbing_hint(self):
        self.dispatched_pair()
        boot = self.oc.sessions[-1][2]
        self.assertIn("git identity", boot)
        self.assertIn("write-tree", boot)
        self.assertIn("hash-object -t commit -w", boot)
        self.assertIn("update-ref", boot)


if __name__ == "__main__":
    unittest.main()
