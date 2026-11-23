"""F130 verify_infra 宽限兜底契约：驻留 done 登记宽限计时（缺省 1800s）→
dispatcher 失效零动作到点＝health tick 服务自动重排恰一次（reverify 同款
gate_q＋审计 verify_infra_backstop/actor=health）→重排后再败 infra 转
blocked（blocked_reason=verify_infra_backstop_exhausted＋升级触发）；owner
活着且动作（reverify/worker 心跳/转 in_gate/dispatch/cancel）即撤销计时；
awaiting_external 永不触发；quality 败路径零改动；AGORA_VERIFY_INFRA_GRACE
坏值启动 fail-fast（F36 口径）。判例：ag-aa8798 rc=137 驻留 13h 死锁。
"""
import os
import queue
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.health import Health
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import TaskService
from agorad.core.verify_hygiene import find_verify_hygiene_issues
from agorad.main import assemble, verify_infra_grace_from_env
from tests.test_core_gate import SHA, FakeRunner, TestCoreGate
from tests.test_core_health import FakeRecover
from tests.test_core_task import FakeGitx, FakeOc


class TestBackstopGate(TestCoreGate):
    """门侧：宽限登记＋exhausted 终局＋quality 零改动。Req: F130"""

    def disp_msgs(self):
        return [m["body"] for m in self.store.read_messages()
                if m["to"] == "disp1"]

    def test_infra_fail_registers_grace(self):  # Req: F130 ①前置
        self.runner = FakeRunner((-9, "Killed"))
        self.mk_gate()
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留语义不变（F122 零回归）
        self.assertEqual(t["fail_streak"], 0)
        g = t["verify_infra_grace"]
        now = time.time()
        self.assertGreaterEqual(g["deadline"], now + 1795)  # 缺省 1800s 宽限
        self.assertLessEqual(g["since"], now)

    def test_backstop_exhausted_blocks(self):  # Req: F130 ④
        self.runner = FakeRunner((137, "Killed"), (137, "Killed again"))
        self.mk_gate()
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")  # 首败：驻留 done＋登记宽限
        # health 兜底已出手（计时撤＋标记落）后的重排再败
        self.state.set_fields("t1", verify_infra_grace=None,
                              verify_infra_backstop=time.time())
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(t["blocked_reason"],
                         "verify_infra_backstop_exhausted")
        self.assertEqual(t["fail_streak"], 0)  # 非质量败不计连败
        self.assertIn(("verify-infra", "t1"), self.health.calls)  # 升级触发
        evs = [e["event"] for e in self.store.read_events("t1")]
        self.assertEqual(evs.count("verify_infra_failed"), 2)
        self.assertTrue(any("blocked" in b for b in self.disp_msgs()))

    def test_content_fail_no_grace_registered(self):  # Req: F130 ⑤
        self.runner = FakeRunner((1, "boom"))
        self.mk_gate()
        self.seed(tb="main")
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "verify_failed")  # quality 语义零改动
        self.assertEqual(t["fail_streak"], 1)
        self.assertFalse(t.get("verify_infra_grace"))


class TestBackstopHealth(unittest.TestCase):
    """health tick 挂点：到点重排恰一次/各撤销面/awaiting_external 永不。Req: F130"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.gitx, self.oc = FakeGitx(), FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.svc = TaskService(self.state, self.auth, self.gitx, self.oc,
                               self.store, self.router, write_task_page,
                               verify_hygiene=find_verify_hygiene_issues)
        self.root = self.auth.issue("main", "root")
        self.q = queue.Queue()
        self.recover = FakeRecover()
        self.app = App(store=self.store, state=self.state, auth=self.auth,
                       router=self.router, task=self.svc, peer=None, gate=None,
                       health=None, recover=self.recover, report=None,
                       oc=self.oc, gitx=self.gitx, main_sid="main",
                       gate_q=self.q)
        self.health = Health(self.state, self.oc, self.router,
                             self.recover, self.app, store=self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid="t1", grace=None, delivery=None):
        now = time.time()
        row = {"title": tid, "goal": "g", "project_dir": "/repo",
               "env_dir": "/envs/t1",
               "verify": {"cmd": "pytest -q", "state": "registered",
                          "registered_by": "disp1", "registered_at": 1.0},
               "state": "done", "dispatcher_sid": "disp1",
               "worker_sid": None, "chain_depth": 1,
               "fail_streak": 0, "created": now, "updated": now,
               "delivery": delivery}
        if grace is not None:
            row["verify_infra_grace"] = grace
        self.state.add(tid, row)
        return tid

    def backstop_events(self):
        return [e for e in self.store.read_events("t1")
                if e["event"] == "verify_infra_backstop"]

    # ---- 断言清单 ----

    def test_expired_grace_requeues_exactly_once(self):  # Req: F130 ①
        self.seed(grace={"deadline": time.time() - 1, "since": time.time()})
        self.health.tick()
        self.assertEqual(self.q.get_nowait(), "t1")  # gate_q 收到
        evs = self.backstop_events()
        self.assertEqual(len(evs), 1)  # 审计行恰一条
        self.assertEqual(evs[0]["actor"], "health")
        t = self.state.get("t1")
        self.assertFalse(t.get("verify_infra_grace"))  # 计时已撤
        self.assertTrue(t["verify_infra_backstop"])  # 兜底已用标记
        self.health.tick()  # 到点后重复 tick 不再重排（恰一次）
        self.assertTrue(self.q.empty())
        self.assertEqual(len(self.backstop_events()), 1)

    def test_unexpired_grace_no_action(self):  # Req: F130 ①
        self.seed(grace={"deadline": time.time() + 1800, "since": time.time()})
        self.health.tick()
        self.assertTrue(self.q.empty())
        self.assertEqual(self.backstop_events(), [])

    def test_reverify_cancels_grace(self):  # Req: F130 ②
        self.seed(grace={"deadline": time.time() - 1, "since": 0.0})
        self.state.set_fields("t1", verify_infra_backstop=1.0)  # 标记一并清
        self.assertEqual(self.svc.reverify("t1", self.root), {
            "ok": True, "requeued": True})
        t = self.state.get("t1")
        self.assertFalse(t["verify_infra_grace"])
        self.assertFalse(t["verify_infra_backstop"])
        self.health.tick()  # 到点也不再自动重排
        self.assertTrue(self.q.empty())
        self.assertEqual(self.backstop_events(), [])

    def test_worker_activity_cancels_grace(self):  # Req: F130 ②
        self.seed(grace={"deadline": time.time() - 1, "since": 100.0})
        self.state.touch_seen("t1")  # 宽限窗内 worker 心跳
        self.health.tick()
        self.assertFalse(self.state.get("t1").get("verify_infra_grace"))  # 撤销
        self.assertTrue(self.q.empty())

    def test_backstop_marker_recheck_guard(self):  # Req: F130 R2（B册/OCR 同发现 TOCTOU）
        now = time.time()
        self.seed(grace={"deadline": now - 1, "since": 0.0})
        self.state.set_fields("t1", verify_infra_grace=None)  # 窗内 reverify 已撤
        self.assertFalse(self.state.mark_infra_backstop("t1"))  # 免投队
        self.assertFalse(self.state.get("t1").get("verify_infra_backstop"))
        self.seed(grace={"deadline": now - 1, "since": 0.0})  # 重新武装
        self.state.set_fields("t1", in_gate=True)  # 窗内已入门
        self.assertFalse(self.state.mark_infra_backstop("t1"))
        self.seed(grace={"deadline": now - 1, "since": 0.0})  # 回驻留态
        self.assertTrue(self.state.mark_infra_backstop("t1"))
        t = self.state.get("t1")
        self.assertFalse(t.get("verify_infra_grace"))
        self.assertTrue(t["verify_infra_backstop"])

    def test_mark_backstop_heartbeat_face(self):  # Req: F130 R3（OCR：mark 漏心跳面）
        now = time.time()
        self.seed(grace={"deadline": now - 1, "since": 100.0})
        self.state.touch_seen("t1")  # 快照到落账窗内 worker 心跳＝owner 活着
        self.assertFalse(self.state.mark_infra_backstop("t1"))
        self.assertFalse(self.state.get("t1").get("verify_infra_backstop"))

    def test_clear_grace_recheck_guard(self):  # Req: F130 R3（OCR：心跳清除误伤新周期）
        now = time.time()
        self.seed(grace={"deadline": now - 1, "since": 5.0})
        self.assertTrue(self.state.clear_infra_grace_if("t1", 5.0))
        self.assertFalse(self.state.get("t1").get("verify_infra_grace"))
        self.seed(grace={"deadline": now - 1, "since": 9.0})  # 窗内已换新宽限
        self.assertFalse(self.state.clear_infra_grace_if("t1", 5.0))  # 陈旧不清
        self.assertTrue(self.state.get("t1")["verify_infra_grace"])

    def test_backstop_audit_failure_still_requeues(self):  # Req: F130 R3（OCR：审计败吞投队）
        self.seed(grace={"deadline": time.time() - 1, "since": time.time()})
        orig = self.store.append_event

        def boom(ev):
            if ev.get("event") == "verify_infra_backstop":
                raise OSError("disk full")
            orig(ev)
        self.store.append_event = boom
        try:
            self.health.tick()  # 审计败（F54 尽力而为）不吞唯一一次投队
        finally:
            self.store.append_event = orig
        self.assertEqual(self.q.get_nowait(), "t1")
        self.assertTrue(self.state.get("t1")["verify_infra_backstop"])

    def test_awaiting_external_never_triggers(self):  # Req: F130 ③
        self.seed(grace={"deadline": time.time() - 1, "since": 0.0},
                  delivery={"mode": "external_branch",
                            "status": "awaiting_external"})
        self.health.tick()
        self.health.tick()
        self.assertTrue(self.q.empty())
        self.assertEqual(self.backstop_events(), [])
        self.assertTrue(self.state.get("t1")["verify_infra_grace"])  # 不动计时


class TestInfraGraceConfig(unittest.TestCase):
    """AGORA_VERIFY_INFRA_GRACE：缺省/env 覆盖/坏值 fail-fast＋装配注入。Req: F130 ⑥"""

    def test_default_and_valid(self):  # Req: F130
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(verify_infra_grace_from_env(), 1800.0)
        with mock.patch.dict(os.environ, {"AGORA_VERIFY_INFRA_GRACE": "90"},
                             clear=True):
            self.assertEqual(verify_infra_grace_from_env(), 90.0)

    def test_invalid_fail_fast(self):  # Req: F130
        for raw in ("", "abc", "0", "-1", "nan", "inf"):
            with self.subTest(raw=raw), \
                 mock.patch.dict(os.environ,
                                 {"AGORA_VERIFY_INFRA_GRACE": raw}, clear=True):
                with self.assertRaises(ValueError) as cm:
                    verify_infra_grace_from_env()
                self.assertIn("AGORA_VERIFY_INFRA_GRACE", str(cm.exception))

    def test_assemble_wires_infra_grace(self):  # Req: F130
        with tempfile.TemporaryDirectory() as tmp:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw",
                           verify_infra_grace=90.0)
            self.assertEqual(app.gate._infra_grace, 90.0)


if __name__ == "__main__":
    unittest.main()
