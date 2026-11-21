"""Req: F142 — v3 HTTP 传输＋MCP 壳对接面（opencode-agora v3 P4-T13a）。

/v3/<tool>：contracts catalog 19 服务端工具（sleep 壳层本地不经服务端；
旧名 reverify/post/check_mail 亦收——壳路由零翻译）；鉴权两段＝
X-Agora-Token→authsvc.identify→may 矩阵（归属细判在服务层）；结构化
错误 error{code,message,details,retryable}＋HTTP 码一处映射（contracts.
ERROR_TABLE）；timer 两端点真接线（Req: F145）；approval 两端点 501
not_implemented 占位（服务本体归 T16 接线——文档注明，非语义缺失）。

legacy 根路由（v2 兼容面，经 F141 适配层→v3 服务）：15 旧工具＋
cred_for_sid/task_nudge/task_alarm＋GET /view、/health；响应形状对
tests/fixtures/v3_contract_golden.json 抽样锚（board 键集/doctor 键/
messages dict 形状）；错误＝v2 平面 {"error": msg}（error_response）。

并发模型：ThreadingHTTPServer（daemon_threads）——写事务经 compose §4
单一全局写锁（RLock 包 uow）串行无 interleaving 半态；读走 storage 独立
读者连接，不被后台 gate 长 verify 阻塞（断言10）。worker 调用即开工
（dispatched/verify_failed/reopened→running，v2 同款）；X-Agora-Sid 换号
自愈（worker 改绑/root 归属）。GET /v3/tools＝MCP tools/list 单源生成
（19＋sleep＋旧别名，壳 v3 模式拉取）；service.json 增 api_version=3/
storage_version=3（旧字段保留）。

边界：不做 v2/v3 差分执行器（T13b）；不接真实 OpenCode（T17）；board
单任务明细 classification＝压缩三态（queued/stalled/ok，无 v2 全判读面）。
"""
import dataclasses
import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agorad.adapter import (LEGACY_ROUTES, adapt_request, board_default,
                            board_full, check_mail, error_response,
                            event_dicts, task_row_detail,
                            detail_classification)
from agorad.contracts import (API_VERSION, CATALOG, ContractError,
                              LEGACY_TO_V3, TASK_CREATE_GROUPS, http_status,
                              make_error, validate_params,
                              validate_task_create)
from agorad.adapter import doctor as doctor_view
from agorad.domain import Event
from agorad.healthsvc import KIND_STALLED

_VERSION = "0.1.0"
_SERVICE_JSON = ".agora/service.json"
_NOT_IMPLEMENTED = frozenset({"approval_request", "approval_reply"})
_PLACEHOLDER_BY = {"approval_request": "T16", "approval_reply": "T16"}
_PULL_STATES = frozenset({"dispatched", "running", "done", "verify_failed",
                          "reopened", "blocked"})  # v2 cred 拉取面在飞口径
_ACTIVE = frozenset({"dispatched", "running", "done", "verify_failed",
                     "reopened", "blocked"})

_TOOL_DESC = {
    "task_create": "登记任务（todo）（dispatcher/root；worker 建嵌套子任务）",
    "task_dispatch": "派发任务（todo/blocked→dispatched；签 worker 牌＋建 env）",
    "task_update": "登记 verify 命令（仅 dispatcher/root；worker 永拒）",
    "task_cancel": "中止任务（级联下级）",
    "task_deliver": "external_branch 外部交付确认（delivered/non_ff/failed）",
    "task_reverify": "done 驻留重排回验证门（v2 旧名 reverify）",
    "task_wait": "受控等待登记/清除（wait_on null/空＝清除）",
    "timer_create": "建定时器（after_seconds/due_at 恰传其一、30 天上限；"
                    "worker 限本任务、dispatcher 归属终审、root 放行）",
    "timer_cancel": "取消定时器（仅 pending 可取消；firing/fired 拒）",
    "message_post": "链边发消息（v2 旧名 post；kind=done 完成回填）",
    "message_pull": "拉取本会话收件（v2 旧名 check_mail）",
    "board": "看板快照（默认三键/full 五键/单任务明细）",
    "doctor": "诊断视图（年龄排名＋环境残留＋候选模型清单）",
    "repo_board": "同仓协作面（在飞摘要＋最近 peer 留言，untrusted）",
    "wiki_read": "读 wiki 页",
    "wiki_write": "写 wiki 页（wiki/tasks/ 前缀服务独占）",
    "enforce": "拨强制旗（仅 root）",
    "sleep": "本地休眠指定秒数（壳层本地执行，不经服务端 /v3）",
}


def _input_schema(spec, field_over=None):
    props = {}
    for f, fs in spec.request.items():
        p = dict(field_over.get(f, {})) if field_over else {}
        p.setdefault("type", ["string", "null"] if fs.get("nullable")
                     else fs["type"])
        if fs.get("enum") and "enum" not in p:
            p["enum"] = list(fs["enum"])
        props[f] = p
    return {"type": "object", "properties": props,
            "required": list(spec.required)}


def _nested_task_create_schema():
    """/v3/task_create 收 nested DTO（validate_task_create）——MCP 广告面
    同形（schema-一致调用可直达；扁平面归 legacy 路由/适配层）。"""
    groups = {}
    for group, fields in TASK_CREATE_GROUPS.items():
        props = {}
        for f, fs in fields.items():
            p = {"type": fs["type"]}
            if fs.get("enum"):
                p["enum"] = list(fs["enum"])
            props[f] = p
        groups[group] = {"type": "object", "properties": props}
    return {"type": "object",
            "properties": {"goal": {"type": "string"},
                           "title": {"type": "string"}, **groups},
            "required": ["goal"]}


def v3_mcp_tools():
    """MCP tools/list 单源生成（断言4）：19 服务端工具＋sleep＋3 旧别名
    （名称面＝canonical∪aliases；schema 自 contracts ToolSpec 生成——
    task_create 为 nested DTO 形，与 /v3 受理面一致）。"""
    tools = []
    for name in sorted(CATALOG):
        spec = CATALOG[name]
        if name == "sleep":
            over = {"seconds": {"type": "integer", "minimum": 0,
                                "maximum": 120}}
            schema = _input_schema(spec, over)
        elif name == "task_create":
            schema = _nested_task_create_schema()
        else:
            schema = _input_schema(spec)
        tools.append({"name": name, "description": _TOOL_DESC.get(name, name),
                      "inputSchema": schema})
    for legacy in sorted(LEGACY_TO_V3):
        spec = CATALOG[LEGACY_TO_V3[legacy]]
        tools.append({
            "name": legacy,
            "description": f"（v2 旧名→{LEGACY_TO_V3[legacy]}）"
                           + _TOOL_DESC.get(LEGACY_TO_V3[legacy], legacy),
            "inputSchema": _input_schema(spec)})
    return tools


class _V3Handler(BaseHTTPRequestHandler):
    stack = None  # make_server_v3 经子类属性注入

    def log_message(self, *a):  # 测试/常驻输出 pristine
        pass

    # ---- HTTP 谓词 ----

    def do_GET(self):
        name = self.path.strip("/").split("?")[0]
        if name == "v3/tools":  # 公开 schema（回环单用户模型，无密钥面）
            return self._json(200, {"tools": v3_mcp_tools()})
        if name not in ("view", "health"):
            return self._json(404, {"error": "not found"})
        try:
            sid, role = self._identify()
            tool = "board" if name == "view" else "doctor"  # 查询面同级
            if not self.stack.auth.may(role, tool):
                self._deny(sid, role, tool, None)
            if name == "view":
                return self._html(self._view_html(sid))
            return self._json(200, {"ok": True,
                                    "enforce_on": self.stack.enforce_on(),
                                    "api_version": API_VERSION})
        except ContractError as e:
            status, payload = error_response(e, transport="http")
            return self._json(status, payload)
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        parts = [p for p in self.path.strip("/").split("?")[0].split("/") if p]
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 须为 JSON 对象")
        except (ValueError, OSError):
            return self._json(400, {"error": "坏 JSON/长度"})
        try:
            if parts and parts[0] == "v3":
                return self._face_v3("/".join(parts[1:]), body)
            if len(parts) == 1:
                return self._face_legacy(parts[0], body)
            return self._json(404, {"error": "not found"})
        except Exception as e:  # 兜底：带因 500，不让连接静默断（v2 同款）
            return self._json(500, {"error": str(e)})

    # ---- /v3 面 ----

    def _face_v3(self, name, body):
        stack = self.stack
        name = LEGACY_TO_V3.get(name, name)  # /v3 亦收旧名（壳路由零翻译）
        try:
            if not name:
                raise ContractError("invalid_argument", "缺工具名")
            sid, role = self._identify()
            if name not in CATALOG or name == "sleep":
                raise ContractError(
                    "invalid_argument",
                    f"{name!r} 非 /v3 服务端工具（sleep 为壳层本地工具）")
            if name in _NOT_IMPLEMENTED:
                return self._json(501, {"error": {
                    "code": "not_implemented",
                    "message": f"{name} 服务本体归 {_PLACEHOLDER_BY[name]}"
                               " 接线——T13a 传输占位",
                    "details": {"tool": name}, "retryable": False}})
            params = (validate_task_create(body) if name == "task_create"
                      else validate_params(name, body))
            result = self._dispatch(name, sid, role, params)
            # Req: F145 timer 观测投影挂 /v3 正名面（legacy 兼容面形状钉死
            # ——v2 无 timer，差分/金锚面零扰动）
            if name == "board" and "task" in params:
                result["timers"] = stack.timers.projection(params["task"])
            elif name == "doctor":
                result["timers_overdue"] = stack.timers.overdue_rows()
            return self._json(200, result)
        except ContractError as e:
            return self._json(http_status(e.code), {"error": e.to_dict()})
        except FileNotFoundError as e:
            return self._json(404, {"error": make_error("not_found", str(e))})
        except OSError as e:  # v2 同档：读面 OSError→400（非 500）
            return self._json(400, {"error": make_error("invalid_argument",
                                                        str(e))})
        except Exception as e:
            return self._json(500, {"error": make_error("internal", str(e))})

    # ---- legacy 面（v2 兼容） ----

    def _face_legacy(self, name, body):
        stack = self.stack
        if name == "cred_for_sid":  # 插件拉取面先于鉴权（缓存未命中才拉）
            return self._cred_for_sid(body)
        if name not in LEGACY_ROUTES and name not in (
                "task_nudge", "task_alarm"):
            return self._json(404, {"error": "not found"})
        try:
            sid, role = self._identify()
            if name == "task_nudge":
                return self._json(200, self._task_nudge(sid, role, body))
            if name == "task_alarm":
                return self._json(200, self._task_alarm(sid, role, body))
            v3name, params = adapt_request(name, body, role)
            return self._json(200, self._dispatch(v3name, sid, role, params))
        except ContractError as e:
            status, payload = error_response(e, transport="http")
            return self._json(status, payload)
        except FileNotFoundError as e:
            return self._json(404, {"error": str(e)})
        except (ValueError, OSError) as e:  # v2 同档：参数/读面错 400
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    # ---- 公共芯（两面共用的工具分派） ----

    def _identify(self):
        self.stack.auth.reload()  # 跨写者对账（dispatch 签牌/终态撤牌后先刷缓存）
        ident = self.stack.auth.identify(
            self.headers.get("X-Agora-Token", ""))
        if ident is None:
            raise ContractError("unauthorized", "无牌或牌无效")
        sid, role = ident
        return self._bind_caller(sid, role), role

    def _bind_caller(self, sid, role):
        """X-Agora-Sid（MCP 壳透传调用者真实会话 sid）：worker 换号自愈
        改绑；root 牌仅取 caller 作本笔归属身份（v3 无 main_sid 活绑定面）；
        无头直连不动绑定（v2 同款三分支）。"""
        caller = self.headers.get("X-Agora-Sid", "")
        if not caller:
            return sid
        if role == "root":
            return caller
        if role == "worker":
            return self.stack.auth.bind_worker(sid, caller)
        return sid

    def _dispatch(self, name, sid, role, params):
        stack = self.stack
        if name == "task_create":
            return {"task": stack.tasks.create(sid, role, params)}
        row, tid = None, params.get("task")
        if tid is not None:
            row = stack.storage.get_task(tid)
            if row is None:
                raise ContractError("not_found", f"task {tid} 不存在",
                                    {"task": tid})
        if not stack.auth.may(role, name, task_exists=row is not None):
            self._deny(sid, role, name, tid)
        stack.health.touch(sid)
        if (row is not None and row.worker_sid == sid
                and row.state in ("dispatched", "verify_failed", "reopened")):
            # actor 缺省 "agorad"（v3 transition 口径；调用者 sid 已在
            # message_posted/cred_pull 审计行留痕）
            stack.tasks.transition(tid, "running")
            row = stack.storage.get_task(tid)  # worker 调用即开工（v2 同款）
        if name == "task_dispatch":
            # 公共响应不带 worker 牌：凭据只经文件指针/拉取面（F43/F85）
            stack.tasks.dispatch(tid, sid, role)
            return {"task": tid}
        if name == "task_update":
            stack.tasks.update_verify(tid, sid, role, params["verify_cmd"],
                                      timeout=params.get("timeout"))
            return {"ok": True}
        if name == "task_cancel":
            # contracts 声明 force（v2 F28 忙碌闸）；v3 服务面未铺闸（T10
            # 边界，F142 台账在案）——恒按 force=true 语义直取消，字段消费
            # 留缝不拒收（CLI `cancel --force` 不破面）
            _ = params.get("force")
            return stack.tasks.cancel(tid, sid, role)
        if name == "task_deliver":
            r = stack.tasks.deliver(tid, sid, role, params["outcome"],
                                    params["commit"],
                                    base=params.get("base"),
                                    reason=params.get("reason") or "")
            if r.get("requeued"):
                stack.enqueue(tid)
            return r
        if name == "task_reverify":
            r = stack.tasks.reverify(tid, sid, role)
            if r.get("requeued"):
                stack.enqueue(tid)
            return r
        if name == "task_wait":
            return stack.tasks.wait(tid, sid, role,
                                    params.get("wait_on") or "")
        if name == "timer_create":  # Req: F145 真接线（替换 T13a 占位）
            return stack.timers.create(sid, role, params)
        if name == "timer_cancel":
            return stack.timers.cancel(sid, role, params["timer_id"])
        if name == "message_post":
            return self._message_post(sid, role, params, row)
        if name == "message_pull":
            return check_mail(stack.messaging.pull(sid))
        tasks_map = {t.id: t for t in stack.storage.tasks_all()}
        if name == "board":
            if "task" in params:
                return self._board_detail(params["task"], row, tasks_map)
            if params.get("view") == "full":
                return board_full(_with_lane(stack, tasks_map),
                                  stack.storage.events_all())
            return board_default(tasks_map, sid)
        if name == "doctor":
            return doctor_view(
                tasks_map, now_ms=stack.clock(),
                env_activity=stack.env_activity,
                on_disk=lambda d: Path(d).is_dir(),
                models=stack.models(),
                verify_profiles=stack.verify_profiles or None,
                home=stack.home)
        if name == "repo_board":
            repo = (stack.peer.partition(sid) if role == "worker"
                    else params.get("repo"))
            return stack.peer.board(stack.storage.tasks_all(), repo)
        if name == "wiki_read":
            p, _ = self._wiki_rel(params["path"])
            return {"text": p.read_text(
                encoding="utf-8", errors="replace")}  # 坏字节降级不 500
        if name == "wiki_write":
            p, inner = self._wiki_rel(params["path"])
            if inner.parts and inner.parts[0] == "tasks":
                # 独占守卫按 resolve 后落点判（词法 parts 可被 x/../tasks 绕过）
                raise ContractError("forbidden",
                                    "wiki/tasks/ 前缀拒写（服务独占）")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params["text"], encoding="utf-8")
            return {"ok": True}
        if name == "enforce":
            return {"enforce_on": stack.set_enforce(params["on"])}
        raise ContractError("invalid_argument", f"未知工具: {name}")

    def _message_post(self, sid, role, params, row):
        stack = self.stack
        kind, body = params["kind"], params["body"]
        if kind == "peer":  # F22 写面：无任务目标，作者＝调用者任务
            if role != "worker":
                raise ContractError("forbidden", "peer 留言仅 worker 牌可写")
            return {"message": stack.peer.post(sid, body)}
        tid = params.get("task")
        if tid is None:
            raise ContractError("invalid_argument", "缺字段: 'task'")
        if kind == "done":  # F7 入口：六拒同步应答，受理绿才落库并入门
            if row is None or row.worker_sid != sid:
                raise ContractError("invalid_argument",
                                    f"{sid}->{tid}: done 仅任务 worker")
            r = stack.gate.accept_done(tid, sid, str(body).strip())
            if r != "accepted":
                return {"result": str(r)}
            stack.messaging.post(sid, row.dispatcher_sid, tid, "done",
                                 str(body))
            stack.enqueue(tid)  # 门体后台串行
            return {"result": "accepted"}
        if (role == "root" and row is not None
                and row.dispatcher_sid != sid):
            stack.auth.bind_dispatcher(role, sid, row)  # F21 换号愈合
            row = stack.storage.get_task(tid)
        to = (row.dispatcher_sid if row.dispatcher_sid != sid
              else row.worker_sid)
        if not to:
            raise ContractError("conflict",
                                f"任务 {tid} 无收件人（worker 未派发）")
        msg = stack.messaging.post(sid, to, tid, kind, str(body))
        return {"message": msg.id}

    def _board_detail(self, tid, row, tasks_map):
        """单任务明细（Req: F144＝v2 report.detail 全形状）：全字段行
        （adapter.task_row_detail）＋事件链＋done 正文＋v2 三态判读
        classification（终态 None/queued/stale_candidate/busy_round/fresh）
        ＋F116 lane 投影注入（内存态，v2 行内投影同款）。"""
        stack = self.stack
        lane = getattr(stack.gate, "lane_projection", lambda tid: None)
        proj = lane(tid)
        if proj is not None:  # F116 lane 投影（内存态，v2 行内投影同款）
            row = dataclasses.replace(
                row, gate=dataclasses.replace(row.gate, gate_resource=proj))
        done = [m for m in stack.storage.messages_all()
                if m.task_id == tid and m.kind == "done"]
        act = stack.env_activity(tid)
        out = {"task": task_row_detail(row),
               "events": event_dicts(
                   stack.storage.events_for_task(tid)),
               "done_body": (done[-1].body.decode("utf-8", "replace")
                             if done else None),
               "classification": detail_classification(
                   row, tasks_map, time.time(), act)}
        if act > 0.0:  # env 回收/缺失 walk 空→省略（v2 同口径）
            out["env_active_seconds_ago"] = int(time.time() - act)
        pub = getattr(stack.gitx, "publication", None)
        if pub is not None and row.state in ("merged", "closed"):
            try:  # 观测面尽力而为：refs 病/缺支省略字段不 500
                out["publication"] = pub(row.project_dir, row.target_branch)
            except Exception:
                pass
        return out

    # ---- legacy 内部面 ----

    def _task_nudge(self, sid, role, body):
        """F73 手动提示：root 任意任务；任务派发方限己任务链。"""
        tid = body.get("task")
        if not isinstance(tid, str):
            raise ContractError("invalid_argument", "缺字段: 'task'")
        row = self.stack.storage.get_task(tid)
        if row is None:
            raise ContractError("not_found", f"task {tid} 不存在")
        if role != "root" and row.dispatcher_sid != sid:
            raise ContractError("forbidden",
                                f"{sid}->{tid}: prompt 仅 root/任务派发方")
        if not row.worker_sid:
            return {"nudged": False, "reason": "no_worker"}
        text = body.get("body")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ContractError("invalid_argument", "prompt body 须为字符串")
        text = text.strip()
        if len(text) > 2000:
            raise ContractError("invalid_argument",
                                "prompt body 长度须≤2000 字符")
        ok = self.stack.health.nudge_worker(tid, text=text or None)
        self._audit(sid, "worker_nudge", tid,
                    f"mode={'custom' if text else 'default'} "
                    f"chars={len(text)} delivered={str(ok).lower()}")
        return {"nudged": True} if ok else {"nudged": False,
                                            "reason": "inject_failed"}

    def _task_alarm(self, sid, role, body):
        """F77 root-only 手动拍僵：直调 escalate＋nudge 两原语。"""
        tid = body.get("task")
        if not isinstance(tid, str):
            raise ContractError("invalid_argument", "缺字段: 'task'")
        row = self.stack.storage.get_task(tid)
        if row is None:
            raise ContractError("not_found", f"task {tid} 不存在")
        if role != "root":
            raise ContractError("forbidden", f"{sid}->{tid}: alarm 仅 root")
        if not row.worker_sid:
            return {"alarmed": False, "reason": "no_worker"}
        if row.state not in _ACTIVE:
            return {"alarmed": False, "reason": "not_active"}
        self.stack.health.escalate(KIND_STALLED, tid)
        ok = self.stack.health.nudge_worker(tid)
        self._audit(sid, "task_alarm", tid,
                    f"manual=1 delivered={str(ok).lower()}")
        return ({"alarmed": True} if ok
                else {"alarmed": False, "reason": "inject_failed"})

    def _cred_for_sid(self, body):
        """F25 推改拉面（v2 httpd 平移）：sid＝在飞任务 worker 且牌活→
        {tid, env_dir, token}；携有效 root 牌改走签发/拉取面（每窗口独立
        派发牌，幂等回旧牌不重签防铸牌膨胀）；否则 404。回环单用户信任
        模型（v2 F27 等价性注释同口径）。"""
        stack = self.stack
        stack.auth.reload()  # 拉取面前先对账（本请求无 identify 先行）
        root = stack.auth.identify(self.headers.get("X-Agora-Token", ""))
        if root is not None and root[1] == "root":
            return self._cred_issue(root[0], str(body.get("sid") or ""))
        asker = str(body.get("sid") or "")
        hit = self._worker_hit(asker)
        tok = stack.auth.token_for(asker) if hit else None
        self._audit(asker or "?", "cred_pull", hit[0] if tok else None, None)
        if not tok:
            return self._json(404, {"error": "no cred for sid"})
        return self._json(200, {"tid": hit[0], "env_dir": hit[1].env_dir,
                                "token": tok})

    def _cred_issue(self, root_sid, asker):
        stack = self.stack
        if not asker:
            return self._json(404, {"error": "no cred for sid"})
        tok = stack.auth.token_for(asker)
        found = stack.auth.identify(tok) if tok else None
        if found is not None and (found[1] != "root" or asker == "root"):
            hit = self._worker_hit(asker)  # worker 在飞行随带 tid/env_dir
            self._audit(root_sid, "cred_pull", hit[0] if hit else None, None)
            ident = stack.auth.identify(tok)  # revoke 竞窗防 None
            if ident is None:
                return self._json(404, {"error": "no cred for sid"})
            return self._json(200, {"tid": hit[0] if hit else None,
                                    "env_dir": hit[1].env_dir if hit else None,
                                    "token": tok, "role": ident[1]})
        new_tok, _ = stack.auth.issue(asker, "dispatcher")
        self._audit(root_sid, "cred_issue", None, f"dispatcher:{asker}")
        return self._json(200, {"token": new_tok, "role": "dispatcher"})

    def _worker_hit(self, asker):
        return next(((t.id, t) for t in self.stack.storage.tasks_all()
                     if t.worker_sid == asker and t.state in _PULL_STATES
                     and t.env_dir), None)

    def _audit(self, actor, event, task, note):
        try:
            with self.stack.repo.uow():
                self.stack.repo.append_event(Event(
                    seq=0, ts_ms=self.stack.clock(), actor=actor,
                    event=event, task=task,
                    detail={"note": note} if note else None))
        except Exception:
            pass  # 审计尽力而为（F54 口径）

    def _deny(self, sid, role, tool, tid):
        """越权 403（两面共用；审计行同形）。"""
        with self.stack.repo.uow():
            self.stack.repo.append_event(Event(
                seq=0, ts_ms=self.stack.clock(), actor=sid,
                event="auth_denied", task=tid,
                detail={"tool": tool, "note": f"role={role}"}))
        raise ContractError("forbidden", f"{tool} 拒绝（role={role}）")

    def _wiki_rel(self, rel):
        """规范化后的 (绝对落点, wiki 内相对路径)：越根（含绝对路径覆盖/
        符号链接逃逸）→ ContractError invalid_argument（两面同档 400，不落
        500）；write 面不要求已存在（新建页合法，v2 同款）。"""
        root = (Path(self.stack.home) / "wiki").resolve()
        p = (root / rel).resolve()
        try:
            inner = p.relative_to(root)
        except ValueError:
            raise ContractError("invalid_argument",
                                f"越出 wiki 根: {rel}") from None
        return p, inner

    def _view_html(self, sid):
        """GET /view 最小页（v2 view_html 同构，数据经 adapter）。"""
        esc = lambda v: html.escape(str(v))
        tasks_map = {t.id: t for t in self.stack.storage.tasks_all()}
        board = board_default(tasks_map, sid)
        parts = ["<html><body><h1>agora board</h1>"]
        for name, tids in board["buckets"].items():
            parts.append(f"<h2>{esc(name)} {len(tids)}</h2><ul>")
            parts.extend(f"<li>{esc(i)}</li>" for i in tids)
            parts.append("</ul>")
        parts.append("<h2>链</h2><ul>")
        for tid in board["forest"]:
            t = tasks_map.get(tid)
            parts.append(f"<li>{esc(tid)}: {esc(t.state if t else '?')} "
                         f"{esc(t.dispatcher_sid if t else '')}→"
                         f"{esc(t.worker_sid if t else '')} "
                         f"{esc(t.title if t else '')}</li>")
        parts.append("</ul></body></html>")
        return "".join(parts)

    # ---- 应答 ----

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _with_lane(stack, tasks_map):
    """Req: F144 full_view 行内 F116 lane 投影注入（白名单键集不变——
    gate_resource 本就在白名单，v3 此前无源）。"""
    lane = getattr(stack.gate, "lane_projection", lambda tid: None)
    out = {}
    for tid, t in tasks_map.items():
        proj = lane(tid)
        out[tid] = (dataclasses.replace(
            t, gate=dataclasses.replace(t.gate, gate_resource=proj))
            if proj is not None else t)
    return out


def _write_service_json(stack, port):
    path = Path(stack.home) / ".agora" / "service.json"
    data = json.dumps({
        "pid": os.getpid(), "port": port, "started": time.time(),
        "version": _VERSION, "oc_version": stack.oc_version(),
        "api_version": API_VERSION,  # 断言5：旧字段保留＋双版本
        "storage_version": 3}, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def make_server_v3(stack, port=0):
    """ThreadingHTTPServer（读不阻塞；写经 compose §4 全局写锁串行）＋
    service.json（api_version=3/storage_version=3＋v2 旧字段保留）。"""
    class Handler(_V3Handler):
        pass
    Handler.stack = stack
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    _write_service_json(stack, server.server_address[1])
    return server


def serve_forever_v3(stack, port=0, health_interval=300, timer_interval=30):
    """进程主循环（装配起点；测试直用 make_server_v3 自驱）。"""
    stack.start()  # 幂等：root bootstrap/清扫恢复/done 重排＋gate 线程
    server = make_server_v3(stack, port)
    stack.start_health_thread(health_interval)
    stack.start_timer_thread(timer_interval)  # Req: F145 timer 级扫描
    print(f"agorad-v3 ready port={server.server_address[1]} api=3",
          flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        stack.close()
    return 0
