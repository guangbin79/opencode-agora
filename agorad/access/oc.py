"""opencode v2 API 唯一封装（北向访问层）：stdlib urllib，access 层内仅此模块接触网络。

API 形状是设计假设（tests/stub_opencode.py 镜像之），T3 e2e 与真 opencode 对账：
POST /session {cwd,message}->{id}；POST /session/{sid}/inject {text}->{ok}（死会话
ok:false）；GET /session/{sid}/status->{alive}；GET /health->{ok,version}。
网络故障（连接拒绝/超时/非 2xx）→OcUnavailable；死会话 inject 返回 False 不抛；
不存在 sid alive 返回 False。
"""
import json
import urllib.request

_TIMEOUT = 10


class OcUnavailable(Exception):
    """opencode 服务不可达。"""


class Oc:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def create(self, cwd: str, boot: str) -> str:
        return self._post("/session", {"cwd": cwd, "message": boot})["id"]

    def inject(self, sid: str, text: str) -> bool:
        return bool(self._post(f"/session/{sid}/inject", {"text": text}).get("ok"))

    def alive(self, sid: str) -> bool:
        return bool(self._get(f"/session/{sid}/status").get("alive"))

    def probe(self) -> dict:
        h = self._get("/health")
        return {"ok": bool(h.get("ok")), "oc_version": h.get("version")}

    def _post(self, path: str, obj: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._do(req)

    def _get(self, path: str) -> dict:
        return self._do(urllib.request.Request(self.base_url + path))

    @staticmethod
    def _do(req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read())
        except OSError as e:  # URLError/HTTPError/socket.timeout 均为 OSError 子类
            raise OcUnavailable(f"{req.full_url}: {e}") from e
