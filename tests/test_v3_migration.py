"""Req: F139 — v3 legacy 迁移器（opencode-agora v3 P4-T11）。

断言清单（任务页 ag-9f8964，逐条机械判定）：1 快照读取（末完整快照/
断尾丢弃/mail_read 并读/无快照中止源零修改）；2 全字段映射（三源权威
清单对账＋task-full-fields 逐字段对账＋瞬态剔除＋未知字段保真）；3 状态
矩阵十态回读；4 消息迁移（全量/逐字节 BLOB hash 对账/id 序＝文件名序）；
5 quarantine（撕裂件原文保全不阻塞；v2 可加载记录解析失败中止）；6 事件
（seq 连续递增/payload 保真）；7 凭据（hash/sid/role/lineage≤16＋secret
随迁 0600＋root.token 保留＋明文不入报告＋孤儿清扫接口）；8 游标转换
（min(pending)-1/最大 id/无记录 0/possible_duplicate_delivery 精确计数）；
9 配置随迁（存在性与权限保真/缺项如实报告）；10 冲突与中止（client_key
冲突清单＋FaultInjector 任一 batch 点＋重跑幂等）；11 结构化报告（JSON
机读全键）；12 架构守卫（源只读目录指纹锚定；分层守卫在
test_architecture.py——migrator 仅 stdlib＋domain＋storage）。
"""
import hashlib
import json
import os
import stat
import unittest
from pathlib import Path

from agorad import domain
from agorad.authsvc import AuthService
from agorad.migrator import (BATCHES, CONFIG_FILES, KNOWN_TASK_FIELDS,
                             MARKER, QUARANTINE_DIR, REPORT_PATH,
                             LegacyMigrator, MigrationError)
from agorad.storage import Storage
from tests import v3_harness as harness

_MSG_COLS = ("id,ts_ms,task_id,kind,from_sid,to_sid,body,replay_of")


def migrate(source, target, **kw):
    return LegacyMigrator(Path(source), Path(target), **kw).migrate()


def open_storage(target):
    st = Storage(Path(target) / "state" / "agora.db")
    return st


def _write_msg_file(home, name, mid, ts, task, kind, frm, to, body, **extra):
    fm = {"id": mid, "ts": ts, "task": task, "kind": kind, "from": frm,
          "to": to, **extra}
    head = "".join(f"{k}: {fm[k]}\n" for k in sorted(fm))
    (home.messages / name).write_text(f"{head}\n{body}", encoding="utf-8")


class _Src(unittest.TestCase):
    """源/目标各一 TempAgoraHome；源指纹先取，用毕比对（源只读锚）。"""

    def setUp(self):
        self.src = harness.TempAgoraHome()
        self.addCleanup(self.src.cleanup)
        self.tgt = harness.TempAgoraHome()
        self.addCleanup(self.tgt.cleanup)
        self.before = harness.digest_tree(self.src.path)

    def assertSourceUntouched(self):
        self.assertEqual(harness.digest_tree(self.src.path), self.before)

    def freeze(self):
        """fixture 写完后、migrate 前重记源指纹（源只读锚的比对基线）。"""
        self.before = harness.digest_tree(self.src.path)

    def run_migrate(self, **kw):
        self.freeze()
        return LegacyMigrator(Path(self.src.path),
                              Path(self.tgt.path), **kw).migrate()

    def storage(self):
        st = open_storage(self.tgt.path)
        self.addCleanup(st.close)
        return st


class TestSnapshotRead(_Src):  # 断言1
    def test_torn_tail_last_complete_wins(self):
        info = harness.legacy_torn(self.src)
        report = self.run_migrate()
        st = self.storage()
        self.assertIsNotNone(st.get_task(info["survivor"]))
        self.assertIsNone(st.get_task(info["lost"]))
        self.assertGreaterEqual(report["snapshot"]["lines_dropped"], 1)
        self.assertTrue((self.tgt.path / MARKER).exists())
        self.assertSourceUntouched()

    def test_no_snapshot_aborts_source_untouched(self):
        with self.assertRaises(MigrationError):
            self.run_migrate()
        self.assertSourceUntouched()
        self.assertFalse((self.tgt.path / MARKER).exists())

    def test_bad_version_aborts(self):
        (self.src.state / "tasks.jsonl").write_text(
            '{"version": 2, "tasks": {}, "mail_read": {}}\n',
            encoding="utf-8")
        with self.assertRaises(MigrationError):
            self.run_migrate()
        self.assertSourceUntouched()

    def test_mail_read_consumed_into_cursors(self):
        info = harness.mail_lineage(self.src)
        old_sid = info["sids"][0]
        self.run_migrate()
        cursor = self.storage().get_cursor(old_sid)
        self.assertIsNotNone(cursor)  # mail_read 一并读取（断言8 细算）


class TestFieldMapping(_Src):  # 断言2
    def test_known_field_inventory_matches_authority(self):
        self.assertEqual(KNOWN_TASK_FIELDS, frozenset(harness.CREATE_KEYS)
                         | frozenset(harness.GATE_HEALTH_KEYS))

    def test_full_fields_roundtrip(self):
        row = harness.task_full_fields(self.src)
        self.run_migrate()
        t = self.storage().get_task("gt-full")
        # task.create 行构造面（标量）
        for key in ("title", "goal", "req_id", "model", "deliverable",
                    "client_key", "project_dir", "target_branch", "env_dir",
                    "waiting_on", "commit", "rebased_from", "env_base",
                    "cancel_reason", "review_cmd", "dispatcher_sid",
                    "worker_sid", "root_sid", "state"):
            self.assertEqual(getattr(t, key), row[key], key)
        self.assertEqual(t.quick, row["quick"])
        self.assertEqual(t.verify_pending, row["verify_pending"])
        self.assertEqual(t.verify_base_exempt, row["verify_base_exempt"])
        self.assertEqual(t.chain_depth, row["chain_depth"])
        self.assertEqual(t.fail_streak, row["fail_streak"])
        self.assertEqual(t.after, ())  # v2 after=None→空元组
        for src, dst in (("created", "created_ms"), ("updated", "updated_ms"),
                         ("last_seen", "last_seen_ms")):
            self.assertEqual(getattr(t, dst), domain.epoch_ms(row[src]), dst)
        # verify 全投影
        self.assertEqual(t.verify, domain.VerifySpec(
            cmd=row["verify"]["cmd"], state=row["verify"]["state"],
            registered_by=row["verify"]["registered_by"],
            registered_at_ms=domain.epoch_ms(row["verify"]["registered_at"]),
            timeout_s=row["verify"]["timeout"],
            timeout_source=row["verify"]["timeout_source"]))
        # delivery 全投影
        d = row["delivery"]
        self.assertEqual(t.delivery, domain.DeliveryState(
            mode=d["mode"], status=d["status"], commit=d["commit"],
            base=d["base"], target_branch=d["target_branch"],
            attempt=d["attempt"], local_status=d["local_status"],
            error=d["error"], conflict_files=None))
        # gate/probe/review_cache 全投影
        g = t.gate
        self.assertEqual((g.in_gate, g.gate_env, g.pending_cancel,
                          g.pending_cancel_reason, g.review_fail_streak),
                         (row["in_gate"], row["gate_env"],
                          row["pending_cancel"], row["pending_cancel_reason"],
                          row["review_fail_streak"]))
        a = row["gate_artifact"]
        self.assertEqual(g.gate_artifact, domain.Artifact(
            task_id="gt-full", phase=a["phase"], path=a["path"], rc=a["rc"],
            failure_kind=a["failure_kind"], duration_s=a["duration_s"],
            bytes=a["bytes"], cmd="", error=None, started_at_ms=None,
            ended_at_ms=None))
        self.assertEqual(g.review_cache, {
            "cmd": row["review_cache"]["cmd"],
            "commit": row["review_cache"]["commit"],
            "ok": row["review_cache"]["ok"],
            "ran_at_ms": domain.epoch_ms(row["review_cache"]["ran_at"])})
        p = row["probe"]
        self.assertEqual(t.probe, domain.BaseProbe(
            result=p["result"], cmd=p["cmd"], base=p["base"],
            ran_at_ms=domain.epoch_ms(p["ran_at"])))
        # 已知无 v3 投影字段→legacy_extra_json 逐字段保真
        # （full-field 语料含 gate_origin_branch=None——值亦保真）
        self.assertEqual(json.loads(t.legacy_extra_json),
                         {"usage": row["usage"],
                          "gate_origin_branch": row["gate_origin_branch"]})

    def test_known_no_projection_fields(self):
        row = harness.full_field_row(gate_origin_branch="feature-x")
        harness._append_snapshot(self.src, {"gt-o": row})
        self.run_migrate()
        t = self.storage().get_task("gt-o")
        self.assertEqual(json.loads(t.legacy_extra_json),
                         {"usage": row["usage"],
                          "gate_origin_branch": "feature-x"})

    def test_unknown_fields_preserved_transient_dropped(self):
        row = harness.legacy_unknown(self.src)
        self.run_migrate()
        t = self.storage().get_task("gt-unknown")
        extra = json.loads(t.legacy_extra_json)
        self.assertEqual(extra["future_v3_field"], row["future_v3_field"])
        self.assertEqual(extra["legacy_note"], row["legacy_note"])
        self.assertNotIn("gate_resource", extra)  # 瞬态清单剔除
        self.assertEqual(t.state, "running")  # typed 面照迁

    def test_after_wraps_tuple(self):
        row = harness.full_field_row(after="ag-prev")
        harness._append_snapshot(self.src, {"gt-a": row})
        self.run_migrate()
        self.assertEqual(self.storage().get_task("gt-a").after, ("ag-prev",))


class TestStateMatrix(_Src):  # 断言3
    def test_ten_states_roundtrip(self):
        tasks = harness.task_state_matrix(self.src)
        report = self.run_migrate()
        st = self.storage()
        for tid, row in tasks.items():
            t = st.get_task(tid)
            self.assertEqual(t.state, row["state"])
            self.assertIn(t.state, domain.TASK_LEGAL)  # v3 校验一致
        self.assertEqual(report["tasks"]["by_state"],
                         {s: 1 for s in sorted(t["state"] for t in
                                               tasks.values())})


class TestMessages(_Src):  # 断言4
    def test_mail_lineage_body_bytewise_and_order(self):
        harness.mail_lineage(self.src)
        report = self.run_migrate()
        st = self.storage()
        reader = st.open_reader()
        rows = reader.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY id").fetchall()
        reader.close()
        files = sorted(self.src.messages.glob("*.md"))
        self.assertEqual(len(rows), len(files))
        self.assertEqual([r[0] for r in rows],
                         sorted(r[0] for r in rows))  # id 序＝文件名序
        hashes = []
        for got, path in zip(rows, files):
            body = path.read_bytes().partition(b"\n\n")[2]
            self.assertEqual(bytes(got[6]), body, path.name)  # 逐字节 BLOB
            hashes.append(hashlib.sha256(body).hexdigest())
            self.assertEqual(got[2], "gt-mail")
            self.assertEqual(got[3], "note")
        self.assertEqual(domain.epoch_seconds(rows[0][1]), 50.0)  # ts 毫秒化
        self.assertEqual(report["messages"]["count"], len(files))
        self.assertEqual(report["messages"]["body_hash"],
                         hashlib.sha256("".join(hashes).encode()).hexdigest())

    def test_replay_of_renumbered(self):
        harness._append_snapshot(
            self.src, {"gt-r": harness._matrix_row("gt-r", "running")})
        _write_msg_file(self.src, "00000002.md", 2, 10.0, "gt-r", "note",
                        "w", "d", "origin")
        _write_msg_file(self.src, "00000005.md", 5, 20.0, "gt-r", "note",
                        "w", "d", "copy", replay_of=2)
        # 文件 1/3/4 缺席制造断号：旧 2、5 → 新 1、2（重编号）
        self.run_migrate()
        st = self.storage()
        reader = st.open_reader()
        rows = reader.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY id").fetchall()
        reader.close()
        self.assertEqual([r[0] for r in rows], [1, 2])
        self.assertEqual(rows[1][7], 1)  # replay_of=2 随重编号改写→1


class TestQuarantine(_Src):  # 断言5
    def test_torn_message_and_bad_event_quarantined(self):
        harness.legacy_torn(self.src)
        with (self.src.path / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 4.0, "actor": "root",
                                "event": "enforce_on",
                                "task": "gt-torn-pre", "note": "n1",
                                "phase": "p"}) + "\n")
        report = self.run_migrate()
        manifest = json.loads(
            (self.tgt.path / QUARANTINE_DIR / "manifest.json")
            .read_text(encoding="utf-8"))
        self.assertEqual([q["file"] for q in manifest["messages"]],
                         ["00000001.md"])
        self.assertIn("tas", manifest["messages"][0]["content"])  # 原文保全
        self.assertEqual(len(manifest["events"]), 1)
        self.assertIn('"ts": 2.0', manifest["events"][0]["content"])
        self.assertEqual(report["quarantine"], {"messages": 1, "events": 1})
        # 不阻塞：快照任务与好事件行照迁
        st = self.storage()
        self.assertIsNotNone(st.get_task("gt-torn-pre"))
        reader = st.open_reader()
        n = reader.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        reader.close()
        self.assertEqual(n, 3)

    def test_parseable_record_fk_failure_aborts(self):
        harness.task_full_fields(self.src)
        _write_msg_file(self.src, "00000009.md", 9, 1.0, "gt-ghost", "note",
                        "w", "d", "hi")  # v2 可解析（引用缺席任务）
        with self.assertRaises(MigrationError):
            self.run_migrate()
        self.assertSourceUntouched()
        self.assertFalse((self.tgt.path / MARKER).exists())


class TestEvents(_Src):  # 断言6
    def test_seq_continuous_and_payload_preserved(self):
        harness.legacy_torn(self.src)
        (self.src.path / "events.jsonl").write_text(
            json.dumps({"ts": 1.5, "actor": "root", "event": "enforce_on",
                        "task": "gt-torn-pre", "note": "n1", "phase": "p"})
            + "\n"
            + json.dumps({"ts": 2.5, "actor": "a", "event": "running",
                          "task": "gt-torn-pre"}) + "\n",
            encoding="utf-8")
        report = self.run_migrate()
        st = self.storage()
        reader = st.open_reader()
        rows = reader.execute(
            "SELECT seq,ts_ms,actor,event,task_id,detail_json FROM events "
            "ORDER BY seq").fetchall()
        reader.close()
        self.assertEqual([r[0] for r in rows], [1, 2])  # seq 连续递增
        self.assertEqual(rows[0][1], domain.epoch_ms(1.5))
        self.assertEqual((rows[0][2], rows[0][3], rows[0][4]),
                         ("root", "enforce_on", "gt-torn-pre"))
        self.assertEqual(json.loads(rows[0][5]),
                         {"note": "n1", "phase": "p"})  # payload 保真→detail
        self.assertEqual(report["events"]["count"], 2)


class TestCredentials(_Src):  # 断言7
    TOK_ROOT, TOK_W, TOK_D = "a" * 32, "b" * 32, "c" * 32

    def _seed(self):
        row = harness.full_field_row(tid="gt-1", state="running",
                                     worker_sid="ses-w1")
        harness._append_snapshot(self.src, {"gt-1": row})
        (self.src.path / "tokens").write_text(json.dumps({
            self.TOK_ROOT: ["root", "root"],
            self.TOK_W: ["ses-w1", "worker",
                         [f"s-old{i}" for i in range(20)]],
            self.TOK_D: ["ses-d1", "dispatcher"]}), encoding="utf-8")
        self.src.write_credential("gt-1", self.TOK_W)
        self.src.write_credential("gt-zombie", "orphan-token-xyz")
        root = self.src.agora / "root.token"
        root.write_text(json.dumps({"token": self.TOK_ROOT}),
                        encoding="utf-8")
        os.chmod(root, 0o600)

    def test_credentials_and_secrets_migrated(self):
        self._seed()
        report = self.run_migrate()
        st = self.storage()
        worker = st.get_credential("ses-w1")
        self.assertEqual((worker.token, worker.task_id),
                         (self.TOK_W, "gt-1"))
        self.assertEqual(worker.lineage,
                         tuple(f"s-old{i}" for i in range(4, 20)))  # ≤16
        self.assertEqual(st.get_credential("root").token, self.TOK_ROOT)
        disp = st.get_credential("ses-d1")
        self.assertEqual((disp.token, disp.task_id, disp.lineage),
                         (self.TOK_D, "", ()))
        # secret 文件随迁（路径/0600）；root.token 保留；孤儿照迁
        copied = self.tgt.credentials / "gt-1.token"
        self.assertEqual(json.loads(copied.read_text(encoding="utf-8")),
                         {"token": self.TOK_W})
        self.assertEqual(stat.S_IMODE(os.stat(copied).st_mode), 0o600)
        self.assertTrue((self.tgt.credentials / "gt-zombie.token").exists())
        kept = self.tgt.agora / "root.token"
        self.assertEqual(json.loads(kept.read_text(encoding="utf-8")),
                         {"token": self.TOK_ROOT})
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o600)
        stats = report["credentials"]
        self.assertEqual((stats["root"], stats["worker"],
                          stats["dispatcher"], stats["orphan_secret_files"],
                          stats["secret_files_copied"]), (1, 1, 1, 1, 2))
        # 明文不入报告
        text = json.dumps(report)
        for tok in (self.TOK_ROOT, self.TOK_W, self.TOK_D, "orphan-token-xyz"):
            self.assertNotIn(tok, text)
        self.assertTrue(all(h.startswith("sha256:")
                            for h in stats["token_hashes"]))

    def test_orphan_sweep_interface(self):
        self._seed()
        self.run_migrate()
        st = self.storage()
        auth = AuthService(st, home=str(self.tgt.path))
        auth.sweep()
        self.assertFalse((self.tgt.credentials / "gt-zombie.token").exists())
        self.assertTrue((self.tgt.credentials / "gt-1.token").exists())

    def test_root_token_file_fallback(self):
        row = harness.full_field_row(tid="gt-1")
        harness._append_snapshot(self.src, {"gt-1": row})
        root = self.src.agora / "root.token"  # map 无 root 条目→文件兜底
        root.write_text(json.dumps({"token": "f" * 32}), encoding="utf-8")
        report = self.run_migrate()
        st = self.storage()
        self.assertEqual(st.get_credential("root").token, "f" * 32)
        self.assertTrue(report["credentials"]["root_from_file"])


class TestCursors(_Src):  # 断言8
    def test_mail_lineage_conversion(self):
        info = harness.mail_lineage(self.src)
        old_sid, new_sid = info["sids"]
        report = self.run_migrate()
        st = self.storage()
        # old_sid 游标 100.0：pending={id2(ts200)} → cursor=min-1=1
        self.assertEqual(st.get_cursor(old_sid).last_read_ms, 1)
        self.assertIsNone(st.get_cursor(new_sid))  # 无记录＝0（不建行）
        self.assertEqual(report["cursors"]["possible_duplicate_delivery"], 0)
        self.assertEqual(report["cursors"]["absent_sids"], 1)

    def test_duplicate_delivery_counted_exactly(self):
        harness._append_snapshot(
            self.src, {"gt-c": harness._matrix_row("gt-c", "running")},
            mail_read={"ses-c": 150.0})
        _write_msg_file(self.src, "00000001.md", 1, 50.0, "gt-c", "note",
                        "w", "ses-c", "read-then-old")
        _write_msg_file(self.src, "00000002.md", 2, 300.0, "gt-c", "note",
                        "w", "ses-c", "pending")
        _write_msg_file(self.src, "00000003.md", 3, 100.0, "gt-c", "note",
                        "w", "ses-c", "already-read-but-after-pending")
        report = self.run_migrate()
        st = self.storage()
        # pending={id2} → cursor=1；id3 已读(ts100≤150)但落入重投窗 → 重复
        self.assertEqual(st.get_cursor("ses-c").last_read_ms, 1)
        self.assertEqual(report["cursors"]["possible_duplicate_delivery"], 1)

    def test_no_pending_takes_max_id(self):
        harness._append_snapshot(
            self.src, {"gt-c": harness._matrix_row("gt-c", "running")},
            mail_read={"ses-c": 500.0})
        _write_msg_file(self.src, "00000001.md", 1, 50.0, "gt-c", "note",
                        "w", "ses-c", "a")
        _write_msg_file(self.src, "00000002.md", 2, 100.0, "gt-c", "note",
                        "w", "ses-c", "b")
        report = self.run_migrate()
        self.assertEqual(self.storage().get_cursor("ses-c").last_read_ms, 2)
        self.assertEqual(report["cursors"]["possible_duplicate_delivery"], 0)


class TestConfig(_Src):  # 断言9
    def test_configs_copied_with_mode(self):
        harness.task_full_fields(self.src)
        for name in CONFIG_FILES:
            path = self.src.agora / name
            path.write_text(f"# {name}\n", encoding="utf-8")
            os.chmod(path, 0o600)
        report = self.run_migrate()
        for name in CONFIG_FILES:
            dst = self.tgt.agora / name
            self.assertEqual(dst.read_text(encoding="utf-8"),
                             f"# {name}\n", name)
            self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o600, name)
            self.assertEqual(report["config"][name], "copied")
        self.assertEqual(report["config"]["root.token"], "missing")

    def test_missing_items_reported_honestly(self):
        harness.task_full_fields(self.src)
        report = self.run_migrate()
        self.assertEqual(report["config"],
                         {name: "missing" for name in CONFIG_FILES}
                         | {"root.token": "missing"})
        self.assertTrue((self.tgt.path / MARKER).exists())


class TestConflictAbort(_Src):  # 断言10
    def test_client_key_conflict_aborts_with_task_list(self):
        rows = {f"gt-k{i}": harness.full_field_row(
            tid=f"gt-k{i}", client_key="dup-key", root_sid="root")
            for i in (1, 2)}
        harness._append_snapshot(self.src, rows)
        with self.assertRaises(MigrationError) as cm:
            self.run_migrate()
        self.assertEqual(cm.exception.details["conflicts"][0]["tasks"],
                         ["gt-k1", "gt-k2"])
        for tid in rows:
            self.assertIn(tid, str(cm.exception))
        self.assertSourceUntouched()
        self.assertFalse((self.tgt.path / MARKER).exists())

    def test_fault_at_any_batch_point(self):
        harness.legacy_torn(self.src)
        (self.src.path / "tokens").write_text(
            json.dumps({"z" * 32: ["root", "root"]}), encoding="utf-8")
        (self.src.agora / "agorad.env").write_text("K=V\n", encoding="utf-8")
        self.freeze()
        for name in BATCHES:
            for phase in ("before", "after"):
                injector = harness.FaultInjector()
                for batch in BATCHES:
                    injector.register(f"batch_{batch}")
                injector.arm(f"batch_{name}", phase)
                with self.assertRaises(harness.FaultInjected, msg=name):
                    migrate(self.src.path, self.tgt.path, injector=injector)
                self.assertFalse((self.tgt.path / MARKER).exists(),
                                 f"{name}/{phase}")
                self.assertSourceUntouched()
                self.run_migrate()  # 重跑收敛
                self.assertTrue((self.tgt.path / MARKER).exists(),
                                f"{name}/{phase}")

    def test_rerun_idempotent(self):
        harness.mail_lineage(self.src)
        self.run_migrate()
        st = self.storage()
        tasks = st.tasks_all()
        reader = st.open_reader()
        msgs = reader.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY id").fetchall()
        reader.close()
        st.close()
        report = self.run_migrate()  # 目标按清单重建
        st2 = open_storage(self.tgt.path)
        self.addCleanup(st2.close)
        self.assertEqual(st2.tasks_all(), tasks)
        reader = st2.open_reader()
        msgs2 = reader.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY id").fetchall()
        reader.close()
        self.assertEqual(msgs, msgs2)
        self.assertTrue((self.tgt.path / MARKER).exists())
        self.assertTrue(report["ok"])
        self.assertSourceUntouched()


class TestReport(_Src):  # 断言11
    def test_report_machine_readable_full_shape(self):
        tasks = {"gt-full": harness.full_field_row()}
        for i, state in enumerate(("todo", "running", "done")):
            tasks[f"gt-r{i}"] = harness._matrix_row(f"gt-r{i}", state)
        harness._append_snapshot(self.src, tasks,
                                 mail_read={"ses-c": 0.0})
        _write_msg_file(self.src, "00000001.md", 1, 10.0, "gt-full", "peer",
                        "ses-d", "", "p1", repo="/repo")
        _write_msg_file(self.src, "00000002.md", 2, 20.0, "gt-full", "note",
                        "w", "ses-c", "n1")
        (self.src.path / "events.jsonl").write_text(
            json.dumps({"ts": 1.0, "actor": "a", "event": "task_created",
                        "task": "gt-full"}) + "\n", encoding="utf-8")
        (self.src.path / "tokens").write_text(
            json.dumps({"d" * 32: ["ses-d", "dispatcher"]}), encoding="utf-8")
        report = self.run_migrate()
        on_disk = json.loads(
            (self.tgt.path / REPORT_PATH).read_text(encoding="utf-8"))
        self.assertEqual(report, on_disk)  # 返回值＝落盘件
        self.assertEqual(report["ok"], True)
        self.assertEqual(report["tasks"]["total"], 4)
        self.assertEqual(report["tasks"]["by_state"],
                         {"done": 1, "running": 2, "todo": 1})
        self.assertEqual(report["tasks"]["verify_states"],
                         {"registered": 4})
        self.assertEqual(report["tasks"]["delivery_modes"],
                         {"managed": 1, "none": 3})
        self.assertEqual(report["tasks"]["delivery_statuses"],
                         {"none": 3, "pending": 1})
        gate = report["tasks"]["gate"]
        self.assertEqual((gate["in_gate"], gate["pending_cancel"],
                          gate["gate_artifacts"], gate["probes"],
                          gate["review_cache"]), (0, 0, 1, 1, 1))
        self.assertEqual(report["unknown_fields"],
                         {"gt-full": ["gate_origin_branch", "usage"]})
        self.assertEqual(report["messages"]["count"], 2)
        self.assertEqual(report["messages"]["frontmatter_extra"], {"repo": 1})
        self.assertEqual(report["events"]["count"], 1)
        self.assertEqual(report["cursors"]["migrated"], 1)
        self.assertEqual(report["credentials"]["dispatcher"], 1)
        json.dumps(report)  # 机读可序列化


class TestReviewLoopR1(_Src):  # 评审环 R1 防回归锚（OCR＋A/B/C 三册修复面）
    def test_reset_clears_stale_target_files(self):
        # B2/OCR-medium：重置清单须含随迁文件面——上轮残留不得存活
        harness.task_full_fields(self.src)
        stale_secret = self.tgt.credentials / "gt-stale.token"
        stale_secret.write_text('{"token": "stale"}\n', encoding="utf-8")
        (self.tgt.agora / "agorad.env").write_text("STALE=1\n",
                                                   encoding="utf-8")
        (self.tgt.agora / "root.token").write_text('{"token": "stale"}\n',
                                                   encoding="utf-8")
        report = self.run_migrate()
        self.assertFalse(stale_secret.exists())
        self.assertFalse((self.tgt.agora / "agorad.env").exists())
        self.assertFalse((self.tgt.agora / "root.token").exists())
        self.assertEqual(report["config"]["agorad.env"], "missing")
        self.assertEqual(report["config"]["root.token"], "missing")
        self.assertEqual(report["credentials"]["secret_files_copied"], 0)

    def test_replay_unmapped_counted(self):
        # B1/OCR-low：前向引用保原值须计数入报告（docstring 承诺兑现）
        harness._append_snapshot(
            self.src, {"gt-r": harness._matrix_row("gt-r", "running")})
        _write_msg_file(self.src, "00000007.md", 7, 10.0, "gt-r", "note",
                        "w", "d", "fwd-ref", replay_of=9)
        report = self.run_migrate()
        self.assertEqual(report["messages"]["replay_unmapped"], 1)

    def test_report_and_manifest_private_modes(self):
        # C-low：manifest（撕裂原文）与报告 0600 落盘
        harness.legacy_torn(self.src)
        self.run_migrate()
        for path in (self.tgt.path / REPORT_PATH,
                     self.tgt.path / QUARANTINE_DIR / "manifest.json"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_tokens_degeneracies_reported(self):
        # C-low/B4：surrogate 键→malformed；未知 role→计数照迁
        harness.task_full_fields(self.src)
        (self.src.path / "tokens").write_text(
            '{"\\ud800": ["ses-x", "dispatcher"],'
            ' "ok": ["ses-ok", "dispatcher"],'
            ' "adm": ["ses-a", "admin"]}', encoding="utf-8")
        report = self.run_migrate()
        stats = report["credentials"]
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["unknown_role"], 1)
        st = self.storage()
        self.assertIsNotNone(st.get_credential("ses-ok"))
        self.assertIsNotNone(st.get_credential("ses-a"))  # 外来 role 照迁入册
        self.assertEqual(stats["dispatcher"], 2)

    def test_tokens_unreadable_flagged(self):
        # B3：tokens 损坏→静默清零须显式申报
        harness.task_full_fields(self.src)
        (self.src.path / "tokens").write_text("{not json", encoding="utf-8")
        report = self.run_migrate()
        self.assertTrue(report["credentials"]["tokens_unreadable"])
        self.assertEqual(report["credentials"]["root"], 0)

    def test_message_ts_non_finite_aborts(self):
        # B5/A3：NaN/Inf 走 MigrationError 中止面（非裸异常）
        harness._append_snapshot(
            self.src, {"gt-n": harness._matrix_row("gt-n", "running")})
        _write_msg_file(self.src, "00000001.md", 1, float("nan"), "gt-n",
                        "note", "w", "d", "x")
        with self.assertRaises(MigrationError):
            self.run_migrate()
        self.assertSourceUntouched()

    def test_mail_read_non_finite_aborts(self):
        harness._append_snapshot(
            self.src, {"gt-c": harness._matrix_row("gt-c", "running")},
            mail_read={"ses-c": float("nan")})
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_created_non_finite_aborts(self):
        row = harness.full_field_row(created=float("inf"))
        harness._append_snapshot(self.src, {"gt-n": row})
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_review_fail_streak_garbage_aborts(self):
        row = harness.full_field_row(review_fail_streak="x")
        harness._append_snapshot(self.src, {"gt-n": row})
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_review_cache_ran_at_both_keys(self):
        # OCR-low：ran_at/ran_at_ms 并存——ms 优先、旧键恒消费
        row = harness.full_field_row(review_cache={
            "cmd": "true", "commit": "a" * 40, "ok": True,
            "ran_at": 5.0, "ran_at_ms": 999})
        harness._append_snapshot(self.src, {"gt-rc": row})
        self.run_migrate()
        cache = self.storage().get_task("gt-rc").gate.review_cache
        self.assertEqual(cache["ran_at_ms"], 999)
        self.assertNotIn("ran_at", cache)


class TestReviewLoopR2(_Src):  # 评审环 R2 防回归锚（OCR 复核 R1 增量修复面）
    def test_quarantine_base64_lossless(self):
        # R1-⑩补锚：非 UTF-8 撕裂件走 content_base64 无损保全
        harness.task_full_fields(self.src)
        torn = b"id: 1\nts: \xff\xfe\xfafrom: w\n\n\x80\x81body"
        (self.src.messages / "00000001.md").write_bytes(torn)
        self.run_migrate()
        manifest = json.loads(
            (self.tgt.path / QUARANTINE_DIR / "manifest.json")
            .read_text(encoding="utf-8"))
        entry = manifest["messages"][0]
        self.assertNotIn("content", entry)
        import base64 as b64
        self.assertEqual(b64.b64decode(entry["content_base64"]), torn)

    def test_non_utf8_snapshot_aborts(self):
        (self.src.state / "tasks.jsonl").write_bytes(b'{"version": 1, "\xff"}')
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_non_utf8_events_aborts(self):
        harness.task_full_fields(self.src)
        (self.src.path / "events.jsonl").write_bytes(b'{"ts": \xff}')
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_huge_int_aborts(self):
        harness._append_snapshot(
            self.src, {"gt-c": harness._matrix_row("gt-c", "running")},
            mail_read={"ses-c": 10 ** 400})  # JSON 任意精度整数→float 溢出
        with self.assertRaises(MigrationError):
            self.run_migrate()

    def test_unhashable_role_counted(self):
        # R2-medium：role 非字符串（JSON list）不可哈希——计数不炸
        harness.task_full_fields(self.src)
        (self.src.path / "tokens").write_text(
            '{"t": ["ses-a", ["admin"]]}', encoding="utf-8")
        report = self.run_migrate()
        self.assertEqual(report["credentials"]["unknown_role"], 1)
        self.assertIsNotNone(self.storage().get_credential("ses-a"))


class TestSourceReadOnly(_Src):  # 断言12（分层守卫在 test_architecture.py）
    def test_full_migration_leaves_source_untouched(self):
        harness.legacy_torn(self.src)
        (self.src.path / "tokens").write_text(
            json.dumps({"e" * 32: ["root", "root"]}), encoding="utf-8")
        for name in CONFIG_FILES:
            (self.src.agora / name).write_text("x\n", encoding="utf-8")
        self.run_migrate()
        self.assertSourceUntouched()
        self.assertTrue((self.tgt.path / MARKER).exists())

    def test_same_directory_rejected(self):
        harness.task_full_fields(self.src)
        self.freeze()
        with self.assertRaises(MigrationError):
            migrate(self.src.path, self.src.path)  # 源＝目标即写源，先拒
        self.assertSourceUntouched()


if __name__ == "__main__":
    unittest.main()
