"""opencode v2 API 唯一封装（北向访问层）：stdlib urllib，access 层内仅此模块接触网络。

实测形状（opencode v2.1.4，tests/e2e/oc_probe.ts → /tmp/opencode/oc-probe.json）：
- 服务发现：`$XDG_STATE_HOME/opencode/service.json` {url,password,pid,version}（serve --service 写）；
  API＝<url>/api/*，Basic auth `opencode:<password>`；discover() 回 (url, password)。
- create：POST /session {}→{data:{id}}；POST /session/{sid}/move {directory}→204（绑任务 env）；
  boot 文本＝POST /session/{sid}/prompt {text}。
- model（F55）是 prompt 逐回合参数非会话属性：create/inject 携 model 时 prompt body 附
  {"model":{providerID,modelID}}，None＝不带键（漏带即静默回落服务默认）；create 的
  move/prompt 任一抛异常→best-effort DELETE /session/{sid}（吞清理异常）后重抛——
  坏 model 串（prompt 400）不清理即漏僵尸会话。
- inject：POST /session/{sid}/prompt {text}→200 {data:{delivery:"steer"}}；busy 会话排队按序；
  未知/已删 sid→404 SessionNotFoundError→False；非 ses_ 形 sid→400→False（T6 实测补）。
- inject_silent：POST /session/{sid}/synthetic {text,resume:false}→204 无体（免通知通道：
  只落转录不调度回合，桌面无「响应就绪」弹出；v2.2.0 binary schema 实证声明 204/400/401/404）；
  失败契约同 inject（400/404→False，其余 OcUnavailable 上抛）。
- alive/session：GET /session/{sid}→200 {data:{...}}＝活；404＝死（不抛）。
  session(sid) 回同一 data 体（含 time:{created,updated,idle,viewed} 毫秒 epoch），404→None。
- last_message：GET /session/{sid}/message→{data:[...]}（2.2.2 实测：记录带 type=user/
  assistant/idle…、time.created 毫秒、assistant.content 内 tool part 带 name＋state，title 可缺；
  旧版 role 字段兼容读）；取尾条，空会话/404→None，OcUnavailable 照抛（吞的决心归调用方）。
- probe：GET /info→{version,...}（无 /health 端点）。
- models（F56）：GET /agent 顶层裸数组（模型键 id）∪ GET /config 顶层裸数组
  （config 文档列表，真机无信封——v2.2.5 热修实证；解析对 dict 信封宽容取 data、
  list 直用，防端点版本漂移；info.model/info.agents.*.model 模型键 model，
  多文档按序合并后覆盖前），按
  provider.use policies 过滤（deny 剔除、allow 白名单、同 provider deny 优先），
  去重排序 "providerID/modelID"；任一端点不可达照抛（吞的决心归 doctor 调用方）。
401（凭据失效：opencode serve 重启轮换密码）＝凭据自愈触发面（ag-dfc5f6）：重读 service.json，
与失败请求所用 url+password 对有变即换认证面并原调用重试恰一次；二次 401/发现不到新凭据照抛。
网络故障（连接拒绝/超时/网络类 URLError）→OcUnavailable；非 2xx→OcUnavailable（.status 带码）；
死会话 inject 返回 False 不抛。
"""
import base64
import json
import os
import threading
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
        self._lock = threading.Lock()  # 凭据交换锁：httpd 多线程＋probe 线程共用同一实例
        self._swap(base_url, password)

    def _swap(self, url: str, password: str | None) -> None:
        """重建认证面（调用方持 _lock）。"""
        self._url, self._password = url, password
        token = base64.b64encode(f"opencode:{password or ''}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.base = url.rstrip("/") + "/api"

    def _refresh(self, stale_face) -> bool:
        """401 自愈：重读 service.json；与失败请求所用凭据对有变才换面回 True。

        判据取失败请求的面（非实例当前面）：他线程已先换面时仍判可重试（再换幂等）。"""
        with self._lock:
            url, password = discover()
            if not url or (url, password or "") == stale_face:
                return False
            self._swap(url, password)
            return True

    def create(self, cwd: str, boot: str, title: str = None, model: str = None) -> str:
        """建 worker 会话并绑任务 env 目录，boot 文本作首条注入；回 sid。title 传给 opencode 供会话命名。

        model（F55）随 boot prompt 逐回合携带；坏 model 串（prompt 400）是新失败面——
        会话已建后 move/prompt 任一抛异常→best-effort DELETE 半建会话再重抛。"""
        body = {"title": title} if title else {}
        data = self._post("/session", body)
        try:
            sid = data["data"]["id"]
        except (KeyError, TypeError) as e:  # 应答形状错（含 200 非 JSON 信封）＝契约 OcUnavailable
            raise OcUnavailable(f"/session 应答形状错: {data!r}") from e
        try:
            self._post(f"/session/{sid}/move", {"directory": cwd})  # 204；location 即任务 env
            self._post(f"/session/{sid}/prompt", self._prompt_body(boot, model))
        except Exception:
            try:
                self._delete(f"/session/{sid}")  # 吞清理异常：原失败面上抛为准
            except Exception:
                pass
            raise
        return sid

    def inject(self, sid: str, text: str, model: str = None) -> bool:
        try:
            self._post(f"/session/{sid}/prompt", self._prompt_body(text, model))
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

    def last_message(self, sid: str) -> dict | None:
        """会话末条消息记录（F52 stalled 摘要原料）：GET /session/{sid}/message 尾条。

        同 session 契约：404→None；空会话/缺 data→None；OcUnavailable 照抛。"""
        try:
            msgs = self._get(f"/session/{sid}/message").get("data") or []
        except OcUnavailable as e:
            if e.status == 404:
                return None
            raise
        return msgs[-1] if isinstance(msgs, list) and msgs else None

    def alive(self, sid: str) -> bool:
        return self.session(sid) is not None

    def probe(self) -> dict:
        info = self._get("/info")
        return {"ok": True, "oc_version": info.get("version")}

    def models(self) -> list[str]:
        """候选模型清单（F56）：/agent 生效绑定 ∪ /config 缺省与 agents 绑定，policies 过滤。

        输出去重排序 "providerID/modelID"（task_create model 字段合法值形态）；
        形状与过滤算法见模块 docstring；任一端点不可达照抛 OcUnavailable。
        """
        models_map, policies = {}, []  # None＝缺省 model 位，agent id＝绑定位（后文档覆盖前）
        cfg = self._get("/config")
        # F56 热修：真机 /config 回顶层裸数组（config 文档列表）无信封——dict 信封取 data、
        # list 直用，双形状都吃（对 /agent 裸数组同款宽容，防御端点版本漂移）。
        docs = cfg.get("data") if isinstance(cfg, dict) else cfg
        for doc in docs or []:
            info = doc.get("info") or {}
            if isinstance(info.get("model"), dict):
                models_map[None] = info["model"]
            for aid, a in (info.get("agents") or {}).items():
                if isinstance(a, dict) and isinstance(a.get("model"), dict):
                    models_map[aid] = a["model"]
            policies.extend(info.get("policies") or [])
        pairs = {(m["providerID"], m["model"]) for m in models_map.values()
                 if m.get("providerID") and m.get("model")}
        for a in self._get("/agent") or []:  # 顶层裸数组无信封，模型键 id
            m = a.get("model") or {}
            if m.get("providerID") and m.get("id"):
                pairs.add((m["providerID"], m["id"]))
        use = [p for p in policies if p.get("action") == "provider.use"]
        deny = {p.get("resource") for p in use if p.get("effect") == "deny"}
        allow = {p.get("resource") for p in use if p.get("effect") == "allow"}
        return sorted(f"{p}/{m}" for p, m in pairs
                      if p not in deny and (not allow or p in allow))

    def _req(self, path: str, data=None, method: str | None = None):
        with self._lock:  # 构造与交换同锁：并发首试不吃撕裂凭据对
            face = (self._url, self._password or "")  # 本请求所用凭据对（401 重试判据）
            return urllib.request.Request(self.base + path, data=data,
                                          headers=self._headers, method=method), face

    @staticmethod
    def _prompt_body(text: str, model: str | None) -> dict:
        """prompt body：model None＝不带键（服务默认兜底），否则拆 providerID/modelID。"""
        body = {"text": text}
        if model is not None:
            p, m = model.split("/", 1)
            body["model"] = {"providerID": p, "modelID": m}
        return body

    def _post(self, path: str, obj: dict) -> dict:
        return self._call(lambda: self._req(path, json.dumps(obj).encode(), "POST"))

    def _get(self, path: str) -> dict:
        return self._call(lambda: self._req(path))

    def _delete(self, path: str) -> dict:
        return self._call(lambda: self._req(path, method="DELETE"))

    def _call(self, mk) -> dict:
        """发请求；401→凭据自愈（重发现有变才重试）恰一次，二次 401 照抛。"""
        req, face = mk()
        try:
            return self._do(req)
        except OcUnavailable as e:
            if e.status != 401 or not self._refresh(face):
                raise
        return self._do(mk()[0])

    @staticmethod
    def _do(req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}  # 204 无体（move/delete）
        except urllib.error.HTTPError as e:  # 非 2xx：服务在，应答错
            e.close()  # 401 自愈面常态命中：不关即漏 socket（ResourceWarning 实证）
            u = OcUnavailable(f"{req.full_url}: HTTP {e.code}", status=e.code)
            raise u from e
        except OSError as e:  # URLError/socket.timeout＝网络故障
            raise OcUnavailable(f"{req.full_url}: {e}") from e
        except ValueError as e:  # 200 但非 JSON（同族服务 SPA 兜底 200 text/html 同族面）
            raise OcUnavailable(f"{req.full_url}: 非 JSON 应答") from e
