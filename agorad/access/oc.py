"""opencode v2 API 唯一封装（北向访问层）：stdlib urllib，access 层内仅此模块接触网络。

实测形状（opencode v2.1.4，tests/e2e/oc_probe.ts → /tmp/opencode/oc-probe.json）：
- 服务发现：`$XDG_STATE_HOME/opencode/service.json` {url,password,pid,version}（serve --service 写）；
  API＝<url>/api/*，Basic auth `opencode:<password>`；discover() 回 (url, password)。
- create：POST /session {}→{data:{id}}；POST /session/{sid}/move {directory}→204（绑任务 env）；
  boot 文本＝POST /session/{sid}/prompt {text}。
- inject：POST /session/{sid}/prompt {text}→200 {data:{delivery:"steer"}}；busy 会话排队按序；
  未知/已删 sid→404 SessionNotFoundError→False。
- alive：GET /session/{sid}→200 {data:{...}}＝活；404＝死（不抛）。
- probe：GET /info→{version,...}（无 /health 端点）。
网络故障（连接拒绝/超时/网络类 URLError）→OcUnavailable；非 2xx→OcUnavailable（.status 带码）；
死会话 inject 返回 False 不抛。
"""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_TIMEOUT = 10


def discover(state_home=None):
    """opencode 服务发现：读 $XDG_STATE_HOME/opencode/service.json → (url, password)。

    缺失/损坏 → (None, None)——调用方回落 AGORA_OC_URL 显式指定或报错。
    """
    root = Path(state_home or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    try:
        sj = json.loads((root / "opencode" / "service.json").read_text())
        return sj.get("url"), sj.get("password")
    except (OSError, ValueError):
        return None, None


class OcUnavailable(Exception):
    """opencode 服务不可达/应答错误（.status＝HTTP 码，网络故障时 None）。"""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class Oc:
    def __init__(self, base_url: str, password: str | None = None):
        self.base = base_url.rstrip("/") + "/api"
        token = base64.b64encode(f"opencode:{password or ''}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def create(self, cwd: str, boot: str) -> str:
        """建 worker 会话并绑任务 env 目录，boot 文本作首条注入；回 sid。"""
        sid = self._post("/session", {})["data"]["id"]
        self._post(f"/session/{sid}/move", {"directory": cwd})  # 204；location 即任务 env
        self._post(f"/session/{sid}/prompt", {"text": boot})
        return sid

    def inject(self, sid: str, text: str) -> bool:
        try:
            self._post(f"/session/{sid}/prompt", {"text": text})
            return True
        except OcUnavailable as e:
            if e.status == 404:  # 死/未知会话：契约 False 不抛
                return False
            raise

    def alive(self, sid: str) -> bool:
        try:
            return bool(self._get(f"/session/{sid}").get("data"))
        except OcUnavailable as e:
            if e.status == 404:
                return False
            raise

    def probe(self) -> dict:
        info = self._get("/info")
        return {"ok": True, "oc_version": info.get("version")}

    def _post(self, path: str, obj: dict) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(),
                                     headers=self._headers, method="POST")
        return self._do(req)

    def _get(self, path: str) -> dict:
        return self._do(urllib.request.Request(self.base + path, headers=self._headers))

    @staticmethod
    def _do(req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}  # 204 无体（move/delete）
        except urllib.error.HTTPError as e:  # 非 2xx：服务在，应答错
            u = OcUnavailable(f"{req.full_url}: HTTP {e.code}", status=e.code)
            raise u from e
        except OSError as e:  # URLError/socket.timeout＝网络故障
            raise OcUnavailable(f"{req.full_url}: {e}") from e
