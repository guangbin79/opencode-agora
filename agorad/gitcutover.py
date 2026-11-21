"""Req: F157 — GitStore Phase 3 cutover 服务面（spec
wiki/spec/gitstore-phase3-cutover.md）。

离线切换动词的 Git 后端：migrate（GitLegacyMigrator 集成）/export
（GitStore 权威派生重建）/backup（canonical journal bundle＋校验门）/
switch（重放完整性门＋切换清单＋同级两步互换）/rollback（清单锚定的
丢写防护门）。SQLite 面（agorad.cutover）缺省零改动——git 仅显式选择
（CLI --backend git），未知后端 fail-closed；后端选择不做目录内容推断
（含混 home 只认显式请求）。

复用面：派生件格式（messages/events/ledger）与孤儿清扫规则、迁移壳
（dry-run 探针/错误归一/报告落盘）、同级两步互换与失败恢复语义全部
复用 agorad.cutover 共用面——两后端派生字节同构。Git 特有面：journal
在位要求（经 JOURNAL_DIRNAME 常量，字面量圈禁 gitstore）、bundle 经
GitStore（git 子进程圈禁不变）、切换清单＝非 canonical 运维元数据
（journal 零触碰，rollback 防丢锚点）。

安全：导出/备份/清单不含凭据明文（canonical 本就只含摘要＋外部 secret
键位；bundle 仅 ref 可达对象——外部 secret 文件与 home 内无关件天然
排除）；失败路径 tmp 全清零残件；回滚防丢门 fail-closed 双侧目录零
动作，人工恢复路径显式入错误文案（无静默丢写回滚）。
"""
import dataclasses
import hashlib
import json
import os
import pathlib
import time

from agorad.cutover import (CutoverError, LEDGER_PATH, SWAPPED_SUFFIX,
                            _atomic_write_bytes, _migrate_cmd,
                            _require_dir, _rollback_swap, _switch_swap,
                            _write_derived)
from agorad.domain import epoch_ms
from agorad.gitmigrate import FOREIGN_LEDGERS, GitLegacyMigrator
from agorad.gitstore import JOURNAL_DIRNAME, JOURNAL_REF, GitStore
from agorad.migrator import MARKER, REPORT_PATH
from agorad.storage import StorageError

BACKEND = "git"
SWITCH_MANIFEST = "state/switch-manifest.json"  # 非 canonical 运维元数据


def _require_journal(home):
    p = pathlib.Path(home) / "state" / JOURNAL_DIRNAME
    if not p.is_dir():
        raise CutoverError(
            "no_journal", f"{p}: canonical journal 缺席"
            "（git 后端须先 migrate --backend git --apply）")


def _open_store(home):
    """开库缝（CLI 面）：重放校验异常（损毁/缺活 secret 等 StorageError
    家族）归一 CutoverError（可恢复——修复后重试），不留裸异常面。"""
    _require_journal(home)
    try:
        return GitStore(pathlib.Path(home) / "state")
    except StorageError as exc:
        raise CutoverError("storage_open_failed",
                           f"{home}: Git 账本不可开: {exc}") from exc


def _ledger(st: GitStore, extra_counts) -> dict:
    """记账快照 canonical JSON 体：tasks/timers/cursors/receipts 可用
    投影全量（GitStore 无 approvals 面——如实缺席不伪造）；credentials
    面不入（canonical 只含摘要＋键位，外部 secret 内容禁出）。"""

    def plain(obj):  # 枚举落 .value（JSON 可载）
        return {**dataclasses.asdict(obj), "state": obj.state.value}

    return {
        "schema": 3,
        "tasks": [dataclasses.asdict(t) for t in st.tasks_all()],
        "timers": [plain(x) for x in st.timers_all()],
        "cursors": {c.sid: c.last_read_ms for c in st.cursors_all()},
        "receipts": [dataclasses.asdict(r) for r in st.receipts_all()],
        "counts": extra_counts,
        "credentials": "excluded (Req: F140 断言8)",
    }


# ---- migrate（GitLegacyMigrator 集成；壳语义复用 cutover）----

def migrate_cmd(source, target, *, apply=False, report_path=None):
    """dry-run＝探针临时目录全流程（source/target 零写入）；apply＝
    GitLegacyMigrator 全量迁移（F156 完成标记/报告语义原样保全）；
    MigrationError 归一 migration_aborted（源零修改）。"""
    return _migrate_cmd(GitLegacyMigrator, source, target, apply=apply,
                        report_path=report_path)


# ---- export_home（GitStore 权威派生重建）----

def export_home(home, out=None):
    """全量派生导出（spec export_home）：messages/events/ledger 一律从
    canonical 对象重建，journal 零触碰（head/tx 前后不变），零 SQLite
    件；孤儿清扫同有界单调 id 规则；删件重跑＝修复（重启等价）。"""
    home = pathlib.Path(home)
    dest = pathlib.Path(out) if out else home
    st = _open_store(home)
    try:
        msgs, events = st.messages_all(), st.events_all()
        ledger = _ledger(st, {"tasks": None, "messages": len(msgs),
                              "events": len(events)})
        ledger["counts"]["tasks"] = len(ledger["tasks"])
        head, txs = st.head, st.tx_count
    finally:
        st.close()
    _write_derived(dest, msgs, events, ledger)
    return {"home": str(home), "out": str(dest),
            "messages": len(msgs), "events": len(events),
            "ledger": LEDGER_PATH, "counts": ledger["counts"],
            "backend": BACKEND, "journal": {"head": head, "txs": txs}}


# ---- backup_home（canonical journal bundle＋校验门）----

def _clean_bundle_tmp(tmp: pathlib.Path):
    """tmp＋git lock 残件全清（失败/陈旧半备件不得存活）。"""
    for suffix in ("", ".lock"):
        pathlib.Path(str(tmp) + suffix).unlink(missing_ok=True)


def backup_home(home, dest):
    """canonical journal bundle 备份（spec backup_home）：tmp 创建→
    git bundle verify＋head 对账→0600 原子落位；失败/未过校验零目的地
    件零残件。仅 refs/agora/journal 可达对象入包（外部 secret 与 home
    内无关件天然排除——明文牌不进任何 canonical 对象）。"""
    home, dest = pathlib.Path(home), pathlib.Path(dest)
    if dest.exists():
        raise CutoverError("backup_exists", f"{dest}: 备份件已存在（拒覆写）")
    st = _open_store(home)
    try:
        if st.head is None:
            raise CutoverError("no_history",
                               f"{home}: journal 空历史——无可打包")
        head = st.head
        counts = {"tasks": len(st.tasks_all()),
                  "messages": len(st.messages_all()),
                  "events": len(st.events_all()), "txs": st.tx_count}
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        _clean_bundle_tmp(tmp)  # kill -9 残件预清
        try:
            try:
                st.bundle_journal(tmp)
            except StorageError as exc:
                raise CutoverError("backup_failed",
                                   f"bundle 创建失败: {exc}") from exc
            try:
                heads = st.verify_bundle(tmp)
            except StorageError as exc:
                raise CutoverError("backup_verify_failed",
                                   f"bundle 校验未过: {exc}") from exc
            if heads.get(JOURNAL_REF) != head:  # 记录面对账账本真值
                raise CutoverError(
                    "backup_verify_failed",
                    f"bundle 记录 {JOURNAL_REF}={heads.get(JOURNAL_REF)}"
                    f" 与账本 head {head} 不符")
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)  # 校验过后才原子落位
        except BaseException:
            _clean_bundle_tmp(tmp)
            raise
    finally:
        st.close()
    return {"backup": str(dest), "backend": BACKEND, "ref": JOURNAL_REF,
            "head": head, "counts": counts, "verified": True}


# ---- switch/rollback（重放完整性门＋切换清单＋丢写防护门）----

def _write_switch_manifest(home, head, txs) -> dict:
    """切换清单（spec switch）：journal head/tx 计数＋迁移报告指纹＋
    时间戳——非 canonical 运维元数据（journal 零触碰：清单写入不前移
    head/tx，rollback「未前移」判据不因此失真）；互换前落盘，失败＝
    零动作（尚未动土）。"""
    report = pathlib.Path(home) / REPORT_PATH
    try:
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
    except OSError:
        digest = None
    manifest = {"backend": BACKEND, "head": head, "txs": txs,
                "migration_report": {"path": REPORT_PATH, "sha256": digest},
                "switched_at_ms": epoch_ms(time.time())}
    _atomic_write_bytes(
        pathlib.Path(home) / SWITCH_MANIFEST,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                   indent=1).encode("utf-8"), private=True)
    return manifest


def switch(old, new):
    """git 切换（spec switch）：完成标记＋journal 在位＋零外来账本 →
    重放完整性门（构造重放：损毁对象/重复 seq/缺活 secret 任一＝零动作
    拒）→冒烟（任务面整读＋head/tx 计数）→兼容 checkpoint →切换清单
    →同级两步互换（cutover 同款失败恢复语义）。"""
    old, new = pathlib.Path(old), pathlib.Path(new)
    _require_dir(old, "old_home")
    _require_dir(new, "new_home")
    if old.parent.resolve() != new.parent.resolve():
        raise CutoverError("not_siblings",
                           "old/new 须同级目录（目录互换前提，设计 §9）")
    if not (new / MARKER).is_file():
        raise CutoverError(
            "new_home_incomplete",
            f"{new / MARKER}: 迁移完成标记缺席"
            "（先 migrate --backend git --apply）")
    foreign = [n for n in FOREIGN_LEDGERS
               if (new / "state" / n).exists()]
    if foreign:  # git 模式零 SQLite 件（外来账本在位＝异源混账）
        raise CutoverError(
            "foreign_ledger",
            f"{new / 'state'}: 外来账本在位 {foreign}——拒切换"
            "（防 split-brain；移走外来账本再切换）")
    _require_journal(new)
    try:
        st = GitStore(new / "state")  # 构造即重放：完整性门主面
    except StorageError as exc:
        raise CutoverError("integrity_gate_failed",
                           f"canonical 重放校验拒绝（零动作）: {exc}"
                           ) from exc
    try:
        try:
            smoke = len(st.tasks_all())  # 冒烟：任务面整读
            head, txs = st.head, st.tx_count
            st.checkpoint_truncate()  # 兼容面（ref 失联 fail-closed）
        except StorageError as exc:
            raise CutoverError("integrity_gate_failed",
                               f"冒烟/checkpoint 失败（拒互换）: {exc}"
                               ) from exc
    finally:
        st.close()
    manifest = _write_switch_manifest(new, head, txs)  # 互换前落盘
    report = _switch_swap(old, new)
    report["backend"] = BACKEND
    report["journal"] = {"head": head, "txs": txs}
    report["smoke_tasks"] = smoke
    report["manifest"] = manifest
    return report


def rollback(old, new):
    """git 回滚（spec rollback）：目录互换原语＋丢写防护门——当前
    journal head/tx 与切换清单不符（业务 tx 已前移）＝fail-closed 双侧
    目录零动作＋显式人工路径；清单缺席/坏形同拒（防丢锚点不在不猜）。"""
    old, new = pathlib.Path(old), pathlib.Path(new)
    swapped = old.with_name(old.name + SWAPPED_SUFFIX)
    _require_dir(old, "current_home")
    _require_dir(swapped, "swapped_old_home")
    if new.exists():
        raise CutoverError("rollback_target_exists",
                           f"{new}: v3 home 原名位非空（先清让）")
    path = old / SWITCH_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CutoverError(
            "switch_manifest_missing",
            f"{path}: 切换清单缺席/不可解析——拒绝无锚点回滚（防丢写）"
            f": {exc}") from exc
    _require_journal(old)
    try:
        st = GitStore(old / "state")
    except StorageError as exc:
        raise CutoverError("integrity_gate_failed",
                           f"canonical 重放校验拒绝（零动作）: {exc}"
                           ) from exc
    try:
        if (st.head != manifest.get("head")
                or st.tx_count != manifest.get("txs")):
            raise CutoverError(
                "rollback_blocked",
                f"journal 已前移（清单 {manifest.get('head')}/"
                f"{manifest.get('txs')} 对 当前 {st.head}/{st.tx_count}"
                "）——业务写入已在 git home，互换即丢写；双侧目录保持"
                "零动作；确需回滚：停服后手工互换目录（业务写入随 git "
                "home 目录走）", recoverable=False)
    finally:
        st.close()
    return _rollback_swap(old, new)
