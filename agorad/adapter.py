"""agorad.adapter — v2↔v3 适配层（Req: F141，设计 §2/§3/§11）。

v2 边缘（15 个根路由扁平参数/旧名/响应形状）↔ v3 服务面（正名/nested 规范
形/domain 实体/epoch 毫秒）纯翻译：零 I/O、不 import storage/access——盘面/
活性/探测经参数注入（T13a 装配），SQL 圈禁与分层由 test_architecture 锁定。
保真锚＝tests/fixtures/v3_contract_golden.json＋tests/test_v3_characterization.py。

口径裁定（F130×F135 张力）：validate_task_create 的边缘缺省
out_of_band 供 v3 原生调用面；本适配层不套用——v2 扁平缺席 deliverable 保持
None 交服务层派生 managed（v2「absent＝普通合并任务」保真、tasksvc None/
out_of_band 语义分立；同名工具经适配语义零变化）。
"""
import time
from pathlib import Path

from agorad.contracts import (ERROR_TABLE, ROLES, LEGACY_TO_V3, ContractError,
                              http_status, mcp_code, validate_params,
                              validate_task_create, task_create_v2_to_nested)
from agorad.domain import epoch_seconds

# ---- 请求向（v2 边缘 → v3 服务面） ----

# ＝v2 httpd._TOOLS 15 根路由（F128 golden http_tool_routes）；sleep 壳层
# 本地休眠不经服务端（v2 同款）；timer/approval/task_* 正名无 v2 路由。
LEGACY_ROUTES = frozenset(LEGACY_TO_V3) | {
    "board", "doctor", "enforce", "repo_board", "task_cancel",
    "task_create", "task_deliver", "task_dispatch", "task_update",
    "task_wait", "wiki_read", "wiki_write"}


def adapt_request(name, params, role):
    """v2 工具名＋扁平参数（＋调用角色）→ (v3 正名, v3 规范参数)。

    - 旧名映射：reverify→task_reverify、post→message_post、check_mail→
      message_pull；余 12 路同名直过（v2 扁平键面＝v3 ToolSpec，F130 已锚）。
    - task_create：扁平校验→分组→nested 规范形；v2 缺席 deliverable 保 None
      （模块 docstring 口径）；受信角色（非 worker）project_dir 必填＝v2
      边缘 KeyError→400 同档拒。
    - worker 显式写面字段原样进 spec（不洗不丢）——拒绝矩阵归服务层
      tasksvc._worker_matrix 同场判定（forbidden/403 与 v2 task.create 同档，
      适配层不越权改码）。
    """
    if name not in LEGACY_ROUTES:
        raise ContractError(
            "invalid_argument",
            f"{name!r} 非 v2 服务端路由（15 根路由；sleep 壳层本地）",
            {"tool": name})
    if role not in ROLES:
        raise ContractError(
            "invalid_argument",
            f"role 须为 root/dispatcher/worker（收到 {role!r}）",
            {"field": "role"})
    v3 = LEGACY_TO_V3.get(name, name)
    if name == "task_create":
        validate_params(name, params)  # 扁平面类型/enum/未知字段
        if role != "worker" and params.get("project_dir") is None:
            raise ContractError("invalid_argument", "缺字段: 'project_dir'",
                                {"field": "project_dir"})
        spec = validate_task_create(
            task_create_v2_to_nested(params))
        if "deliverable" not in params:  # v2 absent＝None（服务层派生 managed）
            spec["delivery"]["deliverable"] = None
        return "task_create", spec
    return v3, validate_params(v3, params)


# ---- 时间向（§3：epoch 毫秒 INTEGER → v2 浮点秒） ----


def _seconds(ms):
    """epoch 毫秒→v2 浮点秒；None 透传（毫秒粒度内往返无损，F131）。"""
    return None if ms is None else epoch_seconds(ms)


# ---- 响应向（v3 domain 实体 → v2 响应形状） ----

# 与 core.report 同值镜像（core 禁兄弟 import，v2 原值 F128 golden 锚定）
_MERGED = {"merged", "closed"}
_STALE_AFTER = 1200
_STALE_STATES = {"dispatched", "running", "done", "verify_failed", "reopened"}
_VERIFY_HYGIENE_STATES = frozenset(
    {"todo", "dispatched", "running", "done", "verify_failed", "reopened",
     "blocked"})
_REWORK_EVENTS = ("verify_failed", "reopened")
_STATS_WIN = 30
_STATS_DRIFT_PP = 0.15
_STATS_WIN_MIN = 10
_MODELS_NOTE = ("清单＝配置绑定面（服务实际可路由），非供应商目录全集——"
                "plan 代理类 provider 模型名不可枚举，以运维配置为准")


def _delivery_row(d):
    """DeliveryState→v2 delivery 投影（task._new_delivery 8 键；F105
    conflict_files 仅终局在键——v2 set_fields 写点同形）。"""
    row = {"mode": d.mode, "status": d.status, "commit": d.commit,
           "base": d.base, "target_branch": d.target_branch,
           "attempt": d.attempt, "local_status": d.local_status,
           "error": d.error}
    if d.conflict_files is not None:
        row["conflict_files"] = d.conflict_files
    return row


def task_row(t):
    """domain.Task→v2 board 行：白名单 15 键条件投影＋verify_budget（F126）。

    shortcut: usage（F57 经济投影）v3 Task 无字段不投影——v2 行键条件在场所
    以合法；T13a 装配若补采 F57 usage 再经行扩展入白名单。
    """
    gate = t.gate
    row = {
        "state": t.state, "title": t.title, "worker_sid": t.worker_sid,
        "dispatcher_sid": t.dispatcher_sid,
        "last_seen": _seconds(t.last_seen_ms),
        "created": _seconds(t.created_ms), "project_dir": t.project_dir,
        "in_gate": gate.in_gate, "model": t.model,
        "delivery": None if t.delivery is None else _delivery_row(t.delivery),
        "waiting_on": t.waiting_on,
        "review_fail_streak": gate.review_fail_streak}
    if gate.gate_resource is not None:
        row["gate_resource"] = gate.gate_resource  # 瞬态投影原样（F116）
    if gate.gate_artifact is not None:
        a = gate.gate_artifact  # v2 行投影 6 键（gate.py set_fields 同形）
        row["gate_artifact"] = {
            "path": a.path, "phase": a.phase, "rc": a.rc,
            "failure_kind": a.failure_kind, "duration_s": a.duration_s,
            "bytes": a.bytes}
    if t.verify is not None:
        v = t.verify
        row["verify_budget"] = {
            "timeout": v.timeout_s,
            "source": v.timeout_source or (
                "task" if v.timeout_s is not None else "default")}
    return row


def buckets(tasks):
    """五桶汇总（report.buckets 镜像：失败＝blocked＋delivery.status=failed）。"""
    out = {"已合并": [], "在飞": [], "失败": [], "阻塞": [], "已取消": []}
    for tid, t in sorted(tasks.items()):
        s = t.state
        if s in _MERGED:
            out["已合并"].append(tid)
        elif s == "blocked":
            failed = t.delivery is not None and t.delivery.status == "failed"
            (out["失败"] if failed else out["阻塞"]).append(tid)
        elif s == "cancelled":
            out["已取消"].append(tid)
        else:
            out["在飞"].append(tid)
    return out


def chain_view(tasks, dispatcher_sid):
    """链级视图（report.chain_view 镜像）：直接派发＋worker 再派逐跳展开。"""
    out, frontier, seen = [], [dispatcher_sid], {dispatcher_sid}
    while frontier:
        sid = frontier.pop(0)
        for tid, t in sorted(tasks.items()):
            if t.dispatcher_sid == sid:
                out.append(tid)
                w = t.worker_sid
                if w and w not in seen:
                    seen.add(w)
                    frontier.append(w)
    return out


def _layer_key(tasks):
    def key(tid):
        c = tasks[tid].created_ms
        return (1, tid) if not c else (-c, tid)
    return key


def forest_view(tasks):
    """全局链森林（report.forest_view 镜像）：DFS 聚簇、环防护、全员在列。"""
    parent = {tid: next((p for p in sorted(tasks)
                         if tasks[p].worker_sid == t.dispatcher_sid), None)
              for tid, t in tasks.items()}
    rank = _layer_key(tasks)
    kids, roots = {tid: [] for tid in tasks}, []
    for tid in sorted(tasks, key=rank):
        p = parent[tid]
        (roots if p is None else kids[p]).append(tid)
    out, seen, stack = [], set(), roots[::-1]
    while stack:
        tid = stack.pop()
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
        stack.extend(reversed(kids[tid]))
    return out + [tid for tid in sorted(tasks, key=rank) if tid not in seen]


def event_dicts(events):
    """domain.Event→v2 events 平展形（ts 浮点秒；detail 键摊平内联）。"""
    out = []
    for e in events:
        row = {"ts": _seconds(e.ts_ms), "actor": e.actor, "event": e.event}
        if e.task is not None:
            row["task"] = e.task
        if e.detail:
            row.update(e.detail)
        out.append(row)
    return out


def _bucket_counts(tasks, rework, tids):
    out = {"tasks": len(tids), "merged": 0, "rework": 0}
    for tid in tids:
        out["rework"] += rework[tid]
        if tasks[tid].state in _MERGED and rework[tid] == 0:
            out["merged"] += 1
    return out


def _stats(tasks, events):
    """F57 按 model 分桶＋F58 窗口对照（report._model_stats 镜像）。"""
    rework = {tid: 0 for tid in tasks}
    for e in events:
        if e.event in _REWORK_EVENTS and e.task in rework:
            rework[e.task] += 1
    by_model = {}
    for tid, t in tasks.items():
        by_model.setdefault(t.model or "default", []).append(tid)
    out = {}
    for model, tids in by_model.items():
        win = sorted(tids, key=lambda i: (-(tasks[i].created_ms or 0), i))
        win = win[:_STATS_WIN]
        b = _bucket_counts(tasks, rework, tids)
        w = _bucket_counts(tasks, rework, win)
        b.update(win_tasks=w["tasks"], win_merged=w["merged"],
                 win_rework=w["rework"])
        created = [_seconds(tasks[i].created_ms) or 0.0 for i in tids]
        b["first_created"] = min(created)
        b["last_created"] = max(created)
        b["drift"] = (None if b["tasks"] == 0 or w["tasks"] < _STATS_WIN_MIN
                      else abs(b["rework"] / b["tasks"]
                               - w["rework"] / w["tasks"]) > _STATS_DRIFT_PP)
        out[model] = b
    return out


def board_default(tasks, sid):
    """v2 默认看板（无 task/view 参数）：buckets＋chain＋forest 三键。"""
    return {"buckets": buckets(tasks), "chain": chain_view(tasks, sid),
            "forest": forest_view(tasks)}


def board_full(tasks, events):
    """v2 board view=full（F48）：五顶层键＝golden top_keys（逐字段锚）。"""
    flat = event_dicts(events)
    return {"buckets": buckets(tasks), "forest": forest_view(tasks),
            "tasks": {tid: task_row(t) for tid, t in tasks.items()},
            "events": [e for e in flat if e.get("event") == "escalate"],
            "stats": _stats(tasks, events)}


def message_dict(m):
    """domain.Message→v2 消息 dict（F128 断言⑤：id int、frontmatter 余值
    str、body 原文；ts＝v2 store 口径 str(浮点秒)；replay_of 在案才在键）。"""
    row = {"id": m.id, "ts": str(_seconds(m.ts_ms)), "task": m.task_id,
           "kind": m.kind, "from": m.from_sid, "to": m.to_sid,
           "body": m.body}
    if m.replay_of is not None:
        row["replay_of"] = str(m.replay_of)
    return row


def check_mail(messages):
    """message_pull 结果→v2 check_mail 响应 {"messages": [...]}。"""
    return {"messages": [message_dict(m) for m in messages]}


def _stale_candidates(tasks, now, base):
    """僵态候选（report.stale_candidates 镜像）：门内行不入候选、F124 运行
    中受控等待免判、门忙 done 排队免判、基线升序最僵在前。"""
    gate_busy = any(t.gate.in_gate
                    or (t.gate.gate_resource or {}).get("waiting")
                    for t in tasks.values())
    cand = []
    for tid, t in tasks.items():
        if (t.state in _STALE_STATES and not t.gate.in_gate
                and not (t.waiting_on and t.state == "running")):
            if gate_busy and t.state == "done":
                continue
            if now - base[tid] > _STALE_AFTER:
                cand.append([tid, base[tid]])
    cand.sort(key=lambda kv: kv[1])
    return cand


def doctor(tasks, *, now_ms=None, env_activity=None, on_disk,
           models=None, verify_hygiene=None, verify_profiles=None, home=""):
    """domain.Tasks→v2 doctor_view 形（golden 顶层键；report.doctor_view
    口径镜像）。

    盘面/活性纯注入（适配层零 I/O）：env_activity(tid)→env 工作区最新 mtime
    秒（缺省 0.0＝v2 缺席口径）；on_disk(env_dir)→在盘谓词（评审环 R1：无
    缺省必注——装配/测试漏注即调用点 TypeError 快败，不作静默假值）。
    models/verify_profiles 非 None 才注入条件键（F56/F126）。
    """
    now = time.time() if now_ms is None else epoch_seconds(now_ms)
    act = env_activity or (lambda tid: 0.0)
    base = {tid: max(_seconds(t.last_seen_ms) or _seconds(t.created_ms)
                     or 0.0, act(tid))
            for tid, t in tasks.items()}
    ranked = sorted(tasks, key=lambda tid: base[tid])
    env = []
    for tid in sorted(tasks):
        e = tasks[tid].env_dir
        if e:
            env.append({"tid": tid, "env_dir": e, "on_disk": on_disk(e)})
    findings = []
    if verify_hygiene is not None:
        for tid in sorted(tasks):
            t = tasks[tid]
            cmd = t.verify.cmd if t.verify is not None else ""
            if (t.state not in _VERIFY_HYGIENE_STATES or not cmd
                    or not t.project_dir):
                continue
            findings.extend({"tid": tid, "state": t.state, **f}
                            for f in verify_hygiene(cmd, t.project_dir))
    out = {"age_rank": [[tid, now - base[tid]] for tid in ranked],
           "last_seen_rank": [[tid, base[tid]] for tid in ranked],
           "stale_candidates": _stale_candidates(tasks, now, base),
           "env": env,
           "verify_hygiene_findings": findings}
    if models is not None:
        out["models"] = models
        out["models_note"] = _MODELS_NOTE
    if verify_profiles is not None:
        out["verify_profiles"] = {
            "count": len(verify_profiles),
            "path": str(Path(home) / ".agora" / "verify-profiles.json"),
            "profiles": [{"path": k, **verify_profiles[k]}
                         for k in sorted(verify_profiles)]}
    return out


# ---- 错误向（v3 结构化 error → v2 平面错误＋状态/负码映射） ----


def error_response(error, transport="http"):
    """v3 error{code,message,details,retryable}（或 ContractError）→v2 平面
    错误：HTTP＝(状态, {"error": "<message>"})；MCP＝{"code": 负码,
    "message": ...}——六档三处一处映射（ERROR_TABLE），状态/负码
    不丢档（golden error_codes 逐键一致）。"""
    if isinstance(error, ContractError):
        error = error.to_dict()
    code = error.get("code")
    if code not in ERROR_TABLE:
        raise ContractError("internal", f"未知错误码: {code!r}",
                            {"code": code})
    if transport == "mcp":
        return {"code": mcp_code(code), "message": error["message"]}
    return http_status(code), {"error": error["message"]}
