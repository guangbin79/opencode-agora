"""agorad.contracts — v3 Contract Catalog（Req: F130，设计页 §1/§2）。

单源生成 MCP schema、参数校验、角色/ownership 与错误映射；transport 接线归
T5 适配层，本模块零 import。task_create 双面：ToolSpec 扁平字段＝v2 MCP/HTTP
边缘面（F128 golden 锚），v3 服务面为 nested DTO（execution/verify/delivery）。
"""
API_VERSION = 3
ROLES = ("root", "dispatcher", "worker")


# ---- 错误模型：业务 code ↔ HTTP 状态 ↔ MCP 负码 三向一处映射 ----
# 表值＝(HTTP 状态, MCP 负码, retryable, 缺省 message)
ERROR_TABLE = {
    "invalid_argument": (400, -32602, False, "Invalid argument"),
    "unauthorized": (401, -32001, False, "Unauthorized"),
    "forbidden": (403, -32003, False, "Forbidden"),
    "not_found": (404, -32004, False, "Not found"),
    "conflict": (409, -32009, True, "Conflict"),
    "internal": (500, -32603, True, "Internal error")}
_HTTP_TO_CODE = {v[0]: k for k, v in ERROR_TABLE.items()}
_MCP_TO_CODE = {v[1]: k for k, v in ERROR_TABLE.items()}


def http_status(code):
    return ERROR_TABLE[code][0]


def mcp_code(code):
    return ERROR_TABLE[code][1]


def error_code_for_http(status): return _HTTP_TO_CODE.get(status)

def error_code_for_mcp(negative_code): return _MCP_TO_CODE.get(negative_code)


def make_error(code, message=None, details=None):
    """构造 error{code,message,details,retryable}（对外错误形）。"""
    row = ERROR_TABLE[code]
    return {"code": code, "message": message or row[3],
            "details": details if details is not None else {},
            "retryable": row[2]}


class ContractError(Exception):
    """契约校验失败（业务 code 携带）；message 含字段名，details 供机器判读。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}

    def to_dict(self):
        return make_error(self.code, self.message, self.details)


# ---- 字段规格与校验 ----

def _f(type_, required=False, enum=None, nullable=False):
    fs = {"type": type_, "required": required, "nullable": nullable}
    if enum is not None:
        fs["enum"] = tuple(enum)
    return fs


_TYPE_OK = {  # bool 是 int 子类，不冒充整数；str 不是 object
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list)}


def _checked(field, value, fs):
    if value is None and fs.get("nullable"):  # 仅 nullable 字段收显式 null
        return value
    if not _TYPE_OK[fs["type"]](value):
        raise ContractError(
            "invalid_argument",
            f"field {field}: expect {fs['type']}, got {type(value).__name__}",
            {"field": field, "expect": fs["type"]})
    if "enum" in fs and value not in fs["enum"]:
        raise ContractError(
            "invalid_argument",
            f"field {field}: not in enum {'/'.join(fs['enum'])}",
            {"field": field, "enum": list(fs["enum"])})
    return value


def _reject_unknown(fields, params, where):
    unknown = sorted(k for k in params if k not in fields)
    if unknown:
        raise ContractError(
            "invalid_argument",
            f"unknown fields ({where}): {', '.join(unknown)}",
            {"fields": unknown})


# ---- ToolSpec 目录：19 个服务端工具＋sleep（设计页 §2） ----

class ToolSpec:
    __slots__ = ("name", "api_version", "request", "required", "enums",
                 "roles", "ownership", "errors")

    def __init__(self, name, request, roles, ownership, errors):
        self.name = name
        self.api_version = API_VERSION
        self.request = request
        self.required = tuple(f for f, fs in request.items() if fs["required"])
        self.enums = {f: tuple(fs["enum"]) for f, fs in request.items() if fs.get("enum")}
        self.roles = tuple(roles)
        self.ownership = ownership  # task=操作既有任务 | global | local
        self.errors = tuple(errors)


DELIVERABLES = ("out_of_band", "external_branch")
POST_KINDS = ("note", "question", "answer", "done", "verify", "verify_reject",
              "peer")

_ANY = ROLES
_STAFF = ("root", "dispatcher")
_BASE_ERR = ("invalid_argument", "unauthorized", "internal")
_TASK_ERR = _BASE_ERR + ("not_found", "forbidden", "conflict")

CATALOG = {
    "task_create": ToolSpec("task_create", {
        "goal": _f("string", True), "title": _f("string"),
        "model": _f("string"), "after": _f("string"), "req_id": _f("string"),
        "project_dir": _f("string"), "target_branch": _f("string"),
        "verify_cmd": _f("string"), "review_cmd": _f("string"),
        "verify_pending": _f("boolean"), "verify_base_exempt": _f("boolean"),
        "quick": _f("boolean"), "timeout": _f("integer"),
        "deliverable": _f("string", enum=DELIVERABLES),
        "client_key": _f("string")},  # client_key 唯一冲突 409
        _ANY, "global", _BASE_ERR + ("conflict",)),
    "task_dispatch": ToolSpec("task_dispatch", {"task": _f("string", True)},
                              _ANY, "task", _TASK_ERR),  # v2 may()：三角色均放行
    "task_update": ToolSpec("task_update", {
        "task": _f("string", True), "verify_cmd": _f("string", True),
        "timeout": _f("integer")}, _STAFF, "task", _TASK_ERR),
    "task_cancel": ToolSpec("task_cancel",
                            {"task": _f("string", True), "force": _f("boolean")},
                            _ANY, "task", _TASK_ERR),
    "task_deliver": ToolSpec("task_deliver", {
        "task": _f("string", True),
        "outcome": _f("string", True, ("delivered", "non_ff", "failed")),
        "commit": _f("string", True), "base": _f("string"),
        "reason": _f("string")}, _STAFF, "task", _TASK_ERR),
    "task_reverify": ToolSpec("task_reverify", {"task": _f("string", True)},
                              _STAFF, "task", _TASK_ERR),
    "task_wait": ToolSpec("task_wait", {
        "task": _f("string", True), "wait_on": _f("string", nullable=True)},
        _STAFF, "task", _TASK_ERR),  # wait_on null/缺省＝清除（F124）
    "timer_create": ToolSpec("timer_create", {
        "target": _f("string", True), "seconds": _f("integer", True),
        "body": _f("string")}, _ANY, "global", _BASE_ERR + ("forbidden",)),
    "timer_cancel": ToolSpec("timer_cancel", {"timer_id": _f("string", True)},
                             _ANY, "global",
                             _BASE_ERR + ("not_found", "forbidden")),
    "approval_request": ToolSpec("approval_request", {
        "business_action": _f("string", True), "task": _f("string"),
        "reason": _f("string"), "timeout_seconds": _f("integer"),
        "approval_key": _f("string")}, _ANY, "task", _TASK_ERR),
    "approval_reply": ToolSpec("approval_reply", {
        "approval_id": _f("string", True),
        "decision": _f("string", True, ("approve", "reject")),
        "reason": _f("string")}, _ANY, "global",
        _BASE_ERR + ("not_found", "forbidden", "conflict")),
    "message_post": ToolSpec("message_post", {
        "kind": _f("string", True, POST_KINDS), "body": _f("string", True),
        "task": _f("string")}, _ANY, "task", _TASK_ERR),
    "message_pull": ToolSpec("message_pull", {}, _ANY, "global", _BASE_ERR),
    "board": ToolSpec("board", {"task": _f("string"),
                                "view": _f("string", enum=("full",))},
                      _STAFF, "global", _BASE_ERR),
    "doctor": ToolSpec("doctor", {}, _STAFF, "global", _BASE_ERR),
    "repo_board": ToolSpec("repo_board", {"repo": _f("string")},
                           _ANY, "global", _BASE_ERR),
    "wiki_read": ToolSpec("wiki_read", {"path": _f("string", True)},
                          _ANY, "global", _BASE_ERR + ("not_found",)),
    "wiki_write": ToolSpec("wiki_write", {
        "path": _f("string", True), "text": _f("string", True)},
        _ANY, "global", _BASE_ERR),
    "enforce": ToolSpec("enforce", {"on": _f("boolean", True)},
                        ("root",), "global", _BASE_ERR + ("forbidden",)),
    "sleep": ToolSpec("sleep", {"seconds": _f("integer")}, _ANY, "local",
                      errors=("invalid_argument",)),
}

# v2 兼容面：15 个服务端路由＋sleep；旧名→v3 正名（timer/approval 无旧 schema）
LEGACY_TO_V3 = {"reverify": "task_reverify", "post": "message_post",
                "check_mail": "message_pull"}
_LEGACY_MCP_TOOLS = frozenset(LEGACY_TO_V3) | {
    "board", "doctor", "enforce", "repo_board", "sleep", "task_cancel",
    "task_create", "task_deliver", "task_dispatch", "task_update",
    "task_wait", "wiki_read", "wiki_write"}


def mcp_schema(name):
    """旧名生成 v2 MCP schema {name, required, enums}（与 F128 golden 等价）。"""
    if name not in _LEGACY_MCP_TOOLS:
        raise ValueError(f"no legacy MCP schema: {name}")
    spec = CATALOG[LEGACY_TO_V3.get(name, name)]
    return {"name": name, "required": list(spec.required),
            "enums": {f: list(v) for f, v in spec.enums.items()}}


def validate_params(name, params):
    """按 ToolSpec 校验扁平（MCP/HTTP 边缘）参数；通过返回校验后的新 dict。"""
    spec = CATALOG[name]
    if not isinstance(params, dict):
        raise ContractError(
            "invalid_argument",
            f"params ({spec.name}): expect object, "
            f"got {type(params).__name__}",
            {"field": "params", "expect": "object"})
    _reject_unknown(spec.request, params, spec.name)
    for f in spec.required:
        if f not in params:
            raise ContractError("invalid_argument",
                                f"missing required field: {f}",
                                {"field": f})
    return {f: _checked(f, v, spec.request[f]) for f, v in params.items()}


# ---- task_create v3 nested DTO（execution/verify/delivery） ----

TASK_CREATE_TOP = {"goal": _f("string", True), "title": _f("string")}
_TASK_CREATE_ALL_TOP = ("goal", "title", "execution", "verify", "delivery")
TASK_CREATE_GROUPS = {
    "execution": {"model": _f("string"), "after": _f("string"),
                  "req_id": _f("string"), "project_dir": _f("string"),
                  "client_key": _f("string"), "quick": _f("boolean")},
    "verify": {"cmd": _f("string"), "review_cmd": _f("string"),
               "verify_pending": _f("boolean"),
               "verify_base_exempt": _f("boolean"),
               "timeout": _f("integer")},
    "delivery": {"deliverable": _f("string", enum=DELIVERABLES),
                 "target_branch": _f("string")}}


def validate_task_create(params):
    """v3 nested DTO 校验＋缺省派生：三组恒存在、缺项补 None、
    deliverable 缺省 out_of_band（v2 语义），产出规范形。"""
    if not isinstance(params, dict):
        raise ContractError(
            "invalid_argument",
            f"params (task_create): expect object, "
            f"got {type(params).__name__}",
            {"field": "params", "expect": "object"})
    _reject_unknown(_TASK_CREATE_ALL_TOP, params, "task_create")
    if "goal" not in params:
        raise ContractError("invalid_argument",
                            "missing required field: goal",
                            {"field": "goal"})
    out = {"goal": _checked("goal", params["goal"], TASK_CREATE_TOP["goal"]),
           "title": params.get("title")}
    if "title" in params:  # 显式 null 按类型拒：None 仅作派生缺省，不收输入
        _checked("title", out["title"], TASK_CREATE_TOP["title"])
    for group, fields in TASK_CREATE_GROUPS.items():
        raw = params[group] if group in params else {}
        if not isinstance(raw, dict):
            raise ContractError(
                "invalid_argument",
                f"field {group}: expect object, got {type(raw).__name__}",
                {"field": group, "expect": "object"})
        _reject_unknown(fields, raw, group)
        out[group] = {f: (_checked(f, raw[f], fs) if f in raw else None)
                      for f, fs in fields.items()}
    if out["delivery"]["deliverable"] is None:
        out["delivery"]["deliverable"] = "out_of_band"
    return out


# v2 扁平→v3 分组映射（T5 适配层消费；键面＝CATALOG task_create 扁平字段）
V2_TASK_CREATE_MAP = {
    "model": ("execution", "model"), "after": ("execution", "after"),
    "req_id": ("execution", "req_id"),
    "project_dir": ("execution", "project_dir"),
    "client_key": ("execution", "client_key"),
    "verify_cmd": ("verify", "cmd"), "timeout": ("verify", "timeout"),
    "review_cmd": ("verify", "review_cmd"),
    "verify_pending": ("verify", "verify_pending"),
    "verify_base_exempt": ("verify", "verify_base_exempt"),
    "quick": ("execution", "quick"),
    "deliverable": ("delivery", "deliverable"),
    "target_branch": ("delivery", "target_branch")}
_V2_FLAT_FIELDS = ("goal", "title", *V2_TASK_CREATE_MAP)


def task_create_v2_to_nested(flat):
    """纯函数：v2 扁平 task_create 参数→v3 nested 形；不校验值，
    交 validate_task_create；未知扁平字段拒。"""
    if not isinstance(flat, dict):
        raise ContractError(
            "invalid_argument",
            f"params (task_create v2): expect object, "
            f"got {type(flat).__name__}",
            {"field": "params", "expect": "object"})
    _reject_unknown(_V2_FLAT_FIELDS, flat, "task_create(v2)")
    nested = {"execution": {}, "verify": {}, "delivery": {}}
    for key in ("goal", "title"):  # 缺席键不输出 None：显式 null 不作输入
        if key in flat:
            nested[key] = flat[key]
    for key, (group, sub) in V2_TASK_CREATE_MAP.items():
        if key in flat:
            nested[group][sub] = flat[key]
    return nested
