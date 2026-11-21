"""Req: F156 — GitStore Phase 3 legacy 迁移（opencode-agora v3，spec
wiki/spec/gitstore-phase3-legacy-migration.md）。

断言清单（spec「Tests」逐条机械判定）：
TestGitLegacyMigration——①全字段/十态矩阵导入且重启重放投影等价；②消息
（字节体/文件名序/撕裂重编号 replay 改写）；③事件 seq/payload；④游标转换
（min(pending)-1/无记录不建行）；⑤凭据：secret 先拷后记、0600、canonical
只含摘要＋home 相对键位、task_id 投影与 SQLite 对齐（""）；⑥配置/wiki 用户
内容/gate 证据随迁＋旧 home 路径改写；⑦报告 backend=git＋journal head/tx/
object 计数（对账 GitJournalObserver 盘面真值）；⑧标记 JSON 最后落盘。
TestGitMigrationDifferential——同一 legacy 源双后端导入，tasks/messages/
events/cursors/credentials 归一化语义叶子级相等（差异须命中显式 allowlist
——event task None↻"" 读回不对称，F149 已记录）；Git-only：零 agora.db、
journal 在位、backend 字段差异。
TestGitMigrationSafety——源指纹成功/各 failpoint 失败均不变；v2 可加载
记录失败中止；secret 缺席 fail-closed（不造重启即砖活牌）；撕裂件隔离不
阻塞；外来账本拒；完成目标活跃拒重跑/不活跃幂等重建；全 Git 对象/tx/
报告零明文 token；零 SQLite 件。
"""
import dataclasses
import json
import os
import stat
import unittest
from pathlib import Path

from agorad import domain
from agorad.gitmigrate import GitLegacyMigrator
from agorad.gitstore import GitStore
from agorad.migrator import (BATCHES, CONFIG_FILES, MARKER, QUARANTINE_DIR,
                             REPORT_PATH, LegacyMigrator, MigrationError)
from agorad.storage import Storage
from tests import v3_harness as harness
from tests.test_v3_backend_diff import _canon, _leaf_diff

TOK_ROOT, TOK_W, TOK_D = "r" * 32, "w" * 32, "d" * 32


def git_migrate(source, target, **kw):
    return GitLegacyMigrator(Path(source), Path(target), **kw).migrate()


def _write_msg_file(home, name, mid, ts, task, kind, frm, to, body, **extra):
    fm = {"id": mid, "ts": ts, "task": task, "kind": kind, "from": frm,
          "to": to, **extra}
    head = "".join(f"{k}: {fm[k]}\n" for k in sorted(fm))
    (home.messages / name).write_text(f"{head}\n{body}", encoding="utf-8")


def _project(st, sids=()):
    """迁移终态归一化投影（双后端公共读面）。"""
    return {
        "tasks": [dataclasses.asdict(t) for t in st.tasks_all()],
        "messages": [dataclasses.asdict(m) for m in st.messages_all()],
        "events": [dataclasses.asdict(e) for e in st.events_all()],
        "cursors": [None if st.get_cursor(s) is None
                    else dataclasses.asdict(st.get_cursor(s)) for s in sids],
        "credentials": [dataclasses.asdict(c) for c in st.credentials_all()],
    }


def _observer(state):
    return harness.GitJournalObserver(state)


def _all_git_text(state):
    """journal 全对象文本面：树 blob＋commit 对象（含 commit message）。"""
    obs = _observer(state)
    chunks = [obs.blob(sha) for sha in obs.paths().values()]
    chunks += [obs._git("cat-file", "commit", c)[1] for c in obs.commits()]
    return b"\n".join(chunks)


def _cred_objects(state):
    """canonical 凭据注册面（envelope 的 data 面）。"""
    obs = _observer(state)
    out = []
    for path, sha in obs.paths().items():
        if path.startswith("objects/credentials/"):
            obj = json.loads(obs.blob(sha).decode("utf-8"))
            out.append((path, obj["data"]))
    return out


class _GitCase(unittest.TestCase):
    """源/目标各一 TempAgoraHome；源指纹锚定（只读证明）。"""

    def setUp(self):
        self.src = harness.TempAgoraHome()
        self.addCleanup(self.src.cleanup)
        self.tgt = harness.TempAgoraHome()
        self.addCleanup(self.tgt.cleanup)
        self.before = None

    def freeze(self):
        self.before = harness.digest_tree(self.src.path)

    def assertSourceUntouched(self):
        self.assertEqual(harness.digest_tree(self.src.path), self.before)

    def run_migrate(self, **kw):
        self.freeze()
        return GitLegacyMigrator(Path(self.src.path),
                                 Path(self.tgt.path), **kw).migrate()

    def open_git(self, **kw):
        st = GitStore(self.tgt.state, **kw)
        self.addCleanup(st.close)
        return st

    def seed_credentials(self):
        """root/worker/dispatcher 三活牌＋secret 文件齐备＋孤儿件。"""
        row = harness.full_field_row(tid="gt-1", state="running",
                                     worker_sid="ses-w1")
        harness._append_snapshot(self.src, {"gt-1": row})
        (self.src.path / "tokens").write_text(json.dumps({
            TOK_ROOT: ["root", "root"],
            TOK_W: ["ses-w1", "worker",
                    [f"s-old{i}" for i in range(20)]],
            TOK_D: ["ses-d1", "dispatcher"]}), encoding="utf-8")
        self.src.write_credential("gt-1", TOK_W)
        self.src.write_credential("ses-d1", TOK_D)
        self.src.write_credential("gt-zombie", "orphan-token-xyz")
        root = self.src.agora / "root.token"
        root.write_text(json.dumps({"token": TOK_ROOT}), encoding="utf-8")
        os.chmod(root, 0o600)


class TestGitLegacyMigration(_GitCase):
    def test_full_fields_and_state_matrix_restart_safe(self):
        row = harness.full_field_row()
        matrix = harness.task_state_matrix(self.src)  # 行1（10 态）
        harness._append_snapshot(self.src, matrix | {"gt-full": row})  # 末行胜出
        report = self.run_migrate()
        first = self.open_git()
        t = first.get_task("gt-full")
        for key in ("title", "goal", "req_id", "project_dir", "target_branch",
                    "env_dir", "commit", "rebased_from", "env_base",
                    "dispatcher_sid", "worker_sid", "root_sid", "state"):
            self.assertEqual(getattr(t, key), row[key], key)
        self.assertEqual(t.verify, domain.VerifySpec(
            cmd=row["verify"]["cmd"], state=row["verify"]["state"],
            registered_by=row["verify"]["registered_by"],
            registered_at_ms=domain.epoch_ms(row["verify"]["registered_at"]),
            timeout_s=row["verify"]["timeout"],
            timeout_source=row["verify"]["timeout_source"]))
        self.assertEqual(t.delivery, domain.DeliveryState(
            mode="managed", status="pending", commit=None, base=None,
            target_branch="main", attempt=0, local_status="pending",
            error=None, conflict_files=None))
        self.assertEqual(json.loads(t.legacy_extra_json),
                         {"usage": row["usage"],
                          "gate_origin_branch": row["gate_origin_branch"]})
        for tid, mrow in matrix.items():
            self.assertEqual(first.get_task(tid).state, mrow["state"])
        self.assertEqual(report["tasks"]["total"], 11)
        snapshot = _project(first)
        first.close()
        reopened = self.open_git()  # 重启重放投影等价（restart-safe）
        self.assertEqual(_project(reopened), snapshot)

    def test_messages_bytewise_order_and_replay_renumbered(self):
        harness._append_snapshot(
            self.src, {"gt-r": harness._matrix_row("gt-r", "running")})
        _write_msg_file(self.src, "00000002.md", 2, 10.0, "gt-r", "note",
                        "w", "d", "bødý-中文")
        _write_msg_file(self.src, "00000005.md", 5, 20.0, "gt-r", "note",
                        "w", "d", "copy", replay_of=2)
        report = self.run_migrate()
        msgs = self.open_git().messages_all()
        self.assertEqual([m.id for m in msgs], [1, 2])  # 文件名序→新 id 序
        self.assertEqual(msgs[0].body,
                         (self.src.messages / "00000002.md").read_bytes()
                         .partition(b"\n\n")[2])  # 逐字节 BLOB
        self.assertEqual(msgs[1].replay_of, 1)  # replay_of=2 随重编号改写
        self.assertEqual(report["messages"]["count"], 2)

    def test_events_seq_and_payload(self):
        harness.legacy_torn(self.src)
        (self.src.path / "events.jsonl").write_text(
            json.dumps({"ts": 1.5, "actor": "root", "event": "enforce_on",
                        "task": "gt-torn-pre", "note": "n1"}) + "\n"
            + json.dumps({"ts": 2.5, "actor": "a", "event": "running",
                          "task": "gt-torn-pre"}) + "\n", encoding="utf-8")
        report = self.run_migrate()
        events = self.open_git().events_all()
        self.assertEqual([e.seq for e in events], [1, 2])  # 撕裂行隔离后连续
        self.assertEqual(events[0].ts_ms, domain.epoch_ms(1.5))
        self.assertEqual((events[0].actor, events[0].event),
                         ("root", "enforce_on"))
        self.assertEqual(events[0].detail, {"note": "n1"})
        self.assertEqual(report["events"]["count"], 2)

    def test_cursor_conversion(self):
        info = harness.mail_lineage(self.src)
        old_sid, new_sid = info["sids"]
        report = self.run_migrate()
        st = self.open_git()
        self.assertEqual(st.get_cursor(old_sid).last_read_ms, 1)  # min-1
        self.assertIsNone(st.get_cursor(new_sid))  # 无记录不建行
        self.assertEqual(report["cursors"]["possible_duplicate_delivery"], 0)
        self.assertEqual(report["cursors"]["absent_sids"], 1)

    def test_credentials_secret_copied_digest_only(self):
        self.seed_credentials()
        report = self.run_migrate()
        st = self.open_git()
        worker = st.get_credential("ses-w1")
        self.assertEqual((worker.token, worker.task_id), (TOK_W, "gt-1"))
        self.assertEqual(worker.lineage,
                         tuple(f"s-old{i}" for i in range(4, 20)))  # ≤16
        self.assertEqual(st.get_credential("root").token, TOK_ROOT)
        disp = st.get_credential("ses-d1")
        self.assertEqual((disp.token, disp.task_id),
                         (TOK_D, None))  # None↻""：Git 保 None（F149 已记录）
        for name, token in (("gt-1", TOK_W), ("ses-d1", TOK_D),
                            ("gt-zombie", "orphan-token-xyz")):
            copied = self.tgt.credentials / f"{name}.token"
            self.assertEqual(
                json.loads(copied.read_text(encoding="utf-8"))["token"],
                token, name)
            self.assertEqual(stat.S_IMODE(os.stat(copied).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(
            os.stat(self.tgt.agora / "root.token").st_mode), 0o600)
        # canonical 只含摘要＋home 相对键位＋注册事实（无明文）
        creds = _cred_objects(self.tgt.state)
        self.assertEqual(len(creds), 3)
        for _path, data in creds:
            self.assertLessEqual(set(data), {"token_sha256", "secret_path",
                                             "task_id", "lineage",
                                             "created_ms"})
            self.assertFalse(data["secret_path"].startswith("/"))
        stats = report["credentials"]
        self.assertEqual((stats["root"], stats["worker"],
                          stats["dispatcher"], stats["secret_files_copied"]),
                         (1, 1, 1, 3))

    def test_config_wiki_and_artifact_evidence(self):
        art = self.src.state / "gate-artifacts" / "gt-art" / \
            "20260101T000000-verify-0001"
        art.mkdir(parents=True)
        (art / "output.log").write_text("out\n", encoding="utf-8")
        (art / "meta.json").write_text("{}\n", encoding="utf-8")
        row = harness.full_field_row(
            tid="gt-art", gate_artifact={"path": str(art), "phase": "verify",
                                         "rc": 0})
        # 单行快照（末行胜出）：gt-art 家内证据＋gt-full 家外引用同迁
        harness._append_snapshot(
            self.src, {"gt-art": row,
                       "gt-full": harness.full_field_row()})
        (self.src.wiki / "tasks").mkdir(parents=True, exist_ok=True)
        (self.src.wiki / "tasks" / "gt-art.md").write_text(
            "# 页\n", encoding="utf-8")
        (self.src.wiki / "drafts").mkdir(parents=True, exist_ok=True)
        (self.src.wiki / "drafts" / "note.md").write_text("d\n",
                                                          encoding="utf-8")
        for name in CONFIG_FILES:
            path = self.src.agora / name
            path.write_text(f"# {name}\n", encoding="utf-8")
            os.chmod(path, 0o600)
        report = self.run_migrate()
        st = self.open_git()
        self.assertEqual(st.get_task("gt-art").gate.gate_artifact.path,
                         str(self.tgt.state / "gate-artifacts" / "gt-art" /
                             "20260101T000000-verify-0001"))  # 旧 home 改写
        moved = self.tgt.state / "gate-artifacts" / "gt-art" / \
            "20260101T000000-verify-0001"
        self.assertEqual((moved / "output.log").read_text(encoding="utf-8"),
                         "out\n")
        self.assertTrue((moved / "meta.json").exists())
        self.assertEqual(  # 家外引用不改写
            st.get_task("gt-full").gate.gate_artifact.path,
            "/h/gate/gt-full/output.log")
        self.assertEqual(
            (self.tgt.wiki / "tasks" / "gt-art.md").read_text(encoding="utf-8"),
            "# 页\n")
        self.assertTrue((self.tgt.wiki / "drafts" / "note.md").exists())
        for name in CONFIG_FILES:
            self.assertEqual(
                stat.S_IMODE(os.stat(self.tgt.agora / name).st_mode),
                0o600, name)
        self.assertEqual(report["config"]["agorad.env"], "copied")
        self.assertEqual(report["wiki"], {"files_copied": 2})
        self.assertEqual(report["gate_artifacts"],
                         {"remapped": 1, "copied": 1, "missing": 0,
                          "in_home_untouched": 0})

    def test_artifact_escape_paths_untouched_reported(self):
        """评审环 C 册 R1：子树外家内引用不拷不改（防 copytree 反噬）；
        子树内符号链接证据不跟随。"""
        art = self.src.state / "gate-artifacts" / "gt-ok" / \
            "20260101T000000-verify-0001"
        art.mkdir(parents=True)
        (art / "output.log").write_text("ok\n", encoding="utf-8")
        outsider = self.src.path / "outside"
        outsider.mkdir()
        (outsider / "loot.txt").write_text("sensitive\n", encoding="utf-8")
        ln_dir = self.src.state / "gate-artifacts" / "gt-ln"
        ln_dir.mkdir(parents=True)
        (ln_dir / "dir").symlink_to(outsider, target_is_directory=True)
        victim = self.src.credentials / "secret.token"  # 家内子树外敏感件
        victim.write_text('{"token": "x"}\n', encoding="utf-8")
        rows = {"gt-ok": harness.full_field_row(
                    tid="gt-ok", gate_artifact={"path": str(art),
                                                "phase": "verify", "rc": 0}),
                "gt-root": harness.full_field_row(
                    tid="gt-root", gate_artifact={"path": str(self.src.path),
                                                  "phase": "verify", "rc": 0}),
                "gt-cred": harness.full_field_row(
                    tid="gt-cred", gate_artifact={"path": str(victim),
                                                  "phase": "verify", "rc": 0}),
                "gt-ln": harness.full_field_row(
                    tid="gt-ln", gate_artifact={"path": str(ln_dir / "dir"),
                                                "phase": "verify", "rc": 0})}
        harness._append_snapshot(self.src, rows)
        report = self.run_migrate()
        st = self.open_git()
        self.assertTrue(str(self.tgt.path) in
                        st.get_task("gt-ok").gate.gate_artifact.path)
        self.assertEqual(st.get_task("gt-root").gate.gate_artifact.path,
                         str(self.src.path))  # 不改写不拷贝
        self.assertEqual(st.get_task("gt-cred").gate.gate_artifact.path,
                         str(victim))
        self.assertFalse((self.tgt.state / "tasks.jsonl").exists())
        self.assertFalse((self.tgt.path / "outside").exists())  # 零反噬
        self.assertTrue(victim.exists())  # 源件原封
        self.assertFalse((self.tgt.state / "gate-artifacts" / "gt-ln" /
                          "dir" / "loot.txt").exists())  # 链接不跟随
        self.assertEqual(report["gate_artifacts"],
                         {"remapped": 2, "copied": 1, "missing": 1,
                          "in_home_untouched": 2})

    def test_wiki_symlinks_not_followed(self):
        """评审环 C 册 R1：源 wiki 符号链接目录/文件不跟随不计数。"""
        harness.task_full_fields(self.src)
        outside = self.src.agora / "outside.txt"
        outside.write_text("secret-ish\n", encoding="utf-8")
        (self.src.wiki / "tasks").mkdir()
        (self.src.wiki / "real.md").write_text("r\n", encoding="utf-8")
        (self.src.wiki / "tasks" / "real2.md").write_text("r2\n",
                                                          encoding="utf-8")
        (self.src.wiki / "linked-dir").symlink_to(self.src.agora,
                                                  target_is_directory=True)
        (self.src.wiki / "linked.md").symlink_to(outside)
        report = self.run_migrate()
        self.assertTrue((self.tgt.wiki / "real.md").exists())
        self.assertTrue((self.tgt.wiki / "tasks" / "real2.md").exists())
        self.assertFalse((self.tgt.wiki / "linked-dir").exists())
        self.assertFalse((self.tgt.wiki / "linked.md").exists())
        self.assertFalse((self.tgt.wiki / "linked-dir" / "agorad.env")
                         .exists())
        self.assertEqual(report["wiki"], {"files_copied": 2})

    def test_nested_source_target_rejected(self):
        """评审环 C 册 R1：src/tgt 互相嵌套即拒（重置面不得删源）。"""
        harness.task_full_fields(self.src)
        nested = harness.TempAgoraHome(root=self.src.path)  # tgt⊂src
        self.addCleanup(nested.cleanup)
        self.freeze()  # 嵌套目录自身建好后取基线
        with self.assertRaises(MigrationError):
            git_migrate(self.src.path, nested.path)
        with self.assertRaises(MigrationError):
            git_migrate(nested.path, self.src.path)
        self.assertSourceUntouched()

    def test_report_and_marker_shape(self):
        self.seed_credentials()
        _write_msg_file(self.src, "00000001.md", 1, 10.0, "gt-1", "note",
                        "w", "ses-w1", "hi")
        (self.src.path / "events.jsonl").write_text(
            json.dumps({"ts": 1.0, "actor": "a", "event": "created",
                        "task": "gt-1"}) + "\n", encoding="utf-8")
        harness._append_snapshot(self.src, {"gt-1": harness._matrix_row(
            "gt-1", "running", worker_sid="ses-w1")},
            mail_read={"ses-w1": 0.0})
        report = self.run_migrate()
        self.assertEqual(report, json.loads(
            (self.tgt.path / REPORT_PATH).read_text(encoding="utf-8")))
        self.assertEqual(report["backend"], "git")
        obs = _observer(self.tgt.state)
        self.assertEqual(report["journal"]["head"], obs.head())
        self.assertEqual(report["journal"]["txs"], len(obs.txs()))
        self.assertEqual(report["journal"]["objects"]["total"],
                         len(obs.paths()))  # 计数对账盘面真值
        self.assertEqual(report["journal"]["objects"]["tasks"], 1)
        self.assertEqual(report["journal"]["objects"]["messages"], 2)
        marker = json.loads(
            (self.tgt.path / MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker, {"complete": True, "backend": "git",
                                  "head": obs.head(),
                                  "txs": len(obs.txs())})

    def test_fault_at_finalize_writes_no_marker(self):
        harness.task_full_fields(self.src)
        injector = harness.FaultInjector()
        for batch in BATCHES:
            injector.register(f"batch_{batch}")
        injector.arm("batch_finalize", "after")
        with self.assertRaises(harness.FaultInjected):
            git_migrate(self.src.path, self.tgt.path, injector=injector)
        self.assertFalse((self.tgt.path / MARKER).exists())
        self.run_migrate()  # 重跑收敛
        self.assertTrue((self.tgt.path / MARKER).exists())


# ---- 差分（spec「Tests」2）----

EVENT_TASK_REASON = ("SQLite events_all 读回 None→''（r[4] or ''），"
                     "GitStore 保 None——同义「非任务事件」（F149 已记录）")
CRED_TASK_REASON = ("SQLite get_credential 读回 None→''（_null 逆映射不"
                    "对称），GitStore 保 None——同义「无签发任务」"
                    "（F149 已记录）")
DIFF_ALLOWED = {"state:events[*].task": EVENT_TASK_REASON,
                "state:credentials[*].task_id": CRED_TASK_REASON}


class _DiffSource(_GitCase):
    """同一 legacy 源双后端导入（差分与后端差异共用语料）。"""

    def build_source(self):
        row = harness.full_field_row(tid="gt-full", worker_sid="ses-w1")
        harness._append_snapshot(self.src, {
            "gt-full": row,
            "gt-r0": harness._matrix_row("gt-r0", "running"),
            "gt-done": harness._matrix_row("gt-done", "done")},
            mail_read={"ses-c": 150.0})
        _write_msg_file(self.src, "00000001.md", 1, 50.0, "gt-full", "note",
                        "w", "ses-c", "read", repo="/repo")
        _write_msg_file(self.src, "00000002.md", 2, 300.0, "gt-full", "note",
                        "w", "ses-c", "pending")
        (self.src.path / "events.jsonl").write_text(
            json.dumps({"ts": 1.0, "actor": "a", "event": "task_created",
                        "task": "gt-full", "k": [1]}) + "\n"
            + json.dumps({"ts": 2.0, "actor": "root",
                          "event": "enforce_on"}) + "\n", encoding="utf-8")
        (self.src.path / "tokens").write_text(json.dumps({
            TOK_ROOT: ["root", "root"],
            TOK_W: ["ses-w1", "worker", ["s-old0"]],
            TOK_D: ["ses-d1", "dispatcher"]}), encoding="utf-8")
        self.src.write_credential("gt-full", TOK_W)
        self.src.write_credential("ses-d1", TOK_D)
        (self.src.agora / "root.token").write_text(
            json.dumps({"token": TOK_ROOT}), encoding="utf-8")

    def migrate_both(self):
        sql = harness.TempAgoraHome()
        self.addCleanup(sql.cleanup)
        report_sql = LegacyMigrator(Path(self.src.path),
                                    Path(sql.path)).migrate()
        report_git = self.run_migrate()
        st_sql = Storage(sql.state / "agora.db")
        self.addCleanup(st_sql.close)
        st_git = self.open_git()
        return (st_sql, st_git), (report_sql, report_git)


class TestGitMigrationDifferential(_DiffSource):
    def test_normalized_semantics_equal(self):
        self.build_source()
        self.freeze()
        (st_sql, st_git), _reports = self.migrate_both()
        sids = ("ses-c", "ses-w1", "ses-d1", "root")
        raw = []
        for family, a in _project(st_sql, sids).items():
            _leaf_diff(a, _project(st_git, sids)[family],
                       f"state:{family}", raw)
        mismatches, suppressed = [], []
        for path, x, y in raw:
            entry = (_canon(path), repr(x)[:120], repr(y)[:120])
            (suppressed if entry[0] in DIFF_ALLOWED
             else mismatches).append(entry)
        self.assertEqual(mismatches, [])
        self.assertEqual([e[0] for e in suppressed],  # 恰两条已知差异家族
                         ["state:events[*].task",
                          "state:credentials[*].task_id",
                          "state:credentials[*].task_id"])

    def test_documented_backend_differences(self):
        self.build_source()
        (st_sql, st_git), (rep_sql, rep_git) = self.migrate_both()
        for name in ("agora.db", "agora.db-wal", "agora.db-shm"):
            self.assertFalse((self.tgt.state / name).exists(), name)
        self.assertTrue((self.tgt.state / "journal.git").is_dir())
        self.assertEqual(rep_git["backend"], "git")
        self.assertNotIn("backend", rep_sql)
        # 同源报告计数一致（差异只在 Git 附加面）
        self.assertEqual(rep_git["messages"], rep_sql["messages"])
        self.assertEqual(rep_git["tasks"]["by_state"],
                         rep_sql["tasks"]["by_state"])
        self.assertIn("journal", rep_git)
        self.assertNotIn("journal", rep_sql)


class TestGitMigrationSafety(_GitCase):
    def test_source_untouched_on_success(self):
        harness.legacy_torn(self.src)
        self.seed_credentials()
        for name in CONFIG_FILES:
            (self.src.agora / name).write_text("x\n", encoding="utf-8")
        (self.src.wiki / "tasks").mkdir(parents=True, exist_ok=True)
        (self.src.wiki / "tasks" / "a.md").write_text("p\n", encoding="utf-8")
        self.run_migrate()
        self.assertSourceUntouched()
        self.assertTrue((self.tgt.path / MARKER).exists())

    def test_fault_at_any_batch_point_aborts_and_rerun_converges(self):
        harness.legacy_torn(self.src)
        (self.src.path / "tokens").write_text(
            json.dumps({"z" * 32: ["root", "root"]}), encoding="utf-8")
        (self.src.agora / "root.token").write_text(
            json.dumps({"token": "z" * 32}), encoding="utf-8")
        (self.src.agora / "agorad.env").write_text("K=V\n", encoding="utf-8")
        self.freeze()
        for name in BATCHES:
            for phase in ("before", "after"):
                injector = harness.FaultInjector()
                for batch in BATCHES:
                    injector.register(f"batch_{batch}")
                injector.arm(f"batch_{name}", phase)
                with self.assertRaises(harness.FaultInjected, msg=name):
                    git_migrate(self.src.path, self.tgt.path,
                                injector=injector)
                self.assertFalse((self.tgt.path / MARKER).exists(),
                                 f"{name}/{phase}")
                self.assertSourceUntouched()
                git_migrate(self.src.path, self.tgt.path)  # 重跑收敛
                self.assertTrue((self.tgt.path / MARKER).exists(),
                                f"{name}/{phase}")

    def test_v2_loadable_failure_aborts(self):
        harness._append_snapshot(
            self.src, {"gt-n": harness._matrix_row("gt-n", "running")})
        _write_msg_file(self.src, "00000001.md", 1, float("nan"), "gt-n",
                        "note", "w", "d", "x")
        with self.assertRaises(MigrationError):
            self.run_migrate()
        self.assertSourceUntouched()
        self.assertFalse((self.tgt.path / MARKER).exists())

    def test_missing_secret_aborts_fail_closed(self):
        harness.task_full_fields(self.src)
        (self.src.path / "tokens").write_text(  # dispatcher 无 secret 文件
            json.dumps({TOK_D: ["ses-d1", "dispatcher"]}), encoding="utf-8")
        with self.assertRaises(MigrationError) as cm:
            self.run_migrate()
        self.assertIn("ses-d1", str(cm.exception))
        self.assertSourceUntouched()
        self.assertFalse((self.tgt.path / MARKER).exists())
        self.assertEqual(_cred_objects(self.tgt.state), [])  # 零凭据对象

    def test_torn_records_quarantined_not_blocking(self):
        harness.legacy_torn(self.src)
        report = self.run_migrate()
        manifest = json.loads(
            (self.tgt.path / QUARANTINE_DIR / "manifest.json")
            .read_text(encoding="utf-8"))
        self.assertEqual([q["file"] for q in manifest["messages"]],
                         ["00000001.md"])
        self.assertIn("tas", manifest["messages"][0]["content"])
        self.assertEqual(len(manifest["events"]), 1)
        self.assertEqual(report["quarantine"], {"messages": 1, "events": 1})
        self.assertIsNotNone(self.open_git().get_task("gt-torn-pre"))

    def test_foreign_ledger_refused(self):
        harness.task_full_fields(self.src)
        self.freeze()
        for name in ("agora.db", "tasks.jsonl"):
            with self.subTest(name=name):
                self.tgt = harness.TempAgoraHome()
                self.addCleanup(self.tgt.cleanup)
                (self.tgt.state / name).write_bytes(b"foreign")
                with self.assertRaises(MigrationError):
                    git_migrate(self.src.path, self.tgt.path)
                self.assertTrue((self.tgt.state / name).exists())  # 零洗白
                self.assertFalse((self.tgt.state / "journal.git").is_dir())
                self.assertFalse((self.tgt.path / MARKER).exists())
                self.assertSourceUntouched()

    def test_foreign_ledger_refused_even_with_incomplete_journal(self):
        """评审环 B 册 R1：journal 在而外来件也在（异源残留）——一律先拒，
        不得先删 journal 后在构造期才拒。"""
        harness.task_full_fields(self.src)
        injector = harness.FaultInjector()  # 半途炸出无标记 journal
        for batch in BATCHES:
            injector.register(f"batch_{batch}")
        injector.arm("batch_tasks", "after")
        with self.assertRaises(harness.FaultInjected):
            git_migrate(self.src.path, self.tgt.path, injector=injector)
        self.assertTrue((self.tgt.state / "journal.git").is_dir())
        (self.tgt.state / "agora.db").write_bytes(b"foreign")
        self.freeze()
        with self.assertRaises(MigrationError):
            git_migrate(self.src.path, self.tgt.path)
        self.assertTrue((self.tgt.state / "journal.git").is_dir())  # 未被删
        self.assertTrue((self.tgt.state / "agora.db").exists())
        self.assertSourceUntouched()

    def test_active_completed_target_refuses_rerun(self):
        harness.task_full_fields(self.src)
        self.run_migrate()
        head_before = _observer(self.tgt.state).head()
        biz = GitStore(self.tgt.state, actor="app")  # 模拟迁移后业务写
        with biz.uow():
            biz.insert_message(ts_ms=1, task_id="gt-full", kind="note",
                               from_sid="app", to_sid="disp", body="biz")
        biz.close()
        head_biz = _observer(self.tgt.state).head()
        with self.assertRaises(MigrationError) as cm:
            self.run_migrate()
        self.assertIn("活跃", str(cm.exception))
        self.assertEqual(_observer(self.tgt.state).head(), head_biz)
        self.assertTrue((self.tgt.path / MARKER).exists())  # 拒绝零动土

    def test_inactive_completed_target_rebuilds_idempotent(self):
        harness.legacy_torn(self.src)
        self.run_migrate()
        first = _project(self.open_git())
        report = self.run_migrate()  # 完成但零业务——确定性重建
        self.assertTrue(report["ok"])
        self.assertEqual(_project(self.open_git()), first)
        marker = json.loads(
            (self.tgt.path / MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["head"], _observer(self.tgt.state).head())
        self.assertSourceUntouched()

    def test_no_plaintext_token_in_objects_report_or_log(self):
        self.seed_credentials()
        self.run_migrate()
        blob = _all_git_text(self.tgt.state)  # 树 blob＋tx＋commit message
        report = json.dumps(json.loads(
            (self.tgt.path / REPORT_PATH).read_text(encoding="utf-8")))
        surface = blob + report.encode() + \
            (self.tgt.path / MARKER).read_bytes()
        for tok in (TOK_ROOT, TOK_W, TOK_D, "orphan-token-xyz"):
            self.assertNotIn(tok.encode(), surface)
        creds = _cred_objects(self.tgt.state)  # 扫描面有判别力：摘要在场
        self.assertTrue(any(d["token_sha256"] for _p, d in creds))

    def test_no_sqlite_artifacts(self):
        harness.task_full_fields(self.src)
        self.run_migrate()
        for name in ("agora.db", "agora.db-wal", "agora.db-shm"):
            self.assertFalse((self.tgt.state / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
