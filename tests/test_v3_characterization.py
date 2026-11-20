"""Req: F128 — v2 契约表征基线（opencode-agora v3 重写 P0-T1）。

零生产码改动：只把 v2 既有外部/边缘契约钉进 tests/fixtures/v3_contract_golden.json
与 tests/fixtures/v3_envelope.json，断言逐键一致——v3 重写以本文件为机械对照基线，
发现实现与本基线冲突时修实现（或经有意差异清单显式偏离），不改断言迁就实现。
断言清单见 docs/reqs/opencode-agora.md F128。
"""
import json
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import agora_mcp
from agorad.access.store import Store
from agorad.core import report
from agorad.core.msg import _REF_KINDS, _REF_LEN, _REF_MARKS, \
    _reference_lines, _wants_refs
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.main import assemble
from agorad.transport import httpd
from agorad.transport.httpd import make_server

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads(
    (ROOT / "tests" / "fixtures" / "v3_contract_golden.json").read_text(
        encoding="utf-8"))
ENVELOPE = json.loads(
    (ROOT / "tests" / "fixtures" / "v3_envelope.json").read_text(
        encoding="utf-8"))


def call(port, path, body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Agora-Token": token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class FakeOc:  # models 故障＝doctor 降级口径；其余面本文件不触达

    def models(self):
        raise OSError("offline")

    def create(self, cwd, boot, title=None, model=None):
        return "sw1"

    def inject(self, sid, text, model=None):
        return False

    def alive(self, sid):
        return False

    def session(self, sid):
        return None

    def probe(self):
        return {"ok": True, "oc_version": "fake"}


class RecordingOc:  # msg 面：只记录注入文本

    def __init__(self):
        self.calls = []

    def inject(self, sid, text):
        self.calls.append((sid, text))
        return True

    def inject_silent(self, sid, text):
        self.calls.append((sid, text))
        return True

    def inject_notify(self, sid, text):
        self.calls.append((sid, text))
        return True


class TestMcpToolsGolden(unittest.TestCase):
    """断言①②：tools/list 16 工具投影与六档错误码映射。"""

    def _tools(self):
        r = agora_mcp.handle({"jsonrpc": "2.0", "id": 1,
                              "method": "tools/list"})
        return r["result"]["tools"]

    def test_tool_count_and_names_match_golden(self):
        tools = self._tools()
        self.assertEqual(len(tools), GOLDEN["mcp"]["tool_count"])  # 15 服务端＋sleep
        self.assertEqual(sorted(t["name"] for t in tools),
                         GOLDEN["mcp"]["tool_names"])

    def test_required_and_enum_fields_match_golden(self):
        projection = {}
        for t in self._tools():
            schema = t["inputSchema"]
            projection[t["name"]] = {
                "required": schema.get("required", []),
                "enums": {k: v["enum"]
                          for k, v in schema.get("properties", {}).items()
                          if isinstance(v, dict) and "enum" in v}}
        self.assertEqual(projection, GOLDEN["mcp"]["tools"])

    def test_mcp_names_eq_http_routes_plus_sleep(self):
        # sleep 壳层本地休眠不走 HTTP：MCP 名集恰＝15 根路由＋sleep
        self.assertEqual(sorted(httpd._TOOLS), GOLDEN["mcp"]["http_tool_routes"])
        self.assertEqual(set(GOLDEN["mcp"]["tool_names"]),
                         set(httpd._TOOLS) | {"sleep"})

    def test_error_codes_six_tiers_distinct_and_golden(self):
        codes = agora_mcp.ERROR_CODES
        self.assertEqual({int(k): v for k, v
                          in GOLDEN["mcp"]["error_codes"].items()}, codes)
        self.assertEqual(len(codes), len(set(codes.values())))  # 负数码互异
        self.assertEqual(set(codes), {400, 401, 403, 404, 409, 500})


class HttpdBase(unittest.TestCase):
    """真 HTTP 夹具：assemble＋fake oc（models 降级）＋root 牌。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=FakeOc(),
                            runner=lambda cwd, cmd, timeout=None,
                            ceiling=None: (0, "pass"))
        self.root = self.app.auth.issue("main", "root")
        self.server = make_server(self.app)
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestHttpStringContract(HttpdBase):
    """断言③：_STRING_FIELDS 全端点清单 golden＋代表端点坏类型 400 不 500。"""

    def test_string_fields_match_golden(self):
        self.assertEqual({k: list(v) for k, v
                          in sorted(httpd._STRING_FIELDS.items())},
                         GOLDEN["http_string_fields"])

    def test_task_create_bad_goal_type_400_no_side_effect(self):
        st, r = call(self.port, "task_create",
                     {"goal": 123, "project_dir": "/repo"}, self.root)
        self.assertEqual(st, 400)  # 坏类型在 HTTP 边界 400，非 500
        self.assertIn("goal", r["error"])
        self.assertEqual(self.app.state.snapshot()["tasks"], {})  # 零半成品

    def test_wiki_write_bad_text_type_400_no_write(self):
        st, r = call(self.port, "wiki_write",
                     {"path": "spec/x.md", "text": {"a": 1}}, self.root)
        self.assertEqual(st, 400)
        self.assertIn("text", r["error"])
        self.assertFalse((self.tmp / "wiki" / "spec" / "x.md").exists())


class TestBoardDoctorViews(HttpdBase):
    """断言④：full_view tasks 白名单字段集＋doctor 顶层键 golden。"""

    ROW_FIELDS = GOLDEN["board_full_view"]["task_fields"]

    def _row(self, **extra):
        row = {k: f"v-{k}" for k in self.ROW_FIELDS}
        row.update(state="running", created=1.0, last_seen=2.0,  # 森林排序/基线要数值
                   usage={}, delivery={}, in_gate=False, review_fail_streak=0,
                   secret="leak")  # 白名单外字段必须被滤除
        row.update(extra)
        return row

    def test_full_view_task_whitelist_filters_and_verify_budget(self):
        store = Store(Path(tempfile.mkdtemp()))
        try:
            out = report.full_view(
                {"tasks": {"t1": self._row(
                    verify={"timeout": 300, "timeout_source": "task",
                            "note": "n"})},
                 "mail_read": {}}, store)
            t = out["tasks"]["t1"]
            self.assertEqual(set(t), set(self.ROW_FIELDS)
                             | {GOLDEN["board_full_view"]["verify_budget_key"]})
            self.assertNotIn("secret", t)
            self.assertEqual(t["verify_budget"],
                             {"timeout": 300, "source": "task", "note": "n"})
            # legacy 行无 timeout_source：按既有 timeout 推断，两观测面不分裂
            out2 = report.full_view(
                {"tasks": {"t1": self._row(verify={"timeout": 300})},
                 "mail_read": {}}, store)
            self.assertEqual(out2["tasks"]["t1"]["verify_budget"]["source"],
                             "task")
            # 无 verify 字典＝不投影 verify_budget
            out3 = report.full_view(
                {"tasks": {"t1": self._row()}, "mail_read": {}}, store)
            self.assertNotIn("verify_budget", out3["tasks"]["t1"])
        finally:
            shutil.rmtree(store.home, ignore_errors=True)

    def test_full_view_top_keys_and_http_wiring(self):
        st, r = call(self.port, "task_create",
                     {"goal": "g", "project_dir": str(self.tmp),
                      "timeout": 300}, self.root)
        self.assertEqual(st, 200)
        st, r = call(self.port, "board", {"view": "full"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r), GOLDEN["board_full_view"]["top_keys"])
        tid = next(iter(r["tasks"]))
        self.assertEqual(r["tasks"][tid]["verify_budget"],
                         {"timeout": 300, "source": "task"})

    def test_doctor_base_keys_http_golden(self):
        st, r = call(self.port, "doctor", {}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r), GOLDEN["doctor"]["base_keys"])

    def test_doctor_conditional_keys(self):
        snap = {"tasks": {}, "mail_read": {}}
        d = report.doctor_view(snap, self.tmp)  # models/画像均缺省＝基础键
        self.assertEqual(sorted(d), GOLDEN["doctor"]["base_keys"])
        d = report.doctor_view(snap, self.tmp, models=["m"],  # oc 可达
                               verify_profiles={})  # 空画像也注入 count=0
        self.assertEqual(sorted(d), sorted(
            GOLDEN["doctor"]["base_keys"] + GOLDEN["doctor"]["models_keys"]
            + [GOLDEN["doctor"]["verify_profiles_key"]]))
        self.assertEqual(d["verify_profiles"]["count"], 0)


class TestCheckMailAndRefs(unittest.TestCase):
    """断言⑤：check_mail 消息 dict 形状＋F125 引用行顺序与触发条件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1, "mail_read": {},
            "tasks": {"t1": {"state": "running", "fail_streak": 0,
                             "title": "x", "created": 0.0, "updated": 0.0,
                             "dispatcher_sid": "d1", "worker_sid": "w1"}}})
        self.oc = RecordingOc()
        self.router = Router(self.store, State(self.store), self.oc)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_message_dict_shape_and_pull_once(self):
        body = "第一行\n第二行"  # 多行原文逐字节保持
        mid = self.router.post("w1", ("up", "t1"), "note", body)
        msgs = self.router.check_mail("d1")
        self.assertEqual([m["id"] for m in msgs], [mid])
        m = msgs[0]
        self.assertIsInstance(m["id"], int)  # id 为 int
        for k, v in m.items():  # frontmatter 其余值一律 str
            if k not in ("id", "body"):
                self.assertIsInstance(v, str, k)
        self.assertEqual(m["body"], body)
        self.assertEqual({k for k in m},
                         {"id", "body", "ts", "task", "kind", "from", "to"})
        self.assertEqual(self.router.check_mail("d1"), [])  # 拉取即送达不重交

    def test_deliver_injects_reference_lines_before_body(self):
        body = "x" * 300  # >240 触发引用优先
        mid = self.router.post("w1", ("up", "t1"), "question", body)
        self.assertEqual(len(self.oc.calls), 1)
        sid, text = self.oc.calls[0]
        self.assertEqual(sid, "d1")
        self.assertEqual(
            text,
            f"[agora] task t1 question from w1\n"
            f"home={self.tmp}\n"
            f"msg=state/messages/{mid:08d}.md\n"
            f"page=wiki/tasks/t1.md\n"
            f"{body}")

    def test_f125_trigger_constants_and_matrix(self):
        g = GOLDEN["f125"]
        self.assertEqual(_REF_LEN, g["ref_len"])
        self.assertEqual(sorted(_REF_KINDS), g["ref_kinds"])
        self.assertEqual(list(_REF_MARKS), g["ref_marks"])
        self.assertFalse(_wants_refs("note", "x" * 240, {}))  # 阈值不含等
        self.assertTrue(_wants_refs("note", "x" * 241, {}))  # 超长即引用
        self.assertTrue(_wants_refs("result", "构建失败", {}))  # 关键 kind＋标记
        self.assertFalse(_wants_refs("note", "构建失败", {}))  # 非关键 kind 不触发
        self.assertFalse(_wants_refs("result", "一切正常", {}))  # 无标记不触发
        art = {"gate_artifact": {"path": "/h/gate/t1/output.log"}}
        self.assertTrue(_wants_refs("note", "见输出尾部", art))  # 产物在案＋尾部
        self.assertFalse(_wants_refs("note", "见输出尾部", {}))  # 无产物不触发

    def test_reference_lines_order_and_artifact_relativization(self):
        g = GOLDEN["f125"]
        art = {"gate_artifact": {"path": "/h/gate/t1/output.log"}}
        order = [ln.split("=", 1)[0] for ln in
                 _reference_lines("t1", 12, art, "/h").splitlines()]
        self.assertEqual(order, g["ref_line_order"])  # golden 键双侧锁定
        self.assertTrue(_reference_lines("t1", 12, art, "/h").endswith(
            "artifact=gate/t1/output.log"))  # home 下相对化（值锁定）
        self.assertEqual(  # 无产物＝home/msg/page 三行，顺序固定
            _reference_lines("t1", 12, {}, "/h"),
            "home=/h\nmsg=state/messages/00000012.md\npage=wiki/tasks/t1.md")
        art_abs = {"gate_artifact": {"path": "/elsewhere/x.log"}}
        self.assertTrue(_reference_lines("t1", 12, art_abs, "/h").endswith(
            "artifact=/elsewhere/x.log"))  # home 外绝对原样


class TestEnvelopeFixture(unittest.TestCase):
    """断言⑥：信封 fixture 三形态样本结构完整＋pattern 语义可复算。"""

    def test_fixture_structure_complete(self):
        for key in ("patterns", "boot", "credential_pointer",
                    "credential_token_legacy"):
            self.assertIn(key, ENVELOPE)
        prefixes = {e["expect"]["tid"].split("-")[0] for e in ENVELOPE["boot"]}
        self.assertEqual(prefixes, {"ag", "task", "gt"})  # 三前缀齐备
        for s in ENVELOPE["boot"]:
            self.assertEqual(set(s["expect"]), {"tid", "env"})
        for s in ENVELOPE["credential_pointer"]:
            self.assertEqual(set(s["expect"]), {"tid", "file", "legit"})
        for s in ENVELOPE["credential_token_legacy"]:
            self.assertEqual(set(s["expect"]), {"tid", "token"})

    def _search(self, name, text):
        p = ENVELOPE["patterns"][name]
        flags = re.MULTILINE if "m" in p["flags"] else 0
        m = re.search(p["source"], text, flags)
        self.assertIsNotNone(  # 失配即报样本与 pattern，不裸抛 None.group
            m, f"信封样本与 pattern {name} 失配: {text[:60]!r}")
        return m

    def test_patterns_reparse_every_sample(self):
        for s in ENVELOPE["boot"]:  # Python re 与 Bun 同语义样本级复算
            self.assertEqual(self._search("boot_msg", s["text"]).group(1),
                             s["expect"]["tid"])
            self.assertEqual(self._search("boot_env", s["text"]).group(1),
                             s["expect"]["env"])
        home = ENVELOPE["agora_home"]
        for s in ENVELOPE["credential_pointer"]:
            m = self._search("cred_pointer", s["text"])
            self.assertEqual(m.group(1), s["expect"]["tid"])
            self.assertEqual(m.group(2), s["expect"]["file"])
            want = Path(home, "credentials", f"{s['expect']['tid']}.token")
            self.assertEqual(  # 指针 pin＝只认本尊路径（路径投毒拒读）
                s["expect"]["legit"],
                Path(s["expect"]["file"]) == want)
        for s in ENVELOPE["credential_token_legacy"]:
            m = self._search("cred_token_legacy", s["text"])
            self.assertEqual((m.group(1), m.group(2)),
                             (s["expect"]["tid"], s["expect"]["token"]))


if __name__ == "__main__":
    unittest.main()
