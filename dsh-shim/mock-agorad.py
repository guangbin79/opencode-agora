#!/usr/bin/env python3
"""mock-agorad（Req: F182）——容器级验证用的隔离可控 agorad 模拟面（三端点）。

三端点（对齐真实 agorad 的 shim 消费面）：
  POST /cred_for_sid  {sid} → 200 {token, env_dir, role:"worker", tid}
      404＝可模拟 not_worker（--not-worker 旗）
  GET  /health        （X-Agora-Token 校验 root.token）→ {enforce_on}
      enforce 旗经控制文件 <home>/mockctl/enforce_on 热切换（verify 两轮切换用）
  POST /gate_deny     → 200（审计落账 JSONL，免鉴权信息面，对齐真实路由序）

另收 agora_mcp.py v2 面的工具 POST /<tool_name>：核 X-Agora-Token/X-Agora-Sid
（V3 断言面——shadow 附牌实收证据），回 {"ok": true, "tool": name}。

启动即写 <home>/.agora/service.json {port}（shim lift 探端口）与
.agora/root.token（pullEnforce 读牌）。全部状态隔离在 --home 目录内，
不触碰真实 AGORA_HOME。日志 <home>/mock-agorad.jsonl（每请求一行）。

用法：mock-agorad.py --home <dir> [--enforce on|off] [--env-dir <path>]
      [--not-worker] [--port N]（缺省 127.0.0.1:0 随机口）
"""
import argparse
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = None  # type: ignore[assignment]


def log(kind: str, **fields) -> None:
    rec = {"kind": kind, "ts": __import__("time").time(), **fields}
    line = json.dumps(rec, ensure_ascii=True)
    with open(os.path.join(args.home, "mock-agorad.jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    sys.stderr.write(line + "\n")


def enforce_on() -> bool:
    path = os.path.join(args.home, "mockctl", "enforce_on")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() != "off"
    except OSError:
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            tok = self.headers.get("X-Agora-Token")
            if tok != args.root_token:
                self._send(401, {"error": "bad token"})
                return
            self._send(200, {"enforce_on": enforce_on()})
            return
        if self.path == "/":
            self._send(200, {"ok": True, "mock": "agorad"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path == "/cred_for_sid":
            body = self._read_json()
            sid = body.get("sid")
            if args.not_worker:
                log("cred_for_sid", sid=sid, status=404)
                self._send(404, {"error": "not a worker session"})
                return
            log("cred_for_sid", sid=sid, status=200, env_dir=args.env_dir)
            self._send(200, {
                "token": args.worker_token,
                "env_dir": args.env_dir,
                "role": "worker",
                "tid": "ag-mocktid",
            })
            return
        if self.path == "/gate_deny":
            body = self._read_json()
            log("gate_deny", **{k: body.get(k) for k in ("sid", "tool", "target", "reason")})
            self._send(200, {"ok": True})
            return
        # agora_mcp.py v2 工具面：根级 POST /<tool>（token/sid 从参数剥进头）。
        name = self.path.lstrip("/").split("?")[0]
        tok = self.headers.get("X-Agora-Token")
        sid = self.headers.get("X-Agora-Sid")
        log("tool", tool=name, x_agora_token=tok, x_agora_sid=sid, token_ok=tok == args.worker_token)
        self._send(200, {"ok": True, "tool": name})

    def log_message(self, fmt, *a):  # 静默默认访问日志（事件已结构化落账）
        pass


def main() -> int:
    global args
    p = argparse.ArgumentParser()
    p.add_argument("--home", required=True, help="隔离 AGORA_HOME（模拟面全部状态驻此）")
    p.add_argument("--env-dir", required=True, help="/cred_for_sid 返回的 env_dir（容器内路径）")
    p.add_argument("--enforce", choices=["on", "off"], default="on")
    p.add_argument("--not-worker", action="store_true", help="/cred_for_sid 恒 404（主会话形态）")
    p.add_argument("--port", type=int, default=0)
    args = p.parse_args()

    ag = os.path.join(args.home, ".agora")
    os.makedirs(ag, exist_ok=True)
    os.makedirs(os.path.join(args.home, "mockctl"), exist_ok=True)
    args.root_token = "mock-root-" + secrets.token_hex(8)
    args.worker_token = "mock-worker-" + secrets.token_hex(8)
    root_token_path = os.path.join(ag, "root.token")
    with open(root_token_path, "w", encoding="utf-8") as f:
        f.write(args.root_token)
    os.chmod(root_token_path, 0o600)  # 与真 agorad authsvc 同则（C 册 R1：牌文件 0600）
    with open(os.path.join(args.home, "mockctl", "enforce_on"), "w", encoding="utf-8") as f:
        f.write(args.enforce)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = srv.server_address[1]
    with open(os.path.join(ag, "service.json"), "w", encoding="utf-8") as f:
        json.dump({"port": port}, f)
    log("listening", port=port, env_dir=args.env_dir, enforce=args.enforce,
        not_worker=args.not_worker, home=args.home)
    sys.stderr.write(f"mock-agorad on 127.0.0.1:{port} home={args.home}\n")
    sys.stderr.flush()
    srv.serve_forever()  # 常驻应答直至被杀（bind 后必须进 accept 循环，否则连接只排队）
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
