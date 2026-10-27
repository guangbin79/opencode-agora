"""T1 MCP 壳契约：stdio newline-JSON-RPC ↔ 真 agorad（make_server）全链。

壳＝薄代理零逻辑：11 工具根级路由 POST /<name>、_token 剥除→X-Agora-Token、
HTTP 错误档→JSON-RPC 负数区 code（message 服务原文）；未知工具/缺牌壳内
直错（不打服务）。夹具形态同 test_transport_httpd（假 gitx/锚/runner＋
StubOpencode，root 牌 sid=main）。
"""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import agora_mcp
from agorad.main import assemble
from agorad.transport.httpd import _TOOLS, make_server
from tests.stub_opencode import StubOpencode


def ok_runner(cwd, cmd):
    return 0, "pass"


class FakeGitx:
    def build_env(self, project_dir, tid, branch=None):
        return f"/envs/{tid}"

    def remove_env(self, env_dir):
        pass

    def merge_ff(self, env_dir, target, branch=None):
        return "ok"

    def head(self, repo_dir, branch=None):
        return "aaa111"

    def rollback(self, target, before_sha):
        pass

    def touches_gate(self, env_dir, patterns, branch=None):
        return False


class McpShellTest(unittest.TestCase):
    """真 agorad 夹具：壳经 AGORAD_URL 指向 make_server 端口。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: os.environ.get(k) for k in ("AGORAD_URL", "AGORA_HOME")}
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
        os.environ["AGORAD_URL"] = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- 夹具 ----

    def rpc(self, method, params=None, rid=1):
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        resp = agora_mcp.handle(req)
        self.assertIsNotNone(resp)
        return resp

    def call_tool(self, name, arguments, rid=1):
        return self.rpc("tools/call", {"name": name, "arguments": arguments}, rid)

    def result_of(self, resp):
        return json.loads(resp["result"]["content"][0]["text"])


class TestHandshakeAndList(McpShellTest):
    def test_initialize_echoes_client_protocol_version(self):  # Req: F2
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "agora")

    def test_initialize_defaults_version_when_absent(self):  # Req: F2
        r = self.rpc("initialize", {})
        self.assertEqual(r["result"]["protocolVersion"],
                         agora_mcp.PROTOCOL_VERSION)

    def test_tools_list_exactly_11_matching_httpd_routes(self):  # Req: F2
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(len(names), 12)  # 11 服务端 + 1 壳层本地 wait
        self.assertEqual(set(names) - {"wait"}, set(_TOOLS))  # 11 服务端五族与 httpd 同集；wait＝壳层本地
        for t in self.rpc("tools/list")["result"]["tools"]:
            self.assertTrue(t.get("description"))
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_wiki_write_param_is_text_per_httpd(self):  # Req: F2
        schema = {t["name"]: t for t in
                  self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(schema["wiki_write"]["inputSchema"]["required"],
                         ["path", "text"])  # httpd 实参名 text（非 plan 的 content）
        self.assertEqual(schema["post"]["inputSchema"]["required"],
                         ["task", "kind", "body"])  # 方向服务侧按角色定，无 direction
        self.assertEqual(schema["task_create"]["inputSchema"]["properties"]
                         ["target_branch"]["type"], "string")  # gt-132747 外部工作流

    def test_notification_silent_parse_error_reported(self):  # Req: F2
        out = io.StringIO()
        agora_mcp.serve(io.StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"), out)
        self.assertEqual(out.getvalue(), "")  # 通知不应答
        out = io.StringIO()
        agora_mcp.serve(io.StringIO("{broken json\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


class TestPassthrough(McpShellTest):
    def test_task_create_passes_token_via_header(self):  # Req: F2
        r = self.call_tool("task_create", {"_token": self.root, "goal": "g",
                                           "project_dir": "/repo"})
        tid = self.result_of(r)["task"]
        self.assertEqual(self.app.state.get(tid)["goal"], "g")  # 服务真落库

    def test_sid_argument_binds_main_and_stripped(self):  # Req: F4（R25-A）
        r = self.call_tool("task_create", {"_token": self.root, "_sid": "ses-mcp",
                                           "goal": "g", "project_dir": "/repo"})
        self.assertIn("result", r)
        tid = self.result_of(r)["task"]
        self.assertEqual(self.app.main_sid, "ses-mcp")  # 壳透传 X-Agora-Sid→绑定
        self.assertEqual(self.app.state.get(tid)["dispatcher_sid"], "ses-mcp")

    def test_bad_token_gets_server_401_not_shell(self):  # Req: F2
        r = self.call_tool("task_create", {"_token": "junk", "goal": "g",
                                           "project_dir": "/repo"})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[401])
        self.assertIn("无牌", r["error"]["message"])  # 服务原文——差分自证头已透传

    def test_dispatch_roundtrip_result_shape(self):  # Req: F3
        r = self.call_tool("task_create", {"_token": self.root, "goal": "g",
                                           "project_dir": "/repo"})
        tid = self.result_of(r)["task"]
        self.call_tool("task_update", {"_token": self.root, "task": tid,
                                       "verify_cmd": "true"})
        r = self.call_tool("task_dispatch", {"_token": self.root, "task": tid})
        payload = self.result_of(r)
        self.assertEqual(payload["task"], tid)
        self.assertTrue(payload["token"])  # worker 凭据回传

    def test_board_and_doctor_pass_through(self):  # Req: F3
        self.assertIn("buckets", self.result_of(
            self.call_tool("board", {"_token": self.root})))
        self.assertIn("age_rank", self.result_of(
            self.call_tool("doctor", {"_token": self.root})))


class TestErrorMapping(McpShellTest):
    def test_403_worker_enforce_denied(self):  # Req: F3
        wtok = self.app.auth.issue("w9", "worker")
        r = self.call_tool("enforce", {"_token": wtok, "on": False})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[403])
        self.assertIn("role=worker", r["error"]["message"])  # 服务原文透传
        self.assertTrue(self.app.auth.enforce_on())  # 旗标未被拨动

    def test_400_wiki_escape_and_missing_field(self):  # Req: F3
        r = self.call_tool("wiki_read", {"_token": self.root,
                                         "path": "../secret"})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[400])
        r = self.call_tool("task_create", {"_token": self.root, "goal": "g"})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[400])
        self.assertIn("缺字段", r["error"]["message"])

    def test_404_wiki_missing_file(self):  # Req: F3
        r = self.call_tool("wiki_read", {"_token": self.root,
                                         "path": "notes/none.md"})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[404])

    def test_409_and_500_table_negative_distinct(self):  # Req: F3
        # 409（InvalidTransition）经壳工具面不可达（worker 开工迁移三态皆合法），
        # 500 为兜底档——两档映射表驱动直验；负数区且互异防串档。
        codes = agora_mcp.ERROR_CODES
        self.assertEqual(sorted(codes), [400, 401, 403, 404, 409, 500])
        self.assertTrue(all(v < 0 for v in codes.values()))
        self.assertEqual(len(set(codes.values())), 6)
        self.assertEqual(codes[409], -32009)
        self.assertEqual(codes[500], -32603)

    def test_unknown_tool_shell_error_not_404(self):  # Req: F3
        r = self.call_tool("merge", {"_token": self.root})
        self.assertEqual(r["error"]["code"], -32602)  # ≠ 404 档 -32004：壳内直错
        self.assertIn("merge", r["error"]["message"])

    def test_missing_token_shell_error_not_server_401(self):  # Req: F3
        r = self.call_tool("board", {})
        self.assertEqual(r["error"]["code"], agora_mcp.ERROR_CODES[401])
        self.assertIn("_token", r["error"]["message"])  # 壳文案，非服务「无牌或牌无效」


class TestUrlDiscovery(McpShellTest):
    def test_service_json_port_used_when_no_env_url(self):  # Req: F3
        os.environ.pop("AGORAD_URL")
        os.environ["AGORA_HOME"] = str(self.tmp)
        sj = self.tmp / ".agora" / "service.json"
        sj.parent.mkdir(exist_ok=True)
        sj.write_text(json.dumps({"port": self.httpd.server_address[1]}),
                      encoding="utf-8")
        r = self.call_tool("doctor", {"_token": self.root})
        self.assertIn("age_rank", self.result_of(r))

    def test_service_json_missing_reports_unreachable(self):  # Req: F3
        os.environ.pop("AGORAD_URL")
        os.environ["AGORA_HOME"] = str(self.tmp / "absent")
        r = self.call_tool("doctor", {"_token": self.root})
        self.assertEqual(r["error"]["code"], -32000)
        self.assertIn("服务不可达", r["error"]["message"])

    def test_env_url_wins_over_service_json(self):  # Req: F3
        os.environ["AGORAD_URL"] = "http://127.0.0.1:1"  # 必连拒＝不可达档
        r = self.call_tool("doctor", {"_token": self.root})
        self.assertEqual(r["error"]["code"], -32000)


if __name__ == "__main__":
    unittest.main()
