"""Req: F156 — GitStore Phase 3 legacy 迁移（opencode-agora v3，spec
wiki/spec/gitstore-phase3-legacy-migration.md）。

v2 home → GitStore 目标 home 一次性导入：快照/消息/事件/凭据/游标的解析、
分类与字段映射全复用 SQLite 迁移器模块面（migrator），写入全部经 GitStore
仓库 UoW（一批一 tx 一 commit），不直接编辑 canonical 对象；源目录全程
只读。本切片只做导入——切换/回滚/生产入口归后续任务。

与 SQLite 迁移器的有意差异（spec 记录在案）：
①凭据：secret 文件（credentials/*.token＋.agora/root.token）先拷（0600）
  后 put——GitStore put 先验外部文件在位且摘要匹配（签发铁则）；缺/错
  文件＝中止（宁可不迁，不造重启即砖的活牌）；canonical 只含摘要＋home
  相对键位（明文不进任何 Git 对象/tx/报告）。无签发任务的投影 Git 保
  None／SQLite 读回 ""（F149 已记录的 None↻"" 读回不对称，差分面归
  allowlist，不在迁移器强扭）。
②任务行 gate_artifact.path 引用源 home 的：仅认 state/gate-artifacts
  子树（v2 ArtifactStore 权威布局）——证据随迁（符号链接不跟随）＋路径
  改写为目标 home 同位路径；子树外的家内引用不拷不改、计数申报（源可控
  路径不进目标写面）；源 home 之外的引用照迁不动。
③wiki 用户内容整树随迁（os.walk 不跟随符号链接；不只骨架）；配置照拷
  （同 SQLite 面）。
④目标重置（pre-marker 确定性重建幂等）：journal 目录＋随迁文件面＋
  wiki/证据＋隔离区/报告/标记；外来账本（agora.db/tasks.jsonl 在而
  journal 缺席）在一切重置动作之前即拒（防 split-brain，且不删外来件
  ——删了就洗掉证据）。完成标记（JSON：head/txs 快照）之后重跑：账本
  现状与标记一致＝零业务写、不活跃，可确定性重建；不一致＝活跃在役，
  fail-closed 拒（安全手工重建＝停服移走目标目录，见 spec §3，不做自动
  重置在役账本）。
⑤报告增 backend=git＋journal head/tx/object 计数＋wiki/证据随迁计数；
  完成标记在全部批次与报告落盘后最后写（failpoint batch_<名> 两相位，
  中途炸＝标记缺席）。
"""
import contextlib
import json
import os
import pathlib
import shutil
import stat

from agorad.gitstore import JOURNAL_DIRNAME, GitStore
from agorad.migrator import (CONFIG_FILES, MARKER, QUARANTINE_DIR,
                             REPORT_PATH, SKELETON_DIRS, LegacyMigrator,
                             MigrationError, _convert_cursors,
                             _load_snapshot, _normalize, _parse_events,
                             _parse_messages, _read_json)
from agorad.storage import StorageError

MIGRATION_ACTOR = "legacy-migration"  # journal tx 的 actor（审计面可分辨）
FOREIGN_LEDGERS = ("agora.db", "tasks.jsonl")


class GitLegacyMigrator(LegacyMigrator):
    """v2 home → GitStore 目标 home（源只读；pre-marker 重建幂等）。"""

    def migrate(self) -> dict:
        src_r, tgt_r = self._src.resolve(), self._tgt.resolve()
        if src_r == tgt_r or src_r.is_relative_to(tgt_r) \
                or tgt_r.is_relative_to(src_r):
            raise MigrationError("source 与 target 不得同目录或互相嵌套"
                                 "（源全程只读；嵌套则重置面可删源）")
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
        creds, cred_stats = self._map_credentials(tokens, cred_files, rows)
        self._refuse_foreign_or_live()
        self._remap_artifact_paths(rows)
        self._reset_target()
        store = self._open_journal(actor=MIGRATION_ACTOR)
        try:
            unknown = {}
            delivered, extra_fm = [], {}
            self._write_tasks(store, rows, unknown)
            _, replay_unmapped = self._write_messages(
                store, messages, delivered, extra_fm)
            self._write_events(store, events)
            secrets, root_kept = self._write_credentials(store, creds)
            cursors, duplicates, absent = _convert_cursors(mail_read,
                                                           delivered)
            self._fire("cursors", "before")
            try:
                with store.uow():
                    for sid, cursor in sorted(cursors.items()):
                        store.upsert_cursor(sid, cursor)
            except Exception as exc:  # 错误面与其余批次一致（评审环 B 册）
                raise MigrationError(f"游标批写入失败: {exc}") from exc
            self._fire("cursors", "after")
            config, wiki_files = self._copy_files()
            return self._finalize(
                store=store, rows=rows, unknown=unknown, messages=messages,
                msg_quar=msg_quar, events=events, ev_quar=ev_quar,
                extra_fm=extra_fm, replay_unmapped=replay_unmapped,
                tokens_unreadable=tokens_unreadable, cred_stats=cred_stats,
                config=config, secrets=secrets, root_kept=root_kept,
                cursors=cursors, duplicates=duplicates, absent=absent,
                dropped=dropped, wiki_files=wiki_files, creds=creds)
        finally:
            store.close()

    # ---- 目标闸（一切重置动作之前；spec §3/§7）----

    def _refuse_foreign_or_live(self):
        # 活性/外来检查＝快照非互斥（检查后到重置间的新提交会被重建抹除）；
        # 排他静止保证归生产接线任务（spec §3 停服手工前提）。
        state = self._tgt / "state"
        foreign = [n for n in FOREIGN_LEDGERS if (state / n).exists()]
        if foreign:  # 异源混账一律拒——先于一切重置，不删 journal 不洗白
            raise MigrationError(
                f"{state}: 外来账本在位 {foreign}——拒迁（防 split-brain；"
                "手工处置＝移走外来账本或换目标目录）")
        marker = self._tgt / MARKER
        if not marker.is_file():
            return  # 未完成/全新目标——pre-marker 确定性重建面
        try:
            snap = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError(
                f"完成标记不可解析（{marker}）——在役推定，拒重跑；"
                "安全手工重建＝停服移走目标目录") from exc
        store = self._open_journal()
        try:
            if (store.head != snap.get("head")
                    or store.tx_count != snap.get("txs")):
                raise MigrationError(
                    "目标已完成且账本有完成标记之外的变更（活跃在役）"
                    "——拒重跑；安全手工重建＝停服移走目标目录再迁")
        finally:
            store.close()

    def _open_journal(self, **kw) -> GitStore:
        try:
            return GitStore(self._tgt / "state", **kw)
        except StorageError as exc:  # ForeignLedger/Corruption/Secret 面
            raise MigrationError(f"目标 Git 账本不可开: {exc}") from exc

    def _reset_target(self):
        """pre-marker 重建清单：journal＋随迁面＋wiki/证据＋报告/标记；
        外来账本件不在清单（_refuse_foreign_or_live 已拒或留给构造闸）。"""
        state = self._tgt / "state"
        for path in (state / JOURNAL_DIRNAME, state / "gate-artifacts",
                     self._tgt / QUARANTINE_DIR, self._tgt / "wiki"):
            shutil.rmtree(path, ignore_errors=True)
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

    # ---- 批次（Git 特有面；tasks/messages/events 复用父类仓库协议）----

    def _write_credentials(self, store, creds):
        """spec §5：secret 先拷（0600）后 put——put 先验文件；缺/错即中止。"""
        self._fire("credentials", "before")
        secrets, root_kept = self._copy_secrets()
        with store.uow():
            for cred in creds:
                try:
                    store.put_credential(cred)  # task_id None＝无签发任务
                except Exception as exc:
                    raise MigrationError(
                        f"凭据 {cred.sid} 记账失败（外部 secret 缺席/不符"
                        f"即中止，不造重启即砖活牌）: {exc}",
                        {"sid": cred.sid}) from exc
        self._fire("credentials", "after")
        return secrets, root_kept

    def _copy_files(self):
        """spec §6：配置照拷＋wiki 用户内容整树＋gate 证据随迁。"""
        self._fire("files", "before")
        config = self._copy_configs()
        wiki_files = self._copy_wiki()
        self._copy_artifacts()
        self._fire("files", "after")
        return config, wiki_files

    def _copy_wiki(self):
        src = self._src / "wiki"
        if not src.is_dir():
            return 0
        count = 0
        # followlinks=False＋符号链接跳过：源 wiki 可控面不引入链接目标
        # 内容、环状链接不死循环（评审环 C 册）
        for root, dirs, files in os.walk(src, followlinks=False):
            dirs[:] = sorted(d for d in dirs
                             if not (pathlib.Path(root) / d).is_symlink())
            dst_root = self._tgt / "wiki" / pathlib.Path(root).relative_to(
                src)
            dst_root.mkdir(parents=True, exist_ok=True)
            os.chmod(dst_root, 0o700)
            for name in sorted(files):
                s = pathlib.Path(root) / name
                if s.is_symlink():
                    continue
                dst = dst_root / name
                shutil.copy2(s, dst)
                os.chmod(dst, stat.S_IMODE(s.stat().st_mode))
                count += 1
        return count

    # ---- gate 证据改写＋随迁（spec §6）----

    def _remap_artifact_paths(self, rows):
        """引用源 home 的 gate_artifact.path：仅 state/gate-artifacts 子树
        （v2 ArtifactStore 权威布局）改写为目标同位路径＋证据随迁——子树
        外的家内引用不拷不改（源可控路径可把任意源目录盖进目标，评审环
        C 册），如实计数申报；家外引用照迁不动。纯串改写确定性。"""
        prefixes = sorted({str(self._src), str(self._src.resolve())},
                          key=len, reverse=True)
        self._art_evidence, self._art_stats = [], {"remapped": 0,
                                                   "copied": 0, "missing": 0,
                                                   "in_home_untouched": 0}
        for tid in sorted(rows):
            art = rows[tid].get("gate_artifact")
            if not isinstance(art, dict) or not isinstance(art.get("path"),
                                                           str):
                continue
            path = art["path"]
            for prefix in prefixes:
                subtree = prefix + os.sep + os.path.join(
                    "state", "gate-artifacts")
                if path == subtree or path.startswith(subtree + os.sep):
                    rel = os.path.relpath(path, prefix)
                    if rel == "." or ".." in pathlib.PurePath(rel).parts:
                        # 词法前缀可被 ".." 绕过（评审环 R2）——按子树外处理
                        self._art_stats["in_home_untouched"] += 1
                        break
                    art["path"] = str(self._tgt / rel)
                    self._art_stats["remapped"] += 1
                    self._art_evidence.append((path, art["path"]))
                    break
                if path == prefix or path.startswith(prefix + os.sep):
                    self._art_stats["in_home_untouched"] += 1
                    break

    def _copy_artifacts(self):
        for src_path, dst_path in self._art_evidence:
            src, dst = pathlib.Path(src_path), pathlib.Path(dst_path)
            if src.is_symlink():  # 符号链接证据不跟随（评审环 C 册）
                self._art_stats["missing"] += 1
                continue
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True,
                                    symlinks=True)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst, follow_symlinks=False)
                else:  # 源侧本就缺席（v2 悬空投影）——改写如实计数不阻塞
                    self._art_stats["missing"] += 1
                    continue
                self._art_stats["copied"] += 1
            except OSError as exc:
                raise MigrationError(
                    f"gate 证据随迁失败（{src_path}）: {exc}") from exc

    # ---- 收口（spec §8）----

    def _finalize(self, store, **parts):
        self._fire("finalize", "before")
        manifest = {"messages": parts["msg_quar"], "events": parts["ev_quar"]}
        self._write_private(self._tgt / QUARANTINE_DIR / "manifest.json",
                            json.dumps(manifest, ensure_ascii=False,
                                       indent=1))
        sizes = {"tasks": len(parts["rows"]),
                 "messages": len(parts["messages"]) * 2,  # meta.json＋body.txt
                 "events": len(parts["events"]),
                 "credentials": len(parts["creds"]),
                 "cursors": len(parts["cursors"])}
        txs = store.tx_count  # 盘面真值（评审环 B 册）——免批次对账假设
        report = self._report(**parts)
        report["backend"] = "git"
        report["journal"] = {"head": store.head, "txs": txs,
                             "objects": sizes | {
                                 "total": sum(sizes.values()) + txs}}
        report["wiki"] = {"files_copied": parts["wiki_files"]}
        report["gate_artifacts"] = dict(self._art_stats)
        self._write_private(self._tgt / REPORT_PATH,
                            json.dumps(report, ensure_ascii=False, indent=1,
                                       sort_keys=True))
        self._fire("finalize", "after")
        self._write_private(  # 标记＝head/txs 快照（活性判据）——最后落盘
            self._tgt / MARKER,
            json.dumps({"complete": True, "backend": "git",
                        "head": store.head, "txs": txs}, sort_keys=True))
        return report
