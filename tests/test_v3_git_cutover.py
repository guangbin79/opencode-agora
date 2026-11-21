"""Req: F157 — GitStore Phase 3 cutover（spec wiki/spec/gitstore-phase3-cutover.md）。

断言清单（spec「Tests」逐条机械判定）：
TestGitCutoverMigrate——dry-run 零写入（源指纹/目标零创建/报告落请求位）、
apply F156 标记/报告语义（JSON head/txs 与盘面真值对账）、重跑幂等（计数
等价）、中止归一 migration_aborted（源零修改）。
TestGitCutoverExport——messages/events/ledger 自 GitStore 权威重建、孤儿
清扫（有界单调 id 规则）、secret 排除（明文牌不出任何导出面）、重启等价
（重开重放再导出字节一致）、零 SQLite 件、journal 零触碰（head/tx 前后
不变）。
TestGitCutoverBackup——bundle 创建＋git bundle verify 校验＋head 对账、
失败 tmp 全清零残件、明文 token 不入包、拒覆写已存在目的地。
TestGitCutoverSwitchRollback——完整性门（对象损毁/缺活 secret＝零动作拒）、
门序（标记/同级/外来账本）、冒烟（任务面整读＋head/tx 计数）、切换清单
（head/tx/迁移报告指纹/时间戳）、同级两步互换与失败恢复、回滚（未前移
放行/业务 tx 前移拒＋双侧目录不动/清单缺席拒）、CLI 后端旗（sqlite 缺省
不变/未知 backend exit 2）。
"""
import hashlib
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path

from agorad.cutover import CutoverError, LEDGER_PATH
from agorad.domain import Credential, DeliveryReceipt, Event, Task, Timer
from agorad.gitcutover import (SWITCH_MANIFEST, backup_home, export_home,
                               migrate_cmd, rollback, switch)
from agorad.gitstore import GitStore
from agorad.migrator import MARKER, REPORT_PATH
from agorad.storage import StorageError
from tests import v3_harness as harness
from tests.test_v3_cutover_cli import TOKEN, _Scratch, agora, seed, v2_fixture

JOURNAL_REF = "refs/agora/journal"


def _write_secret(path, token):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"token": token}, f)


def git_seed(parent, name="v3git", *, with_credential=True):
    """直建 git home：与 SQLite seed 同构语料（2 任务/3 消息含非 UTF-8
    body＋replay_of/2 事件/timer/cursor/receipt/credential）——export/
    backup/switch 断言语料；credential 需外部 secret 先落盘（F151 铁则）。"""
    home = Path(parent) / name
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / ".agora").mkdir(parents=True, exist_ok=True)
    if with_credential:
        _write_secret(home / ".agora" / "root.token", TOKEN)
    st = GitStore(home / "state")
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
            st.upsert_cursor("d", m1.id)
            st.upsert_receipt(DeliveryReceipt(message_id=m1.id,
                                              status="delivered"))
            if with_credential:
                st.put_credential(Credential(sid="root", token=TOKEN))
    finally:
        st.close()
    return home


def _marker(home):
    """F156 形完成标记（JSON：head/txs 快照）。"""
    st = GitStore(Path(home) / "state")
    try:
        head, txs = st.head, st.tx_count
    finally:
        st.close()
    (Path(home) / MARKER).write_text(
        json.dumps({"complete": True, "backend": "git", "head": head,
                    "txs": txs}, sort_keys=True), encoding="utf-8")


def git_pair(work, tag=""):
    """切换对：v2 桩 old（keep.txt）＋git seed new（含完成标记）。"""
    old = work / f"plaza{tag}"
    old.mkdir()
    (old / "state").mkdir()
    (old / "keep.txt").write_text("v2")
    new = git_seed(work, f"v3git{tag}")
    _marker(new)
    return old, new


def _journal_facts(home):
    st = GitStore(Path(home) / "state")
    try:
        return st.head, st.tx_count
    finally:
        st.close()


def _bundle_heads(bundle, state):
    """独立于被测面校验 bundle：git bundle verify＋list-heads 解析。"""
    env = {"PATH": os.environ.get("PATH", ""),
           "GIT_DIR": str(Path(state) / "journal.git"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    ok = subprocess.run(["git", "bundle", "verify", str(bundle)],
                        env=env, capture_output=True)
    assert ok.returncode == 0, ok.stderr
    out = subprocess.run(["git", "bundle", "list-heads", str(bundle)],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return {ref: sha for sha, ref in
            (line.split(" ", 1) for line in out.stdout.splitlines())}


class TestGitCutoverMigrate(_Scratch):
    def test_dry_run_zero_writes_report_readable(self):
        src = v2_fixture(self.work)
        before = harness.digest_tree(src.path)
        target = self.work / "v3git"
        report_path = self.work / "dry.json"
        report = migrate_cmd(src.path, target, report_path=report_path)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["target"], str(target))
        self.assertEqual(report["backend"], "git")
        self.assertIsInstance(report["journal"]["head"], str)
        self.assertGreaterEqual(report["journal"]["txs"], 1)
        self.assertFalse(target.exists())  # 目标零创建
        self.assertEqual(harness.digest_tree(src.path), before)  # 源只读
        on_disk = json.loads(report_path.read_text("utf-8"))
        self.assertEqual(on_disk["tasks"]["total"], 10)
        self.assertEqual(on_disk["messages"]["count"], 2)

    def test_apply_marker_report_idempotent(self):
        src = v2_fixture(self.work)
        before = harness.digest_tree(src.path)
        target = self.work / "v3git"
        report = migrate_cmd(src.path, target, apply=True)
        self.assertIsNone(report.get("dry_run"))
        self.assertEqual(report["backend"], "git")
        marker = json.loads((target / MARKER).read_text("utf-8"))
        self.assertEqual((marker["complete"], marker["backend"]),
                         (True, "git"))
        on_disk = json.loads((target / REPORT_PATH).read_text("utf-8"))
        self.assertEqual(on_disk["journal"]["head"], marker["head"])
        st = GitStore(target / "state")  # 标记与盘面真值对账
        try:
            self.assertEqual((st.head, st.tx_count),
                             (marker["head"], marker["txs"]))
            counts1 = (len(st.tasks_all()), len(st.messages_all()),
                       len(st.events_all()), st.tx_count)
        finally:
            st.close()
        migrate_cmd(src.path, target, apply=True)  # 重跑幂等（确定性重建）
        st = GitStore(target / "state")
        try:
            marker2 = json.loads((target / MARKER).read_text("utf-8"))
            self.assertEqual((st.head, st.tx_count),
                             (marker2["head"], marker2["txs"]))
            self.assertEqual(
                (len(st.tasks_all()), len(st.messages_all()),
                 len(st.events_all()), st.tx_count), counts1)
        finally:
            st.close()
        self.assertEqual(harness.digest_tree(src.path), before)
        r = agora("migrate", "--backend", "git", "--source", str(src.path),
                  "--target", str(self.work / "v3cli"))
        self.assertEqual(r.returncode, 0, r.stderr)  # CLI git 旗贯通
        self.assertEqual(json.loads(r.stdout)["backend"], "git")
        self.assertFalse((self.work / "v3cli").exists())  # 缺省 dry-run

    def test_abort_maps_recoverable_cli(self):
        empty = harness.TempAgoraHome(root=self.work)  # 无快照源
        target = self.work / "v3git"
        r = agora("migrate", "--backend", "git",
                  "--source", str(empty.path), "--target", str(target))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stderr)["error"]["code"],
                         "migration_aborted")
        self.assertFalse(target.exists())


class TestGitCutoverExport(_Scratch):
    def setUp(self):
        super().setUp()
        self.home = git_seed(self.work)

    def _derived(self):
        home = self.home
        return {str(p.relative_to(home)): p.read_bytes() for p in
                [*sorted((home / "state" / "messages").glob("*.md")),
                 home / "events.jsonl", home / LEDGER_PATH]}

    def test_rebuild_messages_events_ledger(self):
        report = export_home(self.home)
        self.assertEqual(report["backend"], "git")
        self.assertEqual((report["messages"], report["events"]), (3, 2))
        names = sorted(p.name for p in
                       (self.home / "state" / "messages").glob("*.md"))
        self.assertEqual(names, ["00000001.md", "00000002.md",
                                 "00000003.md"])
        raw = (self.home / "state" / "messages" / "00000002.md").read_bytes()
        head, sep, body = raw.partition(b"\n\n")
        self.assertEqual(sep, b"\n\n")
        self.assertEqual(head.decode(),
                         "id: 2\nfrom: w\nkind: note\ntask: gt-a\nto: d\n"
                         "ts: 2.0")  # v2 兼容 frontmatter 同构
        self.assertEqual(body, b"\xff\xfe raw \x00 bytes")  # body 逐字节
        lines = (self.home / "events.jsonl").read_text(
            encoding="utf-8").splitlines()
        first, second = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual((first["ts"], first["actor"], first["event"],
                          first["task"]), (1.0, "root", "created", "gt-a"))
        self.assertEqual(second["k"], [1, 2])  # detail 平展保真
        ledger = json.loads((self.home / LEDGER_PATH).read_text("utf-8"))
        self.assertEqual(ledger["counts"],
                         {"tasks": 2, "messages": 3, "events": 2})
        self.assertEqual([t["id"] for t in ledger["tasks"]], ["gt-a", "gt-b"])
        self.assertEqual(ledger["timers"][0]["state"], "pending")  # 枚举落值
        self.assertEqual(ledger["cursors"], {"d": 1})
        self.assertEqual(ledger["receipts"][0]["status"], "delivered")
        self.assertNotIn("approvals", ledger)  # GitStore 无该投影面
        self.assertEqual(ledger["credentials"], "excluded (Req: F140 断言8)")
        text = (self.home / LEDGER_PATH).read_text("utf-8")
        self.assertEqual(json.dumps(ledger, ensure_ascii=False,
                                    sort_keys=True, indent=1), text)

    def test_journal_untouched_stale_sweep_and_restart_equivalence(self):
        before = _journal_facts(self.home)
        first = export_home(self.home)
        self.assertEqual(_journal_facts(self.home),
                         (first["journal"]["head"], first["journal"]["txs"]))
        self.assertEqual(_journal_facts(self.home), before)  # 派生零触碰
        snapshot = self._derived()
        (self.home / "state" / "messages" / "00000002.md").unlink()
        (self.home / "events.jsonl").write_text('{"ts": 4.0, "actor": "t')
        (self.home / "state" / "messages" / "00000000.md").write_bytes(
            b"id: 0\nstale\n\nx")  # id≤快照 max 的库外件——清扫
        (self.home / "state" / "messages" / "00000009.md").write_bytes(
            b"id: 9\nnewer\n\nx")  # 快照外新件——保留
        export_home(self.home)  # 重启等价：重开重放（新实例）再导出
        again = self._derived()
        self.assertEqual(sorted(again), [
            "events.jsonl", "state/ledger.json",
            "state/messages/00000001.md", "state/messages/00000002.md",
            "state/messages/00000003.md", "state/messages/00000009.md"])
        self.assertEqual(again["state/messages/00000009.md"],
                         b"id: 9\nnewer\n\nx")
        for rel in ("state/messages/00000001.md",
                    "state/messages/00000002.md",
                    "state/messages/00000003.md",
                    "events.jsonl", "state/ledger.json"):
            self.assertEqual(again[rel], snapshot[rel], rel)
        self.assertEqual(_journal_facts(self.home), before)

    def test_secret_exclusion_and_no_sqlite_file(self):
        export_home(self.home)
        faces = [(self.home / "events.jsonl").read_bytes(),
                 (self.home / LEDGER_PATH).read_bytes()]
        faces += [p.read_bytes() for p in
                  (self.home / "state" / "messages").glob("*.md")]
        for face in faces:
            self.assertNotIn(TOKEN.encode(), face)  # 明文牌不出导出面
        self.assertFalse((self.home / "state" / "agora.db").exists())

    def test_missing_journal_rejected(self):
        empty = self.work / "nohome"
        empty.mkdir()
        with self.assertRaises(CutoverError) as cm:
            export_home(empty)
        self.assertEqual(cm.exception.code, "no_journal")
        r = agora("export", "--backend", "git", "--home", str(empty))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stderr)["error"]["code"], "no_journal")


class TestGitCutoverBackup(_Scratch):
    def setUp(self):
        super().setUp()
        self.home = git_seed(self.work)
        self.dest = self.work / "journal.bundle"

    def test_bundle_verified_counts_and_no_plaintext(self):
        report = backup_home(self.home, self.dest)
        self.assertEqual(report["backend"], "git")
        self.assertTrue(report["verified"])
        self.assertEqual(report["ref"], JOURNAL_REF)
        self.assertEqual(report["counts"],
                         {"tasks": 2, "messages": 3, "events": 2, "txs": 1})
        self.assertTrue(self.dest.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o600)
        st = GitStore(self.home / "state")  # head 对账（盘面真值）
        try:
            self.assertEqual(report["head"], st.head)
        finally:
            st.close()
        heads = _bundle_heads(self.dest, self.home / "state")  # 独立校验
        self.assertEqual(heads.get(JOURNAL_REF), report["head"])
        self.assertNotIn(TOKEN.encode(), self.dest.read_bytes())

    def test_tmp_cleanup_on_failure(self):
        import unittest.mock
        tmp = self.dest.with_name(self.dest.name + ".tmp")
        with unittest.mock.patch.object(
                GitStore, "bundle_journal",
                side_effect=StorageError("boom")):
            with self.assertRaises(CutoverError) as cm:
                backup_home(self.home, self.dest)
        self.assertEqual(cm.exception.code, "backup_failed")
        self.assertFalse(tmp.exists())  # 失败零残件
        self.assertFalse(self.dest.exists())
        with unittest.mock.patch.object(
                GitStore, "verify_bundle",
                side_effect=StorageError("bad")):
            with self.assertRaises(CutoverError) as cm:
                backup_home(self.home, self.dest)
        self.assertEqual(cm.exception.code, "backup_verify_failed")
        self.assertFalse(tmp.exists())  # 未过校验零目的地件
        self.assertFalse(self.dest.exists())
        r = agora("backup", "--backend", "git", "--home", str(self.home),
                  "--out", str(self.dest))
        self.assertEqual(r.returncode, 0, r.stderr)  # 失败后重试可成功

    def test_existing_destination_refused(self):
        backup_home(self.home, self.dest)
        with self.assertRaises(CutoverError) as cm:
            backup_home(self.home, self.dest)
        self.assertEqual(cm.exception.code, "backup_exists")


class TestGitCutoverSwitchRollback(_Scratch):
    def test_integrity_gate_zero_action(self):
        old, new = git_pair(self.work)
        objs = sorted(p for p in
                      (new / "state" / "journal.git" / "objects").rglob("*")
                      if p.is_file())
        os.chmod(objs[0], 0o644)  # git 松散对象只读（0444）——先放开再腐蚀
        objs[0].write_bytes(b"garbage-not-an-object")  # 对象字节腐蚀
        with self.assertRaises(CutoverError) as cm:
            switch(old, new)
        self.assertEqual(cm.exception.code, "integrity_gate_failed")
        self.assertTrue((old / "keep.txt").is_file())  # 零动作
        self.assertTrue((new / MARKER).is_file())
        self.assertFalse(old.with_name(old.name + ".swapped").exists())
        old2, new2 = git_pair(self.work, "2")
        (new2 / ".agora" / "root.token").unlink()  # 活牌 secret 缺席
        with self.assertRaises(CutoverError) as cm:
            switch(old2, new2)
        self.assertEqual(cm.exception.code, "integrity_gate_failed")
        self.assertTrue((old2 / "keep.txt").is_file())

    def test_gate_order_marker_siblings_foreign_ledger(self):
        old, new = git_pair(self.work)
        (new / MARKER).unlink()
        with self.assertRaises(CutoverError) as cm:
            switch(old, new)
        self.assertEqual(cm.exception.code, "new_home_incomplete")
        _marker(new)
        elsewhere = git_seed(self.work / "elsewhere", "v3")
        _marker(elsewhere)
        with self.assertRaises(CutoverError) as cm:
            switch(old, elsewhere)
        self.assertEqual(cm.exception.code, "not_siblings")
        (new / "state" / "agora.db").write_bytes(b"sqlite here")
        with self.assertRaises(CutoverError) as cm:
            switch(old, new)
        self.assertEqual(cm.exception.code, "foreign_ledger")
        self.assertTrue((old / "keep.txt").is_file())  # 均零动作
        (new / "state" / "agora.db").unlink()

    def test_switch_smoke_manifest_swap_and_rollback(self):
        src = v2_fixture(self.work)  # 真迁移面：报告指纹可对账
        old = self.work / "plaza"
        src.path.rename(old)
        src.cleanup = lambda: None
        new = self.work / "v3git"
        migrate_cmd(old, new, apply=True)
        report = switch(old, new)
        self.assertEqual(report["backend"], "git")
        self.assertEqual(report["smoke_tasks"], 10)  # 冒烟：任务面整读
        manifest = json.loads((old / SWITCH_MANIFEST).read_text("utf-8"))
        self.assertEqual((manifest["head"], manifest["txs"]),
                         (report["journal"]["head"],
                          report["journal"]["txs"]))
        self.assertEqual(
            manifest["migration_report"]["sha256"],
            hashlib.sha256((old / REPORT_PATH).read_bytes()).hexdigest())
        self.assertIsInstance(manifest["switched_at_ms"], int)
        self.assertEqual(_journal_facts(old),
                         (manifest["head"], manifest["txs"]))
        self.assertTrue((old / "state" / "journal.git").is_dir())
        self.assertFalse((old / "state" / "agora.db").exists())
        swapped = old.with_name(old.name + ".swapped")
        self.assertTrue((swapped / "state" / "tasks.jsonl").is_file())
        out = rollback(old, new)  # 切换后零业务写——放行回滚
        self.assertEqual(out["rolled_back_to"], str(old))
        self.assertTrue((old / "state" / "tasks.jsonl").is_file())  # v2 回位
        self.assertFalse(swapped.exists())
        self.assertEqual(_journal_facts(new),
                         (manifest["head"], manifest["txs"]))  # git home 无损

    def test_rollback_blocked_after_business_tx(self):
        old, new = git_pair(self.work)
        switch(old, new)
        st = GitStore(old / "state")  # 业务 tx：新消息前移 journal
        try:
            with st.uow():
                st.insert_message(ts_ms=99_000, task_id="gt-a",
                                  kind="note", from_sid="w", to_sid="d",
                                  body=b"post-switch")
        finally:
            st.close()
        with self.assertRaises(CutoverError) as cm:
            rollback(old, new)
        self.assertEqual(cm.exception.code, "rollback_blocked")
        self.assertFalse(cm.exception.recoverable)
        self.assertIn("手工", cm.exception.message)  # 显式人工路径
        self.assertTrue((old / "state" / "journal.git").is_dir())  # 双侧不动
        self.assertTrue(old.with_name(old.name + ".swapped").is_dir())
        self.assertFalse(new.exists())
        r = agora("rollback", "--backend", "git", "--old", str(old),
                  "--new", str(new))
        self.assertEqual(r.returncode, 2)  # 终局（人工决策面）
        manifest = json.loads((old / SWITCH_MANIFEST).read_text("utf-8"))
        manifest["head"], manifest["txs"] = _journal_facts(old)  # 清障重锚
        (old / SWITCH_MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8")
        out = rollback(old, new)
        self.assertEqual(out["v3_home_at"], str(new))

    def test_rollback_manifest_missing_and_target_exists(self):
        old, new = git_pair(self.work)
        switch(old, new)
        (old / SWITCH_MANIFEST).unlink()
        with self.assertRaises(CutoverError) as cm:
            rollback(old, new)
        self.assertEqual(cm.exception.code, "switch_manifest_missing")
        self.assertTrue((old / "state" / "journal.git").is_dir())
        old2, new2 = git_pair(self.work, "2")
        switch(old2, new2)
        new2.mkdir()
        with self.assertRaises(CutoverError) as cm:
            rollback(old2, new2)
        self.assertEqual(cm.exception.code, "rollback_target_exists")
        self.assertTrue((old2 / "state" / "journal.git").is_dir())

    def test_swap_failure_recovery(self):
        import unittest.mock
        old, new = git_pair(self.work)
        real_rename = os.rename

        def make_flaky(bad):
            calls = {"n": 0}

            def flaky(a, b):
                calls["n"] += 1
                if calls["n"] in bad:
                    raise OSError(5, "popularity")
                return real_rename(a, b)
            return flaky

        with unittest.mock.patch("agorad.cutover.os.rename",
                                 make_flaky({2})):
            with self.assertRaises(CutoverError) as cm:  # 第二步败＝回退
                switch(old, new)
        self.assertEqual(cm.exception.code, "swap_failed")
        self.assertTrue((old / "keep.txt").is_file())  # 第一步已回退
        self.assertTrue((new / MARKER).is_file())
        swapped = old.with_name(old.name + ".swapped")
        with unittest.mock.patch("agorad.cutover.os.rename",
                                 make_flaky({2, 3})):
            with self.assertRaises(CutoverError) as cm:  # 回退也败＝手工
                switch(old, new)
        self.assertEqual(cm.exception.code, "swap_broken")
        self.assertIn("mv", cm.exception.message)
        self.assertTrue(swapped.is_dir())  # 现场保持
        self.assertFalse(old.exists())
        real_rename(swapped, old)  # 手工归位（错误文案给的路）

    def test_cli_backend_flag_defaults_and_unknown(self):
        sqlite_home = seed(self.work / "v3sql")
        r = agora("export", home=sqlite_home)  # 缺省 sqlite 不变
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["messages"], 3)
        git_home = git_seed(self.work / "v3git2")
        r = agora("export", "--backend", "git", "--home", str(git_home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["backend"], "git")
        bad = agora("export", "--backend", "oracle", "--home",
                    str(git_home))
        self.assertEqual(bad.returncode, 2)  # 未知 backend fail-closed


if __name__ == "__main__":
    unittest.main()
