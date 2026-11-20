"""Req: F138 — v3 auth 应用服务（opencode-agora v3 P3-T10，设计 §4 凭据模型）。

会话牌（token→sid/role）＋血统＋换号自愈绑定（bind_worker/bind_dispatcher）
＋角色矩阵，语义自 v2 core/auth.py＋core/app.py 平移。分层（§1）：仓库/oc
全 Protocol 构造注入——不 import access/storage，零 SQL（架构守卫在
test_architecture.py）。

凭据模型（§4）：credentials 表＝唯一记账权威（每 sid 一牌，sid 主键）；
secret 文件（home/credentials/*.token、.agora/root.token）不入库。签发
铁则＝secret 文件先落盘（0600，无「先建后 chmod」窗）后 DB 凭据提交
（UoW）——崩溃窗只留孤儿文件（DB 零残留），启动清扫（sweep）按牌内容
对账＋F43 终态任务文件即清。角色派生：sid=="root"→root（root 牌钉死
daemon 身份）；task_id 非空→worker；否则 dispatcher。identify 按
sha256 摘要 hmac.compare_digest 常时比较（不逐字符比对明文牌）。

血统（F16）：换号 rebind 旧 sid 入血统 FIFO 封顶 16（credential_lineage
表按 position 有序）；same_lineage＝现持牌血统含历史 sid（message_pull
血统补拉的判定面）。bind_worker/bind_dispatcher：已占 sid 拒绑（防冒充）、
活动行复检（TOCTOU 终态冻结不覆写）、嵌套子任务 dispatcher_sid 同步覆写、
死号经 oc alive 探测（oc 不可达≠死免判）、daemon 行（dispatcher_sid=
"root"）恒不愈合。

角色矩阵（Contracts 角色声明为准）：root→任意；worker→白名单
（ToolSpec.roles）；dispatcher→通用面＋task 域单列工具须有任务行
（归属细判＝dispatcher_sid==caller 归 tasksvc._own，同 v2 分工）。
线程模型：transport 单写者装配假设（tasksvc 同款），无锁。
"""
import dataclasses
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import time
import typing

from agorad.contracts import CATALOG, ContractError, ROLES
from agorad.domain import Credential, Event, Task, epoch_ms

LINEAGE_CAP = 16  # 血统封顶（gt-83185d R1）：最近 16 条曾用名，FIFO 最旧出局
ROOT_SID = "root"  # root 牌钉死 daemon 身份（ag-f80309 B 案）

# v2 may() task 域单列工具（语义保持）：dispatcher 须有任务行；归属终审归服务。
# 自 CATALOG.ownership 派生（fail-closed：新 task 域工具自动入闸）减 v2 语义
# 豁免面（行可选——task_dispatch 先建后派/approval_request 与 message_post
# 含 task 缺省的 peer/全局形态），恰为 v2 五件套（评审环 R1 C9）。
_TASK_ROW_OPTIONAL = frozenset({"task_dispatch", "approval_request",
                                "message_post"})
TASK_DOMAIN = (frozenset(n for n, s in CATALOG.items()
                         if s.ownership == "task")
               - _TASK_ROW_OPTIONAL)
# 换号自愈口径活动态（词汇对齐 tasksvc._ACTIVE；终态行冻结不覆写）
_ACTIVE = frozenset({"dispatched", "running", "done", "verify_failed",
                     "reopened", "blocked"})
_NESTED_OPEN = _ACTIVE | {"todo"}  # 已建未派子任务同保父 worker 所有权
_FROZEN = frozenset({"merged", "closed", "cancelled"})

EV_ROOT_ROTATED = "root_rotated"
EV_WORKER_REBOUND = "worker_rebound"
EV_DISPATCHER_REBIND = "dispatcher_rebind"


class AuthRepo(typing.Protocol):
    """storage 公开仓库接口（结构化；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def credentials_all(self) -> list[Credential]: ...
    def get_credential(self, sid) -> Credential | None: ...
    def put_credential(self, cred: Credential): ...
    def delete_credential(self, sid) -> bool: ...
    def get_task(self, tid) -> Task | None: ...
    def tasks_all(self) -> list[Task]: ...
    def upsert_task(self, t: Task): ...
    def append_event(self, ev: Event): ...


class OcProbe(typing.Protocol):
    """oc 注入面：bind_dispatcher 死号探测（调用必在 UoW 外）。"""

    def alive(self, sid) -> bool: ...


class AuthService:
    """牌面/血统/绑定/角色矩阵；内存索引随 DB 变更后整体重载。"""

    def __init__(self, store: AuthRepo, *, home="", clock=None, oc=None):
        self._store = store
        self._home = str(home)
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._oc = oc
        self._creds: list[Credential] = []
        self._reload()

    # ---- 签发／识别／撤销 ----

    def issue(self, sid, role, task_id=None):
        """签发＝secret 文件先落盘（0600）后 DB 提交；返回 (token, path)。
        root 牌钉死 sid="root"；worker 牌须携带签发任务。"""
        if role not in ROLES:
            raise ContractError("invalid_argument",
                                f"role 须为 {'/'.join(ROLES)}（收到 {role!r}）")
        if role == "root":
            sid = ROOT_SID
        elif sid == ROOT_SID:  # 角色按 sid 派生——非 root 角色签 root sid＝提权
            raise ContractError(
                "invalid_argument",
                f"sid={ROOT_SID!r} 为 daemon 身份钉死位，仅 root 牌可签")
        self._check_name(sid)
        if role == "worker" and not task_id:
            raise ContractError("invalid_argument",
                                "worker 牌签发须携带签发任务 task_id")
        if role != "worker":
            task_id = None
        elif task_id:
            self._check_name(task_id)  # 文件名键同受形态约束（防路径穿越）
        path = (pathlib.Path(self._home) / ".agora" / "root.token"
                if role == "root" else
                pathlib.Path(self._home) / "credentials" / f"{task_id or sid}.token")
        token = secrets.token_hex(16)
        self._write_secret(path, token)  # 先落盘：此后任一步败＝孤儿文件（sweep 面）
        with self._store.uow():
            self._store.put_credential(Credential(
                sid=sid, token=token, task_id=task_id,
                created_ms=self._now()))
        self._reload()
        return token, str(path)

    def identify(self, token):
        """token→(sid, role) | None；sha256 摘要 hmac.compare_digest 常时比较。"""
        cred = self._find(token)
        return (cred.sid, self._role(cred)) if cred is not None else None

    def _find(self, token):
        """按牌文本常时查牌（identify/revoke 共用；非常时入口零个）。"""
        if not isinstance(token, str) or not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for cred in self._creds:
            if hmac.compare_digest(
                    digest,
                    hashlib.sha256(cred.token.encode("utf-8")).digest()):
                return cred
        return None

    @staticmethod
    def _role(cred: Credential) -> str:
        if cred.sid == ROOT_SID:
            return "root"
        return "worker" if cred.task_id else "dispatcher"

    def token_for(self, sid):
        """按 sid 查牌文本（只读面）；无牌 None。"""
        return next((c.token for c in self._creds if c.sid == sid), None)

    def same_lineage(self, current_sid, historical_sid) -> bool:
        """F16：current_sid 现持牌的血统集含 historical_sid（换号链边连续）。"""
        return any(c.sid == current_sid and historical_sid in c.lineage
                   for c in self._creds)

    def rebind(self, old_sid, new_sid):
        """按 sid 改绑牌到新会话（token 本体不变）；old_sid 入血统 FIFO 封顶
        16。返回牌文本（供重注入送达），无牌 None。"""
        with self._store.uow():
            token = self._rebind_locked(old_sid, new_sid)
        self._reload()
        return token

    def _rebind_locked(self, old_sid, new_sid):
        """rebind 存储体（身处调用方 UoW；bind_* 复合事务共用）。
        守卫：同 sid 早退（put+delete 序自灭牌）；new_sid=="root" 拒——
        v3 角色按 sid 派生，回流 daemon 身份＝提权面（v2「rebind 不得回流
        触碰 root 牌」不变量，评审环 R1 C1）。"""
        if old_sid == new_sid:
            cred = self._store.get_credential(old_sid)
            return cred.token if cred is not None else None
        if new_sid == ROOT_SID:
            return None
        old = self._store.get_credential(old_sid)
        if old is None:
            return None
        lineage = tuple([*old.lineage, old_sid])[-LINEAGE_CAP:]
        self._store.put_credential(dataclasses.replace(
            old, sid=new_sid, lineage=lineage))
        self._store.delete_credential(old_sid)
        return old.token

    def revoke(self, token) -> bool:
        hit = self._find(token)  # 与 identify 同常时口径（评审环 R1 C7）
        if hit is None:
            return False
        with self._store.uow():
            self._store.delete_credential(hit.sid)
        self._reload()
        return True

    def revoke_session(self, sid) -> bool:
        """按 sid 作废该会话全部牌（每 sid 一牌＝单删）。"""
        with self._store.uow():
            ok = self._store.delete_credential(sid)
        self._reload()
        return ok

    def rotate_root(self):
        """doctor 一键轮换：旧 root 牌即废，新牌钉死 sid="root"；事件审计。
        签发铁则同 issue：secret 文件先落盘后 DB 提交（崩窗＝孤儿新文件，
        sweep 对账回写 DB 权威牌）；文件写败＝零 DB 变更（旧牌续用）。"""
        token = secrets.token_hex(16)
        path = pathlib.Path(self._home) / ".agora" / "root.token"
        try:
            self._write_secret(path, token)  # 先落盘（铁则）
            with self._store.uow():
                revoked = self._store.delete_credential(ROOT_SID)
                self._store.put_credential(Credential(
                    sid=ROOT_SID, token=token,
                    created_ms=self._now()))
                self._store.append_event(Event(
                    seq=0, ts_ms=self._now(), actor="root",
                    event=EV_ROOT_ROTATED, detail={"revoked": revoked}))
        finally:
            self._reload()  # DB 权威先收敛内存（文件写败亦不残陈旧牌面）
        return token, str(path)

    # ---- 启动清扫（F43＋孤儿文件）----

    def sweep(self) -> int:
        """终态任务牌文件启动即清（回收前 kill -9 崩窗）＋孤儿清扫（签发
        崩窗：文件已落、DB 未提交）＋root.token 对账（轮换崩窗：DB 权威牌
        回写文件，DB 无 root 牌即除孤儿）。留判＝牌内容对账（DB 有同 token
        牌）且（非任务键文件 或 任务在册非终态）。返回清除数（对账回写
        不计）。"""
        n = 0
        credsdir = pathlib.Path(self._home) / "credentials"
        root_file = pathlib.Path(self._home) / ".agora" / "root.token"
        root_cred = self._store.get_credential(ROOT_SID)
        if root_file.exists():
            try:
                on_disk = json.loads(
                    root_file.read_text(encoding="utf-8")).get("token")
            except (OSError, ValueError, AttributeError):
                on_disk = None
            if root_cred is None:
                root_file.unlink(missing_ok=True)  # 孤儿 root 牌文件
                n += 1
            elif on_disk != root_cred.token:
                self._write_secret(root_file, root_cred.token)  # DB 权威回写
        if not credsdir.is_dir():
            return n
        tokens = {c.token for c in self._creds}
        for p in sorted(credsdir.glob("*.token")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except OSError:
                continue  # 读失败≠孤儿：留判下轮，免误删活牌文件（R1 B5）
            except ValueError:
                data = None  # 撕裂/坏 JSON＝孤儿（清）
            token = data.get("token") if isinstance(data, dict) else None
            if token in tokens:
                t = self._store.get_task(p.stem)
                if t is None or t.state not in _FROZEN:
                    continue  # 在册活任务/非任务键（dispatcher 牌）文件：留
            p.unlink(missing_ok=True)
            n += 1
        return n

    # ---- 角色矩阵（Contracts 角色声明为准）----

    def may(self, role, tool, task_exists=False) -> bool:
        """root→任意；worker→白名单；dispatcher→通用面＋task 域单列须有
        任务行（归属细判 dispatcher_sid==caller 归 tasksvc._own）。"""
        spec = CATALOG.get(tool)
        if spec is None or role not in spec.roles:
            return False
        if role == "dispatcher" and tool in TASK_DOMAIN:
            return bool(task_exists)
        return True

    # ---- 换号自愈绑定（gt-d55b9e / F21）----

    def bind_worker(self, sid, caller):
        """worker 牌换号自愈：持牌即身份，漂移即改绑。守卫：已占 caller
        拒绑（防跨任务冒充）；活动行复检（TOCTOU 终态冻结）；嵌套子任务
        dispatcher_sid 同步覆写（worker_sid 行不串）。返回改绑后 sid。"""
        if caller == sid:
            return sid
        if caller == ROOT_SID:
            return sid  # daemon 身份不可被自证头冒领（角色按 sid 派生，R1 C2）
        self._check_name(caller)
        tasks = self._store.tasks_all()
        if any(caller in (t.worker_sid, t.dispatcher_sid) for t in tasks):
            return sid
        if not any(t.worker_sid == sid and t.state in _ACTIVE for t in tasks):
            return sid
        with self._store.uow():
            if self._store.get_credential(caller) is not None:
                return sid  # 事务内复检：caller 已持牌（快照后并发/无任务行
                # 的 fresh dispatcher）——每 sid 一牌，改绑＝覆写他人牌，拒
            fresh = [self._store.get_task(t.id) for t in tasks
                     if t.worker_sid == sid]
            if not any(t is not None and t.state in _ACTIVE for t in fresh):
                return sid  # 快照后门线程已结案：零活动行不改绑（TOCTOU）
            self._rebind_locked(sid, caller)
            now = self._now()
            for t in fresh:
                if t is None or t.state not in _ACTIVE:
                    continue  # 改绑窗内结案：终态行冻结不覆写
                self._store.upsert_task(dataclasses.replace(
                    t, worker_sid=caller, updated_ms=now))
                self._store.append_event(self._ev(
                    now, sid, EV_WORKER_REBOUND, t.id,
                    {"from": sid, "to": caller}))
            for t in tasks:  # R2 波及：嵌套所有权随父换号（dispatcher 字段不串 worker 行）
                if t.dispatcher_sid != sid or t.state not in _NESTED_OPEN:
                    continue
                c = self._store.get_task(t.id)
                if c is None or c.state not in _NESTED_OPEN:
                    continue  # 同上：终态行冻结（TOCTOU 复检）
                self._store.upsert_task(dataclasses.replace(
                    c, dispatcher_sid=caller, updated_ms=now))
                self._store.append_event(self._ev(
                    now, sid, EV_WORKER_REBOUND, c.id,
                    {"from": sid, "to": caller}))
        self._reload()
        return caller

    def bind_dispatcher(self, role, sid, task: Task | None):
        """F21 dispatcher 侧换号愈合（root 牌非链边前判）：登记 dispatcher
        已死（oc alive 探测）→ rebind＋活动行 dispatcher_sid 覆写。守卫：非
        root 牌／活号／daemon 行（="root"）／任务终态／已占 sid／oc 不可达
        （≠死，免判）一律不动。"""
        d = task.dispatcher_sid if task is not None else None
        if (role != "root" or not d or d == sid or d == ROOT_SID
                or sid == ROOT_SID):
            return  # daemon 行恒不愈合；治愈目标也不得是 daemon 身份（R1 C1）
        tasks = self._store.tasks_all()
        if not any(t.dispatcher_sid == d and t.state in _ACTIVE for t in tasks):
            return  # 终态不触发改绑
        if any(sid in (t.worker_sid, t.dispatcher_sid) for t in tasks):
            return  # 已占 sid 不抢绑（镜像 bind_worker）
        self._check_name(sid)
        try:
            if self._oc is None or self._oc.alive(d):
                return  # 活号不抢／无探测面不动
        except Exception:
            return  # oc 不可达≠会话死：本轮免判
        with self._store.uow():
            if self._store.get_credential(sid) is not None:
                return  # 事务内复检：sid 已持牌——改绑＝覆写他人牌，拒（R1 B4）
            fresh = [self._store.get_task(t.id) for t in tasks
                     if t.dispatcher_sid == d]
            if not any(t is not None and t.state in _ACTIVE for t in fresh):
                return  # 探活窗内已全数结案：无消费方不换牌（TOCTOU 复检）
            self._rebind_locked(d, sid)
            now = self._now()
            for t in fresh:
                if t is None or t.state not in _ACTIVE:
                    continue  # 终态行冻结不覆写（TOCTOU 复检）
                self._store.upsert_task(dataclasses.replace(
                    t, dispatcher_sid=sid, updated_ms=now))
                self._store.append_event(self._ev(
                    now, sid, EV_DISPATCHER_REBIND, t.id,
                    {"from": d, "to": sid}))
        self._reload()

    # ---- 内部 ----

    def _reload(self):
        self._creds = self._store.credentials_all()

    def _now(self):
        return self._clock()

    def _ev(self, ts_ms, actor, event, task=None, detail=None):
        return Event(seq=0, ts_ms=ts_ms, actor=actor, event=event,
                     task=task, detail=detail)

    @staticmethod
    def _check_name(name):
        if (not isinstance(name, str) or not name or "/" in name
                or "\\" in name or not name.isprintable()):
            raise ContractError("invalid_argument",
                                f"sid/tid 形态非法（收到 {name!r}）",
                                {"name": name})

    @staticmethod
    def _write_secret(path: pathlib.Path, token):
        """原子建 0600（os.open 带模式——无「先建后 chmod」世界可读窗；
        tasksvc._write_credential 同口径）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps({"token": token}) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)  # 不跟符号链接
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
