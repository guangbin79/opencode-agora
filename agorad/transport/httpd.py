"""入向 JSON 口（B4）：11 工具端点＋/view＋/health；单线程串行＋后台门队列。

transport 只 import agorad.core（R2）——access 件经 App 属性触达（store.home
下 wiki 裸读写，无 import）。鉴权两段（R18）：X-Agora-Token→identify（无牌
401）→may 粗判（越权 403＋审计行）；归属细判与保险丝归 TaskService。done
特殊路径＝gate 三拒同步应答，受理绿→落库→1-worker 串行队列后台跑门体。
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


class _Handler(BaseHTTPRequestHandler):
    app = None    # make_server 经子类属性注入
    gate_q = None

    def log_message(self, *a):  # 测试/常驻输出 pristine
        pass

    # ---- HTTP 谓词 ----

    def do_GET(self):
        name = self.path.strip("/").split("?")[0]
        if name == "view":
            self._html(200, report.view_html(self.app.state.snapshot()))
        elif name == "health":
            self._json(200, {"ok": True,
                             "enforce_on": self.app.auth.enforce_on()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
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
            tid = body.get("task")
            row = None
            if tid is not None:
                try:
                    row = self.app.state.get(tid)
                except KeyError:
                    return self._json(404, {"error": f"task {tid} 不存在"})
            if not self.app.auth.may(role, name, tid, row):
                self.app.store.append_event(
                    {"ts": time.time(), "actor": sid, "event": EV_AUTH_DENIED,
                     "tool": name, "task": tid, "note": f"role={role}"})
                return self._json(403, {"error": f"{name} 拒绝（role={role}）"})
            self.app.health.touch(sid)  # last_seen：每次工具调用即刷新
            if row and row["state"] == "dispatched" and row.get("worker_sid") == sid:
                # worker 首次调用即开工：dispatched→running（里程碑迁移＋审计）
                self.app.state.transition(tid, EV_RUNNING)
                self.app.store.append_event({"ts": time.time(), "actor": sid,
                                             "event": EV_RUNNING, "task": tid})
            return self._json(200, self._tool(name, sid, role, token, body))
        except Rejected as e:      # 归属细判/保险丝拒（报因在消息）
            return self._json(403, {"error": str(e)})
        except NotChainEdge as e:
            return self._json(400, {"error": str(e)})
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except FileNotFoundError as e:
            return self._json(404, {"error": str(e)})
        except KeyError as e:      # 缺必填字段
            return self._json(400, {"error": f"缺字段: {e}"})
        except (ValueError, OSError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:     # 兜底：带因 500，不让连接静默断
            return self._json(500, {"error": str(e)})

    # ---- 工具面 ----

    def _tool(self, name, sid, role, token, body):
        app = self.app
        if name == "task_create":
            return {"task": app.task.create(body["goal"], body["project_dir"],
                                            body.get("after"), token)}
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
                    "chain": report.chain_view(snap, sid)}
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
            app.auth.set_enforce(bool(body["on"]), role)
            return {"enforce_on": app.auth.enforce_on()}
        raise KeyError(name)

    def _post(self, sid, body):
        app, tid, kind = self.app, body["task"], body["kind"]
        t = app.state.get(tid)
        if kind == "done":  # F7 入口：三拒同步应答，受理绿才落库并入门
            if t.get("worker_sid") != sid:
                raise NotChainEdge(f"{sid}->{tid}: done 仅任务 worker")
            r = app.gate.accept_done(tid, sid, str(body["body"]).strip())
            if r != "accepted":
                return {"result": r}
            app.router.post(sid, ("up", tid), "done", str(body["body"]))
            self.gate_q.put(tid)  # 门体后台串行
            return {"result": "accepted"}
        edge = ("down", tid) if sid == t["dispatcher_sid"] else ("up", tid)
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
    """单线程 HTTPServer（串行口径 §4.8）＋后台门队列（1 worker 串行）。"""
    q = queue.Queue()

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
