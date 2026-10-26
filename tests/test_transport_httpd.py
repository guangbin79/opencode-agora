"""T11 transport/httpd＋main 装配契约：真 HTTP 全链/鉴权/后台门/健康定时/携带补齐。

真 agorad HTTPServer（urllib 客户端打 127.0.0.1）；StubOpencode 走真 HTTP；
gitx/read_anchor/runner 假注入（main.assemble 单一装配路径，R2）。
携带项（装配期补齐）：gate 尾 cancel 凭据作废＋终止通知、dispatch 凭据
直投失败落库兜底、enforce 端点、anchors 默认实现。
"""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agorad.access import anchors
from agorad.main import assemble, start_health_thread
from agorad.transport.httpd import make_server
from tests.stub_opencode import VERSION, StubOpencode


def ok_runner(cwd, cmd):
    return 0, "pass"


def fail_runner(cwd, cmd):
    return 1, "boom"


class FakeGitx:
    def __init__(self):
        self.removed = []

    def build_env(self, project_dir, tid):
        return f"/envs/{tid}"

    def remove_env(self, env_dir):
        self.removed.append(env_dir)

    def merge_ff(self, env_dir, target):
        return "ok"

    def head(self, repo_dir):
        return "aaa111"

    def rollback(self, target, before_sha):
        pass

    def touches_gate(self, env_dir, patterns):
        return False


class DeadInjectOc:
    """create 成功但 inject 一律拒——凭据直投失败路径。"""

    def create(self, cwd, boot):
        return "sw1"

    def inject(self, sid, text):
        return False

    def alive(self, sid):
        return False

    def probe(self):
        return {"ok": True, "oc_version": "fake"}


def call(port, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agora-Token"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            if r.headers.get("Content-Type", "").startswith("application/json"):
                return r.status, json.loads(raw or b"{}")
            return r.status, raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


class HttpdTest(unittest.TestCase):
    """真服务夹具：StubOpencode＋假 gitx/锚/runner，root 牌 sid=main。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = StubOpencode()
        self.stub.start()
        self.app = assemble(self.tmp, oc_url=self.stub.base_url,
                            gitx=FakeGitx(),
                            read_anchor=lambda env: {"head": "abc123",
                                                     "clean": True},
                            runner=ok_runner)
        self.root = self.app.auth.issue("main", "root")
        self.httpd = make_server(self.app)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def create_task(self):
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo"}, self.root)
        self.assertEqual(st, 200)
        return r["task"]

    def dispatch_task(self, tid):
        st, r = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": "true"}, self.root)
        self.assertEqual(st, 200)
        st, r = call(self.port, "POST", "/task_dispatch", {"task": tid},
                     self.root)
        self.assertEqual(st, 200)
        return r["token"]


class TestHttpSmoke(HttpdTest):
    def test_full_chain_create_dispatch_done_gate_close(self):  # Req: F2/F4/F7
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        self.assertEqual(self.stub.sessions[wsid]["injects"],
                         [f"[agora] credential {tid}\ntoken: {wtok}"])
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": "abc123"}, wtok)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "closed"))
        t = self.app.state.get(tid)
        self.assertEqual(t["commit"], "abc123")  # 证据指针落行
        self.assertEqual(self.app.gitx.removed, [f"/envs/{tid}"])  # 环境回收
        self.assertIn("merged", self.stub.sessions[wsid]["injects"][-1])
        st, r = call(self.port, "POST", "/board", {}, self.root)
        self.assertIn(tid, r["buckets"]["已合并"])
        self.assertEqual(r["chain"], [tid])
        st, r = call(self.port, "POST", "/board", {"task": tid}, self.root)
        self.assertEqual(r["done_body"], "abc123")  # 结论取自消息库 done 正文
        st, html_text = call(self.port, "GET", "/view", token=self.root)
        self.assertEqual(st, 200)
        self.assertIn("<html>", html_text)
        st, r = call(self.port, "GET", "/health", token=self.root)
        self.assertTrue(r["ok"])
        self.assertTrue(r["enforce_on"])

    def test_missing_or_bad_token_401(self):  # Req: F1
        st, _ = call(self.port, "POST", "/board", {})
        self.assertEqual(st, 401)
        st, _ = call(self.port, "POST", "/board", {}, token="junk")
        self.assertEqual(st, 401)

    def test_worker_enforce_denied_403_and_audited(self):  # Req: F1
        wtok = self.app.auth.issue("w9", "worker")
        st, r = call(self.port, "POST", "/enforce", {"on": False}, wtok)
        self.assertEqual(st, 403)
        self.assertTrue(self.app.auth.enforce_on())  # 旗标未被拨动
        ev = self.app.store.read_events()
        self.assertTrue(any(e["event"] == "auth_denied"
                            and e.get("tool") == "enforce" and e["actor"] == "w9"
                            for e in ev))

    def test_get_view_health_authed(self):  # Req: F1
        st, _ = call(self.port, "GET", "/view")
        self.assertEqual(st, 401)
        st, _ = call(self.port, "GET", "/health")
        self.assertEqual(st, 401)
        st, _ = call(self.port, "GET", "/view", token="junk")
        self.assertEqual(st, 401)
        wtok = self.app.auth.issue("w8", "worker")
        st, html_text = call(self.port, "GET", "/view", token=wtok)
        self.assertEqual(st, 200)  # 查询面三角色皆可（裁定）
        self.assertIn("<html>", html_text)
        st, r = call(self.port, "GET", "/health", token=wtok)
        self.assertEqual(st, 200)
        self.assertIn("enforce_on", r)

    def test_enforce_non_bool_on_rejected(self):  # Req: F1
        st, _ = call(self.port, "POST", "/enforce", {"on": "false"}, self.root)
        self.assertEqual(st, 400)  # 字符串 "false" 不得反转成 True
        st, _ = call(self.port, "POST", "/enforce", {"on": 1}, self.root)
        self.assertEqual(st, 400)
        self.assertTrue(self.app.auth.enforce_on())  # 旗标未被拨动
        self.assertEqual([e for e in self.app.store.read_events()
                          if e["event"].startswith("enforce")], [])  # 无审计行

    def test_root_enforce_toggles_and_audits(self):  # Req: F1
        st, r = call(self.port, "POST", "/enforce", {"on": False}, self.root)
        self.assertEqual(st, 200)
        self.assertFalse(r["enforce_on"])
        st, r = call(self.port, "GET", "/health", token=self.root)
        self.assertFalse(r["enforce_on"])
        st, r = call(self.port, "POST", "/enforce", {"on": True}, self.root)
        self.assertTrue(r["enforce_on"])
        evs = [e["event"] for e in self.app.store.read_events()]
        self.assertIn("enforce_off", evs)
        self.assertIn("enforce_on", evs)

    def test_ten_posts_reach_live_worker_in_order(self):  # Req: F4
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        for i in range(10):
            st, r = call(self.port, "POST", "/post",
                         {"task": tid, "kind": "note", "body": f"m{i}"},
                         self.root)
            self.assertEqual(st, 200)
        expect = [f"[agora] task {tid} note from main\nm{i}" for i in range(10)]
        self.assertEqual(self.stub.sessions[wsid]["injects"][-10:], expect)

    def test_wiki_write_read_and_guards(self):  # Req: F1
        st, r = call(self.port, "POST", "/wiki_write",
                     {"path": "notes/a.md", "text": "hello"}, self.root)
        self.assertEqual(st, 200)
        st, r = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/a.md"}, self.root)
        self.assertEqual(r["text"], "hello")
        st, _ = call(self.port, "POST", "/wiki_write",
                     {"path": "tasks/x.md", "text": "no"}, self.root)
        self.assertEqual(st, 403)  # 任务页前缀服务独占
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "../secret"}, self.root)
        self.assertEqual(st, 400)  # 越出 wiki 根
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/none.md"}, self.root)
        self.assertEqual(st, 404)

    def test_doctor_endpoint_view(self):  # Req: F10
        self.create_task()
        st, r = call(self.port, "POST", "/doctor", {}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(len(r["age_rank"]), 1)
        self.assertEqual(r["env"], [])


class TestGateTailCancel(unittest.TestCase):
    """携带项 a：gate 尾 cancelled 分支＝T7 直路径同形（凭据作废＋终止通知）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=DeadInjectOc(), gitx=FakeGitx(),
                            read_anchor=lambda env: {"head": "abc123",
                                                     "clean": True},
                            runner=fail_runner)
        self.root = self.app.auth.issue("main", "root")
        self.app.state.add("t1", {
            "title": "t1", "goal": "g", "project_dir": "/repo",
            "env_dir": "/envs/t1", "after": None,
            "verify": {"cmd": "pytest -q", "state": "registered",
                       "registered_by": "main", "registered_at": 1.0},
            "state": "running", "dispatcher_sid": "main", "worker_sid": "w1",
            "chain_depth": 1, "fail_streak": 0, "created": 0.0, "updated": 0.0})
        self.wtok = self.app.auth.issue("w1", "worker")
        self.assertEqual(self.app.gate.accept_done("t1", "w1", "abc123"),
                         "accepted")
        self.app.task.cancel("t1", self.root)  # done 态→pending_cancel 排队

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gate_tail_cancel_revokes_worker_token(self):  # Req: F11
        self.app.gate.run("t1")
        self.assertEqual(self.app.state.get("t1")["state"], "cancelled")
        self.assertIsNone(self.app.auth.identify(self.wtok))  # 门尾凭据作废

    def test_gate_tail_cancel_notifies_worker(self):  # Req: F11
        self.app.gate.run("t1")
        msgs = [m for m in self.app.store.read_messages()
                if m["to"] == "w1" and m["kind"] == "cancel"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("t1", msgs[0]["body"])


class TestCredentialFallback(unittest.TestCase):
    """携带项 b：dispatch 凭据直投失败→落消息库（check_mail 可取回）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=DeadInjectOc(), gitx=FakeGitx(),
                            read_anchor=lambda env: None, runner=ok_runner)
        self.root = self.app.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dispatch_credential_inject_fail_lands_in_library(self):  # Req: F3
        tid = self.app.task.create("g", "/repo", None, self.root)
        tok = self.app.task.dispatch(tid, self.root)
        self.assertEqual(self.app.state.get(tid)["state"], "dispatched")
        msgs = [m for m in self.app.store.read_messages()
                if m["kind"] == "credential"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["to"], "sw1")
        self.assertIn(f"token: {tok}", msgs[0]["body"])
        got = self.app.router.check_mail("sw1")  # worker 补拉可取回
        self.assertEqual([m["body"] for m in got], [msgs[0]["body"]])


class TestHealthThread(unittest.TestCase):
    """R10：health 定时线程按间隔 tick（死 worker 自愈）＋probe 记 oc_version。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = StubOpencode()
        self.stub.start()
        self.app = assemble(self.tmp, oc_url=self.stub.base_url,
                            gitx=FakeGitx(),
                            read_anchor=lambda env: None, runner=ok_runner)
        self.app.state.add("t9", {
            "title": "t9", "goal": "g", "project_dir": "/repo",
            "env_dir": "/envs/t9", "after": None,
            "verify": {"cmd": "pytest -q", "state": "registered",
                       "registered_by": "main", "registered_at": 1.0},
            "state": "dispatched", "dispatcher_sid": "main",
            "worker_sid": None, "chain_depth": 1, "fail_streak": 0,
            "created": 0.0, "updated": 0.0})

    def tearDown(self):
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_short_interval_ticks_reinject_and_probe(self):  # Req: F5
        dead = self.app.oc.create("/x", "boot")
        self.stub.kill(dead)
        self.app.state.set_fields("t9", worker_sid=dead)
        start_health_thread(self.app, 0.05)
        self.assertTrue(wait_until(
            lambda: self.app.state.get("t9")["worker_sid"] != dead))
        new = self.app.state.get("t9")["worker_sid"]
        self.assertTrue(self.stub.sessions[new]["alive"])
        self.assertTrue(any(e["event"] == "reinjected"
                            for e in self.app.store.read_events("t9")))
        self.assertTrue(wait_until(lambda: (
            self.app.store.read_ephemeral("service.json") or {})
            .get("oc_version") == VERSION))


class TestAnchors(unittest.TestCase):
    """携带项 d：read_anchor access 默认实现——锚＝env 分支 HEAD 即绿。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_anchor_matches_env_head(self):  # Req: F7
        repo = self.tmp / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(
            ["git", *a], cwd=repo, check=True, capture_output=True, text=True)
        git("init", "-q")
        git("-c", "user.email=a@b", "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "x")
        sha = git("rev-parse", "HEAD").stdout.strip()
        self.assertIsNone(anchors.read_anchor(str(repo)))  # 缺锚文件
        marker = repo / ".review-loop" / ".last-reviewed"
        marker.parent.mkdir()
        marker.write_text(sha, encoding="utf-8")
        self.assertEqual(anchors.read_anchor(str(repo)),
                         {"head": sha, "clean": True})
        marker.write_text("old", encoding="utf-8")  # 锚落后
        self.assertIsNone(anchors.read_anchor(str(repo)))
        self.assertIsNone(anchors.read_anchor(None))  # 未派发无环境


if __name__ == "__main__":
    unittest.main()
