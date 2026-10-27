"""可脚本化的 opencode v2 API 本地 HTTP stub（单测线程内用，e2e 亦可作进程跑）。

形状对账自真 opencode v2.1.4（T5 探针 tests/e2e/oc_probe.ts，/tmp/opencode/oc-probe.json）：
- 全路径挂 /api 前缀；POST /session {}→{data:{id}}（多余字段忽略）；move 绑目录→204；
- prompt {text}→{data:{delivery:"steer"}}，busy 排队按序（stub 恒收）；未知/已删 sid→404
  SessionNotFoundError；GET /session/{sid}→{data:{id,location}}；GET /session/active→{data:{}}
  （idle 不在 map）；GET /info→{version,...}（真服务无 /health）。
脚本化接口＝StubOpencode 实例：create 注册会话、kill() 即删（死＝GET/prompt 404）、
sessions[sid]["injects"] 记录注入序、stop() 停机。
作进程跑：`python3 tests/stub_opencode.py`（stdout 打印监听端口，kill 进程即停）。
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "stub-0.1.0"


class StubOpencode:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}  # sid -> {"injects": [text, ...], "directory": str, "req": dict}
        self.poison_html = False  # T5 修正：置 True 时 GET /api/info 回 200 text/html（SPA 兜底同族面）
        self.httpd = None
        self.thread = None

    def start(self) -> int:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音，保持测试输出 pristine
                pass

            def _json(self, code, obj):
                if stub.poison_html:  # T5 修正：毒化＝全端点 200 text/html（SPA 兜底同族面）
                    body = b"<!doctype html><html>spa-fallback</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")

            def _route(self):
                """路径剥 /api 前缀后分派；回 (method, parts, body)。"""
                parts = [p for p in self.path.strip("/").split("/") if p]
                if parts and parts[0] == "api":
                    parts = parts[1:]
                return parts, self._body() if self.command == "POST" else None

            def do_POST(self):
                parts, req = self._route()
                with stub.lock:
                    if parts == ["session"]:  # 真 shape：多余字段（cwd/message）静默忽略；title 接受
                        sid = uuid.uuid4().hex[:12]
                        stub.sessions[sid] = {"injects": [], "directory": "/",
                                              "req": req, "title": (req or {}).get("title")}
                        self._json(200, {"data": {"id": sid, "location": {"directory": "/"}}})
                    elif len(parts) == 3 and parts[0] == "session" and parts[2] == "move":
                        s = stub.sessions.get(parts[1])
                        if not s:
                            self._json(404, self._not_found(parts[1]))
                            return
                        s["directory"] = req.get("directory", s["directory"])
                        self.send_response(204)  # 204 无体（对齐真服务）
                        self.end_headers()
                    elif len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt":
                        s = stub.sessions.get(parts[1])
                        if s:  # busy 排队按序＝stub 恒收（delivery:steer）
                            s["injects"].append(req.get("text", ""))
                            self._json(200, {"data": {"id": "msg_" + uuid.uuid4().hex[:8],
                                                      "delivery": "steer"}})
                        elif not parts[1].startswith("ses_"):  # T6 实测：非 ses_ 形 sid→400（非 404）
                            self._json(400, {"error": "Bad Request"})
                        else:
                            self._json(404, self._not_found(parts[1]))
                    else:
                        self._json(404, {"error": "not found"})

            def do_GET(self):
                parts, _ = self._route()
                with stub.lock:
                    if parts == ["info"]:  # 真服务无 /health：version 来源即 /info
                        self._json(200, {"version": VERSION, "pid": 0, "urls": [], "paths": {}})
                    elif parts == ["session", "active"]:
                        self._json(200, {"data": {}})  # idle 会话不在 active map
                    elif len(parts) == 2 and parts[0] == "session":
                        s = stub.sessions.get(parts[1])
                        if not s:
                            self._json(404, self._not_found(parts[1]))
                            return
                        self._json(200, {"data": {"id": parts[1],
                                                  "location": {"directory": s["directory"]}}})
                    else:
                        self._json(404, {"error": "not found"})

            def do_DELETE(self):
                parts, _ = self._route()
                with stub.lock:
                    if len(parts) == 2 and parts[0] == "session":
                        stub.sessions.pop(parts[1], None)
                        self.send_response(204)
                        self.end_headers()
                    else:
                        self._json(404, {"error": "not found"})

            @staticmethod
            def _not_found(sid):
                return {"_tag": "SessionNotFoundError", "sessionID": sid,
                        "message": f"Session not found: {sid}"}

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        return self.port

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def kill(self, sid: str) -> None:
        """死会话＝会话消失（对齐真服务 DELETE 语义：GET/prompt 均 404）。"""
        with self.lock:
            self.sessions.pop(sid, None)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.httpd = None


if __name__ == "__main__":
    stub = StubOpencode()
    print(stub.start(), flush=True)
    stub.thread.join()  # serve_forever 持续运行，进程被 kill 即停
