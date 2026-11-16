"""P1a 语义（ag-d68faf 事故动机）：F107 base 探针 red-on-127＋reverify 动词。

探针面：base 阶段命令死（rc 126/127）＝判别红放行（缺文件形态——任务自建
验收脚本在起点 base 不存在）；env 阶段 F81/F82 分类零变化（真死命令兜底）。
reverify 面：done 驻留任务（verify-dead 停车后）派发方/root 重排入门队列＋
审计 reverify；非 done 拒；worker 角色永拒（403＋审计，F37 先例）。
"""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from agorad.main import assemble
from agorad.transport.httpd import make_server
from tests.test_agora_wait import mkhome, run_cli
from tests.test_core_gate import OTHER_SHA, SHA, FakeRunner, TestCoreGate
from tests.test_transport_httpd import (DeadInjectOc, FakeGitx, call, ok_runner,
                                        wait_until)


class TestProbeRedOnDead(TestCoreGate):
    """ag-d68faf 修订：base 阶段 126/127＝判别红（探针死不再驻留路由）。"""

    def probe_in_gate(self, streak=0):
        self.seed(streak=streak)
        self.state.set_fields("t1", env_base=OTHER_SHA)
        self.assertEqual(self.gate.accept_done("t1", "s-w1", SHA), "accepted")

    def events(self):
        return [e["event"] for e in self.store.read_events("t1")]

    def test_base_probe_127_is_red_and_passes(self):
        """缺文件形态（verify 引用任务随后才建的脚本）＝红：门流程照常走完。"""
        self.runner = FakeRunner(
            (127, "tests/foo.test.sh: not found"), (0, "pass"), (0, "pass"))
        self.mk_gate()
        self.probe_in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")  # 红放行：门流程零变化
        self.assertEqual(t["fail_streak"], 0)  # 全绿清连败（R12）：探针死红不 bump
        self.assertEqual(t["probe"]["result"], "red")  # 死红照缓存
        self.assertIn("base_probe_red", self.events())
        self.assertNotIn("base_probe_dead", self.events())
        self.assertNotIn(("verify-dead", "t1"), self.health.calls)

    def test_base_probe_126_is_red(self):
        self.runner = FakeRunner((126, "permission denied"),
                                 (0, "pass"), (0, "pass"))
        self.mk_gate()
        self.probe_in_gate()
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "closed")
        self.assertIn("base_probe_red", self.events())

    def test_base_alive_red_rc1_unchanged(self):
        """活红 rc=1 语义不变：判别红放行（既有 F107 行为锚）。"""
        self.runner = FakeRunner((1, "1 failed"), (0, "pass"), (0, "pass"))
        self.mk_gate()
        self.probe_in_gate()
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "closed")
        self.assertEqual(t["probe"]["result"], "red")

    def test_env_stage_127_still_verify_dead(self):
        """env 阶段命令死＝F81/F82 分类不变：驻留 done＋escalate，不计连败。"""
        self.runner = FakeRunner((1, "base red"), (127, "dead"))
        self.mk_gate()
        self.probe_in_gate(streak=1)
        self.gate.run("t1")
        t = self.state.get("t1")
        self.assertEqual(t["state"], "done")  # 驻留：不 transition
        self.assertEqual(t["fail_streak"], 1)
        self.assertIn(("verify-dead", "t1"), self.health.calls)
        self.assertIn("verify_dead", self.events())


class TestReverify(unittest.TestCase):
    """/reverify HTTP 面：done 驻留重排入门队列＋审计；非 done 拒；worker 403。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=DeadInjectOc(), gitx=FakeGitx(self.tmp),
                            read_anchor=lambda env: {"head": SHA, "clean": True},
                            runner=ok_runner)
        self.root = self.app.auth.issue("main", "root")
        self.httpd = make_server(self.app)  # 空账本装配：启动零重排
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, state="done"):
        self.app.state.add("t1", {
            "title": "t1", "goal": "g", "project_dir": "/repo",
            "env_dir": self.app.gitx.build_env("/repo", "t1"), "after": None,
            "verify": {"cmd": "pytest -q", "state": "registered",
                       "registered_by": "main", "registered_at": 1.0},
            "state": state, "commit": SHA, "in_gate": False,
            "dispatcher_sid": "main", "worker_sid": "w1",
            "chain_depth": 1, "fail_streak": 0, "created": 0.0,
            "updated": 0.0})

    def test_reverify_done_requeues_and_audits(self):
        self.seed("done")
        st, r = call(self.port, "POST", "/reverify", {"task": "t1"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"ok": True, "requeued": True})
        self.assertIn("reverify",
                      [e["event"] for e in self.app.store.read_events("t1")])
        # 重排实证：后台门队列消费后任务走完全链终态
        self.assertTrue(wait_until(
            lambda: self.app.state.get("t1")["state"] == "closed"))

    def test_reverify_non_done_rejected(self):
        self.seed("todo")
        st, r = call(self.port, "POST", "/reverify", {"task": "t1"}, self.root)
        self.assertEqual(st, 403)
        self.assertIn("done", r["error"])
        self.assertEqual(self.app.state.get("t1")["state"], "todo")  # 零改写
        self.assertNotIn("reverify",
                         [e["event"] for e in self.app.store.read_events("t1")])

    def test_reverify_external_parked_rejected(self):
        """R1 评审（B 册/OCR 同发现）：done＋awaiting_external 是外部交付稳态
        驻留（门体直通不重验）——重排是谎报 no-op，须拒并指路 task_deliver。"""
        self.seed("done")
        self.app.state.set_fields(
            "t1", delivery={"mode": "external_branch",
                            "status": "awaiting_external",
                            "commit": SHA, "base": "c" * 40, "attempt": 1})
        st, r = call(self.port, "POST", "/reverify", {"task": "t1"}, self.root)
        self.assertEqual(st, 403)
        self.assertIn("task_deliver", r["error"])
        t = self.app.state.get("t1")  # 零改写零审计
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["delivery"]["status"], "awaiting_external")
        self.assertNotIn("reverify",
                         [e["event"] for e in self.app.store.read_events("t1")])

    def test_reverify_worker_denied_with_audit(self):
        self.seed("done")
        wtok = self.app.auth.issue("w1", "worker")
        st, r = call(self.port, "POST", "/reverify", {"task": "t1"}, wtok)
        self.assertEqual(st, 403)  # F37 同口径：角色永拒
        hits = [e for e in self.app.store.read_events()
                if e["event"] == "auth_denied" and e.get("tool") == "reverify"]
        self.assertTrue(hits)  # 拒绝落审计行


class TestReverifyCLI(unittest.TestCase):
    """agora reverify <tid>：薄代理 /reverify（root 牌），退出码 bash 可判定。"""

    def _srv(self, code=200, resp=None):
        import http.server
        import json as _json
        seen = {"paths": [], "bodies": [], "tokens": []}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(
                    int(self.headers.get("Content-Length", 0)))
                seen["paths"].append(self.path.strip("/"))
                seen["bodies"].append(_json.loads(raw or b"{}"))
                seen["tokens"].append(self.headers.get("X-Agora-Token", ""))
                data = (_json.dumps(resp or {"ok": True, "requeued": True})
                        .encode() if code == 200 else b"")
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, seen

    def test_posts_reverify_with_root_token(self):
        srv, seen = self._srv()
        try:
            home = mkhome(srv.server_address[1], token="rtok")
            p = run_cli(["reverify", "ag-123"], home)
            self.assertEqual(p.returncode, 0)
            self.assertIn("requeued", p.stdout)
            self.assertEqual(seen["paths"], ["reverify"])
            self.assertEqual(seen["bodies"], [{"task": "ag-123"}])
            self.assertEqual(seen["tokens"], ["rtok"])
            shutil.rmtree(home, ignore_errors=True)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_rejection_is_exit_two(self):
        srv, _ = self._srv(code=403)
        try:
            home = mkhome(srv.server_address[1])
            p = run_cli(["reverify", "ag-123"], home)
            self.assertEqual(p.returncode, 2)  # 非 done/角色拒＝终局异常
            shutil.rmtree(home, ignore_errors=True)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
