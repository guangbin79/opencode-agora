"""F122 gate 执行产物与机械失败分型契约：ArtifactStore 落盘/清理/权限/防逃逸、
classify_failure 纯分型、门接入（四类 phase 全量 artifact＋signal→verify_infra
驻留 done 不计连败＋任务行/事件/通知可观测＋写败 best-effort）。
"""
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from agorad.core.gate import _TAIL, classify_failure
from agorad.core.gate_artifacts import ArtifactStore
from tests.test_core_gate import OTHER_SHA, SHA, FakeRunner, TestCoreGate


def _metas(root, tid):
    """按创建序（目录名尾缀 seq——全局单调权威序）读回该 tid 全部 meta.json。"""
    out = []
    dirs = [d for d in (Path(root) / tid).iterdir() if (d / "meta.json").is_file()]
    dirs.sort(key=lambda d: d.name.rsplit("-", 1)[-1])
    for d in dirs:
        out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
    return out


class TestArtifactStore(unittest.TestCase):
    """纯存储契约：布局/完整 output/meta 字段/keep 清理/tid 防逃逸/权限。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "gate-artifacts"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_full_output_and_meta_fields(self):  # Req: F122 A1
        store = ArtifactStore(self.root)
        big = "x" * (2 * _TAIL)  # 超 _TAIL：证明完整不截断
        art = store.record("ag-t1", "verify", "/env", "pytest -q", 0, big,
                           1000.0, 1002.5, failure_kind="green")
        d = Path(art["path"])
        self.assertTrue(d.is_dir())
        self.assertEqual((d / "output.log").read_text(encoding="utf-8"), big)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["tid"], "ag-t1")
        self.assertEqual(meta["phase"], "verify")
        self.assertEqual(meta["cmd"], "pytest -q")
        self.assertEqual(meta["cwd"], "/env")
        self.assertEqual(meta["rc"], 0)
        self.assertEqual(meta["started_at"], 1000.0)
        self.assertEqual(meta["ended_at"], 1002.5)
        self.assertEqual(meta["duration_s"], 2.5)
        self.assertEqual(meta["bytes"], len(big.encode("utf-8")))
        self.assertEqual(meta["failure_kind"], "green")
        self.assertIsNone(meta["error"])
        self.assertIsNone(meta["extra"])

    def test_permissions_and_error_extra_roundtrip(self):  # Req: F122 I
        store = ArtifactStore(self.root)
        art = store.record("ag-t1", "review", "/env", "cmd", 137, "killed",
                           1.0, 2.0, error="OSError", extra={"attempt": 1},
                           failure_kind="signal")
        d = Path(art["path"])
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        for f in ("output.log", "meta.json"):
            self.assertEqual(stat.S_IMODE((d / f).stat().st_mode), 0o600)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["error"], "OSError")
        self.assertEqual(meta["extra"], {"attempt": 1})

    def test_keep_prunes_oldest(self):  # Req: F122 H
        store = ArtifactStore(self.root, keep=2)
        for i in range(3):
            store.record("ag-t1", "verify", "/e", "c", 1, f"run{i}",
                         float(i), float(i) + 1,
                         failure_kind="content_failed")
        dirs = sorted((self.root / "ag-t1").iterdir())
        self.assertEqual(len(dirs), 2)
        logs = [(d / "output.log").read_text(encoding="utf-8") for d in dirs]
        self.assertEqual(logs, ["run1", "run2"])  # 最旧清除，保留最新 N

    def test_bad_keep_and_tid_rejected(self):  # Req: F122 I/H
        for bad in (0, -1, "50", True, None):
            with self.assertRaises(ValueError):
                ArtifactStore(self.root, keep=bad)
        store = ArtifactStore(self.root)
        for bad in ("../evil", "a/b", "", "a b", "a.b", None, 3):
            with self.assertRaises(ValueError):
                store.record(bad, "verify", "/e", "c", 0, "", 1.0, 2.0)
        self.assertFalse(self.root.exists())  # 非法 tid 零落盘（防路径逃逸）


class TestClassifyFailure(unittest.TestCase):
    """纯分型单源（gate.py 与 _cmd_dead 同居——core 禁兄弟 import）。"""

    def test_taxonomy(self):  # Req: F122 §4
        self.assertEqual(classify_failure(0), "green")
        self.assertEqual(classify_failure(124), "overrun")
        self.assertEqual(classify_failure(126), "command_dead")
        self.assertEqual(classify_failure(127), "command_dead")
        self.assertEqual(classify_failure(
            2, "sh: 1: x: No such file or directory"), "command_dead")
        self.assertEqual(classify_failure(2, "other"), "content_failed")
        # 文案仅作 rc==2 加严条件：rc=1 带同文案不入死类
        self.assertEqual(classify_failure(1, "No such file or directory"),
                         "content_failed")
        for rc in (129, 134, 137, 139, 165, -9):  # signal 域（137=SIGKILL 语义）
            self.assertEqual(classify_failure(rc), "signal", rc)
        for rc in (1, 128, 166, 255):  # 255 与域外仍归 content
            self.assertEqual(classify_failure(rc), "content_failed", rc)
        self.assertEqual(classify_failure(0, error=RuntimeError("x")),
                         "runner_exception")


class TestGateArtifactFlow(TestCoreGate):
    """门接入：四类 phase 全量 artifact＋signal 分类路由＋可观测三面。Req: F122"""

    def setUp(self):
        super().setUp()
        self.ga_root = self.tmp / "gate-artifacts"
        self.artifacts = ArtifactStore(self.ga_root)
        self.mk_gate(artifact_store=self.artifacts)

    def ga_events(self, name="gate_artifact_written"):
        return [e for e in self.store.read_events("t1") if e["event"] == name]

    def disp_msgs(self):
        return [m["body"] for m in self.store.read_messages()
                if m["to"] == "disp1"]

    def test_green_verify_writes_full_artifacts(self):  # Req: F122 A
        big = "g" * (2 * _TAIL)
        self.runner = FakeRunner((0, big), (0, big))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        metas = _metas(self.ga_root, "t1")
        self.assertEqual([m["phase"] for m in metas],
                         ["verify", "integration_verify"])
        for d in sorted((self.ga_root / "t1").iterdir()):  # 完整不截断
            self.assertEqual((d / "output.log").read_text(encoding="utf-8"),
                             big)
        t = self.state.get("t1")
        self.assertEqual(t["gate_artifact"]["phase"], "integration_verify")
        self.assertEqual(t["gate_artifact"]["failure_kind"], "green")
        for k in ("path", "rc", "duration_s", "bytes"):
            self.assertIn(k, t["gate_artifact"])
        evs = self.ga_events()
        self.assertEqual(len(evs), 2)
        for k in ("task", "phase", "rc", "failure_kind", "bytes", "path"):
            self.assertIn(k, evs[0])

    def test_content_fail_keeps_quality_semantics(self):  # Req: F122 B
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")
        self.assertEqual(t["fail_streak"], 1)
        self.assertEqual(_metas(self.ga_root, "t1")[-1]["failure_kind"],
                         "content_failed")
        self.assertIn("artifact=", self.worker_msgs()[-1]["body"])  # 通知附路径
        self.assertTrue([e for e in self.ga_events()
                         if e["failure_kind"] == "content_failed"])

    def test_signal_routes_verify_infra(self):  # Req: F122 C
        self.runner = FakeRunner((137, "Killed\nOOM"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留 done
        self.assertEqual(t["fail_streak"], 0)  # 不计质量连败
        self.assertIn(("verify-infra", "t1"), self.health.calls)
        evs = [e["event"] for e in self.store.read_events("t1")]
        self.assertIn("verify_infra_failed", evs)
        m = _metas(self.ga_root, "t1")[-1]
        self.assertEqual(m["failure_kind"], "signal")
        self.assertEqual(m["rc"], 137)
        self.assertTrue(any("机械失败" in b and "非质量败" in b
                            for b in self.disp_msgs()))
        self.assertTrue(any("等待通知" in b["body"]
                            for b in self.worker_msgs()))

    def test_overrun_zero_regression(self):  # Req: F122 D
        self.runner = FakeRunner((124, "slow"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["fail_streak"], 0)
        self.assertEqual(_metas(self.ga_root, "t1")[-1]["failure_kind"],
                         "overrun")
        self.assertIn("verify_overrun",
                      [e["event"] for e in self.store.read_events("t1")])

    def test_command_dead_zero_regression(self):  # Req: F122 D
        self.runner = FakeRunner((127, "x: not found"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["fail_streak"], 0)
        self.assertEqual(_metas(self.ga_root, "t1")[-1]["failure_kind"],
                         "command_dead")
        self.assertIn(("verify-dead", "t1"), self.health.calls)

    def test_base_probe_phase_artifact(self):  # Req: F122 E
        self.runner = FakeRunner((1, "red"), (0, "p"), (0, "p"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.state.set_fields("t1", env_base=OTHER_SHA)
        self.anchor = {"head": SHA, "base": OTHER_SHA, "clean": True}
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        phases = [m["phase"] for m in _metas(self.ga_root, "t1")]
        for want in ("base_probe", "verify", "integration_verify"):
            self.assertIn(want, phases)

    def test_review_phase_artifact(self):  # Req: F122 E
        self.runner = FakeRunner((0, "p"), (0, "p"), (0, "p"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.state.set_fields("t1", review_cmd="true", env_base=OTHER_SHA,
                              verify_base_exempt=True)
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        phases = [m["phase"] for m in _metas(self.ga_root, "t1")]
        self.assertIn("review", phases)
        self.assertIn("integration_verify", phases)

    def test_runner_exception_artifact_then_original_path(self):  # F122 F
        class BoomRunner:
            def __call__(self, cwd, cmd, timeout=None, ceiling=None):
                raise RuntimeError("runner blew up")

        self.runner = BoomRunner()
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "reopened")  # 原异常路径
        m = _metas(self.ga_root, "t1")[-1]
        self.assertEqual(m["failure_kind"], "runner_exception")
        self.assertIn("runner blew up", m["error"])

    def test_artifact_write_failure_best_effort(self):  # Req: F122 G
        (self.tmp / "blocker").write_text("not a dir")
        self.artifacts = ArtifactStore(self.tmp / "blocker")
        self.runner = FakeRunner((0, "p"), (0, "p"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")  # 状态机不变
        self.assertTrue(self.ga_events("gate_artifact_write_failed"))
        self.assertEqual(self.ga_events(), [])
        self.assertIsNone(self.state.get("t1").get("gate_artifact"))


if __name__ == "__main__":
    unittest.main()
