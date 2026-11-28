"""Req: F184 — dsh 适配器（mock JSON-RPC 传输）＋harness 分流双代测试。

三段（对齐任务页断言 B1/B2）：
  A. Dsh 适配器本体：_FakeProc（进程内假 JSON-RPC 服务端，注入 spawn 桩）
     驱动真适配器逻辑——握手时序（initialize 应答落地前不得 prompt，RC
     实证口径）/sid 形态/inject 三通道映射（silent 偏差同通道落地）/
     alive/session busy 口径/last_message＋messages 投影/RPC 拒与收口。
  B. HarnessRouter：create 按 harness 路由、注入/探活按 sid 路由、
     model kwarg 兼容（v3 oc 两参面不被 v2 专属 kwarg 打爆）。
  C. 分流双代：v2（assemble＋httpd 真栈＋StubOpencode＋FakeDsh）——
     task_create harness 参数/值域/worker 继承与显式拒/dispatch 路由/
     /cred_for_sid env_dir 容器路径映射（shim 判定链的路径域）；v3
     （TaskService 桩件）——nested DTO harness、worker 继承/拒、路由。
"""
import json
import os
import queue
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agorad.access.dsh import Dsh, DshUnavailable, HarnessRouter  # noqa: E402


# ---- A 段基件：进程内假 JSON-RPC 服务端 ----

class _FakeServer:
    """协议应答器：记录请求序；可注入失败模式与通知流。"""

    def __init__(self, out):
        self.out = out
        self.requests = []          # (method, params) 按到达序
        self.fail_init = False
        self.prompt_error = None

    def handle(self, line):
        frame = json.loads(line)
        rid, method = frame.get("id"), frame.get("method")
        params = frame.get("params") or {}
        self.requests.append((method, params))
        if method == "initialize":
            if self.fail_init:
                self.out.push({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32603, "message": "boom"}})
            else:
                self.out.push({"jsonrpc": "2.0", "id": rid, "result": {
                    "serverInfo": {"name": "deepseek-harness-sdk-runtime",
                                   "version": "0.0.1"}}})
        elif method == "session/prompt":
            if self.prompt_error is not None:
                self.out.push({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32603, "message": self.prompt_error}})
            else:
                self.out.push({"jsonrpc": "2.0", "id": rid,
                               "result": {"messageId": f"m-{rid}"}})
        elif method == "shutdown":
            self.out.push({"jsonrpc": "2.0", "id": rid, "result": {}})

    def emit_status(self, sid, status):
        self.out.push({"jsonrpc": "2.0", "method": "session.status",
                       "params": {"sessionId": sid, "status": status}})

    def emit_event(self, sid, ev):
        self.out.push({"jsonrpc": "2.0", "method": "session.event",
                       "params": {"sessionId": sid, "event": ev}})


class _FakeStdout:
    """行源：服务端推帧入队，Dsh 读者线程迭代消费；close 收尾。"""

    def __init__(self):
        self._q = queue.Queue()

    def push(self, frame):
        self._q.put(json.dumps(frame) + "\n")

    def close(self):
        self._q.put(None)

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item


class _FakeStdin:
    def __init__(self, server):
        self._server, self._buf = server, ""
        self.closed = False

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._server.handle(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    """Popen 形桩：stdin 写→服务端同步应答；stdout 队列行源。"""

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, text=None,
                 encoding=None, errors=None, env=None, cwd=None):
        self.argv = argv
        self.out = _FakeStdout()
        self.server = _FakeServer(self.out)
        self.stdin = _FakeStdin(self.server)
        self.stderr = []
        self._alive = True
        self.terminated = False

    @property
    def stdout(self):
        return self.out

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.out.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0


def _event(etype, seq, ts, data):
    return {"type": etype, "seq": seq, "time": ts, "data": data}


class TestDshAdapter(unittest.TestCase):
    """A 段：真适配器逻辑＋假传输（Req: F184 B1）。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-adapter-t-"))
        self.home = self.tmp / "agora-home"
        self.home.mkdir()
        self.env_dir = self.tmp / "env"
        self.env_dir.mkdir()
        self.proc_holder = {}

        def spawn(argv, **kw):
            proc = _FakeProc(argv, **kw)
            self.proc_holder["proc"] = proc
            return proc

        self.dsh = Dsh(repo_root=str(self.tmp), image="test-img",
                       home=str(self.home), spawn=spawn)
        self._old_key = os.environ.get("GLM_API_KEY")
        os.environ["GLM_API_KEY"] = "test-key"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("GLM_API_KEY", None)
        else:
            os.environ["GLM_API_KEY"] = self._old_key
        self.dsh.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proc(self):
        return self.proc_holder["proc"]

    def test_create_handshake_and_sid(self):
        sid = self.dsh.create(str(self.env_dir), "BOOT-TEXT")
        self.assertTrue(sid.startswith("session-"))  # SDK 侧 sid 单值来源
        methods = [m for m, _ in self._proc().server.requests]
        self.assertEqual(methods[:2], ["initialize", "session/prompt"])
        init = self._proc().server.requests[0][1]
        self.assertEqual(init["cwd"], "/work/task-env")  # 容器挂载位
        self.assertEqual(init["provider"], "glm-gptload")
        prompt = self._proc().server.requests[1][1]
        self.assertEqual(prompt["sessionId"], sid)
        self.assertEqual(prompt["contentBlocks"],
                         [{"type": "text", "text": "BOOT-TEXT"}])
        argv = self._proc().argv  # dsh_bin 形态：sdk profile＋worker patch
        self.assertIn("--profile", argv)
        self.assertEqual(argv[argv.index("--profile") + 1], "sdk")
        patch = argv[argv.index("--patch") + 1]
        self.assertTrue(patch.endswith("dsh-shim/cordis.worker.yml"))
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        targets = {m.split(":")[1] for m in mounts if ":" in m}
        self.assertIn("/work/task-env", targets)
        self.assertIn("/work/repo", targets)
        self.assertIn("/work/agora-home", targets)
        self.assertIn("/work/dsh-home", targets)

    def test_create_waits_initialize_receipt_before_prompt(self):
        """RC 实证口径：prompt 先于 initialize 应答＝协议拒（竞发被
        「SDK server is not initialized」打回）——适配器必须串行等待。"""
        # FakeStdin 同步应答＝请求天然串行；此处断言构造序本身：
        # initialize 帧已应答后 prompt 才可发（requests 序）＋prompt 应答
        # message id 回填成功（create 正常返回即证）。
        sid = self.dsh.create(str(self.env_dir), "b")
        self.assertEqual(self._proc().server.requests[0][0], "initialize")

    def test_inject_three_channels_same_wire(self):
        """三通道映射：inject/inject_notify→session/prompt（唤醒）；
        inject_silent→同通道落地（SDK 协议无免唤醒面——偏差在模块注）。"""
        sid = self.dsh.create(str(self.env_dir), "boot")
        self._proc().server.requests.clear()
        self.assertTrue(self.dsh.inject(sid, "wake1"))
        self.assertTrue(self.dsh.inject_notify(sid, "notify1"))
        self.assertTrue(self.dsh.inject_silent(sid, "silent1"))
        frames = [p for m, p in self._proc().server.requests
                  if m == "session/prompt"]
        self.assertEqual([f["contentBlocks"][0]["text"] for f in frames],
                         ["wake1", "notify1", "silent1"])
        self.assertTrue(all(f["sessionId"] == sid for f in frames))

    def test_inject_unknown_or_dead_session_false(self):
        self.assertFalse(self.dsh.inject("session-none", "x"))
        sid = self.dsh.create(str(self.env_dir), "boot")
        proc = self._proc()
        proc.server.prompt_error = "SDK server is not initialized"
        self.assertFalse(self.dsh.inject(sid, "x"))  # RPC 拒＝死会话 False
        self.assertFalse(self.dsh.inject("session-other", "y"))

    def test_alive_and_session_busy(self):
        sid = self.dsh.create(str(self.env_dir), "boot")
        self.assertTrue(self.dsh.alive(sid))
        srv = self._proc().server
        srv.emit_status(sid, "running")
        srv.emit_event(sid, _event("assistant/message", 3, 2000, {}))
        self._wait_for(lambda: (self.dsh.session(sid) or {}).get(
            "time", {}).get("idle", -1) is not None
            and self.dsh.session(sid)["time"]["idle"] == 0)
        s = self.dsh.session(sid)
        self.assertEqual(s["id"], sid)
        self.assertGreater(s["time"]["updated"], s["time"]["idle"])  # busy
        srv.emit_status(sid, "idle")  # idle 转换后不 busy
        self._wait_for(
            lambda: self.dsh.session(sid) is not None
            and self.dsh.session(sid)["time"]["idle"] > 0)
        s = self.dsh.session(sid)
        self.assertLessEqual(s["time"]["updated"], s["time"]["idle"])
        self._proc().terminate()
        self.assertFalse(self.dsh.alive(sid))
        self.assertIsNone(self.dsh.session(sid))  # 进程死＝会话死

    @staticmethod
    def _wait_for(pred, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.01)
        raise AssertionError("等待适配器消费通知超时")

    def test_last_message_and_messages_projection(self):
        sid = self.dsh.create(str(self.env_dir), "boot")
        srv = self._proc().server
        srv.emit_event(sid, _event(
            "user/message", 8, 1000,
            {"content": ["收到指令"], "role": "user"}))
        srv.emit_event(sid, _event(
            "assistant/message", 14, 2000,
            {"message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": "思考"},
                {"type": "text", "text": "答复"}],
                "usage": {"inputTokens": 10, "outputTokens": 5,
                          "totalTokens": 15}}}))
        srv.emit_event(sid, _event(
            "tool/call", 15, 3000,
            {"callId": "c1", "name": "bash"}))
        srv.emit_event(sid, _event(
            "tool/result", 16, 4000,
            {"message": {"role": "tool", "toolCallId": "c1",
                         "content": [{"type": "text", "text": "ok"}]}}))
        self._wait_for(lambda: len(self.dsh.messages(sid)) >= 3)
        msgs = self.dsh.messages(sid)
        self.assertEqual([m["type"] for m in msgs],
                         ["user", "assistant", "tool"])
        self.assertEqual(msgs[0]["content"][0]["text"], "收到指令")
        self.assertEqual(msgs[1]["usage"]["totalTokens"], 15)
        self.assertEqual(msgs[2]["content"][0]["name"], "bash")
        last = self.dsh.last_message(sid)
        self.assertEqual(last["type"], "tool")
        self.assertEqual(last["time"]["created"], 4000)

    def test_create_rpc_error_rolls_back(self):
        def bad_spawn(argv, **kw):
            proc = _FakeProc(argv, **kw)
            proc.server.fail_init = True
            self.proc_holder["proc"] = proc
            return proc
        self.dsh._spawn = bad_spawn
        with self.assertRaises(DshUnavailable):
            self.dsh.create(str(self.env_dir), "boot")
        self.assertTrue(self._proc().terminated)  # 失败即回收

    def test_close_terminates_workers(self):
        sid = self.dsh.create(str(self.env_dir), "boot")
        proc = self._proc()
        self.dsh.close()
        self.assertFalse(self.dsh.has(sid))
        self.assertTrue(proc.terminated)

    def test_shape_drift_frame_never_kills_reader(self):
        """评审 B 册 W2 回归锚：JSON 合法但字段形状漂移的帧只丢帧——
        读线程存活、后续帧照常入账（僵尸态禁入）。"""
        sid = self.dsh.create(str(self.env_dir), "boot")
        srv = self._proc().server
        srv.emit_event(sid, {"type": "assistant/message",
                             "time": "2026-11-28T00:00:00Z",  # ISO 串
                             "data": []})  # truthy 非 dict
        srv.emit_event(sid, {"type": "tool/call", "time": True,
                             "data": {"callId": "c9", "name": 123}})  # 非串名
        srv.emit_event(sid, _event(
            "user/message", 20, 12345, {"content": ["好帧"], "role": "user"}))
        self._wait_for(
            lambda: any(m.get("content") for m in self.dsh.messages(sid)))
        msgs = self.dsh.messages(sid)
        self.assertEqual(len(msgs), 1)  # 两坏帧被跳过、好帧入账
        self.assertEqual(msgs[0]["content"][0]["text"], "好帧")
        self.assertTrue(self.dsh.alive(sid))
        # 坏帧后注入通道照常（读线程活着）
        self._proc().server.requests.clear()
        self.assertTrue(self.dsh.inject(sid, "still-alive"))


# ---- B 段：HarnessRouter ----

class _OcLike:
    def __init__(self):
        self.created, self.injects = [], []
        self.two_arg_only = False  # v3 oc 两参面拨杆

    def create(self, env, boot, title=None, model=None):
        sid = "ses-oc1"
        self.created.append((env, boot, title, model))
        return sid

    def inject(self, sid, text, model=None):
        if self.two_arg_only and model is not None:
            raise TypeError("v3 oc inject 无 model 形参")
        self.injects.append((sid, text, model))
        return True

    def inject_silent(self, sid, text):
        self.injects.append((sid, text, "silent"))
        return True

    def inject_notify(self, sid, text):
        self.injects.append((sid, text, "notify"))
        return True

    def alive(self, sid):
        return sid == "ses-oc1"

    def session(self, sid):
        return {"id": sid} if sid == "ses-oc1" else None

    def last_message(self, sid):
        return None

    def messages(self, sid):
        return []

    def probe(self):
        return {"ok": True, "oc_version": "v"}

    def models(self):
        return ["p/m"]


class _DshLike:
    """dsh 桩：注册表语义（has）＋调用记录。"""

    def __init__(self, sid="session-d1"):
        self.sid = sid
        self.created, self.injects = [], []

    def has(self, sid):
        return sid == self.sid

    def create(self, env, boot, title=None, model=None):
        self.created.append((env, boot, title, model))
        return self.sid

    def inject(self, sid, text, model=None):
        self.injects.append(("inject", sid, text))
        return True

    def inject_silent(self, sid, text):
        self.injects.append(("silent", sid, text))
        return True

    def inject_notify(self, sid, text):
        self.injects.append(("notify", sid, text))
        return True

    def alive(self, sid):
        return sid == self.sid

    def session(self, sid):
        return {"id": sid, "dsh": True} if sid == self.sid else None

    def last_message(self, sid):
        return None

    def messages(self, sid):
        return []


class TestHarnessRouter(unittest.TestCase):
    """B 段：harness/sid 双路由（Req: F184 B2 前件）。"""

    def test_create_routes_by_harness(self):
        oc, dsh = _OcLike(), _DshLike()
        r = HarnessRouter(oc, dsh)
        self.assertEqual(r.create("/e", "b1", harness="dsh"), "session-d1")
        self.assertEqual(dsh.created, [("/e", "b1", None, None)])
        self.assertEqual(r.create("/e", "b2"), "ses-oc1")
        self.assertEqual(oc.created, [("/e", "b2", None, None)])

    def test_create_dsh_without_adapter_rejected(self):
        r = HarnessRouter(_OcLike(), None)
        with self.assertRaises(DshUnavailable):
            r.create("/e", "b", harness="dsh")

    def test_sid_routing_for_inject_and_probes(self):
        oc, dsh = _OcLike(), _DshLike()
        r = HarnessRouter(oc, dsh)
        self.assertTrue(r.inject("session-d1", "hi"))
        self.assertEqual(dsh.injects, [("inject", "session-d1", "hi")])
        self.assertEqual(oc.injects, [])
        r.inject_silent("ses-oc1", "s")
        r.inject_notify("ses-oc1", "n")
        self.assertEqual([i[2] for i in oc.injects], ["silent", "notify"])
        self.assertTrue(r.alive("session-d1"))
        self.assertEqual(r.session("session-d1")["dsh"], True)
        self.assertEqual(r.session("ses-oc1"), {"id": "ses-oc1"})

    def test_model_kwarg_forwarded_only_when_given(self):
        oc = _OcLike()
        oc.two_arg_only = True
        r = HarnessRouter(oc, None)
        self.assertTrue(r.inject("ses-oc1", "t"))  # 无 model＝两参面安全
        oc2 = _OcLike()
        r2 = HarnessRouter(oc2, None)
        self.assertTrue(r2.inject("ses-oc1", "t", model="p/m"))
        self.assertEqual(oc2.injects[-1][2], "p/m")  # 显式携带才透传

    def test_probe_models_always_oc(self):
        r = HarnessRouter(_OcLike(), _DshLike())
        self.assertEqual(r.probe()["oc_version"], "v")
        self.assertEqual(r.models(), ["p/m"])


# ---- C 段：dispatch 分流双代 ----

class TestV2HarnessRouting(unittest.TestCase):
    """v2 真栈（assemble＋httpd＋StubOpencode＋FakeDsh 桩）。

    断言面（Req: F184 B2）：task_create harness 参数与值域、worker
    继承/显式拒、dispatch 路由、/cred_for_sid env_dir 容器路径映射。"""

    def setUp(self):
        import shutil
        import tempfile
        import tests.stub_opencode as stub_mod
        from agorad.main import assemble
        from agorad.transport.httpd import make_server
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-v2-t-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = stub_mod.StubOpencode()
        self.stub.start()
        self.dsh = _DshLike()
        self.app = assemble(self.home, oc_url=self.stub.base_url,
                            dsh=self.dsh)
        self.token = self.app.auth.issue("main", "root")
        self.httpd = make_server(self.app)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.repo = self.tmp / "repo"
        self._git_init(self.repo)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _git_init(path):
        import subprocess
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path,
                       check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=path,
                       check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=path,
                       check=True)
        (path / "base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path,
                       check=True)

    def _call(self, path, body, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Agora-Token": token or self.token}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _mk_task(self, goal="g", **extra):
        body = {"goal": goal, "project_dir": str(self.repo),
                "verify_cmd": "true", **extra}
        st, r = self._call("task_create", body)
        self.assertEqual(st, 200, r)
        return r["task"]

    def _dispatch(self, tid):
        st, r = self._call("task_dispatch", {"task": tid})
        self.assertEqual(st, 200, r)
        return r

    def test_task_create_harness_value_domain(self):
        tid = self._mk_task(harness="dsh")
        row = self.app.state.get(tid)
        self.assertEqual(row.get("harness"), "dsh")
        tid2 = self._mk_task()
        self.assertEqual(self.app.state.get(tid2).get("harness"), None)
        st, r = self._call("task_create", {
            "goal": "g", "project_dir": str(self.repo),
            "harness": "carrier_pigeon"})
        self.assertEqual(st, 403, r)  # 值域拒（v2 Rejected→403 同款域口径）
        self.assertIn("harness", r["error"])

    def test_dispatch_routes_to_dsh_and_cred_env_dir(self):
        tid = self._mk_task(harness="dsh")
        self.dsh.created.clear()
        self._dispatch(tid)
        self.assertEqual(len(self.dsh.created), 1)
        env, boot, title, model = self.dsh.created[0]
        self.assertEqual(env, self.app.state.get(tid)["env_dir"])
        self.assertIn(tid, boot)  # boot 信封含任务身份（httpd 装配全文）
        row = self.app.state.get(tid)
        self.assertEqual(row["worker_sid"], "session-d1")
        self.assertEqual(row["state"], "dispatched")
        # dispatch 后凭据直投：路由到 dsh（worker_sid 命中注册表）
        self.assertTrue(any(i[1] == "session-d1" for i in self.dsh.injects))
        # /cred_for_sid：dsh 任务 env_dir＝容器挂载位（shim 路径域）
        st, r = self._call("cred_for_sid", {"sid": "session-d1"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["env_dir"], "/work/task-env")
        self.assertEqual(r["tid"], tid)

    def test_cred_for_sid_opencode_env_dir_unchanged(self):
        tid = self._mk_task()
        self._dispatch(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, r = self._call("cred_for_sid", {"sid": wsid})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["env_dir"],
                         self.app.state.get(tid)["env_dir"])  # 主机路径

    def _worker_token(self, tid):
        return json.loads(
            (self.home / "credentials" / f"{tid}.token").read_text(
                encoding="utf-8"))["token"]

    def test_worker_harness_inheritance_and_rejection(self):
        tid = self._mk_task(harness="dsh")
        self._dispatch(tid)
        wtok = self._worker_token(tid)
        # 显式传 harness＝拒（F37 同权：model/deliverable 口径）
        st, r = self._call("task_create", {
            "goal": "child", "harness": "opencode"}, token=wtok)
        self.assertEqual(st, 403, r)
        # 缺省＝继承父任务 harness（dsh）
        st, r = self._call("task_create", {"goal": "child"}, token=wtok)
        self.assertEqual(st, 200, r)
        child = r["task"]
        self.assertEqual(self.app.state.get(child).get("harness"), "dsh")


class TestV3HarnessRouting(unittest.TestCase):
    """v3 tasksvc（桩件直构）：nested DTO harness／worker 继承与拒／路由。"""

    def test_migrator_maps_harness_with_default_coerce(self):
        """评审 B 册 W1/S2 回归锚：v2 行 harness 落 typed 列＋None coerce。"""
        from agorad.migrator import _map_task
        row = {"state": "todo", "title": "T", "goal": "g",
               "harness": "dsh"}
        t = _map_task("ag-m1", row)
        self.assertEqual(t.harness, "dsh")
        self.assertEqual(t.legacy_extra_json, "{}")  # 不落审计位＝typed 落位
        t2 = _map_task("ag-m2", {"state": "todo", "title": "T", "goal": "g"})
        self.assertEqual(t2.harness, "opencode")  # 缺行值→缺省（NOT NULL 面）

    def setUp(self):
        import shutil
        import tempfile
        from agorad.storage import Storage
        from agorad.tasksvc import TaskService
        from tests.test_v3_task_service import GitxStub, OcStub
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-v3-t-"))
        self.storage = Storage(self.tmp / "agora.db")
        self.gitx = GitxStub()
        self.oc = OcStub()
        self.dsh = _DshLike()
        self.router = HarnessRouter(self.oc, self.dsh)
        self.svc = TaskService(self.storage, self.gitx, self.router,
                               home=str(self.tmp))

    def tearDown(self):
        self.storage.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, goal="g", harness=None, worker=False):
        execution = {"model": None, "after": None, "req_id": None,
                     "project_dir": "/repo", "client_key": None,
                     "quick": None}
        if harness is not None:
            execution["harness"] = harness
        nested = {
            "goal": goal, "title": None,
            "execution": execution,
            "verify": {"cmd": None if worker else "true",
                       "review_cmd": None, "verify_pending": None,
                       "verify_base_exempt": None, "timeout": None},
            "delivery": {"deliverable": None, "target_branch": None}}
        return nested

    def test_create_and_dispatch_route_to_dsh(self):
        spec = self._spec(harness="dsh")
        tid = self.svc.create("disp", "dispatcher", spec)
        t = self.storage.get_task(tid)
        self.assertEqual(t.harness, "dsh")
        self.dsh.created.clear()
        self.svc.dispatch(tid, "disp", "dispatcher")
        self.assertEqual(len(self.dsh.created), 1)
        t = self.storage.get_task(tid)
        self.assertEqual(t.worker_sid, "session-d1")
        self.assertEqual(t.state, "dispatched")
        self.assertEqual(self.oc.created, [])  # opencode 面零触碰

    def test_default_harness_is_opencode(self):
        tid = self.svc.create("disp", "dispatcher", self._spec())
        self.assertEqual(self.storage.get_task(tid).harness, "opencode")
        self.svc.dispatch(tid, "disp", "dispatcher")
        self.assertEqual(len(self.oc.created), 1)  # 路由到 oc
        self.assertEqual(self.dsh.created, [])

    def test_worker_inherits_and_explicit_rejected(self):
        tid = self.svc.create("disp", "dispatcher", self._spec(harness="dsh"))
        self.svc.dispatch(tid, "disp", "dispatcher")
        from agorad.contracts import ContractError
        spec = self._spec(goal="child", harness="opencode", worker=True)
        with self.assertRaises(ContractError) as cm:
            self.svc.create("session-d1", "worker", spec)
        self.assertIn("harness", cm.exception.message)
        child = self.svc.create("session-d1", "worker",
                                self._spec(goal="c2", worker=True))
        self.assertEqual(self.storage.get_task(child).harness, "dsh")


if __name__ == "__main__":
    unittest.main()
