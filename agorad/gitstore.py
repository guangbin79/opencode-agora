"""Req: F147 — v3 GitStore Phase 0/1：Git 记账垂直切片（spec gitstore-phase0-1）。
Req: F150 — Phase 2 timer 仓库面（spec gitstore-phase2-timers）：objects/
timers/ 同款扩族＋九仓库方法（due 键集游标/next_due/message_for_timer），
TimerService 零改动跑 Git 后端。
Req: F151 — Phase 2 凭据生命周期（spec gitstore-phase2-credentials）：
canonical 凭据对象只记 token 摘要＋外部 secret 引用（明文牌不进任何 Git
对象），secret 文件按 AuthService 路径约定留在 journal.git 之外；put 先验
外部文件后记账，get/credentials_all 水合并验摘要；删除＝下一版本 tombstone
（历史版本仍在树可审计）；AuthService 零改动跑 Git 后端。
Req: F153 — Phase 2 artifact/event 仓库面（spec gitstore-phase2-artifacts-
gate-verify）：objects/artifacts/<bucket>/<id>.json 不可变追加族（服务端
单调派号，artifact.path 不入 canonical 路径）＋insert_artifact/
artifacts_for_task（newest-first）/events_for_task（SQLite _null("")→NULL
同归一化）；GateVerifyService 零改动跑 Git 后端。

裸仓 state/journal.git（HEAD 符号指向 refs/agora/journal）作不可变文本账本：
canonical 对象（tasks/messages/receipts/mail-cursors＋credentials/events/
timers 同款 bucket 扩族——get_credential/lineage pull、append_event 与
timer 版本化的持久面）＋每笔 UoW 恰一条 tx 记录与全部变更对象版本，合成
一个 commit。

UoW 语义（§4 同 storage 面）：写必须身处 uow；overlay 读己之写，外部读者
只见 HEAD；提交＝临时 GIT_INDEX_FILE 自 HEAD read-tree→逐变更 update-index
→write-tree→commit-tree(-p HEAD)→update-ref CAS（空历史首笔传零旧值断言
ref 不在——首笔也是 CAS）；CAS 败抛 CommitConflict 弃 overlay 不静默重放。
标识符（消息 id/事件 seq/tx seq/对象版本）自高水位分配，失败/CAS 弃单不
复用。外来账本（agora.db/tasks.jsonl 在而 journal 缺席）构造即拒且零动土。
启动重放校验 tx↔对象一致性（含对象头/bucket/tx 序号/operations 形状与
meta↔body 双向配对），canonical 损毁/重复 seq/缺引用对象 fail-closed（专用
错不外逃裸异常）。git 子进程圈禁本模块：谓词白名单精确（仅 plumbing，网络/
业务动词拒），UoW 守卫禁网络/业务子进程（自有 git 调用经 import 时捕获的
真 Popen 旁路）。基线发布＝单次赋值原子切换快照（读者无锁不撕裂）。
"""
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time

from agorad.domain import (Artifact, BaseProbe, Credential, DeliveryReceipt,
                           DeliveryState, Event, GateState, MailCursor,
                           Message, Task, Timer, TimerState, VerifySpec,
                           dispatch_secret_key, epoch_ms)
from agorad.storage import NoActiveUnitOfWork, StorageError, UnitOfWorkViolation

SCHEMA_VERSION = 1
EPOCH = 1
JOURNAL_REF = "refs/agora/journal"
BACKEND = "git"  # compose 后端选择名（Req: F155——名字常量随实现走）
_MISSING = "0" * 40  # update-ref 零旧值＝断言 ref 不在（空历史首笔也 CAS）

# spec §Repository model 初始化配置（精确六键）
JOURNAL_CONFIG = {
    "core.fsync": "object,reference",
    "core.fsyncMethod": "fsync",
    "core.logAllRefUpdates": "false",
    "core.autocrlf": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}

# spec §UoW/read：精确谓词白名单——查 argv 谓词不查可执行名
ALLOWED_VERBS = frozenset({
    "hash-object", "mktree", "read-tree", "update-index", "write-tree",
    "commit-tree", "update-ref", "ls-tree", "cat-file", "rev-parse",
    "config", "symbolic-ref", "show-ref"})

# UoW 守卫面（§4 同 storage：禁网络/子进程/文件遍历）；本模块自有 git 调用
# 走 _Popen（import 时捕获原件）旁路换装
_Popen = subprocess.Popen
_BLOCKED = (
    (socket, ("socket", "create_connection")),
    (subprocess, ("Popen", "run", "call", "check_output", "check_call")),
    (os, ("system", "popen", "walk")))

_GUARD_LOCK = threading.Lock()
_GUARD_REFS = 0
_GUARD_OWNERS = {}
_GUARD_SAVED = []


def _make_guard(original, name):
    def _violation(*args, **kwargs):
        with _GUARD_LOCK:
            guarded = threading.get_ident() in _GUARD_OWNERS
        if not guarded:
            return original(*args, **kwargs)
        raise UnitOfWorkViolation(
            f"UoW 写事务内禁 {name}（设计 §4：无网络/子进程/文件遍历）")
    return _violation


class GitVerbForbidden(StorageError):
    """白名单外 git 谓词（网络/业务动词）。"""


class ForeignLedgerError(StorageError):
    """外来账本在位而 journal 缺席——构造即拒，防 split-brain。"""


class CommitConflict(StorageError):
    """update-ref CAS 败：基线 head 已前移，overlay 已弃（不静默重放）。"""


class JournalCorruptionError(StorageError):
    """重放校验失败（canonical 损毁/重复 seq/缺引用对象）——fail-closed。"""


class CredentialSecretError(StorageError):
    """活牌外部 secret 文件缺席/牌值不符（Req: F151）——fail-closed，
    不静默空牌。"""


def check_verb(verb):
    """argv 谓词白名单闸（fetch/push/clone/remote/add/commit/merge/rebase 拒）。"""
    if verb not in ALLOWED_VERBS:
        raise GitVerbForbidden(
            f"git 谓词 {verb!r} 不在白名单（仅 plumbing：{sorted(ALLOWED_VERBS)}）")


def _v(n) -> str:
    return f"{n:020d}"


def _bucket(key) -> str:
    h = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return f"{h[:2]}/{h[2:4]}"


# canonical 路径段白字符集（引号/非 ASCII/控制符不入路径——ls-tree 转义面
# 与重放往返一致性的写入面闸；真 sid/tid 形如 ses-xxx/ag-xxxx 天然在集内）
_ID_CHARSET = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:@+-]*")


def _safe(name) -> str:
    name = str(name)
    if not _ID_CHARSET.fullmatch(name):
        raise StorageError(
            f"非法 canonical 路径段（白字符集外）: {name!r}")
    return name


def _task_data(t: Task) -> dict:
    d = dataclasses.asdict(t)
    d["after"] = list(t.after)
    return d


def _task_from_data(tid: str, d: dict) -> Task:
    d = dict(d)
    d["id"] = tid
    d["after"] = tuple(d.get("after") or ())
    d["verify"] = VerifySpec(**d["verify"])
    if d["delivery"] is not None:
        d["delivery"] = DeliveryState(**d["delivery"])
    g = dict(d["gate"])
    if g.get("gate_artifact") is not None:
        g["gate_artifact"] = Artifact(**g["gate_artifact"])
    d["gate"] = GateState(**g)
    if d.get("probe") is not None:
        d["probe"] = BaseProbe(**d["probe"])
    return Task(**d)


def _timer_data(t: Timer) -> dict:
    return {"task_id": t.task_id, "target_role": t.target_role,
            "due_ms": t.due_ms, "state": t.state.value,
            "created_ms": t.created_ms, "fired_ms": t.fired_ms,
            "body": t.body, "created_by": t.created_by,
            "client_key": t.client_key}


def _timer_from_data(tid: str, d: dict) -> Timer:
    tm = Timer(id=tid, task_id=d["task_id"],
               target_role=d["target_role"], due_ms=d["due_ms"],
               state=TimerState(d["state"]), created_ms=d["created_ms"],
               fired_ms=d["fired_ms"], body=d["body"],
               created_by=d["created_by"], client_key=d["client_key"])
    # 标量错型 fail-closed（重放面）：错型 due/created 会把 TypeError 延迟到
    # 每次扫描且常驻不收敛——构造期即拒（storage 列类型同语义；bool 非本集）
    if (type(tm.due_ms) is not int
            or not (tm.created_ms is None or type(tm.created_ms) is int)
            or not (tm.fired_ms is None or type(tm.fired_ms) is int)
            or not isinstance(tm.body, str)
            or not isinstance(tm.task_id, str)
            or not isinstance(tm.target_role, str)
            or not (tm.created_by is None or isinstance(tm.created_by, str))
            or not (tm.client_key is None or isinstance(tm.client_key, str))):
        raise ValueError("timer 标量字段类型不符")
    return tm


def _artifact_from_data(d) -> Artifact:
    """canonical artifact data → domain Artifact（Req: F153）；错型
    fail-closed（重放面，SQLite 列约束同语义：bytes/rc/started/ended
    精确 int——bool 不冒充、duration_s 数值、str 列不可 null）。"""
    if not isinstance(d, dict):
        raise TypeError("data 非对象")
    try:
        art = Artifact(task_id=d["task_id"], phase=d["phase"],
                       path=d["path"], cmd=d["cmd"], rc=d["rc"],
                       failure_kind=d["failure_kind"],
                       duration_s=d["duration_s"], bytes=d["bytes"],
                       error=d["error"], started_at_ms=d["started_at_ms"],
                       ended_at_ms=d["ended_at_ms"])
    except (TypeError, KeyError) as exc:
        raise TypeError(f"artifact 字段不符: {exc}")
    if (not isinstance(art.task_id, str) or not isinstance(art.phase, str)
            or not isinstance(art.path, str) or not isinstance(art.cmd, str)
            or type(art.bytes) is not int
            or not (art.rc is None or type(art.rc) is int)
            or not (art.failure_kind is None
                    or isinstance(art.failure_kind, str))
            or not (art.duration_s is None
                    or (isinstance(art.duration_s, (int, float))
                        and not isinstance(art.duration_s, bool)))
            or not (art.error is None or isinstance(art.error, str))
            or not (art.started_at_ms is None
                    or type(art.started_at_ms) is int)
            or not (art.ended_at_ms is None or type(art.ended_at_ms) is int)):
        raise ValueError("artifact 标量字段类型不符")
    return art


_RE_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cred_reg_from_data(d) -> dict | None:
    """canonical 凭据 data → 注册面 dict（tombstone→None）；错型/坏摘要
    形态抛错（重放面 fail-closed，Req: F151）。"""
    if not isinstance(d, dict):
        raise TypeError("data 非对象")
    if d.get("deleted") is True:
        return None
    digest, spath = d.get("token_sha256"), d.get("secret_path")
    if not isinstance(digest, str) or not _RE_SHA256.fullmatch(digest):
        raise ValueError("token_sha256 摘要形态非法")
    if not isinstance(spath, str) or not spath.endswith(".token"):
        raise ValueError("secret_path 缺席/形态非法")
    # 键位白名单（评审环 C 册 R1）：canonical 只许派生键位——绝对路径与
    # ../ 穿越一律拒（_hydrate join home 后直开，防任意文件绑定/存在性神谕）
    if not (spath == ".agora/root.token"
            or (spath.startswith("credentials/")
                and _ID_CHARSET.fullmatch(
                    spath[len("credentials/"):-len(".token")]))):
        raise ValueError("secret_path 须为派生键位（.agora/root.token 或 "
                         "credentials/<白名单键>.token）")
    tid, lineage, created = d.get("task_id"), d.get("lineage"), d.get("created_ms")
    if not (tid is None or isinstance(tid, str)):
        raise TypeError("task_id 类型不符")
    if not isinstance(lineage, list) or not all(
            isinstance(x, str) for x in lineage):
        raise TypeError("lineage 类型不符")
    if not (created is None or type(created) is int):
        raise TypeError("created_ms 类型不符")
    return {"token_sha256": digest, "secret_path": spath, "task_id": tid,
            "lineage": lineage, "created_ms": created}


# canonical 路径（bucket＝sha256(id/seq) 前两字节拆两个 hex 目录）
_H = r"([0-9a-f]{2}/[0-9a-f]{2})"
_RE_TASK = re.compile(rf"^objects/tasks/{_H}/(?P<id>.+)/(?P<v>[0-9]{{20}})\.json$")
_RE_MSG_META = re.compile(rf"^objects/messages/{_H}/(?P<id>[0-9]{{20}})/meta\.json$")
_RE_MSG_BODY = re.compile(rf"^objects/messages/{_H}/(?P<id>[0-9]{{20}})/body\.txt$")
_RE_RECEIPT = re.compile(rf"^objects/receipts/{_H}/(?P<id>[0-9]{{20}})/(?P<v>[0-9]{{20}})\.json$")
_RE_CURSOR = re.compile(rf"^objects/mail-cursors/{_H}/(?P<id>.+)/(?P<v>[0-9]{{20}})\.json$")
_RE_CRED = re.compile(rf"^objects/credentials/{_H}/(?P<id>.+)/(?P<v>[0-9]{{20}})\.json$")
_RE_TIMER = re.compile(rf"^objects/timers/{_H}/(?P<id>.+)/(?P<v>[0-9]{{20}})\.json$")
_RE_ARTIFACT = re.compile(rf"^objects/artifacts/{_H}/(?P<id>[0-9]{{20}})\.json$")
_RE_EVENT = re.compile(rf"^objects/events/{_H}/(?P<seq>[0-9]{{20}})\.json$")
_RE_TX = re.compile(rf"^tx/{_H}/(?P<seq>[0-9]{{20}})\.json$")


@dataclasses.dataclass
class _Write:
    """一笔待提交对象版本（message 另携字节体走 body.txt）。"""
    op: str            # tx 操作名（＝仓库方法名）
    kind: str          # 对象 kind（task/message/receipt/cursor/credential/event）
    ident: object      # id（str）或 号（int）
    version: int
    data: dict
    body: bytes | None = None
    from_version: int | None = None


def _obj_bytes(kind, ident, version, seq, data) -> bytes:
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "kind": kind, "id": ident,
         "version": version, "epoch": EPOCH, "seq": seq, "data": data},
        ensure_ascii=False, sort_keys=True).encode("utf-8")


def _empty_state():
    return {"head": None, "tasks": {}, "messages": {}, "receipts": {},
            "cursors": {}, "credentials": {}, "events": {}, "txs": {},
            "dedup": {}, "timers": {}, "timer_ents": {}, "cred_reg": {},
            "artifacts": {}}


class GitStore:
    """Git 记账后备（MessageStore/task 垂直仓库面；同构可替换 Storage）。

    跨实例内存面不自动跟随（读己之写以实例为单位；跨实例新鲜度＝重开
    重放）。journal.git 字样与 git 子进程圈禁本模块（test_architecture.py）。
    """

    def __init__(self, state_path, *, injector=None, actor="gitstore",
                 clock=None):
        self._state = os.fspath(state_path)
        self._journal = os.path.join(os.path.abspath(self._state),
                                     "journal.git")
        self._home = os.path.dirname(os.path.abspath(self._state))  # secret 文件根（Req: F151）
        self._injector = injector
        self._actor = actor
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._ov = None
        self._owner = None
        self._lock = threading.Lock()
        self._idx_n = 0
        self._snap = _empty_state()  # 已提交基线（单次赋值原子切换）
        self._hwm = {}               # 标识符高水位（message/event/tx）——弃单不复用
        self._vhwm = {}              # (kind, id) -> 已分配最高版本
        self._init_or_refuse()
        self._replay()

    @property
    def head(self):
        return self._snap["head"]

    # ---- git 子进程（谓词白名单＋最小环境）----

    def _git(self, args, *, index=None, input_bytes=None, check=True):
        check_verb(args[0])
        env = {"PATH": os.environ.get("PATH", ""),
               "GIT_DIR": self._journal,
               "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_AUTHOR_NAME": "agora",
               "GIT_AUTHOR_EMAIL": "agora@journal.invalid",
               "GIT_COMMITTER_NAME": "agora",
               "GIT_COMMITTER_EMAIL": "agora@journal.invalid",
               "LC_ALL": "C"}
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        proc = _Popen(
            ["git", *args], env=env,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(input_bytes)
        if check and proc.returncode != 0:
            raise StorageError(
                f"git {args[0]} rc={proc.returncode}: "
                f"{err.decode('utf-8', 'replace').strip()}")
        return proc.returncode, out, err

    # ---- 初始化/外来账本拒建 ----

    def _init_or_refuse(self):
        fresh = not os.path.isdir(self._journal)
        if fresh:
            foreign = [n for n in ("agora.db", "tasks.jsonl")
                       if os.path.exists(os.path.join(self._state, n))]
            if foreign:
                raise ForeignLedgerError(
                    f"{self._state}: 外来账本在位 {foreign} 而 journal.git 缺席"
                    "——拒建（防 split-brain），构造零动土")
        # 骨架幂等补齐（初始化崩溃窗半建自愈：HEAD/objects/refs 恒确保）
        for sub in ("objects", "refs"):
            os.makedirs(os.path.join(self._journal, sub), exist_ok=True)
        head = os.path.join(self._journal, "HEAD")
        if not os.path.exists(head):
            with open(head, "w", encoding="utf-8") as f:
                f.write(f"ref: {JOURNAL_REF}\n")
        if fresh:
            os.chmod(self._journal, 0o700)
        cfg_path = os.path.join(self._journal, "config")
        text = ""
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                text = f.read()
        for key, value in JOURNAL_CONFIG.items():  # 幂等补齐（崩溃窗半配自愈）
            var = key.rsplit(".", 1)[-1]
            if f"{var} = {value}" not in text:
                self._git(("config", "--file", cfg_path, key, value))

    # ---- 启动重放＋fail-closed 校验 ----

    def _replay(self):
        rc, out, _ = self._git(("rev-parse", "--verify", JOURNAL_REF),
                               check=False)
        if rc != 0:
            self._snap = _empty_state()  # ref 消失（CAS 路径重入）＝清空基线
            return
        head = out.decode().strip()
        _, out, _ = self._git(("ls-tree", "-r", "-z", head))
        entries = {}
        for chunk in out.split(b"\x00"):  # -z：路径不转义（quotePath 免疫）
            if not chunk:
                continue
            meta, _, path = chunk.partition(b"\t")
            entries[path.decode("utf-8", "surrogateescape")] = (
                meta.split()[2].decode())

        def bad(path, why):
            raise JournalCorruptionError(
                f"{self._journal}: 重放校验失败 {why}（{path}）")

        def obj_of(sha, path):
            try:
                obj = json.loads(
                    self._git(("cat-file", "blob", sha))[1].decode("utf-8"))
            except (StorageError, UnicodeDecodeError, ValueError):
                bad(path, "canonical JSON 不可解析")
            if not isinstance(obj, dict):
                bad(path, "canonical JSON 非对象")
            return obj

        def checked(obj, path, kind, ident, version, bucket):
            if (obj.get("schema_version") != SCHEMA_VERSION
                    or obj.get("epoch") != EPOCH or obj.get("kind") != kind
                    or obj.get("id") != ident or obj.get("version") != version
                    or type(obj.get("seq")) is not int  # bool 非 int（R2）
                    or _bucket(ident) != bucket):
                bad(path, "对象头/路径不一致")
            return obj

        tasks, receipts, cursors, creds, timer_ents, artifacts = (
            {}, {}, {}, {}, {}, {})
        metas, bodies, events, txs = {}, {}, {}, {}
        obj_seqs = []
        for path, sha in entries.items():
            m = _RE_TASK.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "task", m["id"],
                              int(m["v"]), m.group(1))
                obj_seqs.append(obj["seq"])
                try:
                    ent = _task_from_data(m["id"], obj["data"])
                except (TypeError, KeyError, ValueError) as exc:
                    bad(path, f"task 实体还原失败: {exc}")
                cur = tasks.get(m["id"])
                if cur is None or int(m["v"]) > cur[0]:
                    tasks[m["id"]] = (int(m["v"]), ent)
                continue
            m = _RE_MSG_META.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "message",
                              int(m["id"]), 1, m.group(1))
                obj_seqs.append(obj["seq"])
                metas[int(m["id"])] = obj["data"]
                continue
            m = _RE_MSG_BODY.match(path)
            if m:
                bodies[int(m["id"])] = self._git(("cat-file", "blob",
                                                 sha))[1]
                continue
            m = _RE_RECEIPT.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "receipt",
                              int(m["id"]), int(m["v"]), m.group(1))
                obj_seqs.append(obj["seq"])
                d = obj["data"]
                try:
                    ent = DeliveryReceipt(message_id=int(m["id"]), **d)
                except (TypeError, KeyError, ValueError) as exc:
                    bad(path, f"receipt 实体还原失败: {exc}")
                cur = receipts.get(int(m["id"]))
                if cur is None or int(m["v"]) > cur[0]:
                    receipts[int(m["id"])] = (int(m["v"]), ent)
                continue
            m = _RE_CURSOR.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "cursor", m["id"],
                              int(m["v"]), m.group(1))
                obj_seqs.append(obj["seq"])
                try:
                    ent = MailCursor(sid=m["id"],
                                     last_read_ms=obj["data"]["last_read_ms"])
                except (KeyError, TypeError) as exc:
                    bad(path, f"cursor 实体还原失败: {exc}")
                cur = cursors.get(m["id"])
                if cur is None or int(m["v"]) > cur[0]:
                    cursors[m["id"]] = (int(m["v"]), ent)
                continue
            m = _RE_CRED.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "credential", m["id"],
                              int(m["v"]), m.group(1))
                obj_seqs.append(obj["seq"])
                try:  # Req: F151——tombstone→None；错型/坏摘要 fail-closed
                    reg = _cred_reg_from_data(obj["data"])
                except (TypeError, KeyError, ValueError) as exc:
                    bad(path, f"credential 实体还原失败: {exc}")
                cur = creds.get(m["id"])
                if cur is None or int(m["v"]) > cur[0]:
                    creds[m["id"]] = (int(m["v"]), reg)
                continue
            m = _RE_TIMER.match(path)
            if m:  # timer 版本族：路径唯一即 (id,version) 唯一——重版冒位
                # 由 bucket/头校验 fail-closed；此处恢复每 id 最新版本
                obj = checked(obj_of(sha, path), path, "timer", m["id"],
                              int(m["v"]), m.group(1))
                obj_seqs.append(obj["seq"])
                try:
                    ent = _timer_from_data(m["id"], obj["data"])
                except (TypeError, KeyError, ValueError) as exc:
                    bad(path, f"timer 实体还原失败: {exc}")
                cur = timer_ents.get(m["id"])
                if cur is None or int(m["v"]) > cur[0]:
                    timer_ents[m["id"]] = (int(m["v"]), ent)
                continue
            m = _RE_ARTIFACT.match(path)
            if m:  # artifact 不可变追加族：路径唯一即 id 唯一——冒位重号由
                # bucket/头校验 fail-closed；每对象即一实体（无版本折叠）
                obj = checked(obj_of(sha, path), path, "artifact",
                              int(m["id"]), 1, m.group(1))
                obj_seqs.append(obj["seq"])
                try:  # Req: F153——错型 fail-closed
                    ent = _artifact_from_data(obj["data"])
                except (TypeError, KeyError, ValueError) as exc:
                    bad(path, f"artifact 实体还原失败: {exc}")
                artifacts[int(m["id"])] = ent
                continue
            m = _RE_EVENT.match(path)
            if m:
                obj = checked(obj_of(sha, path), path, "event",
                              int(m["seq"]), int(m["seq"]), m.group(1))
                obj_seqs.append(obj["seq"])
                d = obj["data"]
                try:
                    ent = Event(seq=int(m["seq"]), ts_ms=d["ts_ms"],
                                actor=d["actor"], event=d["event"],
                                task=d.get("task"), detail=d.get("detail"))
                except (KeyError, TypeError) as exc:
                    bad(path, f"event 实体还原失败: {exc}")
                events[int(m["seq"])] = ent
                continue
            m = _RE_TX.match(path)
            if m:
                obj = obj_of(sha, path)
                if (obj.get("schema_version") != SCHEMA_VERSION
                        or obj.get("epoch") != EPOCH
                        or type(obj.get("seq")) is not int
                        or obj.get("seq") != int(m["seq"])
                        or _bucket(int(m["seq"])) != m.group(1)
                        or not isinstance(obj.get("operations"), list)):
                    bad(path, "tx 头/序号/bucket/operations 与路径不符")
                if int(m["seq"]) in txs:
                    bad(path, "tx seq 重复")
                txs[int(m["seq"])] = obj
                continue
            # 非 canonical 路径：前瞻兼容忽略
        # 交叉校验：tx↔对象引用闭合＋meta↔body 双向配对
        seen_tx = set(txs)
        for seq in obj_seqs:
            if seq not in seen_tx:
                bad(f"seq={seq}", "对象引用的 tx 缺席")
        for mid in metas:
            if mid not in bodies:
                bad(f"objects/messages/*/{_v(mid)}/body.txt",
                    "消息字节体缺席")
        for mid in bodies:
            if mid not in metas:
                bad(f"objects/messages/*/{_v(mid)}/body.txt",
                    "孤儿字节体（meta 缺席）")
        for obj in txs.values():
            for op in obj["operations"]:
                path = op.get("path") if isinstance(op, dict) else None
                if not isinstance(path, str) or path not in entries:
                    bad(path if isinstance(path, str)
                        else "<非对象操作/无路径>", "tx 操作引用对象缺席")
        # 落内存（对象/消息字典本体）——坏 meta 一律 fail-closed 专用错
        messages = {}
        for mid, d in metas.items():
            try:
                messages[mid] = {
                    "msg": Message(id=mid, ts_ms=d["ts_ms"],
                                   task_id=d["task_id"], kind=d["kind"],
                                   from_sid=d["from_sid"], to_sid=d["to_sid"],
                                   body=bodies[mid],
                                   dedup_key=d.get("dedup_key"),
                                   replay_of=d.get("replay_of")),
                    "timer_id": d.get("timer_id")}
            except (KeyError, TypeError) as exc:
                bad(f"objects/messages/*/{_v(mid)}/meta.json",
                    f"message 实体还原失败: {exc}")
        dedup = {r["msg"].dedup_key: mid for mid, r in messages.items()
                 if r["msg"].dedup_key is not None}
        timers = {r["timer_id"]: mid for mid, r in messages.items()
                  if r["timer_id"] is not None}
        # Req: F151——最新版本定 live/tombstone；活牌读外部 secret 文件水合
        # 并验摘要（缺/不符 CredentialSecretError fail-closed，不静默空牌）
        cred_reg = {}
        for sid, (v, reg) in creds.items():
            if reg is None:
                continue  # tombstone 最新版＝牌缺席（历史版本仍在树可审计）
            creds[sid] = (v, self._hydrate(sid, reg))
            cred_reg[sid] = reg
        # 原子切换快照（重载场景高水位只升不降——弃单不复用）
        self._snap = {"head": head, "tasks": tasks, "messages": messages,
                      "receipts": receipts, "cursors": cursors,
                      "credentials": creds, "events": events, "txs": txs,
                      "dedup": dedup, "timers": timers,
                      "timer_ents": timer_ents, "cred_reg": cred_reg,
                      "artifacts": artifacts}
        for key, top in (("message", max(messages, default=0)),
                         ("event", max(events, default=0)),
                         ("artifact", max(artifacts, default=0)),
                         ("tx", max(txs, default=0))):
            self._hwm[key] = max(self._hwm.get(key, 0), top)
        for table, kind in ((tasks, "task"), (receipts, "receipt"),
                            (cursors, "cursor"), (creds, "credential"),
                            (timer_ents, "timer")):
            for ident, (v, _e) in table.items():
                k = (kind, str(ident))
                self._vhwm[k] = max(self._vhwm.get(k, 0), v)

    # ---- UoW（单一写者＋守卫＋overlay）----

    @contextlib.contextmanager
    def uow(self):
        if self._ov is not None and self._owner == threading.get_ident():
            raise StorageError("UoW 不可嵌套（单一写者，设计 §4）")
        self._lock.acquire()
        try:
            if self._ov is not None:  # 锁下互斥兜底（理论不可达）
                raise StorageError("UoW 写者互斥冲突")
            self._ov = {"writes": [], "tasks": {}, "messages": {},
                        "receipts": {}, "cursors": {}, "credentials": {},
                        "events": {}, "dedup": {}, "timers": {},
                        "timer_ents": {}, "cred_reg": {}, "artifacts": {}}
            self._owner = threading.get_ident()
            try:
                self._install_guard()
                try:
                    yield self
                    self._commit()
                finally:
                    self._restore_guard()
            finally:
                self._ov = None
                self._owner = None
        finally:
            self._lock.release()

    def _install_guard(self):
        global _GUARD_REFS
        with _GUARD_LOCK:
            me = threading.get_ident()
            _GUARD_OWNERS[me] = _GUARD_OWNERS.get(me, 0) + 1
            _GUARD_REFS += 1
            if _GUARD_REFS == 1:
                for mod, names in _BLOCKED:
                    for name in names:
                        original = getattr(mod, name)
                        _GUARD_SAVED.append((mod, name, original))
                        setattr(mod, name, _make_guard(original, name))

    def _restore_guard(self):
        global _GUARD_REFS
        with _GUARD_LOCK:
            me = threading.get_ident()
            _GUARD_OWNERS[me] -= 1
            if not _GUARD_OWNERS[me]:
                del _GUARD_OWNERS[me]
            _GUARD_REFS -= 1
            if _GUARD_REFS == 0:
                while _GUARD_SAVED:
                    setattr(*_GUARD_SAVED.pop())

    def _require_uow(self):
        if (self._ov is None
                or self._owner != threading.get_ident()):
            raise NoActiveUnitOfWork(
                "写操作必须身处 with st.uow()（设计 §4）")

    def _in_uow(self):
        return (self._ov is not None
                and self._owner == threading.get_ident())

    def _fire(self, name, phase, **context):
        if self._injector is not None:
            self._injector.fire(name, phase, **context)

    # ---- 提交（单 tx＋全部变更对象版本＝单 commit）----

    def _obj_path(self, kind, ident, version):
        if kind == "task":
            return f"objects/tasks/{_bucket(_safe(ident))}/{_safe(ident)}/{_v(version)}.json"
        if kind == "message":
            return f"objects/messages/{_bucket(ident)}/{_v(ident)}/meta.json"
        if kind == "receipt":
            return (f"objects/receipts/{_bucket(ident)}/{_v(ident)}/"
                    f"{_v(version)}.json")
        if kind == "cursor":
            return (f"objects/mail-cursors/{_bucket(_safe(ident))}/"
                    f"{_safe(ident)}/{_v(version)}.json")
        if kind == "credential":
            return (f"objects/credentials/{_bucket(_safe(ident))}/"
                    f"{_safe(ident)}/{_v(version)}.json")
        if kind == "timer":
            return (f"objects/timers/{_bucket(_safe(ident))}/"
                    f"{_safe(ident)}/{_v(version)}.json")
        if kind == "artifact":
            return f"objects/artifacts/{_bucket(ident)}/{_v(ident)}.json"
        return f"objects/events/{_bucket(ident)}/{_v(ident)}.json"  # event

    def _commit(self):
        ov = self._ov
        writes = ov["writes"]
        if not writes:
            return  # 只读 UoW：零提交零 tx
        old = self._snap["head"]
        tx_seq = self._alloc("tx")
        files, ops = [], []
        for w in writes:
            path = self._obj_path(w.kind, w.ident, w.version)
            files.append((path, _obj_bytes(w.kind, w.ident, w.version,
                                           tx_seq, w.data)))
            if w.body is not None:
                files.append((f"objects/messages/{_bucket(w.ident)}/"
                              f"{_v(w.ident)}/body.txt", w.body))
            op = {"op": w.op, "id": w.ident, "path": path}
            if w.from_version is not None:
                op["from_version"] = w.from_version
                op["to_version"] = w.version
            ops.append(op)
        kinds = {w.op for w in writes}
        tx = {"schema_version": SCHEMA_VERSION, "epoch": EPOCH, "seq": tx_seq,
              "kind": kinds.pop() if len(kinds) == 1 else "composite",
              "actor": self._actor, "ts_ms": self._clock(),
              "base_head": old, "operations": ops}
        files.append((f"tx/{_bucket(tx_seq)}/{_v(tx_seq)}.json",
                      json.dumps(tx, ensure_ascii=False,
                                 sort_keys=True).encode("utf-8")))
        self._idx_n += 1
        index = os.path.join(
            self._journal,
            f"index.tmp.{os.getpid()}.{id(self)}.{self._idx_n}")
        try:
            self._fire("write-object", "before", tx=tx_seq, objects=len(writes))
            self._git((("read-tree", old) if old is not None
                       else ("read-tree", "--empty")), index=index)
            for path, blob in files:
                sha = self._git(("hash-object", "-w", "--stdin"),
                                input_bytes=blob, index=index)[1].decode().strip()
                self._git(("update-index", "--add", "--cacheinfo",
                           f"100644,{sha},{path}"), index=index)
            self._fire("write-object", "after", tx=tx_seq)
            self._fire("write-tree", "before", tx=tx_seq)
            tree = self._git(("write-tree",), index=index)[1].decode().strip()
            self._fire("write-tree", "after", tx=tx_seq, tree=tree)
            cargs = ["commit-tree", "-m", f"agora tx {tx_seq} ops={len(ops)}",
                     tree]
            if old is not None:
                cargs += ["-p", old]
            self._fire("commit-tree", "before", tx=tx_seq)
            new = self._git(tuple(cargs), index=index)[1].decode().strip()
            self._fire("commit-tree", "after", tx=tx_seq, commit=new)
            self._fire("update-ref", "before", tx=tx_seq)
            rargs = ("update-ref", JOURNAL_REF, new,
                     old if old is not None else _MISSING)  # 首笔也 CAS
            rc, _, err = self._git(rargs, check=False)
            if rc != 0:
                self._replay()  # 重载基线（高水位只升不降），弃单不复用
                raise CommitConflict(
                    f"update-ref CAS 败（期望 {old}）: "
                    f"{err.decode('utf-8', 'replace').strip()}")
            self._fire("update-ref", "after", tx=tx_seq, commit=new)
            self._fire("memory-publish", "before", tx=tx_seq)
            self._publish(ov, tx_seq, new, tx)
            self._fire("memory-publish", "after", tx=tx_seq)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(index)

    def _publish(self, ov, tx_seq, new, tx):
        """触达族 copy-on-write 合成新快照，单次赋值原子发布（读者不撕裂）。"""
        s = self._snap
        ns = {"head": new, "txs": {**s["txs"], tx_seq: tx}}
        for key in ("tasks", "messages", "receipts", "cursors",
                    "credentials", "events", "dedup", "timers",
                    "timer_ents", "cred_reg", "artifacts"):
            ns[key] = {**s[key], **ov[key]} if ov[key] else s[key]
        self._snap = ns

    # ---- 标识符/版本分配（高水位只升不降）----

    def _alloc(self, key):
        self._hwm[key] = self._hwm.get(key, 0) + 1
        return self._hwm[key]

    def _alloc_version(self, kind, ident, committed):
        key = (kind, str(ident))
        base = max(committed, self._vhwm.get(key, 0))
        self._vhwm[key] = base + 1
        return base + 1

    # ---- tasks（垂直面）----

    def upsert_task(self, t: Task) -> Task:
        self._require_uow()
        tid = _safe(t.id)
        cur = self._task_view(tid)
        fv = cur[0] if cur is not None else 0
        v = self._alloc_version("task", tid, fv)
        self._ov["tasks"][tid] = (v, t)
        self._ov["writes"].append(_Write(
            "upsert_task", "task", tid, v, _task_data(t), from_version=fv))
        return t

    def _task_view(self, tid):
        if self._in_uow() and tid in self._ov["tasks"]:
            return self._ov["tasks"][tid]
        return self._snap["tasks"].get(tid)

    def get_task(self, tid):
        cur = self._task_view(tid)
        return cur[1] if cur is not None else None

    def tasks_all(self):
        merged = dict(self._snap["tasks"])
        if self._in_uow():
            merged.update(self._ov["tasks"])
        return [merged[k][1] for k in sorted(merged)]

    # ---- messages（含 dedup/timer 幂等）----

    def insert_message(self, *, ts_ms, task_id, kind, from_sid, to_sid, body,
                       dedup_key=None, timer_id=None, replay_of=None):
        self._require_uow()
        ov = self._ov
        for column, key in (("dedup", dedup_key), ("timers", timer_id)):
            if key is None:
                continue
            index = ov[column] if column == "dedup" else ov["timers"]
            base = (self._snap["dedup"] if column == "dedup"
                    else self._snap["timers"])
            hit = index.get(key, base.get(key))
            if hit is not None:  # 幂等：回原消息零新建零新 tx
                return self._message_view(hit)["msg"]
        blob = body if isinstance(body, (bytes, bytearray)) else str(
            body).encode("utf-8")
        mid = self._alloc("message")
        data = {"ts_ms": ts_ms, "task_id": task_id, "kind": kind,
                "from_sid": from_sid, "to_sid": to_sid,
                "dedup_key": dedup_key, "timer_id": timer_id,
                "replay_of": replay_of}
        msg = Message(id=mid, ts_ms=ts_ms, task_id=task_id, kind=kind,
                      from_sid=from_sid, to_sid=to_sid, body=bytes(blob),
                      dedup_key=dedup_key, replay_of=replay_of)
        ov["messages"][mid] = {"msg": msg, "timer_id": timer_id}
        if dedup_key is not None:
            ov["dedup"][dedup_key] = mid
        if timer_id is not None:
            ov["timers"][timer_id] = mid
        ov["writes"].append(_Write("insert_message", "message", mid, 1,
                                   data, body=bytes(blob)))
        return msg

    def _message_view(self, mid):
        if self._in_uow() and mid in self._ov["messages"]:
            return self._ov["messages"][mid]
        return self._snap["messages"].get(mid)

    def _messages_view(self):
        merged = dict(self._snap["messages"])
        if self._in_uow():
            merged.update(self._ov["messages"])
        return merged

    def get_message(self, mid):
        rec = self._message_view(mid)
        return rec["msg"] if rec is not None else None

    def messages_for_sid(self, sid, after_id=0):
        merged = self._messages_view()
        return [merged[i]["msg"] for i in sorted(merged)
                if i > after_id and merged[i]["msg"].to_sid == sid]

    def messages_all(self):
        merged = self._messages_view()
        return [merged[i]["msg"] for i in sorted(merged)]

    # ---- receipts ----

    def upsert_receipt(self, r: DeliveryReceipt) -> DeliveryReceipt:
        self._require_uow()
        cur = self._receipt_view(r.message_id)
        fv = cur[0] if cur is not None else 0
        v = self._alloc_version("receipt", r.message_id, fv)
        self._ov["receipts"][r.message_id] = (v, r)
        self._ov["writes"].append(_Write(
            "upsert_receipt", "receipt", r.message_id, v,
            {"status": r.status, "attempts": r.attempts,
             "last_error": r.last_error, "email_status": r.email_status,
             "lease_until_ms": r.lease_until_ms, "updated_ms": r.updated_ms},
            from_version=fv))
        return r

    def _receipt_view(self, mid):
        if self._in_uow() and mid in self._ov["receipts"]:
            return self._ov["receipts"][mid]
        return self._snap["receipts"].get(mid)

    def get_receipt(self, mid):
        cur = self._receipt_view(mid)
        return cur[1] if cur is not None else None

    def receipts_by_status(self, status):
        merged = dict(self._snap["receipts"])
        if self._in_uow():
            merged.update(self._ov["receipts"])
        return [merged[k][1] for k in sorted(merged)
                if merged[k][1].status == status]

    # ---- mail cursors（值＝last-read message id）----

    def upsert_cursor(self, sid, last_read_ms) -> MailCursor:
        self._require_uow()
        sid = _safe(sid)
        cur = self._cursor_view(sid)
        fv = cur[0] if cur is not None else 0
        v = self._alloc_version("cursor", sid, fv)
        ent = MailCursor(sid=sid, last_read_ms=last_read_ms)
        self._ov["cursors"][sid] = (v, ent)
        self._ov["writes"].append(_Write(
            "upsert_cursor", "cursor", sid, v,
            {"last_read_ms": last_read_ms}, from_version=fv))
        return ent

    def _cursor_view(self, sid):
        if self._in_uow() and sid in self._ov["cursors"]:
            return self._ov["cursors"][sid]
        return self._snap["cursors"].get(sid)

    def get_cursor(self, sid):
        cur = self._cursor_view(sid)
        return cur[1] if cur is not None else None

    # ---- credentials（Req: F151——外部 secret＋tombstone 生命周期）----

    def _secret_path(self, cred) -> str:
        """AuthService 路径约定三形态（authsvc.issue 同款；root 牌钉死
        sid="root"）：root→.agora/root.token、worker→credentials/<task_id>、
        dispatcher→credentials/<sid>。返回 home 相对键位——canonical 持久
        面机器无关（整树搬迁/备份还原后重放照常水合），打开时再 join home。"""
        if cred.sid == "root":
            return os.path.join(".agora", "root.token")
        return os.path.join("credentials", f"{cred.task_id or cred.sid}.token")

    def _read_secret_token(self, path, sid) -> str:
        full = os.path.join(self._home, path)  # 相对键位join；键位形态重放已拒
        try:  # O_NOFOLLOW 同 authsvc._write_secret 口径——不跟符号链接
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            token = None
        else:
            try:
                with os.fdopen(fd, encoding="utf-8") as f:
                    data = json.load(f)
                token = data["token"] if isinstance(data, dict) else None
            except (OSError, ValueError, KeyError, TypeError):
                token = None
        if not isinstance(token, str):
            raise CredentialSecretError(
                f"{sid}: 外部 secret 文件缺席/不可解析（{full}）")
        return token

    def _secret_ok(self, path, digest) -> bool:
        try:
            token = self._read_secret_token(path, "?")
        except CredentialSecretError:
            return False
        return _sha256_hex(token) == digest

    def _hydrate(self, sid, reg) -> Credential:
        """canonical 注册面 → domain Credential（读外部 secret 文件验摘要）。"""
        token = self._read_secret_token(reg["secret_path"], sid)
        if _sha256_hex(token) != reg["token_sha256"]:
            raise CredentialSecretError(
                f"{sid}: 外部 secret 牌值与 canonical 摘要不符"
                f"（{reg['secret_path']}）")
        return Credential(sid=sid, token=token, task_id=reg["task_id"],
                          lineage=tuple(reg["lineage"]),
                          created_ms=reg["created_ms"])

    def _prior_secret(self, sid, digest):
        """换号继用（authsvc.rebind 不移 secret 文件）：同摘要活牌的既录
        secret_path——token 不变时文件留在旧键位。"""
        merged = dict(self._snap["cred_reg"])
        merged.update(self._ov["cred_reg"])
        for other, reg in merged.items():
            if (other != sid and reg is not None
                    and reg["token_sha256"] == digest
                    and self._secret_ok(reg["secret_path"], digest)):
                return reg["secret_path"]
        return None

    def put_credential(self, cred: Credential) -> Credential:
        """staging 前先验外部 secret 文件（缺/不符 fail-closed 且零提交）；
        canonical 只记摘要＋secret_path（明文牌不进 Git 对象）；不写不删
        外部文件（清理权归 AuthService/TaskService sweep）。"""
        self._require_uow()
        if not isinstance(cred.token, str) or not cred.token:
            raise StorageError("credential token 缺席（fail-closed）")
        sid = _safe(cred.sid)
        if cred.task_id is not None:
            _safe(cred.task_id)  # secret 键位路径穿越纵深（同 authsvc/tasksvc）
        digest = _sha256_hex(cred.token)
        path = self._secret_path(cred)
        if not self._secret_ok(path, digest):
            prior = self._prior_secret(sid, digest)
            if prior is not None:
                path = prior
            else:
                # Req: F152——派发签牌 per-dispatch 唯一键位（tasksvc 先落盘
                # 后记账；本尊键位留作提交后 BOOT 指针回填，不入 canonical）
                cand = (None if cred.task_id is None else os.path.join(
                    "credentials",
                    dispatch_secret_key(cred.task_id, cred.token)))
                if cand is None or not self._secret_ok(cand, digest):
                    raise CredentialSecretError(
                        f"{sid}: 外部 secret 文件缺席/牌值不符"
                        f"（{os.path.join(self._home, path)}）"
                        "——签发铁则＝文件先落盘后记账")
                path = cand
        cur = self._cred_view(sid)
        fv = cur[0] if cur is not None else 0
        v = self._alloc_version("credential", sid, fv)
        reg = {"token_sha256": digest, "secret_path": path,
               "task_id": cred.task_id, "lineage": list(cred.lineage),
               "created_ms": cred.created_ms}
        self._ov["credentials"][sid] = (v, cred)
        self._ov["cred_reg"][sid] = reg
        self._ov["writes"].append(_Write(
            "put_credential", "credential", sid, v, reg, from_version=fv))
        return cred

    def delete_credential(self, sid) -> bool:
        """删除＝下一不可变版本 tombstone（显式 deleted 标记；历史版本仍
        在对象树/tx 可审计，不作物理抹除）；live 才 True、重删 False；
        外部 secret 文件不在此清理。"""
        self._require_uow()
        sid = _safe(sid)
        cur = self._cred_view(sid)
        if cur is None or cur[1] is None:
            return False
        v = self._alloc_version("credential", sid, cur[0])
        self._ov["credentials"][sid] = (v, None)
        self._ov["cred_reg"][sid] = None
        self._ov["writes"].append(_Write(
            "delete_credential", "credential", sid, v,
            {"deleted": True}, from_version=cur[0]))
        return True

    def _cred_view(self, sid):
        if self._in_uow() and sid in self._ov["credentials"]:
            return self._ov["credentials"][sid]
        return self._snap["credentials"].get(sid)

    def get_credential(self, sid):
        cur = self._cred_view(sid)
        return cur[1] if cur is not None else None

    def credentials_all(self) -> list[Credential]:
        """全量活牌投影（auth 服务内存索引装载/重载面）；tombstone 缺席。"""
        merged = dict(self._snap["credentials"])
        if self._in_uow():
            merged.update(self._ov["credentials"])
        return [merged[k][1] for k in sorted(merged)
                if merged[k][1] is not None]

    # ---- timers（Req: F150——同 SQLite Storage 语义面）----

    def upsert_timer(self, tm: Timer) -> Timer:
        """UPSERT＝下一版本写（取消是终态非删除）；迁移合法性归服务层
        timer_transition（storage 同口径不复查）。"""
        self._require_uow()
        tid = _safe(tm.id)
        cur = self._timer_view(tid)
        fv = cur[0] if cur is not None else 0
        v = self._alloc_version("timer", tid, fv)
        self._ov["timer_ents"][tid] = (v, tm)
        self._ov["writes"].append(_Write(
            "upsert_timer", "timer", tid, v, _timer_data(tm),
            from_version=fv))
        return tm

    def _timer_view(self, tid):
        if self._in_uow() and tid in self._ov["timer_ents"]:
            return self._ov["timer_ents"][tid]
        return self._snap["timer_ents"].get(tid)

    def get_timer(self, tid):
        cur = self._timer_view(tid)
        return cur[1] if cur is not None else None

    def _timers_view(self):
        merged = dict(self._snap["timer_ents"])
        if self._in_uow():
            merged.update(self._ov["timer_ents"])
        return merged

    def timers_all(self):
        merged = self._timers_view()
        return [merged[k][1] for k in sorted(merged)]

    def timers_by_status(self, status):
        merged = self._timers_view()
        return [merged[k][1] for k in sorted(merged)
                if merged[k][1].state == status]  # str 枚举＝值等价

    def timers_for_task(self, task_id):
        merged = self._timers_view()
        return sorted((merged[k][1] for k in merged
                       if merged[k][1].task_id == task_id),
                      key=lambda t: (t.created_ms or 0, t.id))

    def timer_for_key(self, task_id, client_key):
        """(task_id, client_key) 唯一索引查（至多一行；同 SQL 语义＝NULL
        不等——None 键不命中）。"""
        if client_key is None:
            return None
        merged = self._timers_view()
        for k in sorted(merged):
            tm = merged[k][1]
            if tm.task_id == task_id and tm.client_key == client_key:
                return tm
        return None

    def timers_due(self, now_ms, limit=32, after_due=None, after_id=None):
        """到期待发批：pending 且 due<=now，(due,id) 升序每批 ≤limit；
        after 键集游标翻页（越过的行不重出——防门控行队头阻塞）。"""
        merged = self._timers_view()
        rows = [v[1] for v in merged.values()
                if v[1].state == TimerState.PENDING
                and v[1].due_ms <= now_ms]
        rows.sort(key=lambda t: (t.due_ms, t.id))
        if after_due is not None:
            if after_id is None:  # 同 SQL NULL 语义：仅 due 严格大于
                rows = [t for t in rows if t.due_ms > after_due]
            else:
                rows = [t for t in rows
                        if (t.due_ms, t.id) > (after_due, after_id)]
        return rows[:limit]

    def next_due_ms(self):
        """最早 pending due（提前醒计算）；无 pending→None。"""
        merged = self._timers_view()
        return min((v[1].due_ms for v in merged.values()
                    if v[1].state == TimerState.PENDING), default=None)

    def message_for_timer(self, timer_id):
        """按 timer_id 对账：消息 timer 索引（重放后与 UoW overlay 内均
        可见——崩窗补记 fired 的判据面）。"""
        mid = (self._ov["timers"].get(timer_id)
               if self._in_uow() else None)
        if mid is None:
            mid = self._snap["timers"].get(timer_id)
        if mid is None:
            return None
        rec = self._message_view(mid)
        return rec["msg"] if rec is not None else None

    # ---- events（seq 全局单流派号）----

    def append_event(self, ev: Event) -> Event:
        self._require_uow()
        seq = ev.seq or self._alloc("event")
        if self._event_view(seq) is not None:
            raise StorageError(f"event seq={seq} 已在案（单调不复用）")
        self._hwm["event"] = max(self._hwm.get("event", 0), seq)
        ent = dataclasses.replace(ev, seq=seq)
        self._ov["events"][seq] = ent
        self._ov["writes"].append(_Write(
            "append_event", "event", seq, seq,
            {"ts_ms": ev.ts_ms, "actor": ev.actor, "event": ev.event,
             "task": ev.task, "detail": ev.detail}))
        return ent

    def _event_view(self, seq):
        if self._in_uow() and seq in self._ov["events"]:
            return self._ov["events"][seq]
        return self._snap["events"].get(seq)

    def events_all(self):
        merged = dict(self._snap["events"])
        if self._in_uow():
            merged.update(self._ov["events"])
        return [merged[k] for k in sorted(merged)]

    def events_for_task(self, tid):
        """按任务事件流（全局 seq 升序）；task 归一化同 SQLite：_null("")
        →NULL 且 SQL NULL 不等任何参数——空/None tid 恒空集，空 task
        事件不可按任务命中。"""
        merged = dict(self._snap["events"])
        if self._in_uow():
            merged.update(self._ov["events"])
        if not tid:
            return []
        return [merged[k] for k in sorted(merged)
                if merged[k].task == tid]

    # ---- artifacts（Req: F153——不可变追加族，同 SQLite 语义面）----

    def insert_artifact(self, a: Artifact):
        """不可变追加（永不物理删除/更新）；服务端单调派号——artifact.path
        不入 canonical 路径（不编码不可信路径片段）；SQLite 同形无返回值；
        合法性（task 存在性等 FK 面）归服务层，storage 同口径不复查。"""
        self._require_uow()
        aid = self._alloc("artifact")
        self._ov["artifacts"][aid] = a
        self._ov["writes"].append(_Write(
            "insert_artifact", "artifact", aid, 1, dataclasses.asdict(a)))
        return None

    def artifacts_for_task(self, tid):
        """newest-first（id 降序，SQLite ORDER BY id DESC 同形）。"""
        merged = dict(self._snap["artifacts"])
        if self._in_uow():
            merged.update(self._ov["artifacts"])
        return [merged[k] for k in sorted(merged, reverse=True)
                if merged[k].task_id == tid]

    # ---- 存储生命周期兼容面（Req: F155——V3Stack.close 同形收口）----

    def close(self):
        """无持久句柄——幂等 no-op（每笔 UoW 落账即收口，无连接可关）。
        已知与 SQLite 的有意差异（评审环 B4）：close 后迟到写者仍可成功
        提交（无 closed 拒写面）——数据不丢，跨实例交叉由 CAS 防损。"""
        return None

    def checkpoint_truncate(self):
        """SQLite checkpoint 兼容面：durability 由每笔提交 fsync 保证——
        验证性 no-op，零 SQLite 件；账本有历史（重放基线 head 在案）而
        refs/agora/journal 解析失败＝失联失察，fail-closed 不静默放行。"""
        if self._snap["head"] is None:
            return None
        rc, _, _ = self._git(("rev-parse", "--verify", JOURNAL_REF),
                             check=False)
        if rc != 0:
            raise StorageError(
                f"{self._journal}: 账本有历史而 {JOURNAL_REF} 未解析"
                "——checkpoint 拒绝静默放行（fail-closed）")
        return None
