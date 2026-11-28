"""Req: F141 — v2↔v3 适配层（opencode-agora v3 P1-T5）。

锚 agorad/adapter.py（仅 stdlib＋contracts＋domain——test_architecture 守卫）：
请求向 15 根路由扁平/旧名→v3 正名/nested 规范形逐一；响应向 board/doctor/
check_mail 与 F128 golden 逐字段；错误向六档三处映射不丢档；时间向 epoch 毫秒
↔v2 浮点秒（None 透传往返）；旧名别名等价；task_create 深缺省三形态（v2 缺席
deliverable＝None→服务层 managed 保真——F163 边缘缺省 out_of_band 是 v3 原生
调用面口径）；worker 写面拒绝矩阵传导（拒绝归服务层同场判定）。断言清单见
docs/reqs/opencode-agora.md F141。
"""
import json
import unittest
from pathlib import Path

from agorad import adapter, contracts, domain

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads(
    (ROOT / "tests" / "fixtures" / "v3_contract_golden.json").read_text(
        encoding="utf-8"))

EVENTS = [
    domain.Event(seq=1, ts_ms=1697930000123, actor="d1",
                 event="task_created", task="ag-1"),
    domain.Event(seq=2, ts_ms=1697940000456, actor="d1", event="escalate",
                 task="ag-2", detail={"kind": "stalled", "note": "n"}),
]


def _scenario():
    """六任务看板场景：五桶各有代表＋链父子（d1→w1→w2）。"""
    return {
        "ag-1": domain.Task(id="ag-1", state="todo", title="one",
                            dispatcher_sid="d1", created_ms=1697930000000,
                            project_dir="/repo", goal="g"),
        "ag-2": domain.Task(id="ag-2", state="running", title="two",
                            dispatcher_sid="d1", worker_sid="w1",
                            created_ms=1697940000000,
                            last_seen_ms=1697940300000,
                            project_dir="/repo", goal="g",
                            delivery=domain.DeliveryState(mode="managed")),
        "ag-3": domain.Task(id="ag-3", state="merged", title="three",
                            dispatcher_sid="w1", worker_sid="w2",
                            created_ms=1697920000000, project_dir="/repo",
                            goal="g"),
        "ag-4": domain.Task(id="ag-4", state="blocked", title="four",
                            dispatcher_sid="d1", created_ms=1697910000000,
                            project_dir="/repo", goal="g",
                            delivery=domain.DeliveryState(mode="managed",
                                                          status="failed")),
        "ag-5": domain.Task(id="ag-5", state="blocked", title="five",
                            dispatcher_sid="d1", created_ms=1697900000000,
                            project_dir="/repo", goal="g"),
        "ag-6": domain.Task(id="ag-6", state="cancelled", title="six",
                            dispatcher_sid="d1", created_ms=1697890000000,
                            project_dir="/repo", goal="g"),
    }


class TestRequestDirection(unittest.TestCase):
    """断言1：15 个 v2 根路由扁平参数→v3 正名/nested 映射逐一有锚。"""

    def test_legacy_routes_equal_golden(self):
        self.assertEqual(sorted(adapter.LEGACY_ROUTES),
                         GOLDEN["mcp"]["http_tool_routes"])

    def test_same_name_tools_identity(self):  # 12 同名路由：名称与参数零变化
        cases = {
            "task_dispatch": {"task": "ag-1"},
            "task_update": {"task": "ag-1", "verify_cmd": "pytest -q",
                            "timeout": 300},
            "task_cancel": {"task": "ag-1", "force": True},
            "task_deliver": {"task": "ag-1", "outcome": "delivered",
                             "commit": "a" * 40, "base": "b" * 40,
                             "reason": "r"},
            "task_wait": {"task": "ag-1", "wait_on": None},
            "board": {"task": "ag-9", "view": "full"},
            "doctor": {},
            "repo_board": {"repo": "/repo"},
            "wiki_read": {"path": "spec/x.md"},
            "wiki_write": {"path": "spec/x.md", "text": "正文"},
            "enforce": {"on": True},
        }
        for name, params in cases.items():
            self.assertEqual(adapter.adapt_request(name, params, "root"),
                             (name, params), name)

    def test_legacy_alias_names_map_to_v3(self):  # reverify/post/check_mail
        for legacy, v3, params, role in (
                ("reverify", "task_reverify", {"task": "ag-1"}, "root"),
                ("post", "message_post",
                 {"task": "ag-1", "kind": "note", "body": "b"}, "worker"),
                ("check_mail", "message_pull", {}, "dispatcher")):
            self.assertEqual(adapter.adapt_request(legacy, params, role),
                             (v3, params), legacy)

    def test_task_create_flat_to_nested(self):
        flat = {"goal": "migrate", "title": "T", "project_dir": "/repo",
                "verify_cmd": "pytest -q", "review_cmd": "ocr review",
                "verify_pending": False, "verify_base_exempt": True,
                "quick": False, "timeout": 300, "harness": "dsh",
                "deliverable": "external_branch",
                "target_branch": "v3-runtime", "model": "glm-5.3",
                "after": "ag-1", "req_id": "F141", "client_key": "k1"}
        name, spec = adapter.adapt_request("task_create", flat, "root")
        self.assertEqual(name, "task_create")
        self.assertEqual(spec, {
            "goal": "migrate", "title": "T",
            "execution": {"model": "glm-5.3", "after": "ag-1",
                          "req_id": "F141", "project_dir": "/repo",
                          "client_key": "k1", "quick": False,
                          "harness": "dsh"},
            "verify": {"cmd": "pytest -q", "review_cmd": "ocr review",
                       "verify_pending": False, "verify_base_exempt": True,
                       "timeout": 300},
            "delivery": {"deliverable": "external_branch",
                         "target_branch": "v3-runtime"}})

    def test_worker_may_omit_project_dir_but_trusted_may_not(self):
        # v2 边缘：worker 缺省继承（缺席合法）；受信角色缺 project_dir＝400
        _, spec = adapter.adapt_request("task_create", {"goal": "g"},
                                        "worker")
        self.assertIsNone(spec["execution"]["project_dir"])
        with self.assertRaises(contracts.ContractError) as cm:
            adapter.adapt_request("task_create", {"goal": "g"}, "dispatcher")
        self.assertEqual(cm.exception.code, "invalid_argument")
        self.assertIn("project_dir", cm.exception.message)

    def test_non_legacy_tool_rejected(self):
        for name in ("timer_create", "approval_request", "task_reverify",
                     "message_post", "sleep", "nope"):
            with self.assertRaises(contracts.ContractError) as cm:
                adapter.adapt_request(name, {}, "root")
            self.assertEqual(cm.exception.code, "invalid_argument", name)

    def test_flat_validation_carried(self):
        # 扁平面类型/enum/未知字段/缺 required 经 contracts 同判
        for bad in ({"goal": "g", "project_dir": "/r", "timeout": True},
                    {"goal": "g", "project_dir": "/r",
                     "deliverable": "pigeon"},
                    {"goal": "g", "project_dir": "/r", "extra": 1},
                    {"project_dir": "/r"}):
            with self.assertRaises(contracts.ContractError):
                adapter.adapt_request("task_create", bad, "root")


class TestTaskCreateDefaults(unittest.TestCase):
    """断言6：v2 缺席分组字段→缺省派生与 v3 nested/服务层缺省一致
    （managed/out_of_band/external_branch 三形态）。"""

    def _spec(self, flat):
        return adapter.adapt_request("task_create",
                                     {"project_dir": "/r", **flat},
                                     "root")[1]

    def test_absent_deliverable_stays_none_for_managed(self):
        # v2「absent＝普通合并任务」保真：适配层不套用 F163 边缘缺省
        # out_of_band（v3 原生调用面口径）——服务层 None→managed 初始行
        self.assertIsNone(self._spec({"goal": "g"})["delivery"]["deliverable"])

    def test_explicit_deliverables_preserved(self):
        for dl in ("out_of_band", "external_branch"):
            self.assertEqual(
                self._spec({"goal": "g", "deliverable": dl})
                ["delivery"]["deliverable"], dl)

    def test_absent_groups_derive_to_none_canonical(self):
        # 深缺省＝nested 规范形：三组恒在、缺项补 None（deliverable 例外保 None）
        spec = self._spec({"goal": "g"})
        self.assertEqual(spec["execution"],
                         {"model": None, "after": None, "req_id": None,
                          "project_dir": "/r", "client_key": None,
                          "quick": None, "harness": None})
        self.assertEqual(spec["verify"],
                         {"cmd": None, "review_cmd": None,
                          "verify_pending": None, "verify_base_exempt": None,
                          "timeout": None})
        self.assertEqual(spec["delivery"],
                         {"deliverable": None, "target_branch": None})

    def test_service_side_default_agreement(self):
        # 与 contracts.validate_task_create 规范形唯一分歧面＝deliverable，
        # 且方向＝v2 保真（None）；其余逐键一致
        nested = contracts.validate_task_create(
            {"goal": "g", "execution": {"project_dir": "/r"}})
        nested["delivery"]["deliverable"] = None
        self.assertEqual(self._spec({"goal": "g"}), nested)


class TestWorkerMatrixCarriage(unittest.TestCase):
    """断言7：v2 worker 建单显式字段拒绝矩阵经适配后与 contracts 判定一致
    （拒绝归服务层 tasksvc._worker_matrix 同场 forbidden——显式字段原样传导
    不洗不丢、缺席＝None 不误触）。"""

    EXPLICIT = (  # (扁平键, 显式值, spec 路径)——v2 task.create 写面全集
        ("verify_cmd", "pytest -q", ("verify", "cmd")),
        ("timeout", 300, ("verify", "timeout")),
        ("verify_pending", True, ("verify", "verify_pending")),
        ("verify_base_exempt", False, ("verify", "verify_base_exempt")),
        ("review_cmd", "ocr review", ("verify", "review_cmd")),
        ("target_branch", "v3-runtime", ("delivery", "target_branch")),
        ("model", "glm-5.3", ("execution", "model")),
        ("deliverable", "out_of_band", ("delivery", "deliverable")))

    def test_explicit_fields_carried_for_service_matrix(self):
        for key, value, (group, sub) in self.EXPLICIT:
            with self.subTest(key=key):
                _, spec = adapter.adapt_request(
                    "task_create", {"goal": "g", key: value}, "worker")
                carried = spec[group][sub]
                if key == "verify_pending":  # 矩阵按真值判（`or None`）
                    self.assertTrue(carried)
                else:
                    self.assertEqual(carried, value)

    def test_absent_fields_are_none_no_false_trip(self):
        _, spec = adapter.adapt_request("task_create", {"goal": "g"},
                                        "worker")
        for key, _value, (group, sub) in self.EXPLICIT:
            if key == "verify_pending":
                continue
            self.assertIsNone(spec[group][sub], key)

    def test_falsy_verify_pending_not_carried_as_true(self):
        _, spec = adapter.adapt_request(
            "task_create", {"goal": "g", "verify_pending": False}, "worker")
        self.assertFalse(spec["verify"]["verify_pending"])  # `or None` 不触


class TestBoardResponse(unittest.TestCase):
    """断言2（board）：buckets/chain/forest/tasks 键集与 F128 golden 一致。"""

    def test_full_view_top_keys_golden(self):
        out = adapter.board_full(_scenario(), EVENTS)
        self.assertEqual(sorted(out), GOLDEN["board_full_view"]["top_keys"])

    def test_buckets_five_names_and_membership(self):
        b = adapter.board_full(_scenario(), EVENTS)["buckets"]
        self.assertEqual(list(b), ["已合并", "在飞", "失败", "阻塞",
                                   "已取消"])  # v2 桶名与插入序
        self.assertEqual(b["已合并"], ["ag-3"])
        self.assertEqual(b["在飞"], ["ag-1", "ag-2"])
        self.assertEqual(b["失败"], ["ag-4"])  # blocked＋delivery failed（F98）
        self.assertEqual(b["阻塞"], ["ag-5"])
        self.assertEqual(b["已取消"], ["ag-6"])

    def test_tasks_rows_whitelist_and_times(self):
        out = adapter.board_full(_scenario(), EVENTS)
        t = out["tasks"]["ag-2"]
        self.assertLessEqual(
            set(t), set(GOLDEN["board_full_view"]["task_fields"])
            | {"verify_budget"})  # 白名单外键不可达
        self.assertNotIn("client_key", t)
        self.assertEqual(t["created"], 1697940000.0)  # 毫秒→v2 浮点秒
        self.assertEqual(t["last_seen"], 1697940300.0)
        self.assertEqual(t["delivery"]["mode"], "managed")
        self.assertEqual(t["verify_budget"], {"timeout": None,
                                              "source": "default"})

    def test_delivery_none_row_and_conflict_files(self):
        out = adapter.board_full(_scenario(), EVENTS)
        self.assertIsNone(out["tasks"]["ag-1"]["delivery"])  # 旧行＝None
        conflicted = adapter.task_row(domain.Task(
            id="ag-7", delivery=domain.DeliveryState(
                status="failed", conflict_files=["a.py"])))
        self.assertEqual(conflicted["delivery"]["conflict_files"], ["a.py"])
        initial = adapter.task_row(domain.Task(
            id="ag-8", delivery=domain.DeliveryState()))
        self.assertEqual(set(initial["delivery"]),
                         {"mode", "status", "commit", "base", "target_branch",
                          "attempt", "local_status", "error"})  # 初值 8 键
        self.assertNotIn("verify_budget",  # 无 verify 字典＝不投影（防御面）
                         adapter.task_row(domain.Task(
                             id="ag-9", verify=None)))

    def test_gate_projections_conditional(self):
        row = adapter.task_row(domain.Task(
            id="ag-9", state="done",
            gate=domain.GateState(
                in_gate=True, review_fail_streak=2,
                gate_resource={"waiting": True, "limit": 1},
                gate_artifact=domain.Artifact(
                    task_id="ag-9", phase="verify",
                    path="/h/gate/ag-9/output.log", rc=1,
                    failure_kind="quality_red", duration_s=12.5, bytes=99))))
        self.assertEqual(row["gate_resource"], {"waiting": True, "limit": 1})
        self.assertEqual(row["gate_artifact"], {
            "path": "/h/gate/ag-9/output.log", "phase": "verify", "rc": 1,
            "failure_kind": "quality_red", "duration_s": 12.5, "bytes": 99})
        self.assertEqual(row["review_fail_streak"], 2)
        self.assertTrue(row["in_gate"])
        plain = adapter.task_row(domain.Task(id="ag-10"))
        self.assertNotIn("gate_resource", plain)  # 瞬态缺席不投影
        self.assertNotIn("gate_artifact", plain)

    def test_verify_budget_source_fallback(self):
        # legacy 行 timeout_source 缺席→按既有 timeout 推断（F126 R1 两观测
        # 面不分裂）
        t = domain.Task(id="t", verify=domain.VerifySpec(timeout_s=300,
                                                         timeout_source=""))
        self.assertEqual(adapter.task_row(t)["verify_budget"],
                         {"timeout": 300, "source": "task"})

    def test_forest_all_present_and_events_escalate_only(self):
        out = adapter.board_full(_scenario(), EVENTS)
        self.assertEqual(sorted(out["forest"]), sorted(_scenario()))
        self.assertEqual([e["event"] for e in out["events"]], ["escalate"])
        self.assertEqual(out["events"][0]["ts"], 1697940000.456)
        self.assertEqual(out["events"][0]["note"], "n")  # detail 摊平

    def test_stats_model_buckets(self):
        s = adapter.board_full(_scenario(), EVENTS)["stats"]
        self.assertEqual(set(s), {"default"})  # model None 归 default 桶
        self.assertEqual(s["default"]["tasks"], 6)
        self.assertEqual(s["default"]["merged"], 1)  # ag-3 一次过
        self.assertEqual(s["default"]["rework"], 0)
        self.assertEqual(s["default"]["first_created"], 1697890000.0)
        self.assertEqual(s["default"]["last_created"], 1697940000.0)
        self.assertIsNone(s["default"]["drift"])  # 窗口样本 <10 不下判

    def test_default_view_three_keys_and_chain_order(self):
        out = adapter.board_default(_scenario(), "d1")
        self.assertEqual(sorted(out), ["buckets", "chain", "forest"])
        # d1 直派 ag-1/2/4/5/6（字典序）；ag-2 worker w1 再派 ag-3 逐跳展开
        self.assertEqual(out["chain"],
                         ["ag-1", "ag-2", "ag-4", "ag-5", "ag-6", "ag-3"])


class TestDoctorResponse(unittest.TestCase):
    """断言2（doctor）：顶层键与 F128 golden 一致（排行/候选/卫生审计镜像
    core.report 口径；盘面/活性经注入——适配层零 I/O）。"""

    def test_base_keys_golden(self):
        d = adapter.doctor(_scenario(), on_disk=lambda e: True)
        self.assertEqual(sorted(d), GOLDEN["doctor"]["base_keys"])

    def test_rankings_stale_and_env(self):
        tasks = {"ag-2": domain.Task(
            id="ag-2", state="running", env_dir="/env/ag-2",
            created_ms=1697930000000, last_seen_ms=1697930300000,
            project_dir="/repo",
            verify=domain.VerifySpec(cmd="make check"))}
        d = adapter.doctor(tasks, now_ms=1697932000000,  # now-基线 1700s>1200
                           env_activity=lambda tid: 0.0,
                           on_disk=lambda e: False,
                           verify_hygiene=lambda cmd, proj: [{"issue": "shell"}])
        self.assertEqual(d["age_rank"], [["ag-2", 1700.0]])
        self.assertEqual(d["last_seen_rank"], [["ag-2", 1697930300.0]])
        self.assertEqual(d["stale_candidates"], [["ag-2", 1697930300.0]])
        self.assertEqual(d["env"], [{"tid": "ag-2", "env_dir": "/env/ag-2",
                                     "on_disk": False}])
        self.assertEqual(d["verify_hygiene_findings"],
                         [{"tid": "ag-2", "state": "running",
                           "issue": "shell"}])

    def test_stale_waiting_and_gate_busy_exempt(self):
        waiting = domain.Task(id="w", state="running", created_ms=1,
                              waiting_on="ag-9")  # F124 运行中受控等待免判
        queued = domain.Task(id="q", state="done", created_ms=1)  # 门忙排队免判
        in_gate = domain.Task(id="g", state="done", created_ms=1,
                              gate=domain.GateState(in_gate=True))
        stale = domain.Task(id="s", state="running", created_ms=1)
        d = adapter.doctor({"w": waiting, "q": queued, "g": in_gate,
                            "s": stale},
                           now_ms=1000000000000, on_disk=lambda e: True)
        # 门内行不入候选（判僵走门内相位审计）；waiting/门忙 done 免判；
        # 门忙非 done 照候选（v2 stale_candidates 同判）
        self.assertEqual(d["stale_candidates"], [["s", 0.001]])

    def test_on_disk_required_fail_fast(self):  # 评审环 R1：必注关键字无缺省
        with self.assertRaises(TypeError):
            adapter.doctor({}, env_activity=lambda tid: 0.0)

    def test_conditional_keys(self):
        d = adapter.doctor({}, models=["m"], verify_profiles={}, home="/h",
                           on_disk=lambda e: True)
        self.assertEqual(sorted(d), sorted(
            GOLDEN["doctor"]["base_keys"] + GOLDEN["doctor"]["models_keys"]
            + [GOLDEN["doctor"]["verify_profiles_key"]]))
        self.assertEqual(d["verify_profiles"]["count"], 0)
        self.assertEqual(d["verify_profiles"]["path"],
                         "/h/.agora/verify-profiles.json")
        self.assertEqual(d["models_note"], adapter._MODELS_NOTE)


class TestCheckMailResponse(unittest.TestCase):
    """断言2（check_mail）：messages 列表 id=int、frontmatter 其余 str、
    body 原文（F128 断言⑤形状）。"""

    def test_message_dict_shape(self):
        body = "第一行\n第二行"
        m = adapter.message_dict(domain.Message(
            id=7, ts_ms=1697932800123, task_id="t1", kind="note",
            from_sid="w1", to_sid="d1", body=body))
        self.assertEqual({k for k in m},
                         {"id", "body", "ts", "task", "kind", "from", "to"})
        self.assertIsInstance(m["id"], int)
        for k, v in m.items():
            if k not in ("id", "body"):
                self.assertIsInstance(v, str, k)
        self.assertEqual(m["body"], body)  # 多行原文逐字节保持
        self.assertEqual(m["ts"], str(1697932800.123))  # store 口径 str(秒)

    def test_check_mail_envelope_and_replay_of(self):
        out = adapter.check_mail([domain.Message(
            id=1, ts_ms=100, task_id="t", kind="question", from_sid="a",
            to_sid="b", body="x")])
        self.assertEqual(list(out), ["messages"])
        self.assertEqual(out["messages"][0]["id"], 1)
        replay = adapter.message_dict(domain.Message(
            id=2, ts_ms=100, task_id="t", kind="question", from_sid="a",
            to_sid="b", body="x", replay_of=1))
        self.assertEqual(replay["replay_of"], "1")  # F75 在案才在键


class TestErrorDirection(unittest.TestCase):
    """断言3：v3 error{code,message,details,retryable}→v2 平面
    {"error": "<message>"}＋HTTP 状态/MCP 负码六档不丢档。"""

    def test_six_tiers_http_flat_body(self):
        for code in contracts.ERROR_TABLE:
            status, body = adapter.error_response(
                contracts.make_error(code, "boom"))
            self.assertEqual(status, contracts.http_status(code), code)
            self.assertEqual(body, {"error": "boom"}, code)

    def test_mcp_negative_code(self):
        for code in contracts.ERROR_TABLE:
            self.assertEqual(
                adapter.error_response(contracts.make_error(code, "m"),
                                       "mcp"),
                {"code": contracts.mcp_code(code), "message": "m"}, code)

    def test_mapping_matches_golden_no_tier_loss(self):
        derived = {str(adapter.error_response(
                       contracts.make_error(c, "m"))[0]):
                   adapter.error_response(contracts.make_error(c, "m"),
                                          "mcp")["code"]
                   for c in contracts.ERROR_TABLE}
        self.assertEqual(derived, GOLDEN["mcp"]["error_codes"])
        self.assertEqual({adapter.error_response(
            contracts.make_error(c, "m"))[0]
            for c in contracts.ERROR_TABLE},
            {400, 401, 403, 404, 409, 500})  # 六档互异不塌档

    def test_contract_error_and_unknown_code(self):
        status, body = adapter.error_response(
            contracts.ContractError("not_found", "任务 ag-1 不存在"))
        self.assertEqual((status, body), (404, {"error": "任务 ag-1 不存在"}))
        with self.assertRaises(contracts.ContractError):
            adapter.error_response({"code": "teapot", "message": "x"})


class TestTimeDirection(unittest.TestCase):
    """断言4：v3 epoch 毫秒→v2 浮点秒（None 透传）往返一致。"""

    def test_ms_to_seconds_and_none_passthrough(self):
        row = adapter.task_row(domain.Task(id="t", created_ms=1697932800123,
                                           last_seen_ms=None))
        self.assertEqual(row["created"], 1697932800.123)
        self.assertIsNone(row["last_seen"])

    def test_roundtrip_within_ms(self):
        for ms in (0, 1, 123, 2046, 1697932800123, 2400000000000):
            row = adapter.task_row(domain.Task(id="t", created_ms=ms))
            self.assertEqual(domain.epoch_ms(row["created"]), ms, ms)


class TestAliasEquivalence(unittest.TestCase):
    """断言5：reverify/post/check_mail 与 v3 正名等价（同输入同输出）；
    其余同名工具经适配后语义零变化（参数原样、校验同判）。"""

    def test_request_aliases_equal_v3_native_call(self):
        self.assertEqual(
            adapter.adapt_request("reverify", {"task": "ag-1"}, "root"),
            ("task_reverify", {"task": "ag-1"}))
        self.assertEqual(
            adapter.adapt_request(
                "post", {"kind": "note", "body": "b", "task": "ag-1"},
                "worker"),
            ("message_post", {"kind": "note", "body": "b", "task": "ag-1"}))
        self.assertEqual(adapter.adapt_request("check_mail", {},
                                               "dispatcher"),
                         ("message_pull", {}))

    def test_alias_params_validated_same_as_v3(self):
        # 别名与正名同判（enum/类型/未知字段）——错值同码同因
        for legacy, bad, field in (
                ("post", {"kind": "shout", "body": "b"}, "kind"),
                ("reverify", {"task": 3}, "task"),
                ("check_mail", {"surprise": 1}, "surprise")):
            with self.assertRaises(contracts.ContractError) as cm:
                adapter.adapt_request(legacy, bad, "root")
            self.assertEqual(cm.exception.code, "invalid_argument", legacy)
            self.assertIn(field, cm.exception.message, legacy)


class TestDetailRowF144(unittest.TestCase):
    """Req: F144 — board 单任务明细全字段行＋v2 三态判读镜像。"""

    _FULL = {
        "state", "title", "goal", "worker_sid", "dispatcher_sid", "root_sid",
        "chain_depth", "last_seen", "created", "updated", "project_dir",
        "target_branch", "req_id", "model", "quick", "deliverable",
        "client_key", "after", "delivery", "env_dir", "env_base", "commit",
        "rebased_from", "verify", "verify_pending", "verify_base_exempt",
        "review_cmd", "waiting_on", "fail_streak", "in_gate", "gate_env"}

    def test_full_row_key_set(self):
        t = domain.Task(id="ag-9", state="closed", title="t", goal="g",
                        root_sid="root", dispatcher_sid="root",
                        worker_sid="w1", chain_depth=1, created_ms=1000200,
                        updated_ms=1000400, project_dir="/repo",
                        target_branch="main", commit="c" * 40,
                        verify=domain.VerifySpec(
                            cmd="true", state="registered",
                            registered_by="root", registered_at_ms=1000300),
                        delivery=domain.DeliveryState(
                            status="delivered", delivered_at_ms=1000500),
                        gate=domain.GateState(
                            gate_resource={"waiting": False, "resource": "/r",
                                           "limit": 1, "slot": 0},
                            gate_artifact=domain.Artifact(
                                task_id="ag-9", phase="verify", path="/p",
                                rc=0, failure_kind="green", bytes=0)))
        row = adapter.task_row_detail(t)
        self.assertEqual(set(row), self._FULL | {"gate_resource",
                                                 "gate_artifact"})
        self.assertEqual(row["verify"]["registered_at"], 1000.3)
        self.assertEqual(row["delivery"]["delivered_at"], 1000.5)
        self.assertEqual(row["fail_streak"], 0)
        self.assertNotIn("review_fail_streak", row)
        self.assertNotIn("verify_budget", row)
        self.assertIsNone(adapter.task_row_detail(
            domain.Task(id="ag-x"))["delivery"])

    def test_classification_v2_mirror(self):
        tasks = _scenario()
        fresh = tasks["ag-2"]  # running＋last_seen 新
        self.assertEqual(adapter.detail_classification(
            fresh, tasks, now=1697940300.0 + 60, act=0.0), "fresh")
        self.assertEqual(adapter.detail_classification(  # 超窗＋无 env 活性＝僵态候选
            fresh, tasks, now=1697940300.0 + 1300, act=0.0),
            "stale_candidate")
        self.assertEqual(adapter.detail_classification(  # last_seen 老＋env 活跃＝busy
            fresh, tasks, now=1697940300.0 + 1300, act=1697940300.0 + 1250),
            "busy_round")
        self.assertIsNone(adapter.detail_classification(  # 终态＝None
            tasks["ag-3"], tasks, now=1697940300.0, act=0.0))
        self.assertIsNone(adapter.detail_classification(  # 无 worker＝None
            tasks["ag-1"], tasks, now=1697940300.0, act=0.0))
        stale = domain.Task(id="ag-7", state="running", dispatcher_sid="d1",
                            worker_sid="w9", created_ms=1697000000000,
                            project_dir="/repo", goal="g")
        self.assertEqual(adapter.detail_classification(
            stale, {stale.id: stale}, now=1697940300.0, act=0.0),
            "stale_candidate")
        queued = domain.Task(id="ag-8", state="done", dispatcher_sid="d1",
                             worker_sid="w8", created_ms=1697940000000,
                             last_seen_ms=1697940300000, project_dir="/repo",
                             goal="g")
        busy = {queued.id: queued,
                "ag-9": domain.Task(
                    id="ag-9", state="done", dispatcher_sid="d1",
                    worker_sid="wz", created_ms=1697940000001,
                    goal="g", project_dir="/repo",
                    gate=domain.GateState(in_gate=True))}
        self.assertEqual(adapter.detail_classification(
            queued, busy, now=1697940300.0, act=0.0), "queued")


if __name__ == "__main__":
    unittest.main()
