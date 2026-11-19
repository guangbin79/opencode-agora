"""Req: F124 运行中任务受控等待（task_wait/waiting_on）契约。

真 Store/State/Auth/Router/Health＋假 oc（注入记录）；播种行经 State.add
（dispatch 全链归 test_transport_httpd/test_mcp_shell——本套聚焦等待域语义）。
stalled 免判单一过滤点＝report.stale_candidates（health stalled/doctor/
草稿候选同口径，本套测试注明，不另建第二套免判）。
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core import report
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.health import Health
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import Rejected, TaskService


class FakeOc:
    def __init__(self):
        self.injected, self.silent_injected, self.notify_injected = [], [], []
        self.liveness, self.time_of = {}, {}

    def session(self, sid):
        if not self.liveness.get(sid):
            return None
        return ({"time": self.time_of[sid]} if sid in self.time_of else {})

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True

    def inject_silent(self, sid, text):
        self.silent_injected.append((sid, text))
        return True

    def inject_notify(self, sid, text):
        self.notify_injected.append((sid, text))
        return True


class FakeRecover:
    def __init__(self):
        self.calls = []

    def reinject(self, tid, expect_wsid=None):
        self.calls.append((tid, expect_wsid))
        return True


class TestTaskWaiting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.recover = FakeRecover()
        self.app = App(store=self.store, state=self.state, auth=self.auth,
                       router=self.router, task=None, peer=None, gate=None,
                       health=None, recover=self.recover, report=None,
                       oc=self.oc, gitx=None, main_sid="main")
        self.health = Health(self.state, self.oc, self.router, self.recover,
                             self.app, store=self.store,
                             stale_candidates=report.stale_candidates)
        self.svc = TaskService(self.state, self.auth, gitx=None, oc=self.oc,
                               store=self.store, router=self.router,
                               pages=write_task_page)
        self.root = self.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid, state="running", disp="disp1", worker=None):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": None, "after": None,
                             "verify": {"cmd": "true", "state": "registered",
                                        "registered_by": disp,
                                        "registered_at": 1.0},
                             "state": state, "dispatcher_sid": disp,
                             "worker_sid": worker, "chain_depth": 1,
                             "waiting_on": None,  # Req: F124 同生产行形状
                             "fail_streak": 0, "last_seen": None,
                             "created": time.time(), "updated": 0.0})
        return tid

    def events(self, tid=None, name=None):
        f = self.tmp / "events.jsonl"
        rows = ([json.loads(x) for x in f.read_text().splitlines() if x]
                if f.exists() else [])  # 零事件用例不落 events.jsonl（Store 惰性建）
        return [e for e in rows
                if (tid is None or e.get("task") == tid)
                and (name is None or e.get("event") == name)]

    def msgs(self, **kw):
        return [m for m in self.store.read_messages()
                if all(m.get(k) == v for k, v in kw.items())]

    # ---- A. 登记：state 仍 running＋审计 wait_set ----

    def test_wait_set_keeps_running_state_and_audits(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        r = self.svc.wait("t-a", self.root, "t-dep")
        self.assertEqual(r, {"ok": True, "waiting_on": "t-dep"})
        t = self.state.get("t-a")
        self.assertEqual(t["waiting_on"], "t-dep")
        self.assertEqual(t["state"], "running")  # 受控等待非生命周期状态
        ev = self.events("t-a", "wait_set")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["dependency"], "t-dep")
        self.assertEqual(ev[0]["actor"], "main")

    # ---- B. 清除 ----

    def test_wait_clear_audits(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        self.svc.wait("t-a", self.root, "t-dep")
        r = self.svc.wait("t-a", self.root, "")  # 空串清除（人工恢复）
        self.assertEqual(r, {"ok": True, "waiting_on": None})
        self.assertIsNone(self.state.get("t-a")["waiting_on"])
        self.assertEqual(len(self.events("t-a", "wait_cleared")), 1)

    # ---- C. 权限：worker 全局拒；非 owner dispatcher 拒；owner/root 允许 ----

    def test_worker_and_foreign_dispatcher_rejected(self):  # Req: F124
        self.seed("t-a", disp="disp1", worker="s-a")
        wtok = self.auth.issue("s-a", "worker")
        with self.assertRaises(Rejected):
            self.svc.wait("t-a", wtok, "t-dep")  # worker 需等待→post(question)
        other = self.auth.issue("disp2", "dispatcher")
        with self.assertRaises(Rejected):
            self.svc.wait("t-a", other, "t-dep")  # 他 dispatcher 归属拒
        self.assertEqual(len(self.events("t-a", "auth_denied")), 2)
        own = self.auth.issue("disp1", "dispatcher")
        self.seed("t-dep", worker="s-dep")
        self.assertTrue(self.svc.wait("t-a", own, "t-dep")["ok"])  # owner 允许

    # ---- D. 非 running 目标／依赖不存在／自身 ----

    def test_non_running_target_rejected(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        for st in ("todo", "dispatched", "done", "blocked", "merged"):
            self.seed(f"t-{st}", state=st, worker="s-x")
            with self.assertRaises(Rejected):
                self.svc.wait(f"t-{st}", self.root, "t-dep")

    def test_missing_dependency_and_self_rejected(self):  # Req: F124
        self.seed("t-a", worker="s-a")
        with self.assertRaises(Rejected):
            self.svc.wait("t-a", self.root, "t-nope")
        with self.assertRaises(Rejected):
            self.svc.wait("t-a", self.root, "t-a")
        self.assertIsNone(self.state.get("t-a").get("waiting_on"))

    # ---- F. 依赖 merged：tick 清等待＋双通知＋审计 ----

    def test_tick_resolves_merged_dependency_with_notifications(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        self.state.set_fields("t-a", waiting_on="t-dep")
        self.state.transition("t-dep", "done", commit="c" * 40)
        self.state.transition("t-dep", "merged")
        self.oc.liveness["s-a"] = True  # 等待任务 worker 在案：tick 无误报
        self.health.tick()
        self.assertIsNone(self.state.get("t-a")["waiting_on"])
        ev = self.events("t-a", "wait_resolved")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["dependency"], "t-dep")
        self.assertIn("dependency_state=merged", ev[0]["note"])
        w = self.msgs(task="t-a", kind="process")  # worker process 通知
        self.assertEqual(len(w), 1)
        self.assertIn("t-dep", w[0]["body"])
        self.assertIn("merged", w[0]["body"])
        d = self.msgs(task="t-a", kind="result", to="disp1")  # dispatcher 简报
        self.assertEqual(len(d), 1)
        # 幂等：再 tick 零重复
        self.health.tick()
        self.assertEqual(len(self.events("t-a", "wait_resolved")), 1)
        self.assertEqual(len(self.msgs(task="t-a", kind="process")), 1)

    # ---- G. 依赖 cancelled/blocked：同样清除并通知需处置 ----

    def test_tick_resolves_cancelled_dependency_as_needs_attention(self):  # Req: F124
        self.seed("t-a", worker="s-a")
        self.oc.liveness["s-a"] = True
        for st in ("cancelled", "blocked"):
            dep = f"t-{st}"
            self.seed(dep, worker=f"s-{st}")
            self.state.set_fields("t-a", waiting_on=dep)
            if st == "cancelled":
                self.state.transition(dep, "cancelled")
            else:
                self.state.transition(dep, "done", commit="c" * 40)
                self.state.transition(dep, "blocked")  # done→blocked 合法边
            self.health.tick()
            self.assertIsNone(self.state.get("t-a")["waiting_on"])
            body = self.msgs(task="t-a", kind="process")[-1]["body"]
            self.assertIn("需处置", body)

    # ---- H. 等待期间 stalled 免判；解除后恢复候选（单一过滤点） ----

    def test_waiting_task_exempt_from_stale_candidates(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        old = time.time() - 3000  # 远超 _STALE_AFTER：无等待必候选
        self.state.set_fields("t-a", created=old)
        self.assertIn("t-a", [c for c, _ in report.stale_candidates(
            self.state.snapshot(), time.time())])
        self.state.set_fields("t-a", waiting_on="t-dep")
        # 单一过滤点＝stale_candidates：health stalled 免判/doctor/草稿候选
        # 同口径（本测试注明——doctor_view 直接复用同一函数）
        self.assertNotIn("t-a", [c for c, _ in report.stale_candidates(
            self.state.snapshot(), time.time())])
        doc = report.doctor_view(self.state.snapshot(), self.store.home)
        self.assertNotIn("t-a", [c for c, _ in doc["stale_candidates"]])
        self.state.set_fields("t-a", waiting_on=None)  # 解除后恢复候选
        self.assertIn("t-a", [c for c, _ in report.stale_candidates(
            self.state.snapshot(), time.time())])

    # ---- I. 登记时依赖已 terminal：立即 resolved，不落无效等待 ----

    def test_register_with_terminal_dependency_resolves_immediately(self):  # Req: F124
        self.seed("t-dep", state="merged", worker="s-dep")
        self.seed("t-a", worker="s-a")
        r = self.svc.wait("t-a", self.root, "t-dep")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["dependency_state"], "merged")
        self.assertIsNone(self.state.get("t-a")["waiting_on"])
        self.assertFalse(self.events("t-a", "wait_set"))  # 不制造无效等待
        self.assertEqual(len(self.events("t-a", "wait_resolved")), 1)
        self.assertEqual(len(self.msgs(task="t-a", kind="process")), 1)
        self.assertEqual(len(self.msgs(task="t-a", kind="result")), 1)

    # ---- A7. 任务页显示 waiting_on 与依赖状态 ----

    def test_task_page_shows_waiting_on_with_dependency_state(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        self.svc.wait("t-a", self.root, "t-dep")
        text = (self.tmp / "wiki" / "tasks" / "t-a.md").read_text(encoding="utf-8")
        self.assertIn("waiting_on: t-dep", text)

    # ---- 评审环 round1：竞态 CAS＋残留范围 ----

    def test_clear_waiting_on_if_cas_guards_concurrent_reregister(self):  # Req: F124
        self.seed("t-a", worker="s-a")
        self.state.set_fields("t-a", waiting_on="t-d1")
        self.assertFalse(self.state.clear_waiting_on_if("t-a", "t-d2"))
        self.assertEqual(self.state.get("t-a")["waiting_on"], "t-d1")  # 换登记不抹
        self.assertTrue(self.state.clear_waiting_on_if("t-a", "t-d1"))
        self.assertIsNone(self.state.get("t-a")["waiting_on"])

    def test_terminal_waiter_keeps_waiting_on_as_history_silent(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        self.state.set_fields("t-a", waiting_on="t-dep")
        self.state.transition("t-a", "done", commit="c" * 40)
        self.state.transition("t-a", "merged")
        self.state.transition("t-dep", "cancelled")
        self.oc.liveness["s-a"] = True
        self.health.tick()  # 等待任务自身已终态：留史不解析、不通知
        self.assertEqual(self.state.get("t-a")["waiting_on"], "t-dep")
        self.assertEqual(self.events("t-a", "wait_resolved"), [])
        self.assertEqual(self.msgs(task="t-a", kind="process"), [])
        self.assertEqual(self.msgs(task="t-a", kind="result"), [])

    def test_waiting_residue_off_running_does_not_extend_exemption(self):  # Req: F124
        self.seed("t-dep", worker="s-dep")
        self.seed("t-a", worker="s-a")
        old = time.time() - 3000
        self.state.set_fields("t-a", created=old, waiting_on="t-dep")
        self.state.transition("t-a", "done", commit="c" * 40)
        self.state.transition("t-a", "verify_failed")  # 离开等待语义
        self.assertIn("t-a", [c for c, _ in report.stale_candidates(
            self.state.snapshot(), time.time())])  # 残留不扩大免判


if __name__ == "__main__":
    unittest.main()
