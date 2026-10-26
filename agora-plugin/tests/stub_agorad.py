"""最小 agorad 形态桩（lift 测试用）：绑 0 号口→写 service.json→任意应答 401。

与真 agorad 的契约面对齐：$AGORA_HOME/.agora/service.json 写 {pid, port}，
HTTP 层任意应答（含 401）即 lift 视为就绪。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    home = os.environ["AGORA_HOME"]
    os.makedirs(os.path.join(home, ".agora"), exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    with open(os.path.join(home, ".agora", "service.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": srv.server_address[1]}, f)
    srv.serve_forever()


main()
