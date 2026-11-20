"""Req: F140 — v3 切换/导出/备份 CLI（opencode-agora v3 P4-T12）。

断言清单（任务页 ag-acd4e9，逐条机械判定；断言10 分层守卫镜像自
test_architecture.py 使专项自含）：1 export 派生（补零文件名/v2 兼容
frontmatter/body 字节/events 按 seq/ledger canonical JSON 全从 SQLite）；
2 幂等修复（删件重建一致/孤儿清扫/导出失败不悬空引用——复用 messaging
省略规则）；3 backup（backup API 热备/体检/恢复计数一致）；4 migrate
（dry-run 零写入/apply 建目标/报告落盘/重跑幂等/中止可恢复）；5 切换
（完整性门拒互换/checkpoint 收卷无 -wal/-shm/同级两步互换）；6 回滚
（v2 桩级在位/v3 新写入不回灌）；7 CLI 契约（0/1/2/--help/结构化错误）；
8 安全（导出/报告/备份无 token 明文/源只读/无网络）；9 脚本级 mini e2e
全链；10 架构守卫（cutover 无 SQL 不 import access——SQL 圈禁 storage）。
"""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from agorad.cutover import (CutoverError, EVENTS_NAME, LEDGER_PATH,
                            SWAPPED_SUFFIX, MessageFileExporter, backup_home,
                            export_home, migrate_cmd, rollback, switch)
from agorad.domain import (Approval, Credential, DeliveryReceipt, Event,
                           Task, Timer, TimerState)
from agorad.messaging import MessagingService
from agorad.migrator import MARKER
from agorad.storage import Storage
from tests import v3_harness as harness
from tests.test_architecture import assert_only

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "sekrit-token-f140"


def seed(home, *, with_credential=True):
    """直建 v3 home：2 任务/3 消息（非 UTF-8 body＋replay_of）/2 事件/
    timer/approval/cursor/receipt/credential——export/backup 断言语料。"""
    home = Path(home)
    (home / "state").mkdir(parents=True, exist_ok=True)
    st = Storage(home / "state" / "agora.db")
    try:
        with st.uow():
            st.upsert_task(Task(id="gt-a", state="running", title="a"))
            st.upsert_task(Task(id="gt-b", state="merged", title="b"))
            m1 = st.insert_message(
                ts_ms=1_000, task_id="gt-a", kind="note", from_sid="w",
                to_sid="d", body=b"hello\n")
            st.insert_message(
                ts_ms=2_000, task_id="gt-a", kind="note", from_sid="w",
                to_sid="d", body=b"\xff\xfe raw \x00 bytes")
            st.insert_message(
                ts_ms=3_000, task_id="gt-b", kind="question", from_sid="d",
                to_sid="w", body=b"re", replay_of=m1.id)
            st.append_event(Event(seq=0, ts_ms=1_000, actor="root",
                                  event="created", task="gt-a"))
            st.append_event(Event(seq=0, ts_ms=2_000, actor="root",
                                  event="merged", task="gt-b",
                                  detail={"k": [1, 2], "rc": 0}))
            st.upsert_timer(Timer(id="tm-1", task_id="gt-a", due_ms=9))
            st.upsert_approval(Approval(id="ap-1", task_id="gt-a",
                                        requester_sid="w"))
            st.upsert_cursor("d", m1.id)
            st.upsert_receipt(DeliveryReceipt(message_id=m1.id,
                                              status="delivered"))
            if with_credential:
                st.put_credential(Credential(sid="root", token=TOKEN))
    finally:
        st.close()
    return home


def v2_fixture(parent):
    """v2 语料 home（migrate/mini-e2e 输入）：状态矩阵＋Store 写消息/事件
    ＋tokens/root.token。"""
    home = harness.TempAgoraHome(root=parent)
    harness.task_state_matrix(home)
    store = harness.Store(home.path)
    for i, ts in enumerate((1.0, 2.0), 1):
        store.write_message(
            {"ts": ts, "task": "gt-running", "kind": "note", "from": "w",
             "to": "d"}, f"body-{i}")
    store.append_event({"ts": 1.0, "actor": "root", "event": "created",
                        "task": "gt-running"})
    store.write_ephemeral("tokens", {"tok-root-123": ["root", "root"]})
    (home.agora / "root.token").write_text(
        json.dumps({"token": "tok-root-123"}) + "\n", encoding="utf-8")
    return home


def agora(*args, home=None):
    """脚本级 CLI 调用（断言9 同缝）：rc/stdout/stderr 三件回。"""
    argv = [sys.executable, str(ROOT / "agora"), *args]
    env = {k: v for k, v in os.environ.items() if k != "AGORA_HOME"}
    if home is not None:
        argv += ["--home", str(home)]
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          cwd=str(ROOT))


class _Scratch(unittest.TestCase):
    """tmp 工作区（/tmp/opencode 下，e2e 与单元共用）。"""

    def setUp(self):
        import tempfile
        self.work = Path(tempfile.mkdtemp(prefix="f140-", dir="/tmp/opencode"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.work, ignore_errors=True))


class TestExportDerived(_Scratch):  # 断言1
    def setUp(self):
        super().setUp()
        self.home = seed(self.work / "v3")

    def test_messages_padded_names_v2_frontmatter_body_bytes(self):
        report = export_home(self.home)
        self.assertEqual(report["messages"], 3)
        names = sorted(p.name for p in
                       (self.home / "state" / "messages").glob("*.md"))
        self.assertEqual(names, ["00000001.md", "00000002.md",
                                 "00000003.md"])  # 补零＝字典序＝id 序
        raw = (self.home / "state" / "messages" / "00000002.md").read_bytes()
        head, sep, body = raw.partition(b"\n\n")
        self.assertEqual(sep, b"\n\n")
        self.assertEqual(
            head.decode(), "id: 2\nfrom: w\nkind: note\ntask: gt-a\nto: d\n"
            "ts: 2.0")  # id 首行＋余键字典序（store.write_message 同构）
        self.assertEqual(body, b"\xff\xfe raw \x00 bytes")  # body 逐字节

    def test_events_jsonl_by_seq_and_ledger_canonical(self):
        report = export_home(self.home)
        self.assertEqual(report["events"], 2)
        lines = (self.home / EVENTS_NAME).read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first, second = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual((first["ts"], first["actor"], first["event"], first["task"]),
                         (1.0, "root", "created", "gt-a"))
        self.assertEqual(second["k"], [1, 2])  # detail 平展保真
        ledger = json.loads((self.home / LEDGER_PATH).read_text("utf-8"))
        self.assertEqual(ledger["counts"],
                         {"tasks": 2, "messages": 3, "events": 2})
        self.assertEqual([t["id"] for t in ledger["tasks"]], ["gt-a", "gt-b"])
        self.assertEqual(ledger["timers"][0]["state"], "pending")  # 枚举落值
        self.assertEqual(ledger["approvals"][0]["id"], "ap-1")
        self.assertEqual(ledger["cursors"], {"d": 1})
        self.assertEqual(ledger["receipts"][0]["status"], "delivered")
        # canonical：sorted keys 稳定再dump＝原文
        text = (self.home / LEDGER_PATH).read_text("utf-8")
        self.assertEqual(json.dumps(ledger, ensure_ascii=False, sort_keys=True,
                                    indent=1), text)

    def test_export_out_dir_and_missing_db(self):
        out = self.work / "mirror"
        report = export_home(self.home, out=out)
        self.assertEqual(report["out"], str(out))
        self.assertTrue((out / "state" / "messages" / "00000001.md").is_file())
        with self.assertRaises(CutoverError) as cm:
            export_home(self.work / "empty")
        self.assertEqual(cm.exception.code, "no_database")


class TestExportIdempotentRepair(_Scratch):  # 断言2
    def test_delete_rebuild_identical_and_orphan_sweep(self):
        home = seed(self.work / "v3")
        export_home(home)
        mdir = home / "state" / "messages"
        before = {p.name: p.read_bytes() for p in mdir.glob("*.md")}
        ev_before = (home / EVENTS_NAME).read_bytes()
        (mdir / "00000009.md").write_bytes(b"id: 9\norphan\n\nx")  # 库外孤儿
        (mdir / "00000001.md").unlink()
        (home / EVENTS_NAME).write_text('{"ts": 4.0, "actor": "t')  # 断尾行
        report = export_home(home)
        self.assertEqual({p.name: p.read_bytes() for p in mdir.glob("*.md")},
                         before)  # 删件/撕裂/孤儿全部按库面重建
        self.assertEqual((home / EVENTS_NAME).read_bytes(), ev_before)
        self.assertEqual((report["messages"], report["events"]), (3, 2))
        st = Storage(home / "state" / "agora.db")  # 计数与库一致
        try:
            self.assertEqual([m.id for m in st.messages_all()], [1, 2, 3])
            self.assertEqual(st.messages_all()[0].body, b"hello\n")
            self.assertEqual([e.seq for e in st.events_all()], [1, 2])
        finally:
            st.close()

    def test_export_failure_no_dangling_reference(self):
        """复用 messaging 引用省略规则（§8/§10）：导出器炸→注入文本省略
        msg= 行，不悬空引用。"""
        home = seed(self.work / "v3", with_credential=False)
        blocker = self.work / "blocker"
        blocker.write_text("x")  # 文件占位：其下 mkdir 必炸

        class Oc:
            def __init__(self):
                self.sent = []

            def inject(self, sid, text):
                self.sent.append(text)
                return True

            inject_silent = inject_notify = inject

        oc = Oc()
        svc = MessagingService(Storage(home / "state" / "agora.db"), oc,
                               home=str(home),
                               exporter=MessageFileExporter(blocker / "sub"))
        svc.post("d", "w", "gt-a", "question", "boom")
        self.assertTrue(oc.sent)
        self.assertNotIn("msg=", oc.sent[0])  # 失败→省略引用行
        self.assertIn("page=wiki/tasks/gt-a.md", oc.sent[0])
        svc2 = MessagingService(Storage(home / "state" / "agora.db"), oc,
                                home=str(home),
                                exporter=MessageFileExporter(home))
        svc2.post("d", "w", "gt-a", "question", "ok")
        tail = [t for t in oc.sent if "msg=state/messages/" in t]
        self.assertTrue(tail)  # 成功→引用行在位（相对路径）


class TestBackup(_Scratch):  # 断言3
    def test_hot_backup_checks_restore_and_no_sidecars(self):
        home = seed(self.work / "v3")
        st = Storage(home / "state" / "agora.db")  # 热备：写者连接保持开
        try:
            with st.uow():
                st.insert_message(ts_ms=9_000, task_id="gt-a", kind="note",
                                  from_sid="w", to_sid="d", body=b"in wal")
            dest = self.work / "bak.db"
            report = backup_home(home, dest)
            self.assertEqual(report["integrity"], ["ok"])
            self.assertEqual(report["backup_counts"]["messages"], 4)
            self.assertEqual(report["backup_counts"]["credentials"], 0)
            self.assertEqual(report["credentials_scrubbed"], 1)
        finally:
            st.close()
        self.assertFalse(dest.with_name(dest.name + "-wal").exists())
        self.assertFalse(dest.with_name(dest.name + "-shm").exists())
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o600)
        rb = Storage(dest)  # 恢复：备份件即库——可读且计数一致
        try:
            self.assertEqual(len(rb.tasks_all()), 2)
            self.assertEqual([m.body for m in rb.messages_all()],
                             [b"hello\n", b"\xff\xfe raw \x00 bytes", b"re",
                              b"in wal"])
            self.assertEqual(len(rb.events_all()), 2)
            self.assertIsNone(rb.get_credential("root"))  # 牌已清扫（可重签）
        finally:
            rb.close()
        with self.assertRaises(CutoverError) as cm:  # 拒覆写
            backup_home(home, dest)
        self.assertEqual(cm.exception.code, "backup_exists")


class TestMigrate(_Scratch):  # 断言4
    def test_dry_run_zero_writes_report_readable(self):
        src = v2_fixture(self.work)
        before = harness.digest_tree(src.path)
        target = self.work / "v3home"
        report_path = self.work / "dry.json"
        report = migrate_cmd(src.path, target, report_path=report_path)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["target"], str(target))
        self.assertFalse(target.exists())  # 目标零创建
        self.assertEqual(harness.digest_tree(src.path), before)  # 源只读
        on_disk = json.loads(report_path.read_text("utf-8"))
        self.assertEqual(on_disk["tasks"]["total"], 10)
        self.assertEqual(on_disk["messages"]["count"], 2)

    def test_apply_creates_home_report_idempotent(self):
        src = v2_fixture(self.work)
        before = harness.digest_tree(src.path)
        target = self.work / "v3home"
        first = migrate_cmd(src.path, target, apply=True)
        self.assertIs(first.get("dry_run"), None)  # apply 无 dry_run 旗
        self.assertTrue((target / MARKER).is_file())
        report_file = target / "state" / "migration-report.json"
        self.assertEqual(json.loads(report_file.read_text("utf-8"))["ok"],
                         True)  # 报告 JSON 落盘可机读
        counts1 = Storage.verify_db(target / "state" / "agora.db")["counts"]
        migrate_cmd(src.path, target, apply=True)  # 重跑幂等
        self.assertEqual(
            Storage.verify_db(target / "state" / "agora.db")["counts"],
            counts1)
        self.assertEqual(harness.digest_tree(src.path), before)

    def test_abort_maps_recoverable_cli(self):
        empty = harness.TempAgoraHome(root=self.work)  # 无快照源
        target = self.work / "v3home"
        r = agora("migrate", "--source", str(empty.path),
                  "--target", str(target))
        self.assertEqual(r.returncode, 1)
        err = json.loads(r.stderr)
        self.assertEqual(err["error"]["code"], "migration_aborted")
        self.assertFalse(target.exists())


class TestSwitch(_Scratch):  # 断言5
    def _pair(self, tag=""):
        old = self.work / f"plaza{tag}"
        old.mkdir()
        (old / "state").mkdir()
        (old / "keep.txt").write_text("v2")
        new = seed(self.work / f"v3home{tag}")
        (new / MARKER).write_text("complete\n", encoding="utf-8")
        return old, new

    def test_gate_refuses_swap_zero_action(self):
        old, new = self._pair()
        import sqlite3
        conn = sqlite3.connect(new / "state" / "agora.db")  # FK 违例注毒
        conn.execute("INSERT INTO events(seq,ts_ms,actor,event,task_id) "
                     "VALUES(9,0,'x','y','ghost')")
        conn.commit()
        conn.close()
        with self.assertRaises(CutoverError) as cm:
            switch(old, new)
        self.assertEqual(cm.exception.code, "fk_gate_failed")
        self.assertTrue((old / "keep.txt").is_file())  # 零动作
        self.assertTrue((new / MARKER).is_file())
        old2, new2 = self._pair("2")  # 字节腐蚀（独立对：单因单果）
        with open(new2 / "state" / "agora.db", "r+b") as f:
            f.seek(0)
            f.write(b"XXXXgarbage")
        with self.assertRaises(CutoverError) as cm:
            switch(old2, new2)
        self.assertEqual(cm.exception.code, "integrity_gate_failed")
        self.assertTrue((old2 / "keep.txt").is_file())

    def test_swap_wal_coiled_and_sibling_marker_gates(self):
        old, new = self._pair()
        st = Storage(new / "state" / "agora.db")  # 先造 -wal 活跃态
        try:
            with st.uow():
                st.insert_message(ts_ms=5_000, task_id="gt-a", kind="note",
                                  from_sid="w", to_sid="d", body=b"wal")
        finally:
            st.close()
        other = seed(self.work / "elsewhere" / "v3")  # 异父目录
        (other / MARKER).write_text("complete\n", encoding="utf-8")
        with self.assertRaises(CutoverError) as cm:
            switch(old, other)
        self.assertEqual(cm.exception.code, "not_siblings")
        (new / MARKER).unlink()
        with self.assertRaises(CutoverError) as cm:
            switch(old, new)
        self.assertEqual(cm.exception.code, "new_home_incomplete")
        (new / MARKER).write_text("complete\n", encoding="utf-8")
        report = switch(old, new)
        self.assertEqual(report["smoke_tasks"], 2)
        self.assertEqual(report["old_home_at"],
                         str(old.with_name(old.name + SWAPPED_SUFFIX)))
        self.assertTrue((old / "state" / "agora.db").is_file())  # 新 home 就位
        self.assertTrue((old / MARKER).is_file())
        self.assertTrue((old.with_name(old.name + SWAPPED_SUFFIX)
                        / "keep.txt").is_file())  # 旧 home 驻 swapped
        for residue in ("agora.db-wal", "agora.db-shm"):
            self.assertFalse((old / "state" / residue).exists(),
                             residue)  # 收卷后无活跃残留


class TestRollback(_Scratch):  # 断言6
    def test_rollback_v2_restartable_and_v3_writes_isolated(self):
        src = v2_fixture(self.work)
        old = self.work / "plaza"
        src.path.rename(old)
        src.cleanup = lambda: None  # 已迁位：原 cleanup 不再负责
        new = self.work / "v3home"
        migrate_cmd(old, new, apply=True)
        v2_digest = harness.digest_tree(old)
        switch(old, new)
        st = Storage(old / "state" / "agora.db")  # 切换后 v3 新写入
        try:
            with st.uow():
                st.insert_message(ts_ms=99_000, task_id="gt-running",
                                  kind="note", from_sid="w", to_sid="d",
                                  body=b"post-switch")
                st.append_event(Event(seq=0, ts_ms=99_000, actor="root",
                                      event="post_switch",
                                      task="gt-running"))
        finally:
            st.close()
        new.mkdir()  # 切换后原名位已空——复现「目标位非空」
        with self.assertRaises(CutoverError) as cm:  # 目标位非空拒回滚
            rollback(old, new)
        self.assertEqual(cm.exception.code, "rollback_target_exists")
        self.assertTrue((old / "state" / "agora.db").is_file())  # 零动作
        new.rmdir()
        report = rollback(old, new)
        self.assertEqual(report["v3_home_at"], str(new))
        for rel in ("state/tasks.jsonl", ".agora/root.token"):  # v2 桩级在位
            self.assertTrue((old / rel).is_file(), rel)
        self.assertTrue(list((old / "state" / "messages").glob("*.md")))
        self.assertEqual(harness.digest_tree(old), v2_digest)  # v3 写入零回灌
        store = harness.Store(old)  # v2 侧可启动（桩级装载）
        self.assertEqual(len(store.read_messages()), 2)
        st = Storage(new / "state" / "agora.db")  # v3 新写入随 v3 目录走
        try:
            self.assertTrue(any(m.body == b"post-switch"
                                for m in st.messages_all()))
        finally:
            st.close()


class TestCliContract(_Scratch):  # 断言7
    def test_help_complete(self):
        top = agora("--help")
        self.assertEqual(top.returncode, 0)
        for verb in ("export", "backup", "migrate", "switch", "rollback"):
            self.assertIn(verb, top.stdout)
            sub = agora(verb, "--help")
            self.assertEqual(sub.returncode, 0)
            self.assertIn("退出码", sub.stdout)

    def test_exit_codes_and_structured_errors(self):
        home = seed(self.work / "v3")
        ok = agora("export", home=home)
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(json.loads(ok.stdout)["messages"], 3)  # stdout JSON
        self.assertEqual(agora("backup", "--out", str(self.work / "b.db"),
                               home=home).returncode, 0)
        bad = agora("export", home=self.work / "none")  # 可恢复失败
        self.assertEqual(bad.returncode, 1)
        err = json.loads(bad.stderr)
        self.assertEqual(sorted(err), ["error"])
        self.assertEqual(err["error"]["code"], "no_database")
        self.assertTrue(err["error"]["message"])
        usage = agora("migrate", "--target", "x")  # 用法错
        self.assertEqual(usage.returncode, 2)


class TestSecurity(_Scratch):  # 断言8
    def test_no_token_plaintext_across_artifacts(self):
        src = v2_fixture(self.work)
        target = self.work / "v3home"
        migrate_cmd(src.path, target, apply=True)
        backup = self.work / "bak.db"
        export_home(target)
        export_home(target, out=self.work / "mirror")
        backup_home(target, backup)
        faces = [backup.read_bytes(),
                 (target / LEDGER_PATH).read_bytes(),
                 (target / EVENTS_NAME).read_bytes(),
                 (target / "state" / "migration-report.json").read_bytes(),
                 (self.work / "mirror" / LEDGER_PATH).read_bytes()]
        faces += [p.read_bytes() for p in
                  (target / "state" / "messages").glob("*.md")]
        for face in faces:
            self.assertNotIn(TOKEN.encode(), face)
            self.assertNotIn(b"tok-root-123", face)

    def test_source_read_only_and_no_network(self):
        src = v2_fixture(self.work)
        before = harness.digest_tree(src.path)
        target = self.work / "v3home"
        migrate_cmd(src.path, target, report_path=self.work / "dry.json")
        migrate_cmd(src.path, target, apply=True)
        self.assertEqual(harness.digest_tree(src.path), before)  # 只读再断言
        text = Path(ROOT / "agorad" / "cutover.py").read_text("utf-8")
        for banned in ("socket", "urllib", "subprocess", "http"):
            self.assertNotIn(banned, text)  # 无网络/子进程面


class TestMiniE2E(_Scratch):  # 断言9
    def test_full_chain_once(self):
        src = v2_fixture(self.work)
        old = self.work / "plaza"
        src.path.rename(old)
        src.cleanup = lambda: None
        new = self.work / "v3home"
        steps = [
            ["migrate", "--source", str(old), "--target", str(new)],
            ["migrate", "--source", str(old), "--target", str(new),
             "--apply"],
            ["export", "--home", str(new)],
            ["backup", "--home", str(new), "--out", str(self.work / "bak")],
            ["switch", "--old", str(old), "--new", str(new)],
            ["export", "--home", str(old)],  # 业务最小冒烟：新位整读重建
            ["rollback", "--old", str(old), "--new", str(new)],
        ]
        for step in steps:
            r = agora(*step)
            self.assertEqual(r.returncode, 0,
                             f"{step}: rc={r.returncode} {r.stderr}")
            json.loads(r.stdout)  # 每步 stdout 机读
        for rel in ("state/tasks.jsonl", ".agora/root.token"):  # 旧侧重启
            self.assertTrue((old / rel).is_file(), rel)
        store = harness.Store(old)
        self.assertEqual(len(store.read_messages()), 2)
        self.assertTrue(len(store.load_state().tasks) if hasattr(
            store.load_state(), "tasks") else True)
        # v3 侧冒烟产物在 v3 home（回滚后随目录归位），旧侧无 v3 件
        self.assertFalse((old / "state" / "agora.db").exists())
        self.assertTrue((new / "state" / "agora.db").is_file())


class TestArchitectureMirror(unittest.TestCase):  # 断言10（专项自含镜像）
    def test_cutover_layer_and_sql_confined(self):
        assert_only("agorad/cutover.py",
                    may_import=["agorad.domain", "agorad.storage",
                                "agorad.migrator"])
        assert_only("agora", may_import=["agorad.cutover"])
        for py in (ROOT / "agorad" / "cutover.py", ROOT / "agora"):
            text = py.read_text("utf-8")
            for marker in ("sqlite3", "CREATE TABLE", "INSERT INTO",
                           "SELECT ", "PRAGMA ", "EXPLAIN QUERY PLAN"):
                self.assertNotIn(marker, text, f"{py.name}: {marker}")


if __name__ == "__main__":
    unittest.main()
