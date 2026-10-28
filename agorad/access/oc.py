"""opencode v2 API 唯一封装（北向访问层）：stdlib urllib，access 层内仅此模块接触网络。

实测形状（opencode v2.1.4，tests/e2e/oc_probe.ts → /tmp/opencode/oc-probe.json）：
- 服务发现：`$XDG_STATE_HOME/opencode/service.json` {url,password,pid,version}（serve --service 写）；
  API＝<url>/api/*，Basic auth `opencode:<password>`；discover() 回 (url, password)。
- create：POST /session {}→{data:{id}}；POST /session/{sid}/move {directory}→204（绑任务 env）；
  boot 文本＝POST /session/{sid}/prompt {text}。
- inject：POST /session/{sid}/prompt {text}→200 {data:{delivery:"steer"}}；busy 会话排队按序；
  未知/已删 sid→404 SessionNotFoundError→False；非 ses_ 形 sid→400→False（T6 实测补）。
- alive/session：GET /session/{sid}→200 {data:{...}}＝活；404＝死（不抛）。
  session(sid) 回同一 data 体（含 time:{created,updated,idle,viewed} 毫秒 epoch），404→None。
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

    def create(self, cwd: str, boot: str, title: str = None) -> str:
        """建 worker 会话并绑任务 env 目录，boot 文本作首条注入；回 sid。title 传给 opencode 供会话命名。"""
        body = {"title": title} if title else {}
        data = self._post("/session", body)
        try:
            sid = data["data"]["id"]
        except (KeyError, TypeError) as e:  # 应答形状错（含 200 非 JSON 信封）＝契约 OcUnavailable
            raise OcUnavailable(f"/session 应答形状错: {data!r}") from e
        self._post(f"/session/{sid}/move", {"directory": cwd})  # 204；location 即任务 env
        self._post(f"/session/{sid}/prompt", {"text": boot})
        return sid

    def inject(self, sid: str, text: str) -> bool:
        try:
            self._post(f"/session/{sid}/prompt", {"text": text})
            return True
        except OcUnavailable as e:
            if e.status in (400, 404):  # 死/未知会话：契约 False 不抛（T6 实测：非 ses_ 形 sid→400、未知 ses_→404）
                return False
            raise

    def inject_silent(self, sid: str, text: str) -> bool:
        """免通知注入（stalled 降噪 gt-5bc312）：synthetic 消息 resume=false——
        只落会话转录，不调度模型回合，桌面端无「响应就绪」弹出；失败契约同 inject。"""
        try:
            self._post(f"/session/{sid}/synthetic", {"text": text, "resume": False})
            return True
        except OcUnavailable as e:
            if e.status in (400, 404):
                return False
            raise

    def session(self, sid: str) -> dict | None:
        """会话详情（同 alive 契约：404→None，OcUnavailable 照抛）。"""
        try:
            return self._get(f"/session/{sid}").get("data")
        except OcUnavailable as e:
            if e.status == 404:
                return None
            raise

    def alive(self, sid: str) -> bool:
        return self.session(sid) is not None

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
        except ValueError as e:  # 200 但非 JSON（同族服务 SPA 兜底 200 text/html 同族面）
            raise OcUnavailable(f"{req.full_url}: 非 JSON 应答") from e
