"""dsh worker 北向适配器（Req: F184）——agorad 多 harness 指挥通道。

形态（F181/F182 实证链上的服务侧收口）：create 经 ``docker run -i``
（run-worker.sh 同形态：host 网络＋宿主 UID＋GLM key 只进进程 env＋
AGORA_HOME 只读挂载）拉起容器化 dsh worker，容器内跑
``dsh --profile sdk --patch dsh-shim/cordis.worker.yml``——即 DeepSeek
Harness SDK 运行时：stdout 专承载 newline-delimited JSON-RPC 2.0 帧，
本适配器在其 stdin/stdout 上指挥 worker 全生命周期：

- ``initialize{cwd,provider,model}``＝握手；须等应答后才能 prompt（RC
  实证：竞发直接被「SDK server is not initialized」拒）。
- ``session/prompt{sessionId,contentBlocks}``＝排一条 user 消息并唤醒
  （followup：idle 起新 turn、busy 排队）；sessionId 由客户端提供（未知
  id 惰性建 agent+session 对），故适配器自造 ``session-<uuid>`` 作为
  worker_sid 记账单值来源（与 agorad authsvc 按 sid 解析天然同源）。
- ``session.event``/``session.status`` 通知流＝会话事实（消息/工具回合/
  用量/running-idle 转换），适配器累计为 last_message/messages/session
  （健康摘要与用量原料）。

注入语义映射（任务页设计定则＋本任务容器实证落地）：
inject/inject_notify→session/prompt（唤醒 idle 起 turn）＝定则原样；
inject_silent→同通道落地——SDK 协议 0.2.2-rc.5 无免唤醒输入面
（agent.inject 仅进程内 API、jsonrpc-server handleRequest 为封闭
switch），偏差＝silent 类低频 note digest 以一次 turn 代价落上下文
（部署注记在 docs/reqs F184 条目）。

写闸/凭据/休眠全在 shim 侧（F182 交付，本模块只消费不改动）：
dsh-home 每任务独立（``<env>/.dsh-worker-home``，随 env 回收）；
worker 视角的 env 固定挂载位 /work/task-env（/cred_for_sid 的 env_dir
按 harness 分流回容器路径，见 httpd/serverv3 接线）。GLM key 只经进程
env 透传（部署/测试侧供给——agorad 零凭据库接触，禁打印禁落盘）。
网络故障/进程死亡→DshUnavailable（OcUnavailable 同角色）；死会话
inject 返回 False 不抛（oc 契约同款）。

HarnessRouter＝harness 分流面（create 按任务 harness 路由；注入/探活
按 sid 路由——dsh 注册表命中走 dsh，否则透传 oc；v2 main 与 v3 compose
共用本面，dispatch/health/messaging 由此分流）。
"""
import collections
import itertools
import json
import os
import pathlib
import subprocess
import threading
import time
import uuid

HARNESSES = ("opencode", "dsh")
_DEFAULT_IMAGE = "opencode-agora/f182-dsh-worker:1"
_DEFAULT_MODEL = "glm-gptload/glm-5.3"
_INIT_TIMEOUT = 180.0   # 握手：profile 装载＋插件树就绪（含冷启 pnpm workspace）
_REQ_TIMEOUT = 30.0     # 请求应答（prompt 回执即时返回）
_EVENT_CAP = 4000       # 每会话累计事件上限（用量原料/健康摘要足够，防无界内存）
_STDERR_TAIL = 60       # 容器诊断尾舱行数（失败报因用）


def payload_root():
    """dsh 载荷根（仓/安装 payload 顶层：dsh-shim/＋agora-plugin/ 所在）。"""
    return pathlib.Path(__file__).resolve().parents[2]


class DshUnavailable(Exception):
    """dsh worker 不可达/应答错误（OcUnavailable 同角色）。"""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class _Worker:
    """一个容器化 worker 会话：docker 进程＋JSON-RPC 传输＋事件账。"""

    def __init__(self, sid, env_dir, dsh_home, created_ms):
        self.sid = sid
        self.env_dir = str(env_dir)
        self.dsh_home = str(dsh_home)
        self.created_ms = created_ms
        self.proc = None            # spawn 后回填
        self.status = None          # session.status：running/idle
        self.idle_ms = 0            # 末次 idle 转换时刻（busy=updated>idle 口径）
        self.last_event_ms = created_ms
        self.events = []            # session.event 的 event 体（顺序累计）
        self.calls = {}             # tool callId -> 工具名（tool/result 配名）
        self.pending = {}           # rpc id -> _Pending
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.stderr_tail = collections.deque(maxlen=_STDERR_TAIL)
        self.dead_reason = None


class Dsh:
    """dsh worker 容器会话适配器：Oc 同形消费面（create/inject×3/alive/
    session/last_message/messages/probe/models）；spawn 可注入桩（测试）。"""

    def __init__(self, repo_root=None, image=None, home=None, *,
                 model=None, glm_key_env="GLM_API_KEY", spawn=None):
        self.repo_root = str(pathlib.Path(repo_root or payload_root()))
        self.image = image or os.environ.get("AGORA_DSH_IMAGE") or _DEFAULT_IMAGE
        self.home = home  # None＝create 时取进程 env AGORA_HOME
        self.model = model or _DEFAULT_MODEL
        self._glm_key_env = glm_key_env
        self._spawn = spawn or subprocess.Popen
        self._workers = {}          # sid -> _Worker
        self._lock = threading.RLock()

    # ---- Oc 同形消费面 ----

    def create(self, env_dir, boot, title=None, model=None) -> str:
        """建容器化 worker 会话：boot 文本作首条 prompt（followup 起首 turn）。

        dsh-home＝<env>/.dsh-worker-home（每任务独立、随 env 回收）；
        会话 cwd＝容器挂载位 /work/task-env。返回自造 sid（session-<uuid>）。
        title 无 SDK 通道（运行时禁模型标题）——接受形参但不落地。"""
        env_dir = str(pathlib.Path(env_dir))
        agora_home = str(self.home or os.environ.get("AGORA_HOME") or "")
        if not os.path.isdir(env_dir):
            raise DshUnavailable(f"env 不在盘: {env_dir}")
        if not agora_home or not os.path.isdir(agora_home):
            raise DshUnavailable(f"AGORA_HOME 不可用（{agora_home!r}）")
        key = self._glm_key()
        if not key:
            raise DshUnavailable(
                "GLM key 缺失（进程 env GLM_API_KEY 或 opencode 凭据库）")
        dsh_home = pathlib.Path(env_dir) / ".dsh-worker-home"
        dsh_home.mkdir(parents=True, exist_ok=True)
        provider, mname = (model or self.model).split("/", 1)
        sid = f"session-{uuid.uuid4()}"
        w = _Worker(sid, env_dir, dsh_home, _now_ms())
        try:  # spawn 纳入契约 try（评审 B 册 S1：docker 缺失→DshUnavailable
            # 而非裸 FileNotFoundError 穿透 OcUnavailable 同角色契约）
            w.proc = self._spawn(self._argv(env_dir, dsh_home, agora_home),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace",
                                 env={**os.environ, self._glm_key_env: key},
                                 cwd=str(pathlib.Path.cwd()))
        except OSError as e:
            raise DshUnavailable(f"dsh worker 拉起失败: {e}") from e
        threading.Thread(target=self._read, args=(w,), daemon=True,
                         name=f"dsh-io-{sid[:16]}").start()
        threading.Thread(target=self._drain_stderr, args=(w,), daemon=True,
                         name=f"dsh-err-{sid[:16]}").start()
        try:
            self._rpc(w, "initialize", {
                "cwd": "/work/task-env", "provider": provider,
                "model": mname}, timeout=_INIT_TIMEOUT)
            self._rpc(w, "session/prompt", {
                "sessionId": sid,
                "contentBlocks": [{"type": "text", "text": boot}]})
        except Exception as e:
            self._kill(w)
            raise DshUnavailable(f"dsh worker 启动失败: {e}") from e
        with self._lock:
            self._workers[sid] = w
        return sid

    def inject(self, sid, text, model=None) -> bool:
        """唤醒注入（定则 inject/inject_notify→session_prompt）：排 user 消息。"""
        return self._prompt(sid, text)

    def inject_notify(self, sid, text) -> bool:
        return self._prompt(sid, text)

    def inject_silent(self, sid, text) -> bool:
        """免通知注入→同通道落地（SDK 协议无免唤醒面，模块 docstring 偏差注）。"""
        return self._prompt(sid, text)

    def _prompt(self, sid, text) -> bool:
        w = self._get(sid)
        if w is None:
            return False  # 未知会话：oc inject 契约同款（False 不抛）
        try:
            self._rpc(w, "session/prompt", {
                "sessionId": sid,
                "contentBlocks": [{"type": "text", "text": text}]})
            return True
        except DshUnavailable:
            return False  # 进程死/传输败＝死会话口径（上游同败兜底）

    def alive(self, sid) -> bool:
        w = self._get(sid)
        return w is not None and w.proc is not None and w.proc.poll() is None

    def session(self, sid):
        """会话态（F28 busy 口径：time.updated>idle＝回合进行中）。"""
        w = self._get(sid)
        if w is None or not self.alive(sid):
            return None
        with w.lock:
            return {"id": sid, "status": w.status,
                    "time": {"created": w.created_ms,
                             "updated": w.last_event_ms, "idle": w.idle_ms}}

    def last_message(self, sid):
        """会话末条消息投影（F52 stalled 摘要原料：role/type＋tool 名＋created）。"""
        w = self._get(sid)
        if w is None:
            return None
        with w.lock:
            for ev in reversed(w.events):
                m = _message_view(ev, w.calls)
                if m is not None:
                    return m
        return None

    def messages(self, sid) -> list:
        """会话消息投影全量（F57 用量原料：assistant 带用量字段）。"""
        w = self._get(sid)
        if w is None:
            return []
        with w.lock:
            calls = dict(w.calls)
            events = list(w.events)
        return [m for m in (_message_view(ev, calls) for ev in events)
                if m is not None]

    def probe(self) -> dict:
        return {"ok": True, "dsh_image": self.image}

    def models(self) -> list:
        """候选模型最小面（cordis.worker.yml 钉死的 gpt-load 路由单模）。"""
        return [self.model]

    # ---- 装配/测试面 ----

    def has(self, sid) -> bool:
        """sid 注册表命中（HarnessRouter 按 sid 分流的判据）。"""
        with self._lock:
            return sid in self._workers

    def close(self):
        """收口：shutdown 全部 worker（容器随 docker client 退出被 --rm 回收）。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            self._kill(w)

    # ---- 内部 ----

    def _argv(self, env_dir, dsh_home, agora_home):
        env_dir = os.path.realpath(env_dir)
        dsh_home = os.path.realpath(dsh_home)
        agora_home = os.path.realpath(agora_home)
        # dsh_bin 形态（复用 run-worker.sh）：写闸归 shim 判定链（F182 V1
        # 实证 danger-full-access 下越界写仍拒）＋headless worker 零交互。
        return [self._docker_bin(), "run", "-i", "--rm",
                "--network=host",
                "-u", f"{os.getuid()}:{os.getgid()}",
                "-e", self._glm_key_env,
                "-e", "HOME=/work/dsh-home", "-e", "DSH_HOME=/work/dsh-home",
                "-e", "AGORA_HOME=/work/agora-home",
                "-e", "AGORAD_SCRIPT=/dev/null",
                "-e", "DSH_PERMISSION_MODE=danger-full-access",
                "-e", "DSH_APPROVAL_POLICY=never",
                "-v", f"{self.repo_root}:/work/repo:ro",
                "-v", f"{env_dir}:/work/task-env",
                "-v", f"{agora_home}:/work/agora-home:ro",
                "-v", f"{dsh_home}:/work/dsh-home",
                self.image,
                "dsh", "--profile", "sdk",
                "--patch", "/work/repo/dsh-shim/cordis.worker.yml"]

    @staticmethod
    def _docker_bin():
        return os.environ.get("AGORA_DSH_DOCKER", "docker")

    def _get(self, sid):
        with self._lock:
            return self._workers.get(sid)

    def _glm_key(self):
        """GLM key 只经进程 env（部署/测试侧供给——提取式纪律归调用方，
        agorad 自身零凭据库接触；禁打印禁落盘）。"""
        return os.environ.get(self._glm_key_env, "").strip()

    def _rpc(self, w, method, params, timeout=_REQ_TIMEOUT):
        if w.proc is None or w.proc.poll() is not None:
            raise DshUnavailable(f"{w.sid}: dsh 进程已退出")
        with w.lock:
            rid = next(w.ids)
            p = _Pending()
            w.pending[rid] = p
        frame = json.dumps({"jsonrpc": "2.0", "id": rid,
                            "method": method, "params": params})
        try:
            w.proc.stdin.write(frame + "\n")
            w.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            with w.lock:
                w.pending.pop(rid, None)
            raise DshUnavailable(f"{w.sid}: stdin 断（进程死亡）: {e}") from e
        if not p.event.wait(timeout):
            with w.lock:
                w.pending.pop(rid, None)
            raise DshUnavailable(f"{w.sid}: {method} 应答超时 {timeout}s")
        if p.error is not None:
            raise DshUnavailable(
                f"{w.sid}: {method} 拒: {p.error}", status=-32603)
        return p.result

    def _read(self, w):
        """stdout 读者：按帧分派（id→pending 应答；method→通知记账）。"""
        try:
            for line in w.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue  # 坏帧忽略（协议口径：格式错误行被忽略）
                if not isinstance(frame, dict):
                    continue
                rid = frame.get("id")
                if "method" not in frame and rid is not None:
                    with w.lock:
                        p = w.pending.pop(rid, None)
                    if p is not None:
                        if "error" in frame and frame["error"] is not None:
                            p.error = (frame["error"] or {}).get(
                                "message", "unknown error")
                        else:
                            p.result = frame.get("result")
                        p.event.set()
                    continue
                try:
                    self._notify(w, frame)
                except Exception:
                    # 评审环 B 册 W2：单帧形状漂移只丢该帧不杀读线程——
                    # 读线程死＝进程活而无人应答（inject 恒超时的僵尸态）。
                    continue
        except Exception:
            pass  # 进程退出/管道关闭：读者自然收尾（alive 由 proc.poll 判）
        finally:
            with w.lock:
                for p in w.pending.values():
                    p.error = "dsh 进程退出"
                    p.event.set()
                w.pending.clear()
                # 评审环 B 册 W3：进程亡后注册表条目瘦身（保留近窗诊断史，
                # 防 daemon 长驻逐任务累计内存）；容器侧 EOF→sdk 有界关闭
                # →--rm 回收（agorad 退出即 docker client 亡→stdin 断）。
                del w.events[:-50]
                w.calls.clear()

    def _notify(self, w, frame):
        method = frame.get("method")
        params = frame.get("params")
        if not isinstance(params, dict):  # B 册 W2：形状守卫
            params = {}
        if method == "session.status":
            with w.lock:
                w.status = params.get("status")
                if params.get("status") == "idle":
                    w.idle_ms = _now_ms()
                w.last_event_ms = _now_ms()
            return
        if method == "session.event":
            ev = params.get("event")
            if not isinstance(ev, dict):
                return
            data = ev.get("data")
            if not isinstance(data, dict):  # B 册 W2：形状守卫
                data = {}
            with w.lock:
                w.last_event_ms = max(w.last_event_ms, _safe_ms(
                    ev.get("time"), w.last_event_ms))
                if ev.get("type") == "tool/call":
                    name = data.get("name")
                    call_id = data.get("callId")
                    if name and call_id:
                        w.calls[call_id] = name
                w.events.append(ev)
                if len(w.events) > _EVENT_CAP:
                    del w.events[:-_EVENT_CAP]
                if len(w.calls) > _EVENT_CAP:  # 与 events 同窗截断（评审
                    # C 册 suggestion：防长驻会话 callId 无界累计）
                    for stale in list(w.calls)[:len(w.calls) - _EVENT_CAP // 2]:
                        del w.calls[stale]

    def _drain_stderr(self, w):
        try:
            for line in w.proc.stderr:
                with w.lock:
                    w.stderr_tail.append(line.rstrip("\n"))
        except Exception:
            pass

    def _kill(self, w):
        try:
            if w.proc is not None and w.proc.poll() is None:
                try:
                    w.proc.stdin.close()
                except Exception:
                    pass
                try:
                    w.proc.terminate()
                    w.proc.wait(timeout=10)
                except Exception:
                    try:
                        w.proc.kill()
                    except Exception:
                        pass
        finally:
            with self._lock:
                self._workers.pop(w.sid, None)


def _now_ms():
    return int(time.time() * 1000)


def _safe_ms(value, fallback):
    """数值时间戳宽容提取（评审 B 册 W2：ISO 串/布尔/None 回退不抛）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return fallback


def _message_view(ev, calls):
    """session.event→Oc 消息投影（role/type＋content parts＋time.created）。

    F52 摘要消费面：role 或 type；content 内 tool part 带 name（opencode
    形状）；assistant 附 usage（F57 用量原料）。非消息类事件回 None。"""
    etype = ev.get("type")
    data = ev.get("data")
    if not isinstance(data, dict):  # 评审 B 册 W2：形状守卫
        data = {}
    # user/message 载荷直落 data（实测）；assistant/tool 包在 data.message。
    msg = data.get("message")
    if not isinstance(msg, dict):
        msg = data
    if etype in ("user/message", "assistant/message"):
        content = []
        for blk in (msg.get("content")
                    if isinstance(msg.get("content"), list) else []):
            if isinstance(blk, str):  # 实测：纯文本 user 消息＝字符串块直落
                if blk:
                    content.append({"type": "text", "text": blk})
            elif isinstance(blk, dict):
                text = blk.get("text")
                if text:
                    content.append({"type": "text", "text": str(text)})
        if not content and not msg.get("role"):
            return None  # 无信号帧（形状漂移残余——B 册 W2 收口）
        out = {"type": etype.split("/")[0], "role": msg.get("role"),
               "content": content,
               "time": {"created": _safe_ms(ev.get("time"), _now_ms())}}
        if etype == "assistant/message":
            usage = msg.get("usage") or {}
            out["usage"] = {
                "inputTokens": usage.get("inputTokens"),
                "outputTokens": usage.get("outputTokens"),
                "totalTokens": usage.get("totalTokens")}
        return out
    if etype == "tool/result":
        name = calls.get(msg.get("toolCallId") or data.get("callId"), "tool")
        return {"type": "tool", "role": "tool",
                "content": [{"type": "tool", "name": name}],
                "time": {"created": _safe_ms(ev.get("time"), _now_ms())}}
    return None


class HarnessRouter:
    """harness 分流面（Req: F184）——v2 main 与 v3 compose 共用。

    create 按任务 harness 路由（dsh 缺席即拒＝can_spawn 熔断口径）；
    注入/探活按 sid 路由：dsh 注册表命中走 Dsh，否则透传 oc。probe/models
    恒走 oc（doctor 面口径不变）。model 仅显式携带时透传（v3 oc inject
    两参面不被 v2 专属 kwarg 打爆）。"""

    def __init__(self, oc, dsh=None):
        self._oc = oc
        self._dsh = dsh

    def _route(self, sid):
        if self._dsh is not None and self._dsh.has(sid):
            return self._dsh
        return self._oc

    def create(self, env, boot, title=None, model=None, harness=None):
        if harness == "dsh":
            if self._dsh is None:
                raise DshUnavailable(
                    "harness=dsh 未配置（Dsh 适配器缺席）——部署注记 docs/reqs")
            return self._dsh.create(env, boot, title=title, model=model)
        return self._oc.create(env, boot, title=title, model=model)

    def inject(self, sid, text, model=None):
        r = self._route(sid)
        if model is None:
            return r.inject(sid, text)
        return r.inject(sid, text, model=model)

    def inject_silent(self, sid, text):
        return self._route(sid).inject_silent(sid, text)

    def inject_notify(self, sid, text):
        return self._route(sid).inject_notify(sid, text)

    def alive(self, sid):
        return self._route(sid).alive(sid)

    def session(self, sid):
        return self._route(sid).session(sid)

    def last_message(self, sid):
        return self._route(sid).last_message(sid)

    def messages(self, sid):
        return self._route(sid).messages(sid)

    def probe(self):
        return self._oc.probe()

    def models(self):
        return self._oc.models()
