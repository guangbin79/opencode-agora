"""可脚本化的 opencode v2 API 本地 HTTP stub（单测线程内用，e2e 亦可作进程跑）。

API 形状是 T0/T1 阶段设计假设（与 agorad/access/oc.py 互为镜像），T3 e2e 与真
opencode 对账。脚本化接口＝StubOpencode 实例本身：经 API create 注册会话、
kill() 杀会话、sessions[sid]["injects"] 记录注入序、stop() 停机。
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
        self.sessions = {}  # sid -> {"alive": bool, "injects": [text, ...], "req": dict}
        self.httpd = None
        self.thread = None

    def start(self) -> int:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音，保持测试输出 pristine
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                parts = self.path.strip("/").split("/")
                with stub.lock:
                    if parts == ["session"]:
                        sid = uuid.uuid4().hex[:12]
                        stub.sessions[sid] = {"alive": True, "injects": [], "req": req}
                        self._json(200, {"id": sid})
                    elif len(parts) == 3 and parts[0] == "session" and parts[2] == "inject":
                        s = stub.sessions.get(parts[1])
                        if s and s["alive"]:
                            s["injects"].append(req.get("text", ""))
                            self._json(200, {"ok": True})
                        else:  # 死会话/未知 sid：ok:false（契约：非 404）
                            self._json(200, {"ok": False})
                    else:
                        self._json(404, {"error": "not found"})

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                with stub.lock:
                    if parts == ["health"]:
                        self._json(200, {"ok": True, "version": VERSION})
                    elif len(parts) == 3 and parts[0] == "session" and parts[2] == "status":
                        s = stub.sessions.get(parts[1])
                        self._json(200, {"alive": bool(s and s["alive"])})
                    else:
                        self._json(404, {"error": "not found"})

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
        with self.lock:
            if sid in self.sessions:
                self.sessions[sid]["alive"] = False

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.httpd = None


if __name__ == "__main__":
    stub = StubOpencode()
    print(stub.start(), flush=True)
    stub.thread.join()  # serve_forever 持续运行，进程被 kill 即停
