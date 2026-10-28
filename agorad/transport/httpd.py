"""入向 JSON 口（B4）：11 工具端点＋/view＋/health；单线程串行＋后台门队列。

transport 只 import agorad.core（R2）——access 件经 App 属性触达（store.home
下 wiki 裸读写，无 import）。鉴权两段（R18）：X-Agora-Token→identify（无牌
401）→may 粗判（越权 403＋审计行）；归属细判与保险丝归 TaskService。done
特殊路径＝gate 五拒同步应答（拒收落审计），受理绿→落库→1-worker 串行队列后台跑门体。
"""
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agorad.core import report
from agorad.core.auth import EV_AUTH_DENIED
from agorad.core.msg import NotChainEdge
from agorad.core.state import EV_RUNNING, InvalidTransition
from agorad.core.task import Rejected

_TOOLS = frozenset({"task_create", "task_dispatch", "task_update", "task_cancel",
                    "post", "check_mail", "board", "doctor", "wiki_read",
                    "wiki_write", "enforce"})
_POST_KINDS = ("note", "question", "answer", "done", "verify", "verify_reject")
_DENY_HINT = {"worker": "worker 面：post/check_mail/wiki_read/wiki_write/"
                        "task_dispatch/wait；需登记 verify→post(kind=verify) 提案验收命令"}


class _Handler(BaseHTTPRequestHandler):
    app = None    # make_server 经子类属性注入
    gate_q = None

    def log_message(self, *a):  # 测试/常驻输出 pristine
        pass

    # ---- HTTP 谓词 ----

    def do_GET(self):
        name = self.path.strip("/").split("?")[0]
        if name not in ("view", "health"):
            return self._json(404, {"error": "not found"})
        ident = self.app.auth.identify(self.headers.get("X-Agora-Token", ""))
        if ident is None:
            return self._json(401, {"error": "无牌或牌无效"})
        sid, role = ident
        self._bind_main(sid, role)
        tool = "board" if name == "view" else "doctor"  # 查询面同级（may 矩阵）
        if not self.app.auth.may(role, tool, None, None):
            return self._deny(sid, role, tool, None)
        if name == "view":
            self._html(200, report.view_html(self.app.state.snapshot()))
        else:
            self._json(200, {"ok": True,
                             "enforce_on": self.app.auth.enforce_on()})

    def do_POST(self):
        sid = None
        try:
            name = self.path.strip("/").split("?")[0]
            if name not in _TOOLS:
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 须为 JSON 对象")
            token = self.headers.get("X-Agora-Token", "")
            ident = self.app.auth.identify(token)
            if ident is None:
                return self._json(401, {"error": "无牌或牌无效"})
            sid, role = ident
            self._bind_main(sid, role)
            tid = body.get("task")
            row = None
            if tid is not None:
                try:
                    row = self.app.state.get(tid)
                except KeyError:
                    return self._json(404, {"error": f"task {tid} 不存在"})
            if not self.app.auth.may(role, name, tid, row):
                return self._deny(sid, role, name, tid)
            self.app.health.touch(sid)  # last_seen：每次工具调用即刷新
            if (row and row["state"] in ("dispatched", "verify_failed", "reopened")
                    and row.get("worker_sid") == sid):
                # worker 调用即开工（含 verify_failed/reopened 续修边）：→running＋审计
                self.app.state.transition(tid, EV_RUNNING)
                self.app.store.append_event({"ts": time.time(), "actor": sid,
                                             "event": EV_RUNNING, "task": tid})
            return self._json(200, self._tool(name, sid, role, token, body))
        except Rejected as e:      # 归属细判/保险丝拒（报因在消息）
            return self._json(403, {"error": str(e)})
        except NotChainEdge as e:
            return self._bad(sid, name, str(e))
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except FileNotFoundError as e:
            return self._json(404, {"error": str(e)})
        except KeyError as e:      # 缺必填字段
            return self._bad(sid, name, f"缺字段: {e}")
        except (ValueError, OSError) as e:
            return self._bad(sid, name, str(e))
        except Exception as e:     # 兜底：带因 500，不让连接静默断
            return self._json(500, {"error": str(e)})

    # ---- 工具面 ----

    def _tool(self, name, sid, role, token, body):
        app = self.app
        if name == "task_create":
            return {"task": app.task.create(body["goal"], body["project_dir"],
                                            body.get("after"), token,
                                            title=body.get("title"),
                                            target_branch=body.get("target_branch"))}
        if name == "task_dispatch":
            return {"task": body["task"],
                    "token": app.task.dispatch(body["task"], token)}
        if name == "task_update":
            app.task.update_verify(body["task"], token, body["verify_cmd"])
            return {"ok": True}
        if name == "task_cancel":
            return {"cancelled": app.task.cancel(body["task"], token)}
        if name == "post":
            return self._post(sid, body)
        if name == "check_mail":
            return {"messages": app.router.check_mail(sid)}
        if name == "board":
            snap = app.state.snapshot()
            if "task" in body:
                return report.detail(snap, app.store, body["task"])
            return {"buckets": report.buckets(snap),
                    "chain": report.chain_view(snap, sid),
                    "forest": report.forest_view(snap)}
        if name == "doctor":
            return report.doctor_view(app.state.snapshot(), app.store.home)
        if name == "wiki_read":
            return {"text": self._wiki(body["path"]).read_text(
                encoding="utf-8")}
        if name == "wiki_write":
            rel = Path(body["path"])
            if rel.parts and rel.parts[0] == "tasks":  # 任务页服务独占刷新
                raise Rejected("wiki/tasks/ 前缀拒写（服务独占）")
            p = self._wiki(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(body["text"]), encoding="utf-8")
            return {"ok": True}
        if name == "enforce":  # may() 已滤非 root；开/关审计在 Auth
            on = body["on"]
            if not isinstance(on, bool):  # "false" 字符串得 True——语义反转拒收
                raise ValueError("on 须为布尔")
            app.auth.set_enforce(on, role)
            return {"enforce_on": app.auth.enforce_on()}
        raise KeyError(name)

    def _bind_main(self, sid, role):
        """R25-A：root 牌调用携 X-Agora-Sid（MCP 壳透传真实会话 sid）即登记为
        主会话绑定（最近活跃覆盖）；无头直连（T4 冒烟/看板 tui）不动绑定。"""
        caller = self.headers.get("X-Agora-Sid", "")
        if role == "root" and caller:
            self.app.bind_main(sid, caller)

    def _deny(self, sid, role, tool, tid):
        """越权 403：POST/GET 共用（审计行同形）；hint 非空附可用面退路（F13）。"""
        hint = _DENY_HINT.get(role, "")
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid, "event": EV_AUTH_DENIED,
             "tool": tool, "task": tid, "note": f"role={role}"})
        msg = f"{tool} 拒绝（role={role}）"
        if hint:
            msg += f"——{hint}"
        return self._json(403, {"error": msg})

    def _bad(self, sid, tool, msg):
        """F13 参数错 400：落 bad_request 审计行（可计量）后带因返回。"""
        if sid is not None:  # 鉴权前解析错属协议噪声，不混入参数错计量
            try:  # best-effort：审计写失败不影响 400 应答
                self.app.store.append_event(
                    {"ts": time.time(), "actor": sid,
                     "event": "bad_request", "tool": tool or "?",
                     "note": msg[:200]})
            except Exception:
                pass
        return self._json(400, {"error": msg})

    def _post(self, sid, body):
        app, tid, kind = self.app, body["task"], body["kind"]
        if kind not in _POST_KINDS:  # F13 客户端 kind 白名单；内部直调 Router 不经此路
            raise ValueError(f"kind 须为 {'/'.join(_POST_KINDS)} 之一（收到 {kind!r}）")
        t = app.state.get(tid)
        if kind == "done":  # F7 入口：五拒同步应答，受理绿才落库并入门
            if t.get("worker_sid") != sid:
                raise NotChainEdge(f"{sid}->{tid}: done 仅任务 worker")
            r = app.gate.accept_done(tid, sid, str(body["body"]).strip())
            if r != "accepted":
                return {"result": r}
            app.router.post(sid, ("up", tid), "done", str(body["body"]))
            self.gate_q.put(tid)  # 门体后台串行
            return {"result": "accepted"}
        edge = ("down", tid) if self.app.router.is_dispatcher(sid, t) else ("up", tid)
        return {"message": app.router.post(sid, edge, kind, str(body["body"]))}

    def _wiki(self, rel):
        root = (Path(self.app.store.home) / "wiki").resolve()
        p = (root / rel).resolve()
        p.relative_to(root)  # 越出 wiki 根即 ValueError → 400
        return p

    # ---- 应答 ----

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(app, port: int = 0):
    """单线程 HTTPServer（串行口径 §4.8）＋后台门队列（1 worker 串行）。

    装配即重排 done 残留（kill -9 门中崩溃）：重启后 verify 幂等重跑，
    pending-cancel 与门闩照常由门体结算（F-4）。
    """
    q = queue.Queue()
    for tid, t in app.state.snapshot()["tasks"].items():
        if t["state"] == "done":
            q.put(tid)

    def consume():
        while True:
            tid = q.get()
            try:
                app.gate.run(tid)
            except Exception:  # 门体异常不杀 worker 线程：状态以快照行兜底
                pass
            finally:
                q.task_done()

    threading.Thread(target=consume, daemon=True, name="gate").start()

    class Handler(_Handler):
        pass
    Handler.app = app
    Handler.gate_q = q
    return HTTPServer(("127.0.0.1", port), Handler)
