"""Req: F139 — v3 legacy 迁移器（opencode-agora v3 P4-T11，设计 §9）。

离线一次性迁移：v2 账本 home → v3 SQLite home。源目录全程只读（测试以
目录指纹锚定）；SQL 仅经 agorad.storage；无网络/子进程/源侧写入。

中止语义（§9）：无有效快照/client_key 冲突/v2 可加载记录解析失败→
MigrationError 且源零修改；v2 本就静默跳过的撕裂件（撕裂消息/坏事件行）
→legacy_quarantine＋manifest 原文保全（UTF-8 文本面原样；非 UTF-8 字节
走 content_base64 无损），不阻塞。快照口径＝store.load_state
同款：末完整行胜出（断尾行丢弃，报告留痕）；mail_read 一并读取。

字段映射三源权威＝task.create 行构造（_CREATE_KEYS）∪ gate/health 写点
（_GATE_HEALTH_KEYS）∪ _normalize_snapshot 补默认：known 字段落 typed 列；
瞬态清单（gate_resource——_normalize/_persist 同款剔除）丢弃；已知无 v3
投影字段（usage/gate_origin_branch——F138 语义等价改 derived）与未知合法
字段→legacy_extra_json 逐字段保真（域逻辑不读，只读审计位）。

游标转换（§9）：mail_read 时间戳游标→v3 last-read message id——有
pending（ts>游标）取 min(pending id)-1（重投优先，at-least-once 承重）；
无 pending 取最大 id；无记录 sid 不建行（v3 pull 缺省 0＝血统全量补拉）。
已读但落入重投窗的消息计 possible_duplicate_delivery（报告精确计数）。

消息：frontmatter 无 v3 列的键（peer 的 repo 等）不入行——报告
frontmatter_extra 计数如实申报（消息面 T13 装配再议）。body 逐字节 BLOB
保全；插入按补零文件名序，新 id 序与文件名序一致（撕裂件跳过产生重编号，
replay_of 随 new_by_old 映射同步改写，映射缺席保原值计数）。

凭据：tokens map→credentials 表（报告只含 sha256 摘要，明文不入报告/
导出；血统 FIFO≤16；root 多牌取排序首者、root.token 文件兜底）；secret
文件随迁保 0600（孤儿文件照迁——清扫归 AuthService.sweep，迁移不代跑）；
root.token 保留。

目标重建幂等：migrate() 起点重置目标库（agora.db/-wal/-shm＋quarantine
＋报告＋完成标记），源只读故重跑结果确定；.home-complete 完成标记最后
落盘（FaultInjector failpoint「batch_<名>」before/after 两相位——中途炸＝
目标不完整且不可标记完成）。
"""
import base64
import collections
import contextlib
import hashlib
import json
import math
import os
import pathlib
import shutil
import stat

from agorad.domain import (TASK_LEGAL, Artifact, BaseProbe, Credential,
                           DeliveryState, Event, GateState, Task, VerifySpec,
                           epoch_ms)
from agorad.storage import Storage

MARKER = ".home-complete"  # 完成标记：全部批次与报告落盘后最后写（harness 同名口径）
QUARANTINE_DIR = "legacy_quarantine"
REPORT_PATH = "state/migration-report.json"
CONFIG_FILES = ("agorad.env", "verify-profiles.json", "email.json",
                "inactive-repos", "ignore.roster")  # §9 配置随迁面＋root.token
SKELETON_DIRS = ("state", "state/messages", "wiki", ".agora", "credentials",
                 QUARANTINE_DIR)
BATCHES = ("tasks", "messages", "events", "credentials", "cursors", "files",
           "finalize")

# 字段映射三源权威（与 tests/v3_harness.CREATE_KEYS/GATE_HEALTH_KEYS 锚定
# 防漂移）：known 落 typed；DROP_FIELDS 瞬态剔除；其余→legacy_extra_json。
_CREATE_KEYS = (
    "title", "goal", "project_dir", "target_branch", "req_id", "model",
    "deliverable", "client_key", "delivery", "env_dir", "after", "verify",
    "verify_pending", "waiting_on", "quick", "verify_base_exempt",
    "review_cmd", "state", "dispatcher_sid", "worker_sid", "root_sid",
    "chain_depth", "fail_streak", "last_seen", "created", "updated")
_GATE_HEALTH_KEYS = (
    "commit", "rebased_from", "env_base", "usage", "in_gate", "gate_env",
    "gate_origin_branch", "gate_artifact", "review_cache", "probe",
    "review_fail_streak", "pending_cancel", "pending_cancel_reason",
    # main F130 verify_infra 宽限兜底写键（ag-60f870 并 main 集成补齐）
    "verify_infra_grace", "verify_infra_backstop", "blocked_reason",
    "cancel_reason")
KNOWN_TASK_FIELDS = frozenset(_CREATE_KEYS) | frozenset(_GATE_HEALTH_KEYS)
DROP_FIELDS = frozenset({"gate_resource"})  # 瞬态清单：只进内存、不落账本
# 已知无 v3 列→审计位（含 main F130 三写键，ag-60f870 并 main 集成补齐）
_KNOWN_NO_PROJECTION = ("usage", "gate_origin_branch", "verify_infra_grace",
                        "verify_infra_backstop", "blocked_reason")

_MSG_FM_KEYS = frozenset({"id", "ts", "task", "kind", "from", "to",
                          "replay_of"})
_MSG_REQUIRED = ("ts", "task", "kind", "from", "to")
_EV_REQUIRED = ("ts", "actor", "event")
_ACTIVE_TASK_STATES = frozenset({"dispatched", "running", "done",
                                 "verify_failed", "reopened", "blocked"})
_ROOT_SID = "root"
_KNOWN_ROLES = frozenset({"root", "worker", "dispatcher"})


class MigrationError(Exception):
    """迁移中止（§9）：报因携上下文；源目录零修改。"""

    def __init__(self, message, details=None):
        self.details = details or {}
        super().__init__(message)


def _ms(value):
    """v2 浮点 epoch 秒→epoch 毫秒（None 保 None；不可迁移数值→中止）。"""
    if value is None:
        return None
    try:
        return epoch_ms(value if isinstance(value, (int, float))
                        else float(value))
    except (ValueError, OverflowError, TypeError) as exc:
        raise MigrationError(
            f"时间字段非可迁移数值（收到 {value!r}）") from exc


def _finite(value, where):
    """数值转换＋有限性 fail-fast（NaN/Infinity→中止，MigrationError 面）。"""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MigrationError(f"{where}: 数值字段不可转换（{value!r}）") from exc
    if not math.isfinite(out):
        raise MigrationError(f"{where}: 数值字段非有限（{value!r}）")
    return out


def _read_json(path: pathlib.Path):
    """容错 JSON 读（坏/缺席＝None——read_ephemeral 同口径）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_snapshot(source):
    """末完整快照行胜出（断尾行丢弃）；返回 (snap|None, 丢弃行数)。

    shortcut: 全文件读入内存——离线一次性、源＝自有 home（现实 KB 级），不做 v2 TAIL_BYTES 尾读窗
    """
    path = source / "state" / "tasks.jsonl"
    if not path.is_file():
        return None, 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError(f"{path}: 快照不可读（{exc}）") from exc
    snap, dropped = None, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            snap = json.loads(line)
        except ValueError:
            dropped += 1
    return snap, dropped


def _normalize(snap):
    """v2 装载口径镜像（core.state._normalize_snapshot）：坏形→中止。"""
    if not isinstance(snap, dict):
        raise MigrationError(f"快照行非对象（{type(snap).__name__}）")
    if snap.get("version") != 1:
        raise MigrationError(f"快照 version 须为 1（收到 {snap.get('version')!r}）")
    tasks = snap.get("tasks", {})
    if not isinstance(tasks, dict):
        raise MigrationError("快照 tasks 须为 dict")
    out = {}
    for tid, row in tasks.items():
        if not isinstance(row, dict):
            raise MigrationError(f"任务 {tid}: 行须为 dict")
        r = dict(row)
        if r.get("state") not in TASK_LEGAL:
            raise MigrationError(
                f"任务 {tid}: state 未知（收到 {r.get('state')!r}）")
        verify = r.get("verify")
        verify = {} if verify is None else verify
        if not isinstance(verify, dict):
            raise MigrationError(f"任务 {tid}: verify 须为 dict")
        verify = dict(verify)
        verify.setdefault("cmd", "")
        if not isinstance(verify["cmd"], str):
            raise MigrationError(f"任务 {tid}: verify.cmd 须为字符串")
        verify.setdefault("state", "pending")
        verify.setdefault("registered_by", None)
        verify.setdefault("registered_at", None)
        verify.setdefault("timeout", None)
        r["verify"] = verify
        if "chain_depth" not in r:
            r["chain_depth"] = 1
        elif (isinstance(r["chain_depth"], bool)
                or not isinstance(r["chain_depth"], int)
                or r["chain_depth"] <= 0):
            raise MigrationError(f"任务 {tid}: chain_depth 须为正整数")
        if "fail_streak" not in r:
            r["fail_streak"] = 0
        elif (isinstance(r["fail_streak"], bool)
                or not isinstance(r["fail_streak"], int)
                or r["fail_streak"] < 0):
            raise MigrationError(f"任务 {tid}: fail_streak 须为非负整数")
        if "review_fail_streak" in r and (
                isinstance(r["review_fail_streak"], bool)
                or not isinstance(r["review_fail_streak"], int)
                or r["review_fail_streak"] < 0):
            raise MigrationError(f"任务 {tid}: review_fail_streak 须为非负整数")
        for clock in ("created", "updated", "last_seen"):
            if r.get(clock) is not None:
                _finite(r[clock], f"任务 {tid}.{clock}")
        r.setdefault("quick", False)
        r.setdefault("verify_pending", False)
        r.setdefault("model", None)
        r.setdefault("deliverable", None)
        r.setdefault("delivery", None)
        r.setdefault("client_key", None)
        r.setdefault("waiting_on", None)
        r.pop("gate_resource", None)  # 瞬态清单：渗漏旧行防御性剔除
        r.setdefault("root_sid", r.get("dispatcher_sid"))
        out[tid] = r
    mail = snap.get("mail_read", {})
    if not isinstance(mail, dict):
        raise MigrationError("快照 mail_read 须为 dict")
    for sid, mark in mail.items():
        _finite(mark, f"mail_read[{sid!r}]")
    return out, dict(mail)


def _map_task(tid, row) -> Task:
    """v2 任务行→v3 Task：known 落 typed、无投影/未知→legacy_extra_json。"""
    extra = {}

    def note(key, value):
        extra[key] = value

    fields = {"id": tid, "state": row["state"]}
    for src, dst in (("title", "title"), ("goal", "goal"), ("req_id", "req_id"),
                     ("model", "model"), ("deliverable", "deliverable"),
                     ("client_key", "client_key"),
                     ("project_dir", "project_dir"),
                     ("target_branch", "target_branch"), ("env_dir", "env_dir"),
                     ("waiting_on", "waiting_on"), ("commit", "commit"),
                     ("rebased_from", "rebased_from"), ("env_base", "env_base"),
                     ("cancel_reason", "cancel_reason"),
                     ("review_cmd", "review_cmd"),
                     ("dispatcher_sid", "dispatcher_sid"),
                     ("worker_sid", "worker_sid"), ("root_sid", "root_sid")):
        if src in row:
            fields[dst] = row[src]
    fields["created_ms"] = _ms(row.get("created"))
    fields["updated_ms"] = _ms(row.get("updated"))
    fields["last_seen_ms"] = _ms(row.get("last_seen"))
    fields["quick"] = bool(row.get("quick", False))
    fields["verify_pending"] = bool(row.get("verify_pending", False))
    fields["verify_base_exempt"] = bool(row.get("verify_base_exempt", False))
    fields["chain_depth"] = int(row.get("chain_depth", 1))
    fields["fail_streak"] = int(row.get("fail_streak", 0))
    after = row.get("after")
    fields["after"] = (str(after),) if after else ()

    v = dict(row.get("verify") or {})
    fields["verify"] = VerifySpec(
        cmd=v.pop("cmd", ""), state=v.pop("state", "pending"),
        registered_by=v.pop("registered_by", None),
        registered_at_ms=_ms(v.pop("registered_at", None)),
        timeout_s=v.pop("timeout", None),
        timeout_source=v.pop("timeout_source", "default"))
    for key, value in v.items():
        note(f"verify.{key}", value)

    d = row.get("delivery")
    if isinstance(d, dict) and d:
        d = dict(d)
        conflict = d.pop("conflict_files", None)
        fields["delivery"] = DeliveryState(
            mode=d.pop("mode", "managed"), status=d.pop("status", "pending"),
            commit=d.pop("commit", None), base=d.pop("base", None),
            target_branch=d.pop("target_branch", None),
            attempt=d.pop("attempt", 0),
            local_status=d.pop("local_status", "pending"),
            error=d.pop("error", None), conflict_files=conflict)
        for key, value in d.items():
            note(f"delivery.{key}", value)
    elif d is not None:
        note("delivery", d)

    art = row.get("gate_artifact")
    artifact = None
    if isinstance(art, dict) and art:
        art = dict(art)
        artifact = Artifact(
            task_id=tid, phase=art.pop("phase", ""), path=art.pop("path", ""),
            rc=art.pop("rc", None), failure_kind=art.pop("failure_kind", None),
            duration_s=art.pop("duration_s", None), bytes=art.pop("bytes", 0),
            cmd=art.pop("cmd", ""), error=art.pop("error", None),
            started_at_ms=_ms(art.pop("started_at", None)),
            ended_at_ms=_ms(art.pop("ended_at", None)))
        for key, value in art.items():
            note(f"gate_artifact.{key}", value)
    elif art is not None:
        note("gate_artifact", art)

    cache = row.get("review_cache")
    if isinstance(cache, dict):
        cache = dict(cache)
        ran_ms = cache.pop("ran_at_ms", None)
        if "ran_at" in cache:  # 旧秒值恒消费：已有 ms 投影则弃旧键（择一）
            legacy_ran = cache.pop("ran_at")
            ran_ms = ran_ms if ran_ms is not None else _ms(legacy_ran)
        if ran_ms is not None:
            cache["ran_at_ms"] = ran_ms
        if not cache:
            cache = None
    else:
        if cache is not None:
            note("review_cache", cache)
        cache = None

    p = row.get("probe")
    probe = None
    if isinstance(p, dict) and p:
        p = dict(p)
        probe = BaseProbe(result=p.pop("result", ""), cmd=p.pop("cmd", ""),
                          base=p.pop("base", ""),
                          ran_at_ms=_ms(p.pop("ran_at", None)))
        for key, value in p.items():
            note(f"probe.{key}", value)
    elif p is not None:
        note("probe", p)

    fields["gate"] = GateState(
        in_gate=bool(row.get("in_gate", False)),
        gate_env=row.get("gate_env"),
        pending_cancel=bool(row.get("pending_cancel", False)),
        pending_cancel_reason=row.get("pending_cancel_reason"),
        gate_artifact=artifact, review_cache=cache,
        review_fail_streak=int(row.get("review_fail_streak", 0)))
    fields["probe"] = probe

    for key in _KNOWN_NO_PROJECTION:
        if key in row:
            note(key, row[key])
    for key, value in row.items():
        if key in KNOWN_TASK_FIELDS or key in DROP_FIELDS:
            continue
        note(key, value)
    fields["legacy_extra_json"] = (json.dumps(extra, ensure_ascii=False,
                                               sort_keys=True)
                                   if extra else "{}")
    return Task(**fields)


def _parse_messages(source):
    """撕裂件（v2 静默跳过口径）→quarantine；可解析缺键/转换失败→中止。"""
    mdir = source / "state" / "messages"
    records, quarantined = [], []
    if not mdir.is_dir():
        return records, quarantined
    for path in sorted(mdir.glob("*.md")):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            quarantined.append({"file": path.name,
                                "reason": f"OSError: {exc}", "content": ""})
            continue
        try:  # v2 _load_messages_from_disk 容错面：decode/frontmatter/int(id)
            head, _, body = raw.partition(b"\n\n")
            fm = {}
            for line in head.split(b"\n"):
                if not line:
                    continue
                text = line.decode("utf-8")
                key, sep, value = text.partition(": ")
                if not sep:
                    raise ValueError(f"frontmatter 行无分隔符: {text!r}")
                fm[key] = value
            mid = int(fm["id"])
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            entry = {"file": path.name,
                     "reason": f"{type(exc).__name__}: {exc}"}
            try:
                entry["content"] = raw.decode("utf-8")  # 文本面原样
            except UnicodeDecodeError:
                entry["content_base64"] = base64.b64encode(raw).decode(
                    "ascii")  # 非 UTF-8 字节无损保全
            quarantined.append(entry)
            continue
        missing = [k for k in _MSG_REQUIRED if k not in fm]
        if missing:
            raise MigrationError(
                f"{path.name}: v2 可解析消息缺必需键 {missing}——中止",
                {"file": path.name})
        try:
            ts = _finite(fm["ts"], f"{path.name}: ts")
            replay = int(fm["replay_of"]) if "replay_of" in fm else None
        except ValueError as exc:
            raise MigrationError(
                f"{path.name}: 字段转换失败（{exc}）——v2 可加载记录须全迁"
            ) from exc
        records.append({"file": path.name, "id": mid, "ts": ts,
                        "task": fm["task"], "kind": fm["kind"],
                        "from": fm["from"], "to": fm["to"],
                        "replay_of": replay, "body": body,
                        "extra": {k: v for k, v in fm.items()
                                  if k not in _MSG_FM_KEYS}})
    return records, quarantined


def _parse_events(source):
    path = source / "events.jsonl"
    records, quarantined = [], []
    if not path.exists():
        return records, quarantined
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError(f"{path}: 事件账本不可读（{exc}）") from exc
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            if not isinstance(ev, dict):
                raise ValueError("非对象行")
        except ValueError as exc:
            quarantined.append({"line": no, "reason": f"ValueError: {exc}",
                                "content": line})
            continue
        missing = [k for k in _EV_REQUIRED if k not in ev]
        if missing:
            raise MigrationError(
                f"events.jsonl:{no}: 事件缺必需键 {missing}——中止",
                {"line": no})
        _finite(ev["ts"], f"events.jsonl:{no}: ts")
        records.append((no, ev))
    return records, quarantined


def _convert_cursors(mail_read, delivered):
    """mail_read 时间戳游标→v3 last-read message id（§9 口径）。"""
    cursors, duplicates = {}, 0
    for sid, mark in sorted(mail_read.items()):
        cursor_ts = float(mark)  # _normalize 已 finite 预检
        inbox = [m for m in delivered if m["to"] == sid]
        pending = [m for m in inbox if m["ts"] > cursor_ts]
        if pending:
            cursor = min(m["new_id"] for m in pending) - 1  # 重投优先
        else:
            cursor = max((m["new_id"] for m in inbox), default=0)
        cursors[sid] = cursor
        duplicates += sum(1 for m in inbox
                          if m["new_id"] > cursor and m["ts"] <= cursor_ts)
    absent = len({m["to"] for m in delivered} - set(mail_read))
    return cursors, duplicates, absent


def _sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


class LegacyMigrator:
    """v2 home → v3 home 一次性迁移（源只读；目标按清单重建幂等）。"""

    def __init__(self, source, target, *, injector=None):
        self._src = pathlib.Path(source)
        self._tgt = pathlib.Path(target)
        self._injector = injector  # tests.v3_harness.FaultInjector（failpoint batch_<名>）

    def migrate(self) -> dict:
        if self._src.resolve() == self._tgt.resolve():
            raise MigrationError("source 与 target 不得同目录（源全程只读）")
        snap, dropped = _load_snapshot(self._src)
        if snap is None:
            raise MigrationError(
                f"{self._src}: 无有效快照（state/tasks.jsonl 缺席或无完整行）"
                "——中止且源零修改")
        rows, mail_read = _normalize(snap)
        conflicts = self._client_key_conflicts(rows)
        if conflicts:
            raise MigrationError(
                f"client_key 冲突（root_sid＋key 撞唯一约束）——中止：{conflicts}",
                {"conflicts": conflicts})
        messages, msg_quar = _parse_messages(self._src)
        events, ev_quar = _parse_events(self._src)
        tokens_path = self._src / "tokens"
        tokens = _read_json(tokens_path)
        tokens_unreadable = (tokens_path.is_file()
                             and not isinstance(tokens, dict))
        tokens = tokens if isinstance(tokens, dict) else {}
        cred_files = self._credential_files()
        self._reset_target()
        unknown = {}
        delivered, extra_fm = [], {}
        storage = Storage(self._tgt / "state" / "agora.db")
        try:
            self._write_tasks(storage, rows, unknown)
            _, replay_unmapped = self._write_messages(
                storage, messages, delivered, extra_fm)
            self._write_events(storage, events)
            creds, cred_stats = self._map_credentials(tokens, cred_files, rows)
            self._fire("credentials", "before")
            with storage.uow():
                for cred in creds:
                    storage.put_credential(cred)
            self._fire("credentials", "after")
            cursors, duplicates, absent = _convert_cursors(mail_read, delivered)
            self._fire("cursors", "before")
            with storage.uow():
                for sid, cursor in sorted(cursors.items()):
                    storage.upsert_cursor(sid, cursor)
            self._fire("cursors", "after")
            config, secrets, root_kept = self._copy_files()
            return self._finalize(
                rows=rows, unknown=unknown, messages=messages,
                msg_quar=msg_quar, events=events, ev_quar=ev_quar,
                extra_fm=extra_fm, replay_unmapped=replay_unmapped,
                tokens_unreadable=tokens_unreadable,
                cred_stats=cred_stats, config=config, secrets=secrets,
                root_kept=root_kept, cursors=cursors, duplicates=duplicates,
                absent=absent, dropped=dropped)
        finally:
            storage.close()

    # ---- 批次（每批 fire batch_<名> before/after；异常＝中止，标记缺席）----

    def _write_tasks(self, storage, rows, unknown):
        self._fire("tasks", "before")
        with storage.uow():
            for tid in sorted(rows):
                task = _map_task(tid, rows[tid])
                extras = json.loads(task.legacy_extra_json)
                if extras:
                    unknown[tid] = sorted(extras)
                try:
                    storage.upsert_task(task)
                except Exception as exc:
                    raise MigrationError(
                        f"任务 {tid} 写入失败（v2 可加载记录须全迁）: {exc}",
                        {"task": tid}) from exc
        self._fire("tasks", "after")

    def _write_messages(self, storage, messages, delivered, extra_fm):
        new_by_old = {}
        unmapped = 0
        self._fire("messages", "before")
        with storage.uow():
            for rec in messages:
                replay = rec["replay_of"]
                if replay is not None:
                    if replay in new_by_old:
                        replay = new_by_old[replay]  # 重编号随迁改写
                    else:
                        unmapped += 1  # 前向/自引用（损坏面）——保原值如实报数
                try:
                    msg = storage.insert_message(
                        ts_ms=epoch_ms(rec["ts"]), task_id=rec["task"],
                        kind=rec["kind"], from_sid=rec["from"],
                        to_sid=rec["to"], body=rec["body"], replay_of=replay)
                except Exception as exc:
                    raise MigrationError(
                        f"消息 {rec['file']} 写入失败"
                        f"（v2 可加载记录须全迁）: {exc}",
                        {"file": rec["file"]}) from exc
                new_by_old[rec["id"]] = msg.id
                delivered.append({"new_id": msg.id, "ts": rec["ts"],
                                  "to": rec["to"]})
                for key in rec["extra"]:
                    extra_fm[key] = extra_fm.get(key, 0) + 1
        self._fire("messages", "after")
        return new_by_old, unmapped

    def _write_events(self, storage, events):
        self._fire("events", "before")
        with storage.uow():
            for no, ev in events:
                detail = {k: v for k, v in ev.items()
                          if k not in ("ts", "actor", "event", "task")}
                try:
                    storage.append_event(Event(
                        seq=0, ts_ms=epoch_ms(float(ev["ts"])),
                        actor=ev["actor"], event=ev["event"],
                        task=ev.get("task"), detail=detail or None))
                except Exception as exc:
                    raise MigrationError(
                        f"events.jsonl:{no} 写入失败（v2 可加载记录须全迁）"
                        f": {exc}", {"line": no}) from exc
        self._fire("events", "after")

    def _copy_files(self):
        """配置＋secret 文件随迁（权限保真）；缺项如实报告。

        secret/root.token 经 0600 建时即收紧（authsvc._write_secret 同口径，
        免 copy2 的 umask 窗）；配置文件按源 mode 保真复制。
        """
        self._fire("files", "before")
        config = self._copy_configs()
        secrets, root_kept = self._copy_secrets()
        self._fire("files", "after")
        return config, secrets, root_kept

    def _copy_configs(self):
        """配置保 mode 照拷（Req: F156 抽取面——Git 后端 files 批次复用）。"""
        config = {}
        for name in CONFIG_FILES:
            src = self._src / ".agora" / name
            if src.is_file():
                try:
                    dst = self._tgt / ".agora" / name
                    shutil.copy2(src, dst)
                    os.chmod(dst, stat.S_IMODE(src.stat().st_mode))
                except OSError as exc:
                    raise MigrationError(
                        f"配置 {name} 随迁失败（{exc}）") from exc
                config[name] = "copied"
            else:
                config[name] = "missing"
        return config

    def _copy_secrets(self):
        """credentials/*.token＋root.token 0600 随迁（孤儿照迁——清扫归
        AuthService.sweep，迁移不代跑）。Req: F156 抽取面——Git 后端凭据
        put 前先拷（spec gitstore-phase3-legacy-migration §5）。"""
        secrets = 0
        cdir = self._src / "credentials"
        if cdir.is_dir():
            for path in sorted(cdir.glob("*.token")):
                self._copy_secret(path,
                                  self._tgt / "credentials" / path.name)
                secrets += 1
        root = self._src / ".agora" / "root.token"
        root_kept = False
        if root.is_file():
            self._copy_secret(root, self._tgt / ".agora" / "root.token")
            root_kept = True
        return secrets, root_kept

    @staticmethod
    def _copy_secret(src: pathlib.Path, dst: pathlib.Path):
        try:
            data = src.read_bytes()
            # 写面不跟符号链接（与 gitstore 读面/authsvc 写面同口径；评审环 C 册）
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as exc:
            raise MigrationError(f"secret 文件随迁失败（{src}）: {exc}"
                                 ) from exc

    def _finalize(self, **parts):
        self._fire("finalize", "before")
        manifest = {"messages": parts["msg_quar"], "events": parts["ev_quar"]}
        self._write_private(self._tgt / QUARANTINE_DIR / "manifest.json",
                            json.dumps(manifest, ensure_ascii=False,
                                       indent=1))
        report = self._report(**parts)
        self._write_private(self._tgt / REPORT_PATH,
                            json.dumps(report, ensure_ascii=False, indent=1,
                                       sort_keys=True))
        self._fire("finalize", "after")
        (self._tgt / MARKER).write_text("complete\n", encoding="utf-8")
        return report

    def _report(self, **parts) -> dict:
        """结构化报告装配（纯函数面；Req: F156 抽取——Git 后端增补复用）。"""
        rows = parts["rows"]
        by_state = collections.Counter(r["state"] for r in rows.values())
        verify_states = collections.Counter(
            r["verify"]["state"] for r in rows.values())
        modes = collections.Counter(
            (r.get("delivery") or {}).get("mode", "none")
            for r in rows.values())
        statuses = collections.Counter(
            (r.get("delivery") or {}).get("status", "none")
            for r in rows.values())
        report = {
            "ok": True,
            "source": str(self._src), "target": str(self._tgt),
            "snapshot": {"lines_dropped": parts["dropped"]},
            "tasks": {
                "total": len(rows), "by_state": dict(sorted(by_state.items())),
                "verify_states": dict(sorted(verify_states.items())),
                "delivery_modes": dict(sorted(modes.items())),
                "delivery_statuses": dict(sorted(statuses.items())),
                "gate": {
                    "in_gate": sum(1 for r in rows.values() if r.get("in_gate")),
                    "pending_cancel": sum(
                        1 for r in rows.values() if r.get("pending_cancel")),
                    "gate_artifacts": sum(
                        1 for r in rows.values() if r.get("gate_artifact")),
                    "probes": sum(1 for r in rows.values() if r.get("probe")),
                    "review_cache": sum(
                        1 for r in rows.values() if r.get("review_cache"))}},
            "unknown_fields": parts["unknown"],
            "messages": {
                "count": len(parts["messages"]),
                "quarantined": len(parts["msg_quar"]),
                "body_hash": _sha("".join(
                    _sha(rec["body"]) for rec in parts["messages"]).encode()),
                "frontmatter_extra": dict(sorted(parts["extra_fm"].items())),
                "replay_unmapped": parts["replay_unmapped"]},
            "events": {
                "count": len(parts["events"]),
                "quarantined": len(parts["ev_quar"]),
                "hash": _sha("\n".join(
                    json.dumps(ev, ensure_ascii=False, sort_keys=True)
                    for _, ev in parts["events"]).encode())},
            "cursors": {
                "migrated": len(parts["cursors"]),
                "possible_duplicate_delivery": parts["duplicates"],
                "absent_sids": parts["absent"]},
            "credentials": parts["cred_stats"] | {
                "secret_files_copied": parts["secrets"],
                "tokens_unreadable": parts["tokens_unreadable"]},
            "config": {**parts["config"],
                       "root.token": "kept" if parts["root_kept"] else "missing"},
            "quarantine": {"messages": len(parts["msg_quar"]),
                           "events": len(parts["ev_quar"])},
        }
        return report

    @staticmethod
    def _write_private(path: pathlib.Path, text):
        """报告/manifest 0600 落盘（撕裂原文与凭据摘要面——目录 0700 之外的纵深）。"""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            raise MigrationError(f"私密件落盘失败（{path}）: {exc}") from exc

    # ---- 源读取辅助 ----

    def _credential_files(self):
        out = {}
        cdir = self._src / "credentials"
        if not cdir.is_dir():
            return out
        for path in sorted(cdir.glob("*.token")):
            data = _read_json(path)
            token = data.get("token") if isinstance(data, dict) else None
            out[path.stem] = token if isinstance(token, str) else None
        return out

    def _map_credentials(self, tokens, files, rows):
        token_to_tid = {tok: tid for tid, tok in files.items() if tok}
        creds, seen = [], set()
        stats = {"dropped_roots": 0, "sid_collisions": 0,
                 "worker_without_file": 0, "malformed": 0,
                 "unknown_role": 0, "root_from_file": False}
        for tok, value in sorted(tokens.items()):
            try:
                tok.encode("utf-8")  # lone surrogate 键在报告 hash/authsvc 均炸
            except UnicodeEncodeError:
                stats["malformed"] += 1
                continue
            if (not isinstance(value, (list, tuple)) or len(value) < 2
                    or not isinstance(value[0], str)):
                stats["malformed"] += 1
                continue
            sid, role = value[0], value[1]
            if not isinstance(role, str) or role not in _KNOWN_ROLES:
                stats["unknown_role"] += 1  # 外来畸形值（含非字符串 role）——照迁入 dispatcher 桶但如实报数
            lineage = (tuple(x for x in value[2] if isinstance(x, str))[-16:]
                       if len(value) > 2 and isinstance(value[2], list) else ())
            task_id = None
            if role == "root":
                if _ROOT_SID in seen:
                    stats["dropped_roots"] += 1  # v3 每 sid 一牌：排序首者胜出
                    continue
                if sid != _ROOT_SID:  # 污染条目归位 daemon 身份（pin_roots 同款）
                    lineage = (*lineage, sid)[-16:]
                sid = _ROOT_SID
            elif role == "worker":
                task_id = token_to_tid.get(tok)
                if task_id is None:  # 文件缺席→活动任务行回退（无活动＝dispatcher 桶报数）
                    cands = [tid for tid, r in rows.items()
                             if r.get("worker_sid") == sid
                             and r.get("state") in _ACTIVE_TASK_STATES]
                    task_id = (max(cands,
                                   key=lambda t: rows[t].get("updated") or 0.0)
                               if cands else None)
                    if task_id is None:
                        stats["worker_without_file"] += 1
            if sid in seen:
                stats["sid_collisions"] += 1
                continue
            seen.add(sid)
            creds.append(Credential(sid=sid, token=tok, task_id=task_id,
                                    lineage=lineage))
        if _ROOT_SID not in seen:  # map 无 root 条目→root.token 文件兜底
            data = _read_json(self._src / ".agora" / "root.token")
            tok = data.get("token") if isinstance(data, dict) else None
            if isinstance(tok, str) and tok:
                creds.append(Credential(sid=_ROOT_SID, token=tok))
                stats["root_from_file"] = True
        stats["root"] = sum(1 for c in creds if c.sid == _ROOT_SID)
        stats["worker"] = sum(1 for c in creds if c.task_id)
        stats["dispatcher"] = sum(
            1 for c in creds if c.sid != _ROOT_SID and not c.task_id)
        stats["token_hashes"] = [
            f"sha256:{_sha(c.token.encode())[:16]}"
            for c in sorted(creds, key=lambda c: c.sid)]
        stats["orphan_secret_files"] = sum(
            1 for tok in files.values() if not tok or tok not in tokens)
        return creds, stats

    @staticmethod
    def _client_key_conflicts(rows):
        buckets = {}
        for tid, r in rows.items():
            key, root = r.get("client_key"), r.get("root_sid")
            if key and root:  # (root_sid, client_key) 唯一约束的非空面
                buckets.setdefault((root, key), []).append(tid)
        return [{"root_sid": root, "client_key": key, "tasks": sorted(tids)}
                for (root, key), tids in sorted(buckets.items())
                if len(tids) > 1]

    def _reset_target(self):
        """目标按清单重建（幂等）：库/隔离区/报告/标记/随迁文件面清空重写——
        上轮残留（异源迁移/源侧删件）不得存活，报告与盘面一致。"""
        db = self._tgt / "state" / "agora.db"
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(str(db) + suffix)
        shutil.rmtree(self._tgt / QUARANTINE_DIR, ignore_errors=True)
        for path in (self._tgt / REPORT_PATH, self._tgt / MARKER):
            with contextlib.suppress(OSError):
                os.remove(path)
        creds = self._tgt / "credentials"
        if creds.is_dir():
            for path in sorted(creds.glob("*.token")):
                with contextlib.suppress(OSError):
                    path.unlink()
        for name in (*CONFIG_FILES, "root.token"):
            with contextlib.suppress(OSError):
                (self._tgt / ".agora" / name).unlink()
        for name in SKELETON_DIRS:
            directory = self._tgt / name
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def _fire(self, batch, phase):
        if self._injector is not None:
            self._injector.fire(f"batch_{batch}", phase, batch=batch)
