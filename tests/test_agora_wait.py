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
import time
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


def _detail_stub(task, stale=(), code=200):
    """F28 status 双端点 stub：POST /board 单任务视图＋/doctor（僵态候选）。"""

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
            if self.path.strip("/") == "board":
                data = json.dumps({"task": task, "events": [],
                                   "done_body": None})
            else:
                data = json.dumps({"age_rank": [], "last_seen_rank": [],
                                   "stale_candidates": [[t, 0.0] for t in stale],
                                   "env": []})
            raw = data.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return H


class _DetailSrv:
    def __init__(self, task, stale=(), code=200):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0),
                                          _detail_stub(task, stale, code))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    @property
    def port(self):
        return self.srv.server_address[1]

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

    def test_follows_pointer_rotation_on_transient(self):  # Req: F14  换模跟随
        dead = f"http://127.0.0.1:{_dead_port()}"
        srv = _Srv(["merged"], commit="c" * 40)
        try:
            reads = {"n": 0}

            def reread():
                reads["n"] += 1
                return (srv.base, "tok2") if reads["n"] > 1 else (dead, "tok")

            clock, pause = self._fake()
            self.assertEqual(
                agora.wait("t1", dead, "tok", timeout=100,
                           clock=clock, pause=pause, reread=reread),
                (0, "merged @" + "c" * 12))
        finally:
            srv.stop()

    def test_reread_failure_keeps_old_endpoint(self):  # Req: F14  半写/缺失瞬间
        srv = _Srv(["merged"], commit="c" * 40)
        try:
            def broken():
                raise FileNotFoundError("service.json")

            clock, pause = self._fake()
            self.assertEqual(
                agora.wait("t1", srv.base, "tok", timeout=100,
                           clock=clock, pause=pause, reread=broken),
                (0, "merged @" + "c" * 12))
        finally:
            srv.stop()

    def test_reread_incomplete_pair_keeps_old(self):  # Req: F14  空端点/空牌不采纳
        srv = _Srv(["merged"], commit="c" * 40)
        try:
            clock, pause = self._fake()
            self.assertEqual(
                agora.wait("t1", srv.base, "tok", timeout=100,
                           clock=clock, pause=pause,
                           reread=lambda: ("", "")),
                (0, "merged @" + "c" * 12))
        finally:
            srv.stop()

    def test_auth_rejection_absorbed_when_following(self):  # Req: F14  撕裂对窗口
        bad = _Srv(["running"], code=403)
        try:
            good = _Srv(["merged"], commit="e" * 40)
            try:
                reads = {"n": 0}

                def reread():
                    reads["n"] += 1
                    return ((bad.base, "tok") if reads["n"] == 1
                            else (good.base, "tok2"))

                clock, pause = self._fake()
                self.assertEqual(
                    agora.wait("t1", bad.base, "tok", timeout=100,
                               clock=clock, pause=pause, reread=reread),
                    (0, "merged @" + "e" * 12))
            finally:
                good.stop()
        finally:
            bad.stop()

    def test_auth_rejection_fatal_without_reread(self):  # Req: F14  语义保持
        srv = _Srv(["running"], code=403)
        try:
            clock, pause = self._fake()
            with self.assertRaises(agora.Fail) as cm:
                agora.wait("t1", srv.base, "tok", timeout=100,
                           clock=clock, pause=pause)
            self.assertIn("403", str(cm.exception))
        finally:
            srv.stop()

    def test_discover_rejects_torn_pointer(self):  # Req: F14  半写形状不外溢 fetch
        for port, token in [("abc", "tok"), ([], "tok"), (8080, "a\nb"),
                            (8080, "a\rb")]:
            with self.subTest(port=port, token=token):
                home = mkhome(port, token)
                self.addCleanup(shutil.rmtree, home, True)
                with self.assertRaises((ValueError, TypeError)):
                    agora.discover(home)

    def test_reread_type_error_keeps_old_endpoint(self):  # Req: F14  非 dict JSON
        srv = _Srv(["merged"], commit="c" * 40)
        try:
            def nondict():  # service.json 半写成形如 5 / [] 的合法非对象 JSON
                json.loads("5")["port"]

            clock, pause = self._fake()
            self.assertEqual(
                agora.wait("t1", srv.base, "tok", timeout=100,
                           clock=clock, pause=pause, reread=nondict),
                (0, "merged @" + "c" * 12))
        finally:
            srv.stop()

    def test_auth_rejection_head_labels_auth(self):  # Req: F14  排障口径不混可达性
        srv = _Srv(["running"], code=403)
        try:
            clock, pause = self._fake()
            with self.assertRaises(agora.Fail) as cm:
                agora.wait("t1", srv.base, "tok", timeout=30,
                           clock=clock, pause=pause, reread=lambda: (srv.base, "tok"))
            self.assertIn("认牌失败", str(cm.exception))
        finally:
            srv.stop()


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

    def test_follows_service_json_repoint_after_remodel(self):  # Req: F14
        a = _Srv(["running"])
        home = self.home(a)
        a.stop()  # 换模：CLI 首轮 discover 读到的是死端口 A
        b = _Srv(["merged"], commit="d" * 40)
        try:
            def repoint():  # 进程起后才改指新端口——钉住 reread=discover 接线
                time.sleep(1)
                (home / ".agora" / "service.json").write_text(
                    json.dumps({"port": b.srv.server_address[1]}))
            threading.Thread(target=repoint, daemon=True).start()
            p = run_cli(["wait", "t1", "--timeout", "30"], home)
            self.assertEqual((p.returncode, p.stdout.strip()),
                             (0, "merged @" + "d" * 12))
        finally:
            b.stop()

    def test_once_auth_rejected_exit_two(self):  # Req: F14  单查认牌失败仍终局
        srv = _Srv(["running"], code=403)
        try:
            p = run_cli(["wait", "t1", "--once"], self.home(srv))
        finally:
            srv.stop()
        self.assertEqual(p.returncode, 2)
        self.assertIn("403", p.stderr)

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
        self.assertIn("须为非负有限秒数", p.stderr)  # 归一中文报错（非 argparse 英文默认）
        for bad in ("nan", "inf", "-1"):  # nan deadline 比较恒 False＝热轮询
            p = run_cli(["wait", "t1", "--timeout", bad], self.home(None))
            self.assertEqual(p.returncode, 2, bad)

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


class TestStatus(unittest.TestCase):
    """F28 agora status 三态判读：数据 /board＋/doctor，分类客户端推导。"""

    NOW = 1_000_000.0

    @staticmethod
    def _task(state, last_seen=None, worker="s-w1"):
        return {"state": state, "last_seen": last_seen, "worker_sid": worker}

    def test_status_three_classes_and_edges(self):  # Req: F28
        cases = [
            (self._task("running", self.NOW - 60), set(), "fresh"),  # 心跳近
            (self._task("running", self.NOW - 3600), set(), "busy_round"),  # 心跳冷＋非候选
            (self._task("running", self.NOW - 3600), {"t1"}, "stale_candidate"),
            (self._task("running", None), set(), "busy_round"),  # 无心跳＝冷
            (self._task("cancelled", self.NOW - 3600), set(), None),  # 终态无判读
            (self._task("todo", None, worker=None), set(), None),  # 无 worker 无判读
        ]
        for task, stale, want in cases:
            v = agora.status_view("t1", task, stale, self.NOW)
            self.assertEqual(v["class"], want, task["state"])
            self.assertEqual(v["state"], task["state"])

    def test_status_last_seen_ago_seconds(self):  # Req: F28
        v = agora.status_view("t1", self._task("running", self.NOW - 90),
                              set(), self.NOW)
        self.assertEqual(v["last_seen_ago"], 90)

    def test_stale_after_parity_with_server(self):  # Req: F28 客户端锚不漂移
        from agorad.core import report
        self.assertEqual(agora._STALE_AFTER, report._STALE_AFTER)

    def test_status_cli_text_and_json(self):  # Req: F28
        srv = _DetailSrv(self._task("running", time.time() - 60))  # fresh
        home = mkhome(srv.port)
        try:
            p = run_cli(["status", "t1"], home)
            self.assertEqual((p.returncode, p.stdout.strip()),
                             (0, "t1 state=running last_seen=60s class=fresh"))
            p = run_cli(["status", "t1", "--json"], home)
            self.assertEqual(p.returncode, 0)
            v = json.loads(p.stdout)
            self.assertEqual(v, {"task": "t1", "state": "running",
                                 "last_seen_ago": 60, "class": "fresh"})
        finally:
            srv.stop()

    def test_status_cli_busy_and_stale(self):  # Req: F28
        old = time.time() - 3600
        cases = [(_DetailSrv(self._task("running", old)), "busy_round"),
                 (_DetailSrv(self._task("running", old), ["t1"]),
                  "stale_candidate")]
        for srv, want in cases:
            try:
                p = run_cli(["status", "t1"], mkhome(srv.port))
                self.assertEqual(p.returncode, 0)
                self.assertIn(f"class={want}", p.stdout)
            finally:
                srv.stop()

    def test_status_cli_unknown_task_exit_two(self):  # Req: F28
        srv = _DetailSrv(self._task("running"), code=404)
        try:
            p = run_cli(["status", "gt-none"], mkhome(srv.port))
        finally:
            srv.stop()
        self.assertEqual(p.returncode, 2)
        self.assertIn("404", p.stderr)
        self.assertIn("gt-none", p.stderr)  # 报因尾附 tid（镜像 fetch：看门狗可归任务）

    def test_status_cli_unreachable_carries_tid(self):  # Req: F28
        home = mkhome(_dead_port())
        p = run_cli(["status", "gt-lost"], home)
        shutil.rmtree(home, ignore_errors=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("gt-lost", p.stderr)  # 不可达同契约：归任务不悬空


if __name__ == "__main__":
    unittest.main()
