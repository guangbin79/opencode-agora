"""Req: F130 — v3 Contract Catalog（opencode-agora v3 P1-T3）。

锚 agorad/contracts.py（仅 stdlib，test_architecture 守卫）单源：CATALOG＝19
服务端工具＋sleep 的 ToolSpec（§2 清单）；校验器（bool≠int、str≠object、未知
字段列名、enum 白名单、显式 null 拒）；错误三向映射与 F128 golden 一致；
task_create nested DTO 缺省派生＋v2 扁平→nested 纯转换；MCP 桥接 schema 与
golden 逐字段等价。断言清单见 docs/reqs/opencode-agora.md F130（T5 接线外）。
"""
import copy
import json
import unittest
from pathlib import Path

from agorad import contracts

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads(
    (ROOT / "tests" / "fixtures" / "v3_contract_golden.json").read_text(
        encoding="utf-8"))

# 设计页 §2：19 个服务端工具＋sleep（reverify→task_reverify、post→message_post、
# check_mail→message_pull 为 v3 正名，timer/approval 为 v3 新增）
V3_TOOL_NAMES = set("""task_create task_dispatch task_update task_cancel
    task_deliver task_reverify task_wait timer_create timer_cancel approval_request
    approval_reply message_post message_pull board doctor repo_board wiki_read
    wiki_write enforce sleep""".split())


class TestCatalog(unittest.TestCase):

    def test_tool_set_matches_design(self):  # §2：19 服务端工具＋sleep
        self.assertEqual(set(contracts.CATALOG), V3_TOOL_NAMES)

    def test_spec_dimensions(self):
        for name, spec in contracts.CATALOG.items():
            self.assertEqual(spec.api_version, contracts.API_VERSION)  # =3
            self.assertEqual(spec.name, name)
            self.assertTrue(spec.roles)
            self.assertLessEqual(set(spec.roles), set(contracts.ROLES))
            self.assertIn(spec.ownership, ("task", "global", "local"))
            self.assertTrue(spec.errors)
            self.assertLessEqual(set(spec.errors), set(contracts.ERROR_TABLE))
            # required/enums 由 request 字段单源派生
            self.assertEqual(
                tuple(spec.required),
                tuple(f for f, fs in spec.request.items() if fs["required"]))
            self.assertEqual(
                spec.enums,
                {f: tuple(fs["enum"]) for f, fs in spec.request.items()
                 if fs.get("enum")})

    def test_ownership_follows_task_param(self):
        for spec in contracts.CATALOG.values():  # task 域工具必含 task 字段
            if spec.ownership == "task":
                self.assertIn("task", spec.request, spec.name)
        self.assertEqual(contracts.CATALOG["board"].ownership, "global")  # task 仅明细参数
        self.assertEqual(contracts.CATALOG["sleep"].ownership, "local")

    def test_role_anchors(self):  # 与 v2 语义一致的角色面
        self.assertEqual(contracts.CATALOG["enforce"].roles, ("root",))
        self.assertEqual(contracts.CATALOG["task_dispatch"].roles,
                         contracts.ROLES)  # v2 may() 三角色均放行（F75 root CLI）
        for locked in ("board", "doctor", "task_update", "task_wait",
                       "task_deliver", "task_reverify"):
            self.assertNotIn("worker", contracts.CATALOG[locked].roles,
                             locked)

    def test_error_surface(self):
        base = {"invalid_argument", "unauthorized", "internal"}
        for spec in contracts.CATALOG.values():
            if spec.ownership == "task":
                self.assertLessEqual(base | {"not_found", "forbidden",
                                             "conflict"}, set(spec.errors),
                                     spec.name)
        self.assertIn("forbidden", contracts.CATALOG["enforce"].errors)
        self.assertEqual(contracts.CATALOG["sleep"].errors,
                         ("invalid_argument",))


class TestMcpBridge(unittest.TestCase):

    def test_legacy_schema_golden_equivalence(self):
        tools = GOLDEN["mcp"]["tools"]
        routes = GOLDEN["mcp"]["http_tool_routes"]
        self.assertEqual(len(routes), 15)
        for legacy in [*routes, "sleep"]:
            schema = contracts.mcp_schema(legacy)
            self.assertEqual(sorted(schema), ["enums", "name", "required"],
                             legacy)
            self.assertEqual(schema["name"], legacy)
            self.assertEqual(schema["required"], tools[legacy]["required"],
                             legacy)
            self.assertEqual(schema["enums"], tools[legacy]["enums"], legacy)

    def test_v3_only_tool_has_no_legacy_schema(self):
        for fresh in ("timer_create", "approval_request", "task_reverify"):
            with self.assertRaises(ValueError):
                contracts.mcp_schema(fresh)


class TestValidator(unittest.TestCase):

    def test_valid_passthrough(self):
        self.assertEqual(contracts.validate_params("task_update", {
            "task": "ag-1", "verify_cmd": "pytest -q", "timeout": 300}),
            {"task": "ag-1", "verify_cmd": "pytest -q", "timeout": 300})
        self.assertEqual(contracts.validate_params("enforce", {"on": True}),
                         {"on": True})
        for name, ok in (("board", {"task": "ag-9", "view": "full"}),  # F127
                         ("task_wait", {"task": "ag-1", "wait_on": None})):
            self.assertEqual(contracts.validate_params(name, ok), ok)

    def test_missing_required(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_params("task_update", {"task": "ag-1"})
        self.assertEqual((cm.exception.code, cm.exception.details["field"]),
                         ("invalid_argument", "verify_cmd"))
        self.assertIn("verify_cmd", cm.exception.message)

    def test_bool_is_not_integer(self):
        for bad in (True, False):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_params("task_create",
                                          {"goal": "g", "timeout": bad})
        self.assertEqual(contracts.validate_params(
            "task_create", {"goal": "g", "timeout": 30})["timeout"], 30)

    def test_string_is_not_object(self):
        for bad in ("ag-1", ["ag-1"], None):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_params("task_cancel", bad)

    def test_unknown_fields_listed(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_params(
                "task_cancel", {"task": "ag-1", "why": "x", "extra": 1})
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertIn("why", cm.exception.message)
        self.assertIn("extra", cm.exception.message)
        self.assertEqual(cm.exception.details["fields"], ["extra", "why"])

    def test_enum_whitelist(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_params("message_post",
                                      {"kind": "shout", "body": "b"})
        self.assertIn("kind", cm.exception.message)
        self.assertEqual(contracts.validate_params(
            "message_post", {"kind": "note", "body": "b"})["kind"], "note")

    def test_type_mismatches(self):
        for name, bad in (("message_post", {"kind": "note", "body": 3}),
                          ("enforce", {"on": "yes"}),
                          ("wiki_write", {"path": "p", "text": {"a": 1}})):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_params(name, bad)


class TestErrorModel(unittest.TestCase):

    def test_make_error_shape(self):
        self.assertEqual(
            contracts.make_error("conflict", "task closed", {"task": "ag-1"}),
            {"code": "conflict", "message": "task closed",
             "details": {"task": "ag-1"}, "retryable": True})
        self.assertEqual(
            contracts.make_error("invalid_argument", "m")["retryable"], False)
        self.assertEqual(contracts.make_error("forbidden", "m")["details"], {})

    def test_three_way_mapping_complete_and_distinct(self):
        codes = list(contracts.ERROR_TABLE)
        self.assertEqual(len(codes), 6)
        self.assertEqual(len({contracts.http_status(c) for c in codes}), 6)
        self.assertEqual(len({contracts.mcp_code(c) for c in codes}), 6)
        for c in codes:
            self.assertEqual(contracts.error_code_for_http(
                contracts.http_status(c)), c)
            self.assertEqual(contracts.error_code_for_mcp(
                contracts.mcp_code(c)), c)
        self.assertIsNone(contracts.error_code_for_http(418))
        self.assertIsNone(contracts.error_code_for_mcp(-32601))

    def test_mapping_matches_golden(self):
        derived = {str(contracts.http_status(c)): contracts.mcp_code(c)
                   for c in contracts.ERROR_TABLE}
        self.assertEqual(derived, GOLDEN["mcp"]["error_codes"])


class TestTaskCreateNested(unittest.TestCase):

    def test_defaults_derived(self):
        self.assertEqual(contracts.validate_task_create({"goal": "g"}), {
            "goal": "g", "title": None,
            "execution": {"model": None, "after": None, "req_id": None,
                          "project_dir": None, "client_key": None,
                          "quick": None},
            "verify": {"cmd": None, "review_cmd": None,
                       "verify_pending": None, "verify_base_exempt": None,
                       "timeout": None},
            "delivery": {"deliverable": "out_of_band", "target_branch": None}})

    def test_groups_kept_and_validated(self):
        n = contracts.validate_task_create({
            "goal": "g", "title": "T", "execution": {"model": "glm-5.3"},
            "verify": {"cmd": "pytest -q", "timeout": 300},
            "delivery": {"deliverable": "external_branch",
                         "target_branch": "v3-runtime"}})
        self.assertEqual((n["title"], n["execution"]["model"],
                          n["delivery"]["deliverable"]),
                         ("T", "glm-5.3", "external_branch"))
        self.assertEqual((n["verify"]["cmd"], n["verify"]["timeout"]),
                         ("pytest -q", 300))

    def test_missing_goal(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_task_create({"verify": {}})
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertIn("goal", cm.exception.message)

    def test_string_is_not_object_group(self):
        for group in ("verify", "execution"):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_task_create({"goal": "g", group: ["m"]})
        with self.assertRaises(contracts.ContractError):  # falsy 同拒（0/""/[]/False）
            contracts.validate_task_create({"goal": "g", "verify": 0})

    def test_explicit_null_rejected(self):  # 显式 null 按类型拒，None 仅缺省
        for bad in ({"goal": "g", "title": None},
                    {"goal": "g", "verify": {"cmd": None}},
                    {"goal": "g", "execution": None}):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_task_create(bad)

    def test_unknown_fields_listed(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_task_create({"goal": "g", "verfy": {}})
        self.assertIn("verfy", cm.exception.message)
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.validate_task_create({"goal": "g", "verify": {"cm": "x"}})
        self.assertIn("cm", cm.exception.message)

    def test_type_and_enum_in_groups(self):
        with self.assertRaises(contracts.ContractError):  # bool 冒充 int
            contracts.validate_task_create({"goal": "g", "verify": {"timeout": True}})
        with self.assertRaises(contracts.ContractError):  # 白名单外
            contracts.validate_task_create(
                {"goal": "g", "delivery": {"deliverable": "carrier_pigeon"}})
        self.assertEqual(  # 显式合法值经校验原样保留
            contracts.validate_task_create(
                {"goal": "g", "delivery": {"deliverable": "out_of_band"}})
            ["delivery"]["deliverable"], "out_of_band")


class TestV2ToNested(unittest.TestCase):
    FLAT = {"goal": "migrate", "title": "T", "verify_cmd": "pytest -q",
            "review_cmd": "ocr review", "verify_pending": True,
            "verify_base_exempt": True, "quick": True,
            "timeout": 300, "deliverable": "external_branch",
            "target_branch": "v3-runtime", "model": "glm-5.3", "after": "ag-1",
            "req_id": "F130", "project_dir": "/repo", "client_key": "k1"}
    NESTED = {
        "goal": "migrate", "title": "T",
        "execution": {"model": "glm-5.3", "after": "ag-1", "req_id": "F130",
                      "project_dir": "/repo", "client_key": "k1",
                      "quick": True},
        "verify": {"cmd": "pytest -q", "review_cmd": "ocr review",
                   "verify_pending": True, "verify_base_exempt": True,
                   "timeout": 300},
        "delivery": {"deliverable": "external_branch", "target_branch":
                     "v3-runtime"}}

    def test_conversion(self):
        self.assertEqual(contracts.task_create_v2_to_nested(self.FLAT),
                         self.NESTED)
        self.assertEqual(  # 全字段经校验＝规范形（新旗字段类型面同过）
            contracts.validate_task_create(self.NESTED), self.NESTED)

    def test_absent_goal_omitted(self):  # 缺席键不输出 None（两侧诊断一致）
        self.assertNotIn("goal", contracts.task_create_v2_to_nested({}))

    def test_pure_function(self):
        frozen = copy.deepcopy(self.FLAT)
        out1 = contracts.task_create_v2_to_nested(self.FLAT)
        self.assertEqual(out1, contracts.task_create_v2_to_nested(self.FLAT))
        self.assertEqual(self.FLAT, frozen)  # 输入不被改动
        out1["verify"]["cmd"] = "mutated"   # 输出与输入不共享可变结构
        self.assertEqual(self.FLAT, frozen)
        self.assertNotEqual(contracts.task_create_v2_to_nested(self.FLAT)
                            ["verify"]["cmd"], "mutated")

    def test_unknown_flat_field_rejected(self):
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.task_create_v2_to_nested({"goal": "g",
                                                "verify_cmd2": "x"})
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertIn("verify_cmd2", cm.exception.message)

    def test_flat_surface_equals_spec(self):
        self.assertEqual(set(contracts.V2_TASK_CREATE_MAP),
                         set(contracts.CATALOG["task_create"].request)
                         - {"goal", "title"})

    def test_roundtrip_validates(self):
        norm = contracts.validate_task_create(
            contracts.task_create_v2_to_nested(
                {"goal": "g", "verify_cmd": "pytest -q"}))
        self.assertEqual(norm["verify"]["cmd"], "pytest -q")
        self.assertEqual(norm["delivery"]["deliverable"], "out_of_band")


if __name__ == "__main__":
    unittest.main()
