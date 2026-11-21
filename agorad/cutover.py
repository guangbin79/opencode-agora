"""Req: F140 — v3 切换/导出/备份服务面（opencode-agora v3 P4-T12，设计 §8/§9）。

./agora CLI 的离线动词（export/backup/migrate/switch/rollback）背后件：
SQLite 权威→派生物全量重建、backup API 热备＋凭据清扫、T11 迁移器集成
（dry-run 探针目录零写入）、切换完整性门＋WAL 收卷＋同级两步互换与回滚。

分层（断言10）：仅 stdlib＋domain＋storage＋migrator——SQL 圈禁 storage
（backup/checkpoint/体检/全量扫描全走 Storage 面），不 import access；
无网络；迁移源目录只读（migrator 契约，digest_tree 锚定在测试侧）。
Req: F157——派生落盘/_migrate_cmd 壳/同级两步互换原语抽为共用面（git
后端 agorad.gitcutover 复用；SQLite 行为/报告字节序不变），git 特有面
不入本模块（分层守卫镜像）。

安全（断言8）：导出派生物/报告/备份件不含 token 明文——ledger.json 不含
credentials 面；备份件 DELETE credentials＋VACUUM 压实（牌值字节不驻
freelist 暗面）；迁移报告 token_hashes 摘要面系 T11 既有口径。

切换语义（§9）：回滚＝目录互换——切换后 v3 新写入随 v3 目录走，旧 home
驻 <old>.swapped 全程不被 v3 写入触碰（边界：目录级隔离，文档与测试双面
申报）。完整性门（integrity/FK/最小冒烟）不过即拒互换，零动作。
"""
import contextlib
import dataclasses
import json
import os
import pathlib
import tempfile

from agorad.migrator import MARKER, LegacyMigrator, MigrationError
from agorad.storage import Storage

SWAPPED_SUFFIX = ".swapped"   # 切换后旧 home 驻名（回滚对端，确定性派生）
LEDGER_PATH = "state/ledger.json"
EVENTS_NAME = "events.jsonl"


class CutoverError(Exception):
    """结构化失败：code/message/recoverable——CLI 契约 1 面（可恢复）来源。"""

    def __init__(self, code, message, *, recoverable=True):
        self.code, self.message, self.recoverable = code, message, recoverable
        super().__init__(message)


def _db_path(home: pathlib.Path) -> pathlib.Path:
    p = home / "state" / "agora.db"
    if not p.is_file():
        raise CutoverError("no_database", f"{p}: 权威库缺席（先 migrate --apply）")
    return p


def _require_dir(path, what):
    if not pathlib.Path(path).is_dir():
        raise CutoverError(f"{what}_missing", f"{path}: 目录缺席")


def _atomic_write_bytes(path: pathlib.Path, data: bytes, *, private=False):
    """tmp＋fsync＋os.replace 原子落盘（撕裂不可见半态；v2 store 同款纪律）。
    短写循环收口（ENOSPC 边界 os.write 允许部分写）＋O_NOFOLLOW（tmp 位
    预置符号链接即拒）＋失败清 tmp（不留残垃圾）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0),
                 0o600 if private else 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)


# ---- §8 派生导出（断言1/2：SQLite 权威，删件重跑＝修复）----

def message_document(msg) -> bytes:
    """v2 兼容消息件字节：id 首行＋余键字典序 frontmatter＋空行＋body 逐字节
    （store.write_message 同构；ts 出秒浮点＝v2 语义）。"""
    fm = {"ts": msg.ts_ms / 1000.0, "task": msg.task_id, "kind": msg.kind,
          "from": msg.from_sid, "to": msg.to_sid}
    if msg.replay_of is not None:
        fm["replay_of"] = msg.replay_of
    head = f"id: {msg.id}\n" + "".join(
        f"{k}: {fm[k]}\n" for k in sorted(fm))
    return head.encode("utf-8") + b"\n" + bytes(msg.body)


def event_line(ev) -> str:
    """v2 兼容事件行：ts/actor/event(/task)＋detail 平展，ensure_ascii=False。
    核心键后置——detail 同名键不得覆写 ts/actor/event/task（防伪造行首语义）。"""
    row = {"ts": ev.ts_ms / 1000.0, "actor": ev.actor, "event": ev.event}
    if ev.task:
        row["task"] = ev.task
    if ev.detail:
        row = {**ev.detail, **row}
    return json.dumps(row, ensure_ascii=False)


class MessageFileExporter:
    """§8 单件导出器（messaging.MessageExporter Protocol 同形——注入
    MessagingService 即复用「导出失败→引用省略」规则）；原子写，失败抛异常。"""

    def __init__(self, home):
        self._home = pathlib.Path(home)

    def export(self, message):
        rel = f"state/messages/{message.id:08d}.md"  # 补零＝文件名字典序＝id 序
        _atomic_write_bytes(self._home / rel, message_document(message))
        return rel


def _ledger(st: Storage, extra_counts) -> dict:
    """记账快照 canonical JSON 体（断言1）：任务/计时/审批/游标/receipts
    全投影；credentials 面不入（断言8——token 明文禁出，牌可重签）。"""

    def plain(obj):  # 枚举落 .value（JSON 可载）
        return {**dataclasses.asdict(obj), "state": obj.state.value}

    return {
        "schema": 3,
        "tasks": [dataclasses.asdict(t) for t in st.tasks_all()],
        "timers": [plain(x) for x in st.timers_all()],
        "approvals": [plain(x) for x in st.approvals_all()],
        "cursors": {c.sid: c.last_read_ms for c in st.cursors_all()},
        "receipts": [dataclasses.asdict(r) for r in st.receipts_all()],
        "counts": extra_counts,
        "credentials": "excluded (Req: F140 断言8)",
    }


def _open_storage(db: pathlib.Path) -> Storage:
    """Storage 开库缝（CLI 面）：开库异常（腐蚀库等）归一 CutoverError
    （可恢复——修复/换位后重试），不留裸异常面。"""
    try:
        return Storage(db)
    except Exception as exc:
        raise CutoverError("storage_open_failed", f"{db}: {exc}") from exc


def export_home(home, out=None):
    """全量派生导出：state/messages/*.md＋events.jsonl＋state/ledger.json——
    一律从 SQLite 重建（库为权威，孤儿派生件清扫）；返回机读报告。"""
    home = pathlib.Path(home)
    dest = pathlib.Path(out) if out else home
    db = _db_path(home)
    st = _open_storage(db)
    try:
        msgs, events = st.messages_all(), st.events_all()
        ledger = _ledger(st, {"tasks": None, "messages": len(msgs),
                              "events": len(events)})
        ledger["counts"]["tasks"] = len(ledger["tasks"])
    finally:
        st.close()
    _write_derived(dest, msgs, events, ledger)
    return {"home": str(home), "out": str(dest),
            "messages": len(msgs), "events": len(events),
            "ledger": LEDGER_PATH, "counts": ledger["counts"]}


def _write_derived(dest, msgs, events, ledger):
    """派生件落盘＋孤儿清扫（Req: F157 抽取面——git 后端导出复用，格式
    面与 SQLite 完全同构）。"""
    exporter = MessageFileExporter(dest)
    for m in msgs:
        exporter.export(m)
    keep = {f"{m.id:08d}.md" for m in msgs}
    mdir = dest / "state" / "messages"
    # 孤儿清扫界：仅清 id≤快照 max 的库外件（AUTOINCREMENT 单调——并发新
    # 提交 id 必大于快照 max，不误删）；messages 目录为符号链接时跳过（不
    # 追随出界删件）。
    top = max((m.id for m in msgs), default=0)
    if not mdir.is_symlink():
        for stale in sorted(mdir.glob("*.md")):
            if stale.name not in keep:
                with contextlib.suppress(ValueError):
                    if int(stale.stem) <= top:
                        stale.unlink()
    lines = "".join(event_line(e) + "\n" for e in events)
    _atomic_write_bytes(dest / EVENTS_NAME, lines.encode("utf-8"))
    _atomic_write_bytes(dest / LEDGER_PATH,
                        json.dumps(ledger, ensure_ascii=False, sort_keys=True,
                                   indent=1).encode("utf-8"), private=True)


# ---- backup（断言3：backup API 一致性热备＋体检＋凭据清扫）----

def _scrub_tmp(tmp: pathlib.Path):
    """tmp＋sidecar 全清（失败路径/陈旧残件——含牌明文窗的半备件不得存活）。"""
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(tmp) + suffix).unlink(missing_ok=True)


def backup_home(home, dest):
    db = _db_path(pathlib.Path(home))
    dest = pathlib.Path(dest)
    if dest.exists():
        raise CutoverError("backup_exists", f"{dest}: 备份件已存在（拒覆写）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    _scrub_tmp(tmp)  # kill -9 残件预清：陈旧半备件会让重试炸「非库」且含牌面
    st = _open_storage(db)
    try:
        try:
            scrub = st.backup_to(tmp, scrub_credentials=True)
            before = _verify(db)["counts"]
        except BaseException:
            _scrub_tmp(tmp)  # 任一失败面：含牌 tmp 不留盘
            raise
    finally:
        st.close()
    try:
        report = _verify(tmp)
    except BaseException:
        _scrub_tmp(tmp)
        raise
    if (report["integrity"] != ["ok"] or report["fk_violations"]
            or report["counts"]["credentials"]):
        _scrub_tmp(tmp)
        raise CutoverError(
            "backup_verify_failed",
            f"备份件体检未过（integrity={report['integrity'][:2]} "
            f"fk={report['fk_violations']} credentials={report['counts']['credentials']}）")
    # tmp 内容耐久由 sqlite synchronous=FULL 收口（backup_to 收敛 journal 后
    # 关连接）；replace 原子性同 v2 store._compact 纪律（目录项 fsync 不另做）
    os.replace(tmp, dest)
    return {"backup": str(dest), "integrity": report["integrity"],
            "source_counts": before, "backup_counts": report["counts"],
            "credentials_scrubbed": scrub["credentials_dropped"]}


# ---- migrate（断言4：T11 集成——dry-run 零写入/apply 建目标/幂等）----

def migrate_cmd(source, target, *, apply=False, report_path=None):
    """dry-run＝探针临时目录全流程验证（source/target 零写入）后报告改指
    真实 target；apply＝LegacyMigrator 全量迁移（报告落 target/state/
    migration-report.json，重跑幂等＝T11 契约）。"""
    return _migrate_cmd(LegacyMigrator, source, target, apply=apply,
                        report_path=report_path)


def _migrate_cmd(migrator_cls, source, target, *, apply, report_path):
    """迁移壳通用面（Req: F157 抽取——git 后端（GitLegacyMigrator）经此
    复用 dry-run 探针/错误归一/报告落盘语义，行为逐字节同构）。"""
    source, target = pathlib.Path(source), pathlib.Path(target)
    report_path = pathlib.Path(report_path) if report_path else None
    try:
        if apply:
            report = migrator_cls(source, target).migrate()
        else:
            with tempfile.TemporaryDirectory(
                    prefix="agora-migrate-dryrun-") as scratch:
                report = migrator_cls(
                    source, pathlib.Path(scratch) / "probe").migrate()
            report["dry_run"] = True
            report["target"] = str(target)
    except MigrationError as exc:
        raise CutoverError("migration_aborted",
                           f"迁移中止（源零修改）: {exc}") from exc
    if report_path:
        _atomic_write_bytes(
            report_path,
            json.dumps(report, ensure_ascii=False, indent=1,
                       sort_keys=True).encode("utf-8"), private=True)
    return report


# ---- switch/rollback（断言5/6：完整性门→WAL 收卷→同级两步互换）----

def _verify(path: pathlib.Path):
    """体检缝（fail-closed）：库不可开/不可读（非库字节等）异常即门拒。"""
    try:
        return Storage.verify_db(path)
    except Exception as exc:
        raise CutoverError("verify_unreadable",
                           f"{path}: 体检不可执行: {exc}") from exc


def _gate(db: pathlib.Path):
    """切换前完整性门：integrity/FK/最小冒烟（任务面整读）。"""
    try:
        report = _verify(db)
    except CutoverError as exc:  # 库不可读＝完整性门不过（同拒互换）
        raise CutoverError("integrity_gate_failed",
                           exc.message) from exc
    if report["integrity"] != ["ok"]:
        raise CutoverError("integrity_gate_failed",
                           f"integrity_check 未过: {report['integrity'][:3]}")
    if report["fk_violations"]:
        raise CutoverError("fk_gate_failed",
                           f"foreign_key_check 违例 {report['fk_violations']} 条")
    return report


def switch(old, new):
    old, new = pathlib.Path(old), pathlib.Path(new)
    _require_dir(old, "old_home")
    _require_dir(new, "new_home")
    if old.parent.resolve() != new.parent.resolve():
        raise CutoverError("not_siblings",
                           "old/new 须同级目录（目录互换前提，设计 §9）")
    if not (new / MARKER).is_file():
        raise CutoverError("new_home_incomplete",
                           f"{new / MARKER}: 迁移完成标记缺席（先 migrate --apply）")
    db = new / "state" / "agora.db"
    gate = _gate(db)
    st = _open_storage(db)
    try:
        try:
            smoke = len(st.tasks_all())  # 最小业务冒烟：任务面整读可读
            ckpt = st.checkpoint_truncate()
        except Exception as exc:  # 冒烟/收卷异常＝门拒（零动作，可处置重试）
            raise CutoverError("integrity_gate_failed",
                               f"冒烟/收卷失败（拒互换）: {exc}") from exc
    finally:
        st.close()  # 关闭全部连接——互换前提
    for suffix in ("-wal", "-shm"):
        if pathlib.Path(str(db) + suffix).exists():
            raise CutoverError("wal_residue",
                               f"{db}{suffix}: 收卷后仍活跃——拒互换")
    report = _switch_swap(old, new)
    report["smoke_tasks"] = smoke
    report["checkpoint"] = list(ckpt)
    report["gate_counts"] = gate["counts"]
    return report


def _switch_swap(old, new):
    """同级两步互换（Req: F157 抽取面——git 后端复用同款失败恢复语义：
    第二步败＝第一步回退；回退也败＝裸现场给手工恢复指令）。"""
    swapped = old.with_name(old.name + SWAPPED_SUFFIX)
    if swapped.exists():
        raise CutoverError(
            "swapped_exists",
            f"{swapped}: 已在（上次切换未回滚？先 rollback；若上次互换中途"
            "崩溃致 old 缺席，手工归位：mv 该目录回原 old 路径）")
    os.rename(old, swapped)
    try:
        os.rename(new, old)
    except OSError as exc:  # 第二步败＝第一步回退，old 归位零动作
        try:
            os.rename(swapped, old)
        except OSError as back:  # 回退也败＝裸现场：给手工恢复指令
            raise CutoverError(
                "swap_broken", f"互换第二步失败且回退失败（现场保持）: {exc}；"
                f"手工归位：mv {swapped} {old}",
                recoverable=False) from back
        raise CutoverError("swap_failed", f"互换第二步失败（已回退）: {exc}",
                           recoverable=False) from exc
    return {"switched_to": str(old), "old_home_at": str(swapped)}


def rollback(old, new):
    """互换回旧 home（断言6）：v3 home 归位原名，旧 home 回原名。切换后
    v3 新写入随 v3 目录走——旧侧零沾染（边界：目录级隔离）。"""
    old, new = pathlib.Path(old), pathlib.Path(new)
    swapped = old.with_name(old.name + SWAPPED_SUFFIX)
    _require_dir(old, "current_home")
    _require_dir(swapped, "swapped_old_home")
    if new.exists():
        raise CutoverError("rollback_target_exists",
                           f"{new}: v3 home 原名位非空（先清让）")
    return _rollback_swap(old, new)


def _rollback_swap(old, new):
    """回滚互换原语（Req: F157 抽取面——git 后端复用同款失败恢复语义）。"""
    swapped = old.with_name(old.name + SWAPPED_SUFFIX)
    os.rename(old, new)
    try:
        os.rename(swapped, old)
    except OSError as exc:
        try:
            os.rename(new, old)
        except OSError as back:
            raise CutoverError(
                "rollback_broken",
                f"回滚第二步失败且回退失败（现场保持）: {exc}；"
                f"手工归位：mv {new} {old}",
                recoverable=False) from back
        raise CutoverError("rollback_failed", f"回滚第二步失败（已回退）: {exc}",
                           recoverable=False) from exc
    return {"rolled_back_to": str(old), "v3_home_at": str(new)}
