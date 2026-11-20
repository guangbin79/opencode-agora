"""Req: F133 — v3 SQLite 存储 core：schema／UoW／仓库（设计 §4/§5）。

stdlib sqlite3 记账权威：14 表基线（表名自设计 §4 枚举名单——任务页断言
计数「15」系笔误，两处枚举一致，以名单为准）；WAL＋synchronous=FULL＋
foreign_keys=ON＋busy_timeout（缺省 5000）；user_version=3；单一写连接＋
BEGIN IMMEDIATE（不可嵌套）；读者独立连接（WAL 未提交快照不可见）。

UoW 硬规（§4）：写事务内禁网络/子进程/文件遍历（守卫拦截）；写操作必须
身处 UoW；FaultInjector failpoint「uow」before＝BEGIN 后写前／after＝
写完 COMMIT 前。SQL/sqlite3 只许出现在本模块（test_architecture.py 守卫）。

行↔实体列名映射（任务页/设计页两处文本名差，仓库层消化）：
timers.status↔Timer.state、approvals.session_id·status↔
Approval.requester_sid·state。消息 body 恒 BLOB 出入（str 入＝UTF-8 落库，
非 UTF-8 字节逐字节往返；域内 str 解码归 T7 服务层）。
"""
import contextlib
import dataclasses
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time

from agorad.domain import (Approval, ApprovalState, Artifact, BaseProbe,
                           Credential, DeliveryReceipt, DeliveryState, Event,
                           GateState, MailCursor, Message, Task, Timer,
                           TimerState, VerifySpec, epoch_ms)

SCHEMA_VERSION = 4  # v4＝task_delivery.conflict_files_json（Req: F137）；v3＝14 表基线
BUSY_TIMEOUT_MS = 5000

# 设计 §4 表清单（枚举权威，14 名；任务页计数「15」系笔误）
TABLES = (
    "schema_migrations", "tasks", "task_verify", "task_delivery", "task_gate",
    "timers", "approvals", "messages", "delivery_receipts", "mail_cursors",
    "events", "artifacts", "credentials", "credential_lineage")

# 热路径索引见 §4；EXPLAIN QUERY PLAN 守护在 test_v3_storage.py（断言8）。
DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'todo',
  title TEXT NOT NULL DEFAULT '', goal TEXT NOT NULL DEFAULT '',
  req_id TEXT, model TEXT, quick INTEGER NOT NULL DEFAULT 0,
  deliverable TEXT, chain_depth INTEGER NOT NULL DEFAULT 1,
  root_sid TEXT, dispatcher_sid TEXT, worker_sid TEXT,
  created_ms INTEGER, updated_ms INTEGER,
  fail_streak INTEGER NOT NULL DEFAULT 0,
  cancel_reason TEXT, waiting_on TEXT, client_key TEXT, last_seen_ms INTEGER,
  project_dir TEXT, target_branch TEXT, env_dir TEXT, env_base TEXT,
  commit_sha TEXT, rebased_from TEXT,
  verify_pending INTEGER NOT NULL DEFAULT 0,
  verify_base_exempt INTEGER NOT NULL DEFAULT 0, review_cmd TEXT,
  after_json TEXT NOT NULL DEFAULT '[]',
  legacy_extra_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(root_sid, client_key));
CREATE TABLE IF NOT EXISTS task_verify(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  cmd TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
  registered_by TEXT, registered_at_ms INTEGER, timeout_s INTEGER,
  timeout_source TEXT NOT NULL DEFAULT 'default');
CREATE TABLE IF NOT EXISTS task_delivery(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  mode TEXT NOT NULL DEFAULT 'managed', status TEXT NOT NULL DEFAULT 'pending',
  commit_sha TEXT, base TEXT, target_branch TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  local_status TEXT NOT NULL DEFAULT 'pending', error TEXT,
  conflict_files_json TEXT);
CREATE TABLE IF NOT EXISTS task_gate(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  in_gate INTEGER NOT NULL DEFAULT 0, gate_env TEXT,
  pending_cancel INTEGER NOT NULL DEFAULT 0, pending_cancel_reason TEXT,
  gate_artifact_json TEXT, review_cache_json TEXT,
  review_fail_streak INTEGER NOT NULL DEFAULT 0, probe_json TEXT);
CREATE TABLE IF NOT EXISTS timers(
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  target_role TEXT NOT NULL DEFAULT 'worker',
  due_ms INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
  created_ms INTEGER, fired_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_timers_status_due ON timers(status, due_ms);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  session_id TEXT, backend TEXT NOT NULL DEFAULT 'protocol',
  status TEXT NOT NULL DEFAULT 'pending', question TEXT NOT NULL DEFAULT '',
  metadata_json TEXT, native_request_id TEXT UNIQUE,
  attempted_decision TEXT, deadline_ms INTEGER, orphan_reason TEXT,
  created_ms INTEGER, resolved_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_approvals_session_status
  ON approvals(session_id, status);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, from_sid TEXT NOT NULL, to_sid TEXT NOT NULL,
  body BLOB NOT NULL, dedup_key TEXT UNIQUE,
  timer_id TEXT UNIQUE REFERENCES timers(id), replay_of INTEGER);
CREATE INDEX IF NOT EXISTS idx_messages_to_id ON messages(to_sid, id);
CREATE TABLE IF NOT EXISTS delivery_receipts(
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, email_status TEXT, lease_until_ms INTEGER, updated_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_receipts_status
  ON delivery_receipts(status, message_id);
CREATE TABLE IF NOT EXISTS mail_cursors(
  sid TEXT PRIMARY KEY, last_read_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, actor TEXT NOT NULL,
  event TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, detail_json TEXT);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, seq);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  phase TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', cmd TEXT NOT NULL DEFAULT '',
  rc INTEGER, failure_kind TEXT, duration_s REAL,
  bytes INTEGER NOT NULL DEFAULT 0,
  error TEXT, started_at_ms INTEGER, ended_at_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);
CREATE TABLE IF NOT EXISTS credentials(
  sid TEXT PRIMARY KEY, token TEXT NOT NULL, task_id TEXT, created_ms INTEGER);
CREATE TABLE IF NOT EXISTS credential_lineage(
  sid TEXT NOT NULL REFERENCES credentials(sid) ON DELETE CASCADE,
  position INTEGER NOT NULL, ancestor_sid TEXT NOT NULL,
  PRIMARY KEY(sid, position));
"""

# UoW 守卫面（§4：禁网络/子进程/文件遍历）——模块级共享包装（首装存真
# 原件、引用计数归零恢复；跨实例重叠不叠层防闭包泄漏——终闸 R2 F1），
# 只拦正身处任一 uow 的线程；uow 前先 from socket import socket 不在拦截面。
_BLOCKED = (
    (socket, ("socket", "create_connection")),
    (subprocess, ("Popen", "run", "call", "check_output", "check_call")),
    (os, ("system", "popen", "walk")))


_GUARD_LOCK = threading.Lock()
_GUARD_REFS = 0  # 活跃 uow 计数（跨 Storage 实例共享）
_GUARD_OWNERS = {}  # 线程 ident→持锁计数：正身处写事务的线程集合
_GUARD_SAVED = []  # [(mod, name, original)]——首装保存的真原件


def _make_guard(original, name):
    def _violation(*args, **kwargs):
        with _GUARD_LOCK:
            guarded = threading.get_ident() in _GUARD_OWNERS
        if not guarded:
            return original(*args, **kwargs)  # 事务外线程放行原实现
        raise UnitOfWorkViolation(
            f"UoW 写事务内禁 {name}（设计 §4：无网络/子进程/文件遍历）")
    return _violation


class StorageError(Exception):
    """存储层基错。"""


class SchemaVersionError(StorageError):
    """库 user_version 高于已知 schema——启动即拒，不静默降级。"""


class NoActiveUnitOfWork(StorageError):
    """写操作必须身处 with st.uow()（read-decide-write 同事务，设计 §4）。"""


class UnitOfWorkViolation(StorageError):
    """写事务内危险调用（网络/子进程/文件遍历）被守卫拦截。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _null(text):  # 域缺省 ""→NULL（可空 FK/TEXT 列）
    return None if not text else text


_TASK_SCALARS = (  # 域标量列（DDL 顺序）；after/legacy_extra 走 JSON 列另算
    "id", "state", "title", "goal", "req_id", "model", "quick", "deliverable",
    "chain_depth", "root_sid", "dispatcher_sid", "worker_sid", "created_ms",
    "updated_ms", "fail_streak", "cancel_reason", "waiting_on", "client_key",
    "last_seen_ms", "project_dir", "target_branch", "env_dir", "env_base",
    "commit_sha", "rebased_from", "verify_pending", "verify_base_exempt",
    "review_cmd")
_TASK_COLS = ", ".join([*_TASK_SCALARS, "after_json", "legacy_extra_json"])
_TASK_UPSERT = ", ".join(
    f"{n}=excluded.{n}" for n in
    [*_TASK_SCALARS, "after_json", "legacy_extra_json"])


def _connect(path, busy_timeout_ms):
    # check_same_thread=False：写连接纪律由单一全局写锁保证（§4），线程边界
    # 归服务层（T7+ 装配）；读者连接各自独立。
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000,
                           isolation_level=None, check_same_thread=False)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


class Storage:
    """单一写连接＋独立读者连接。db 文件与 -wal/-shm 统一 0600。"""

    def __init__(self, db_path, *, busy_timeout_ms=BUSY_TIMEOUT_MS,
                 injector=None):
        self.path = os.fspath(db_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._injector = injector  # tests.v3_harness.FaultInjector（failpoint uow）
        self._depth = 0
        self._owner_ident = None
        conn = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000,
                               isolation_level=None, check_same_thread=False)
        try:  # 任一步失败先关连接再抛（防 fd 泄漏——终闸 R2 F3）
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"{self.path}: user_version={version} 高于已知 "
                    f"{SCHEMA_VERSION}，拒开不静默（设计 §4）")
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            if version < SCHEMA_VERSION:  # 仅初始化/补齐才拿写锁（重开不写）
                conn.executescript(DDL)
                # v3→v4（Req: F137）：CREATE IF NOT EXISTS 不改既有表——旧 v3
                # 库补列（评审环 R1 B1：无此迁移则旧库首读写即 no such
                # column）。显式 IMMEDIATE 事务串行化并发迁移者（评审环 R2：
                # 自动提交下 check-then-ALTER 竞窗——后来者 duplicate column
                # 外抛开库失败；醒后重读已见列即跳过）
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cols = {r[1] for r in conn.execute(
                        "PRAGMA table_info(task_delivery)")}
                    if "conflict_files_json" not in cols:
                        conn.execute("ALTER TABLE task_delivery "
                                     "ADD COLUMN conflict_files_json TEXT")
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations "
                        "VALUES(?,?,?)",
                        (SCHEMA_VERSION, "v4-conflict-files",
                         epoch_ms(time.time())))
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(self.path + suffix, 0o600)

    # ---- UoW（设计 §4：单一全局写锁＋BEGIN IMMEDIATE）----

    @contextlib.contextmanager
    def uow(self):
        if self._depth:
            raise StorageError("UoW 不可嵌套（单一全局写锁，设计 §4）")
        # 先 BEGIN 再装守卫：BEGIN 失败（锁冲突）不得留下已替换的模块入口
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        self._owner_ident = threading.get_ident()
        try:
            self._install_guard()
            self._fire("uow", "before")
            yield self
            self._fire("uow", "after")  # 写完、COMMIT 前——崩溃窗（断言2）
            self._conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0
            self._owner_ident = None
            self._restore_guard()

    def _install_guard(self):
        global _GUARD_REFS
        with _GUARD_LOCK:
            me = threading.get_ident()
            _GUARD_OWNERS[me] = _GUARD_OWNERS.get(me, 0) + 1
            _GUARD_REFS += 1
            if _GUARD_REFS == 1:  # 首装存真原件；跨实例共享包装不叠层
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

    def _fire(self, name, phase):
        if self._injector is not None:
            self._injector.fire(name, phase)

    def _require_uow(self):
        if not self._depth:
            raise NoActiveUnitOfWork("写操作必须身处 with st.uow()（设计 §4）")

    def open_reader(self):
        """读者独立连接：WAL 下未提交写不可见，commit 后立见（断言6）。"""
        return _connect(self.path, self._busy_timeout_ms)

    def _read_conn(self):
        """读路由（终闸 R2 F2/N1）：持锁线程读自身事务视图（read-decide-
        write 同事务）；其余现开短命读者——WAL 提交快照，读完即关
        （_read_done），无任何跨线程滞留。"""
        if (self._depth and self._owner_ident is not None
                and threading.get_ident() == self._owner_ident):
            return self._conn
        return self.open_reader()

    def _read_done(self, conn):
        if conn is not self._conn:  # 短命读者即关；写连接不在此关
            conn.close()

    def close(self):
        self._conn.close()

    # ---- tasks＋1:1 子表（断言4：frozen 实体全字段往返）----

    def upsert_task(self, t: Task):
        """主行 UPSERT 保级联子表（messages/events/timers/…皆 ON DELETE
        CASCADE，删主行即全灭）；三张 1:1 子表删重插（其下无子行），
        delivery None 撤行。"""
        self._require_uow()
        c = self._conn
        scalars = ["commit" if n == "commit_sha" else n for n in _TASK_SCALARS]
        c.execute(
            f"INSERT INTO tasks({_TASK_COLS}) "
            f"VALUES({','.join(['?'] * (len(_TASK_SCALARS) + 2))}) "
            "ON CONFLICT(id) DO UPDATE SET " + _TASK_UPSERT,
            (*[getattr(t, n) for n in scalars],
             _json(list(t.after)), t.legacy_extra_json))
        for sub in ("task_verify", "task_delivery", "task_gate"):
            c.execute(f"DELETE FROM {sub} WHERE task_id=?", (t.id,))
        c.execute(
            "INSERT INTO task_verify(task_id,cmd,state,registered_by,"
            "registered_at_ms,timeout_s,timeout_source) VALUES(?,?,?,?,?,?,?)",
            (t.id, t.verify.cmd, t.verify.state, t.verify.registered_by,
             t.verify.registered_at_ms, t.verify.timeout_s,
             t.verify.timeout_source))
        if t.delivery is not None:
            c.execute(
                "INSERT INTO task_delivery(task_id,mode,status,commit_sha,base,"
                "target_branch,attempt,local_status,error,conflict_files_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.delivery.mode, t.delivery.status, t.delivery.commit,
                 t.delivery.base, t.delivery.target_branch, t.delivery.attempt,
                 t.delivery.local_status, t.delivery.error,
                 _json(t.delivery.conflict_files)
                 if t.delivery.conflict_files else None))
        art = None if t.gate.gate_artifact is None else {
            k: v for k, v in dataclasses.asdict(t.gate.gate_artifact).items()
            if k != "task_id"}
        c.execute(
            "INSERT INTO task_gate(task_id,in_gate,gate_env,pending_cancel,"
            "pending_cancel_reason,gate_artifact_json,review_cache_json,"
            "review_fail_streak,probe_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (t.id, t.gate.in_gate, t.gate.gate_env, t.gate.pending_cancel,
             t.gate.pending_cancel_reason, _json(art) if art else None,
             _json(t.gate.review_cache) if t.gate.review_cache is not None else None,
             t.gate.review_fail_streak,
             _json(dataclasses.asdict(t.probe)) if t.probe else None))
        return t

    def tasks_all(self):
        """全任务投影（Req: F135——task 服务父任务/级联/幂等/深度只读查询面；
        单读者连接单快照，免逐 id 建连）。"""
        read = self._read_conn()
        try:
            ids = [r[0] for r in read.execute("SELECT id FROM tasks").fetchall()]
            return [t for i in ids if (t := self._get_task_on(read, i))
                    is not None]
        finally:
            self._read_done(read)

    def get_task(self, tid):
        read = self._read_conn()
        try:
            return self._get_task_on(read, tid)
        finally:
            self._read_done(read)

    def _get_task_on(self, read, tid):
        """单连接读体（get_task/tasks_all 共用）。"""
        row = read.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return None

        def one(sql):
            return read.execute(sql, (tid,)).fetchone()

        verify = one("SELECT cmd,state,registered_by,registered_at_ms,"
                     "timeout_s,timeout_source FROM task_verify WHERE task_id=?")
        delivery = one("SELECT mode,status,commit_sha,base,target_branch,"
                       "attempt,local_status,error,conflict_files_json "
                       "FROM task_delivery WHERE task_id=?")
        g = one("SELECT in_gate,gate_env,pending_cancel,"
                "pending_cancel_reason,gate_artifact_json,review_cache_json,"
                "review_fail_streak,probe_json FROM task_gate WHERE task_id=?")
        g = g or (None,) * 8
        scalars = dict(zip(
            ("id", "state", "title", "goal", "req_id", "model", "quick",
             "deliverable", "chain_depth", "root_sid", "dispatcher_sid",
             "worker_sid", "created_ms", "updated_ms", "fail_streak",
             "cancel_reason", "waiting_on", "client_key", "last_seen_ms",
             "project_dir", "target_branch", "env_dir", "env_base", "commit",
             "rebased_from", "verify_pending", "verify_base_exempt",
             "review_cmd"), row[:28]))
        for k in ("quick", "verify_pending", "verify_base_exempt"):
            scalars[k] = bool(scalars[k])  # INTEGER→bool（域类型保真；同 in_gate 口径）
        return Task(
            **scalars,
            after=tuple(json.loads(row[28])),
            legacy_extra_json=row[29],
            verify=VerifySpec(*verify) if verify else VerifySpec(),
            delivery=(DeliveryState(*delivery[:8],
                                    conflict_files=json.loads(delivery[8])
                                    if delivery[8] else None)
                      if delivery else None),
            probe=BaseProbe(**json.loads(g[7])) if g[7] else None,
            gate=GateState(
                in_gate=bool(g[0]), gate_env=g[1], pending_cancel=bool(g[2]),
                pending_cancel_reason=g[3],
                gate_artifact=(Artifact(task_id=tid, **json.loads(g[4]))
                               if g[4] else None),
                review_cache=json.loads(g[5]) if g[5] else None,
                review_fail_streak=g[6]))

    # ---- timers / approvals（断言4/5）----

    def upsert_timer(self, tm: Timer) -> Timer:
        self._require_uow()
        # UPSERT 不删行：messages.timer_id 引用本行且默认 NO ACTION——
        # fire 后补 fired／§6 按 timer_id 对账路径必需（评审环 R1 修2）
        self._conn.execute(
            "INSERT INTO timers(id,task_id,target_role,due_ms,status,"
            "created_ms,fired_ms) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET target_role=excluded.target_role,"
            "due_ms=excluded.due_ms,status=excluded.status,"
            "created_ms=excluded.created_ms,fired_ms=excluded.fired_ms",
            (tm.id, tm.task_id, tm.target_role, tm.due_ms, tm.state.value,
             tm.created_ms, tm.fired_ms))
        return tm  # 孤儿 task（无主行）→sqlite3.IntegrityError（FK 拒）

    @staticmethod
    def _timer(row):
        return Timer(id=row[0], task_id=row[1], target_role=row[2],
                     due_ms=row[3], state=TimerState(row[4]),
                     created_ms=row[5], fired_ms=row[6])

    def get_timer(self, tid):
        read = self._read_conn()
        try:
            row = read.execute(
                "SELECT id,task_id,target_role,due_ms,status,created_ms,fired_ms "
                "FROM timers WHERE id=?", (tid,)).fetchone()
        finally:
            self._read_done(read)
        return self._timer(row) if row is not None else None

    def upsert_approval(self, ap: Approval) -> Approval:
        self._require_uow()
        self._conn.execute("DELETE FROM approvals WHERE id=?", (ap.id,))
        self._conn.execute(
            "INSERT INTO approvals(id,task_id,session_id,backend,status,"
            "question,metadata_json,native_request_id,attempted_decision,"
            "deadline_ms,orphan_reason,created_ms,resolved_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ap.id, _null(ap.task_id), ap.requester_sid, ap.backend,
             ap.state.value, ap.question,
             _json(ap.metadata) if ap.metadata is not None else None,
             ap.native_request_id, ap.attempted_decision, ap.deadline_ms,
             ap.orphan_reason, ap.created_ms, ap.resolved_ms))
        return ap

    @staticmethod
    def _approval(row):
        return Approval(
            id=row[0], task_id=row[1] or "", requester_sid=row[2],
            backend=row[3], state=ApprovalState(row[4]), question=row[5],
            metadata=json.loads(row[6]) if row[6] else None,
            native_request_id=row[7], attempted_decision=row[8],
            deadline_ms=row[9], orphan_reason=row[10], created_ms=row[11],
            resolved_ms=row[12])

    def get_approval(self, aid):
        read = self._read_conn()
        try:
            row = read.execute(
                "SELECT id,task_id,session_id,backend,status,question,metadata_json,"
                "native_request_id,attempted_decision,deadline_ms,orphan_reason,"
                "created_ms,resolved_ms FROM approvals WHERE id=?", (aid,)).fetchone()
        finally:
            self._read_done(read)
        return self._approval(row) if row is not None else None

    # ---- messages / receipts / cursors（断言2/5/6/10；outbox 服务归 T7）----

    _MSG_COLS = ("id,ts_ms,task_id,kind,from_sid,to_sid,body,dedup_key,"
                 "timer_id,replay_of")

    def insert_message(self, *, ts_ms, task_id, kind, from_sid, to_sid, body,
                       dedup_key=None, timer_id=None, replay_of=None):
        """幂等：dedup_key/timer_id 撞既有行→回读既有行不新建（断言5）。"""
        self._require_uow()
        blob = bytes(body) if isinstance(
            body, (bytes, bytearray, memoryview)) else str(body).encode()
        try:
            cur = self._conn.execute(
                "INSERT INTO messages(ts_ms,task_id,kind,from_sid,to_sid,body,"
                "dedup_key,timer_id,replay_of) VALUES(?,?,?,?,?,?,?,?,?)",
                (ts_ms, task_id, kind, from_sid, to_sid, sqlite3.Binary(blob),
                 dedup_key, timer_id, replay_of))
            return Message(id=cur.lastrowid, ts_ms=ts_ms,
                                  task_id=task_id, kind=kind, from_sid=from_sid,
                                  to_sid=to_sid, body=blob, dedup_key=dedup_key,
                                  replay_of=replay_of)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc) or dedup_key is timer_id is None:
                raise
            row = None
            for column, value in (("dedup_key", dedup_key),
                                  ("timer_id", timer_id)):
                if value is None:
                    continue
                row = self._conn.execute(
                    f"SELECT {self._MSG_COLS} FROM messages WHERE {column}=?",
                    (value,)).fetchone()
                if row is not None:
                    break
            if row is None:  # 撞的不是消息唯一键（理论不可达）——抛原错不吞
                raise
            return self._message(row)

    def get_message(self, mid):
        read = self._read_conn()
        try:
            row = read.execute(
                f"SELECT {self._MSG_COLS} FROM messages WHERE id=?",
                (mid,)).fetchone()
        finally:
            self._read_done(read)
        return self._message(row)

    def messages_for_sid(self, sid, after_id=0):
        read = self._read_conn()
        try:
            rows = read.execute(
                f"SELECT {self._MSG_COLS} FROM messages WHERE to_sid=? AND id>? "
                "ORDER BY id", (sid, after_id)).fetchall()
        finally:
            self._read_done(read)
        return [self._message(r) for r in rows]

    @staticmethod
    def _message(row):
        if row is None:
            return None
        return Message(id=row[0], ts_ms=row[1], task_id=row[2],
                              kind=row[3], from_sid=row[4], to_sid=row[5],
                              body=row[6], dedup_key=row[7], replay_of=row[9])

    def upsert_receipt(self, r: DeliveryReceipt):
        self._require_uow()
        self._conn.execute(
            "DELETE FROM delivery_receipts WHERE message_id=?", (r.message_id,))
        self._conn.execute(
            "INSERT INTO delivery_receipts(message_id,status,attempts,"
            "last_error,email_status,lease_until_ms,updated_ms) "
            "VALUES(?,?,?,?,?,?,?)",
            (r.message_id, r.status, r.attempts, r.last_error, r.email_status,
             r.lease_until_ms, r.updated_ms))
        return r

    def get_receipt(self, mid):
        read = self._read_conn()
        try:
            row = read.execute(
                "SELECT message_id,status,attempts,last_error,email_status,"
                "lease_until_ms,updated_ms FROM delivery_receipts "
                "WHERE message_id=?", (mid,)).fetchone()
        finally:
            self._read_done(read)
        return DeliveryReceipt(*row) if row else None

    def receipts_by_status(self, status):
        """按投递状态扫 receipt（Req: F134 outbox 恢复面——durable inflight
        扫描归仓库查询，SQL 不出层）。"""
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT message_id,status,attempts,last_error,email_status,"
                "lease_until_ms,updated_ms FROM delivery_receipts "
                "WHERE status=? ORDER BY message_id", (status,)).fetchall()
        finally:
            self._read_done(read)
        return [DeliveryReceipt(*r) for r in rows]

    def upsert_cursor(self, sid, last_read_ms) -> MailCursor:
        self._require_uow()
        self._conn.execute(
            "INSERT INTO mail_cursors(sid,last_read_ms) VALUES(?,?) "
            "ON CONFLICT(sid) DO UPDATE SET last_read_ms=excluded.last_read_ms",
            (sid, last_read_ms))
        return MailCursor(sid=sid, last_read_ms=last_read_ms)

    def get_cursor(self, sid):
        read = self._read_conn()
        try:
            row = read.execute(
                "SELECT sid,last_read_ms FROM mail_cursors WHERE sid=?",
                (sid,)).fetchone()
        finally:
            self._read_done(read)
        return MailCursor(*row) if row else None

    # ---- events / artifacts（断言4；events 全局 seq 单流派号，§8）----

    def append_event(self, ev: Event) -> Event:
        self._require_uow()
        seq = ev.seq or self._conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        self._conn.execute(
            "INSERT INTO events(seq,ts_ms,actor,event,task_id,detail_json) "
            "VALUES(?,?,?,?,?,?)",
            (seq, ev.ts_ms, ev.actor, ev.event, _null(ev.task),
             _json(ev.detail) if ev.detail is not None else None))
        return dataclasses.replace(ev, seq=seq)

    def events_for_task(self, tid):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT seq,ts_ms,actor,event,task_id,detail_json FROM events "
                "WHERE task_id=? ORDER BY seq", (tid,)).fetchall()
        finally:
            self._read_done(read)
        return [Event(seq=r[0], ts_ms=r[1], actor=r[2], event=r[3],
                             task=r[4] or "", detail=json.loads(r[5]) if r[5] else None)
                for r in rows]

    def insert_artifact(self, a: Artifact):
        self._require_uow()
        self._conn.execute(
            "INSERT INTO artifacts(task_id,phase,path,cmd,rc,failure_kind,"
            "duration_s,bytes,error,started_at_ms,ended_at_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (a.task_id, a.phase, a.path, a.cmd, a.rc, a.failure_kind,
             a.duration_s, a.bytes, a.error, a.started_at_ms, a.ended_at_ms))

    def artifacts_for_task(self, tid):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT task_id,phase,path,cmd,rc,failure_kind,duration_s,bytes,"
                "error,started_at_ms,ended_at_ms FROM artifacts WHERE task_id=? "
                "ORDER BY id DESC", (tid,)).fetchall()
        finally:
            self._read_done(read)
        return [Artifact(*r) for r in rows]

    # ---- credentials＋lineage（断言4；血统补拉语义归 T7/T10）----

    def put_credential(self, cred: Credential):
        self._require_uow()
        self._conn.execute("DELETE FROM credentials WHERE sid=?", (cred.sid,))
        self._conn.execute(
            "INSERT INTO credentials(sid,token,task_id,created_ms) "
            "VALUES(?,?,?,?)",
            (cred.sid, cred.token, _null(cred.task_id), cred.created_ms))
        for position, ancestor in enumerate(cred.lineage):
            self._conn.execute(
                "INSERT INTO credential_lineage(sid,position,ancestor_sid) "
                "VALUES(?,?,?)", (cred.sid, position, ancestor))
        return cred

    def get_credential(self, sid):
        read = self._read_conn()
        try:
            row = read.execute(
                "SELECT sid,token,task_id,created_ms FROM credentials WHERE sid=?",
                (sid,)).fetchone()
            if row is None:
                return None
            lineage = tuple(r[0] for r in read.execute(
                "SELECT ancestor_sid FROM credential_lineage WHERE sid=? "
                "ORDER BY position", (sid,)))
        finally:
            self._read_done(read)
        return Credential(sid=row[0], token=row[1], task_id=row[2] or "",
                                 lineage=lineage, created_ms=row[3])


    def credentials_all(self) -> list[Credential]:
        """全量凭据投影（Req: F138——auth 服务内存索引装载/重载面）。"""
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT sid,token,task_id,created_ms FROM credentials "
                "ORDER BY sid").fetchall()
            out = []
            for r in rows:
                lineage = tuple(x[0] for x in read.execute(
                    "SELECT ancestor_sid FROM credential_lineage WHERE sid=? "
                    "ORDER BY position", (r[0],)))
                out.append(Credential(sid=r[0], token=r[1], task_id=r[2] or "",
                                      lineage=lineage, created_ms=r[3]))
            return out
        finally:
            self._read_done(read)

    def delete_credential(self, sid) -> bool:
        """撤销＝删 credentials 行（Req: F135 cancel 侧；lineage 级联随 FK）。"""
        self._require_uow()
        cur = self._conn.execute("DELETE FROM credentials WHERE sid=?", (sid,))
        return cur.rowcount > 0

    # ---- cutover 面（Req: F140——切换/导出/备份 CLI；SQL 圈禁本模块不变）----

    def backup_to(self, dest, *, scrub_credentials=False):
        """SQLite backup API 一致性热备（Req: F140 断言3）。scrub＝备份件清
        credentials（lineage 随 FK 级联）＋VACUUM 压实——凭据可重签不入备份
        暗面（断言8）；journal 收敛 DELETE 模式后关闭，无 -wal/-shm 残留。"""
        dest = os.fspath(dest)
        conn = sqlite3.connect(dest, timeout=self._busy_timeout_ms / 1000,
                               isolation_level=None)
        try:
            self._conn.backup(conn)  # 源写连接快照：在飞写者下仍一致
            dropped = None
            if scrub_credentials:
                conn.execute("PRAGMA foreign_keys=ON")
                dropped = conn.execute(
                    "DELETE FROM credentials").rowcount
                conn.execute("VACUUM")  # 压实清 freelist——牌值字节不驻文件暗面
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode != "delete":
                raise StorageError(f"备份件 journal 收敛失败（{mode}）")
            return {"credentials_dropped": dropped}
        finally:
            conn.close()
            with contextlib.suppress(OSError):
                os.chmod(dest, 0o600)

    def checkpoint_truncate(self):
        """wal_checkpoint(TRUNCATE)（Req: F140 断言5：切换前收卷，主库归零
        -wal）。返回 (busy, log_pages, checkpointed_pages)。"""
        row = self._conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row)

    @staticmethod
    def verify_db(path):
        """机读体检（Req: F140 断言3/5）：integrity_check＋foreign_key_check
        ＋逐表计数，只读 URI 连接（体检零写入）。返回 dict。"""
        path = os.fspath(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            counts = {t: conn.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
            return {"integrity": integrity, "fk_violations": fk,
                    "counts": counts}
        finally:
            conn.close()

    def events_all(self):
        """全量事件按 seq（Req: F140 §8 派生导出面）。"""
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT seq,ts_ms,actor,event,task_id,detail_json FROM events "
                "ORDER BY seq").fetchall()
        finally:
            self._read_done(read)
        return [Event(seq=r[0], ts_ms=r[1], actor=r[2], event=r[3],
                      task=r[4] or "", detail=json.loads(r[5]) if r[5] else None)
                for r in rows]

    def messages_all(self):
        """全量消息按 id（Req: F140 §8 派生导出面）。"""
        read = self._read_conn()
        try:
            rows = read.execute(
                f"SELECT {self._MSG_COLS} FROM messages ORDER BY id").fetchall()
        finally:
            self._read_done(read)
        return [self._message(r) for r in rows]

    def timers_all(self):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT id,task_id,target_role,due_ms,status,created_ms,fired_ms "
                "FROM timers ORDER BY id").fetchall()
        finally:
            self._read_done(read)
        return [self._timer(r) for r in rows]

    def approvals_all(self):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT id,task_id,session_id,backend,status,question,metadata_json,"
                "native_request_id,attempted_decision,deadline_ms,orphan_reason,"
                "created_ms,resolved_ms FROM approvals ORDER BY id").fetchall()
        finally:
            self._read_done(read)
        return [self._approval(r) for r in rows]

    def cursors_all(self):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT sid,last_read_ms FROM mail_cursors ORDER BY sid").fetchall()
        finally:
            self._read_done(read)
        return [MailCursor(*r) for r in rows]

    def receipts_all(self):
        read = self._read_conn()
        try:
            rows = read.execute(
                "SELECT message_id,status,attempts,last_error,email_status,"
                "lease_until_ms,updated_ms FROM delivery_receipts "
                "ORDER BY message_id").fetchall()
        finally:
            self._read_done(read)
        return [DeliveryReceipt(*r) for r in rows]
