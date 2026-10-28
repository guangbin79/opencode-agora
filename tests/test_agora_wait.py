"""F14 agora CLI wait 子命令契约：阻塞等待任务终态（看门狗规范条件谓词）。

extensionless 脚本经 SourceFileLoader 载入直测状态分类/取数/轮询缝；
壳与 --help 走子进程真连 stub agorad（test_board_tui 同法）。
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_loader = importlib.machinery.SourceFileLoader("agora_cli", str(ROOT / "agora"))
_spec = importlib.util.spec_from_loader("agora_cli", _loader)
agora = importlib.util.module_from_spec(_spec)
_loader.exec_module(agora)


def _stub(states, code=200, commit=None):
    """按请求数走 states 序列的 /board 单任务视图；耗尽后停在末态。"""
    count = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if code != 200:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            state = states[min(count["n"], len(states) - 1)]
            count["n"] += 1
            task = {"state": state}
            if commit:
                task["commit"] = commit
            data = json.dumps({"task": task, "events": [],
                               "done_body": None}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H, count


class _Srv:
    def __init__(self, states, code=200, commit=None):
        h, count = _stub(states, code, commit)
        self.count = count
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def _dead_port():  # Req: F14
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def mkhome(port, token="tok"):  # Req: F14
    home = Path(tempfile.mkdtemp())
    (home / ".agora").mkdir(parents=True)
    (home / ".agora" / "service.json").write_text(json.dumps({"port": port}))
    (home / ".agora" / "root.token").write_text(token + "\n")  # token=""＝牌缺失
    return home


def run_cli(args, home):  # Req: F14
    env = dict(os.environ, AGORA_HOME=str(home))
    return subprocess.run([sys.executable, str(ROOT / "agora"), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=60)


class TestClassify(unittest.TestCase):
    def test_terminal_four_vs_inflight_six(self):  # Req: F14
        for s in ("merged", "closed", "blocked", "cancelled"):
            self.assertEqual(agora.classify(s), 0, s)
        for s in ("todo", "dispatched", "running", "done",
                  "verify_failed", "reopened"):
            self.assertEqual(agora.classify(s), 1, s)


class TestFetch(unittest.TestCase):
    def test_returns_state_and_commit(self):  # Req: F14
        srv = _Srv(["merged"], commit="a" * 40)
        try:
            self.assertEqual(agora.fetch(srv.base, "tok", "t1"),
                             ("merged", "a" * 40))
            srv2 = _Srv(["running"])
            try:
                self.assertEqual(agora.fetch(srv2.base, "tok", "t1"),
                                 ("running", None))
            finally:
                srv2.stop()
        finally:
            srv.stop()

    def test_404_is_fatal(self):  # Req: F14
        srv = _Srv(["running"], code=404)
        try:
            with self.assertRaises(agora.Fail) as cm:
                agora.fetch(srv.base, "tok", "gt-none")
            self.assertIn("404", str(cm.exception))
        finally:
            srv.stop()


class TestWait(unittest.TestCase):
    def _fake(self):  # Req: F14  假时钟：pause 只推进不真睡
        t = [0.0]
        return (lambda: t[0]), (lambda dt: t.__setitem__(0, t[0] + dt))

    def test_polls_until_terminal_then_zero(self):  # Req: F14
        srv = _Srv(["running", "running", "merged"], commit="b" * 40)
        clock, pause = self._fake()
        try:
            self.assertEqual(
                agora.wait("t1", srv.base, "tok", timeout=100,
                           clock=clock, pause=pause),
                (0, "merged @" + "b" * 12))
        finally:
            srv.stop()

    def test_terminal_without_commit_prints_state_only(self):  # Req: F14
        srv = _Srv(["blocked"])
        clock, pause = self._fake()
        try:
            self.assertEqual(agora.wait("t1", srv.base, "tok",
                                        clock=clock, pause=pause),
                             (0, "blocked"))
        finally:
            srv.stop()

    def test_timeout_expiry_still_inflight_is_one(self):  # Req: F14
        srv = _Srv(["running"])
        clock, pause = self._fake()
        try:
            self.assertEqual(
                agora.wait("t1", srv.base, "tok", timeout=30,
                           clock=clock, pause=pause),
                (1, "in-flight: running"))
        finally:
            srv.stop()

    def test_once_single_query(self):  # Req: F14
        srv = _Srv(["running"])
        clock, pause = self._fake()
        try:
            self.assertEqual(agora.wait("t1", srv.base, "tok", once=True,
                                        clock=clock, pause=pause),
                             (1, "in-flight: running"))
            self.assertEqual(srv.count["n"], 1)  # 单查不轮询
            srv2 = _Srv(["cancelled"])
            try:
                self.assertEqual(agora.wait("t1", srv2.base, "tok", once=True,
                                            clock=clock, pause=pause),
                                 (0, "cancelled"))
            finally:
                srv2.stop()
        finally:
            srv.stop()

    def test_transient_retry_exhausts_to_fail(self):  # Req: F14
        clock, pause = self._fake()
        with self.assertRaises(agora.Fail):
            agora.wait("t1", f"http://127.0.0.1:{_dead_port()}", "tok",
                       timeout=30, clock=clock, pause=pause)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.homes = []

    def tearDown(self):
        for h in self.homes:
            shutil.rmtree(h, ignore_errors=True)

    def home(self, srv, token="tok"):  # Req: F14
        port = srv.srv.server_address[1] if srv else _dead_port()
        h = mkhome(port, token)
        self.homes.append(h)
        return h

    def test_terminal_states_exit_zero(self):  # Req: F14
        cases = [("merged", "a" * 40, "merged @" + "a" * 12),
                 ("closed", None, "closed"),
                 ("blocked", None, "blocked"),
                 ("cancelled", None, "cancelled")]
        for state, commit, want in cases:
            srv = _Srv([state], commit=commit)
            try:
                p = run_cli(["wait", "t1"], self.home(srv))
            finally:
                srv.stop()
            self.assertEqual((p.returncode, p.stdout.strip()), (0, want), state)

    def test_once_inflight_exit_one(self):  # Req: F14
        srv = _Srv(["running"])
        try:
            p = run_cli(["wait", "t1", "--once"], self.home(srv))
        finally:
            srv.stop()
        self.assertEqual((p.returncode, p.stdout.strip()),
                         (1, "in-flight: running"))

    def test_timeout_expiry_exit_one(self):  # Req: F14
        srv = _Srv(["running"])
        try:
            p = run_cli(["wait", "t1", "--timeout", "1"], self.home(srv))
        finally:
            srv.stop()
        self.assertEqual((p.returncode, p.stdout.strip()),
                         (1, "in-flight: running"))

    def test_404_exit_two(self):  # Req: F14
        srv = _Srv(["running"], code=404)
        try:
            p = run_cli(["wait", "gt-none", "--once"], self.home(srv))
        finally:
            srv.stop()
        self.assertEqual(p.returncode, 2)
        self.assertIn("404", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_unreachable_retries_then_exit_two(self):  # Req: F14
        p = run_cli(["wait", "t1", "--timeout", "1"], self.home(None))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")

    def test_missing_token_exit_two(self):  # Req: F14
        srv = _Srv(["running"])
        try:
            p = run_cli(["wait", "t1", "--once"], self.home(srv, token=""))
        finally:
            srv.stop()
        self.assertEqual(p.returncode, 2)
        self.assertIn("牌", p.stderr)

    def test_usage_errors_exit_two(self):  # Req: F14
        p = run_cli([], self.home(None))
        self.assertEqual(p.returncode, 2)
        p = run_cli(["wait"], self.home(None))
        self.assertEqual(p.returncode, 2)
        p = run_cli(["wait", "t1", "--timeout", "abc"], self.home(None))
        self.assertEqual(p.returncode, 2)

    def test_help_usage_and_exit_code_table(self):  # Req: F14
        p = run_cli(["wait", "--help"], self.home(None))
        self.assertEqual(p.returncode, 0)
        for frag in ("agora wait", "task-id", "--timeout", "--once",
                     "退出码", "非 MCP agora.wait(seconds)"):
            self.assertIn(frag, p.stdout, frag)


class TestMcpSchema(unittest.TestCase):
    def test_wait_description_disambiguates_cli(self):  # Req: F14
        import agora_mcp
        tools = {t["name"]: t for t in agora_mcp._TOOLS}
        self.assertIn("agora wait", tools["wait"]["description"])


if __name__ == "__main__":
    unittest.main()
