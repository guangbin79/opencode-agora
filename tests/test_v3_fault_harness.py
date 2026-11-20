"""Req: F129 — 故障注入与差分设施测试（opencode-agora v3 重写 P0-T2）。

断言清单与裁定见 docs/reqs/opencode-agora.md F129；设施本体＝tests/v3_harness.py。
live 交叉锚：full-fields 字段集以真 TaskService.create（真 Store/State/Auth/
Router＋假 oc/gitx，同 test_core_task 装配套路）对照，防冻结清单漂移。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.msg import Router
from agorad.core.state import LEGAL, State
from agorad.core.task import TaskService
from agorad.core.verify_hygiene import find_verify_hygiene_issues
from tests.v3_harness import (
    CREATE_KEYS, DIR_MODE, GATE_HEALTH_KEYS, FaultInjected, FaultInjector,
    StubSide, TempAgoraHome, digest_tree, diff_mismatches, guarded_home_copy,
    home_is_complete, legacy_torn, legacy_unknown, mail_lineage, run_diff,
    task_full_fields, task_state_matrix)


class _FakeGitx:  # create 触达面最小件（symref 钉支＋分支名校验）
    def symref(self, repo_dir):
        return "main"

    def check_branch(self, repo_dir, branch):
        pass


class _FakeOc:  # create 触达面最小件
    def create(self, cwd, boot, title=None, model=None):
        return "s1"

    def inject(self, sid, text, model=None):
        return True

    def session(self, sid):
        return None


class TestFaultInjector(unittest.TestCase):
    """断言①：命名 failpoint，before/after 两相位，可控异常携上下文。"""

    def test_default_fault_carries_context_and_hit(self):
        inj = FaultInjector().register("copy")
        inj.arm("copy", "before")
        with self.assertRaises(FaultInjected) as cm:
            inj.fire("copy", "before", step="tree", dst="/dst")
        e = cm.exception
        self.assertEqual((e.failpoint, e.phase),
                         ("copy", "before"))
        self.assertEqual(e.context, {"step": "tree", "dst": "/dst"})
        self.assertEqual(inj.hits, [("copy", "before", e.context)])

    def test_custom_exception_instance_raised_as_is(self):
        exc = OSError("disk full")
        inj = FaultInjector().register("io").arm("io", "after", exc=exc)
        with self.assertRaises(OSError) as cm:
            inj.fire("io", "after", path="/x")
        self.assertIs(cm.exception, exc)  # 可控异常＝原样抛

    def test_phases_independent_and_unarmed_noop(self):
        inj = FaultInjector().register("fp").arm("fp", "before")
        self.assertIsNone(inj.fire("fp", "after"))  # after 未装填＝no-op
        self.assertEqual(inj.hits, [])
        with self.assertRaises(FaultInjected):
            inj.fire("fp", "before")
        inj.disarm("fp")
        self.assertIsNone(inj.fire("fp", "before"))  # 撤装填后哑火
        self.assertEqual(len(inj.hits), 1)

    def test_unregistered_name_or_phase_rejected(self):
        inj = FaultInjector().register("fp", phases=("before",))
        with self.assertRaises(ValueError):
            inj.fire("ghost", "before")  # 未注册名＝炸（防拼写哑火）
        with self.assertRaises(ValueError):
            inj.arm("fp", "after")  # 未注册相位＝炸


class TestTempAgoraHome(unittest.TestCase):
    """断言②：tmp AGORA_HOME 骨架＋权限可断言＋退出自动清理。"""

    def test_skeleton_permissions_and_credential(self):
        with TempAgoraHome() as h:
            for d in ("state", "state/messages", "wiki", ".agora", "credentials"):
                self.assertTrue((h.path / d).is_dir(), d)
            self.assertTrue(h.permissions_ok())
            p = h.write_credential("ag-1", "tok")
            self.assertEqual(json.loads(p.read_text()), {"token": "tok"})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(h.state.stat().st_mode & 0o777, DIR_MODE)
            self.assertTrue(h.permissions_ok())
            path = h.path
        self.assertFalse(path.exists())  # ctx 退出自动清理

    def test_addcleanup_cleanup(self):
        h = TempAgoraHome()
        self.addCleanup(h.cleanup)
        self.assertTrue(h.path.is_dir())


class TestLegacyTaskFixtures(unittest.TestCase):
    """断言③：legacy fixture 五件可程序化生成且经真 v2 State/Store 加载。"""

    def setUp(self):
        self.home = TempAgoraHome()
        self.addCleanup(self.home.cleanup)

    def test_full_fields_cover_live_create_and_gate_health(self):
        row = task_full_fields(self.home)
        # live 锚：真 TaskService.create 出的行字段集（防冻结清单漂移）
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = Store(tmp)
        state = State(store)
        auth, gitx, oc = Auth(store), _FakeGitx(), _FakeOc()
        svc = TaskService(state, auth, gitx, oc, store, Router(store, state, oc),
                          write_task_page, verify_hygiene=find_verify_hygiene_issues)
        tid = svc.create("g", "/repo", None, auth.issue("main", "root"),
                         verify_cmd="true")
        live = state.get(tid)
        self.assertLessEqual(set(live), set(row))  # create 全字段被覆盖
        self.assertLessEqual(set(CREATE_KEYS), set(row))
        self.assertLessEqual(set(GATE_HEALTH_KEYS), set(row))
        self.assertLessEqual(set(live["verify"]), set(row["verify"]))
        # 经真 State 重载逐字段不丢（v2 快照口径 round-trip）
        self.assertEqual(State(Store(self.home.path)).get("gt-full"), row)

    def test_state_matrix_covers_all_ten_states(self):
        tasks = task_state_matrix(self.home)
        self.assertEqual(len(tasks), 10)
        reloaded = State(Store(self.home.path)).snapshot()["tasks"]
        self.assertEqual({r["state"] for r in reloaded.values()}, set(LEGAL))

    def test_legacy_torn_keeps_last_complete_and_skips_torn(self):
        info = legacy_torn(self.home)
        store = Store(self.home.path)
        tasks = State(store).snapshot()["tasks"]
        self.assertIn(info["survivor"], tasks)  # 末完整快照行胜出
        self.assertNotIn(info["lost"], tasks)   # 断尾行丢弃
        self.assertEqual(store.read_messages(), [])  # 撕裂消息跳过
        self.assertEqual([e["event"] for e in store.read_events()],
                         ["created", "running"])  # 坏事件行丢弃

    def test_legacy_unknown_preserved_and_transient_stripped(self):
        row = legacy_unknown(self.home)
        got = State(Store(self.home.path)).get("gt-unknown")
        self.assertEqual(got["future_v3_field"], row["future_v3_field"])  # 未知字段保留
        self.assertEqual(got["legacy_note"], row["legacy_note"])
        self.assertNotIn("gate_resource", got)  # F116 瞬态：normalize 剔除
        raw = (self.home.state / "tasks.jsonl").read_text(encoding="utf-8")
        self.assertIn("gate_resource", raw)  # 账本原文不动（只规范内存视图）

    def test_mail_lineage_pulls_pending_only(self):
        kit = mail_lineage(self.home)
        old, new = kit["sids"]
        ids = kit["ids"]
        got = kit["router"].check_mail(new)
        # 新 sid 游标=0：旧收件 m1 全量血统补拉＋lineage pending＋新收件 pending
        self.assertEqual([m["id"] for m in got],
                         [ids["read_old"], ids["pending_lineage"],
                          ids["pending_new"]])
        self.assertEqual(kit["router"].check_mail(new), [])  # 拉取即送达不重交
        self.assertEqual(kit["router"].check_mail(old), [])  # 旧 sid：游标下已读
        # F87 游标帽：不越过本收件更老 inflight（ts=300）
        cursor = kit["state"].snapshot()["mail_read"][new]
        self.assertLess(cursor, 300.0)


class TestDiffSkeleton(unittest.TestCase):
    """断言④：同操作序列→外部可见结果记录；桩侧跑通形状。"""

    OPS = [{"tool": "task_create", "args": {"goal": "g"}},
           {"tool": "task_cancel", "args": {"task": "ag-1"}},
           {"tool": "check_mail"}]

    @staticmethod
    def _side():
        s = StubSide()

        def create(args):
            s.emit({"task": "ag-1", "kind": "result", "to": "d"}, "created")
            return 200, {"task": "ag-1"}

        s.script["task_create"] = create
        s.script["task_cancel"] = (403, {"error": {"code": -32003,
                                                   "message": "denied"}})
        return s

    def test_same_script_same_records_and_shape(self):
        a, b = self._side(), self._side()
        recs = run_diff({"v2": a, "v3": b}, self.OPS)
        self.assertEqual(a.calls, b.calls)  # 同序列重放
        self.assertEqual(diff_mismatches(recs), [])
        for rec in recs["v2"]:
            self.assertEqual(set(rec), {"tool", "status", "json", "visible"})
        lens = [len(r["visible"]["messages"]) for r in recs["v2"]]
        self.assertEqual(lens, [1, 1, 1])  # 每步后快照外部可见面
        self.assertEqual(recs["v2"][1]["status"], 403)

    def test_divergence_detected_at_op(self):
        a, b = self._side(), self._side()
        b.script["task_cancel"] = (200, {"ok": True})
        recs = run_diff({"v2": a, "v3": b}, self.OPS)
        mm = diff_mismatches(recs)
        self.assertIn((1, "v2", "v3", "status"), mm)
        self.assertIn((1, "v2", "v3", "json"), mm)
        self.assertNotIn((0, "v2", "v3", "json"), mm)  # 前置一致步不误报


class TestCrashSimulation(unittest.TestCase):
    """断言⑥：failpoint 触发后目标不可用且源目录不变。"""

    def setUp(self):
        self.src = TempAgoraHome()
        self.addCleanup(self.src.cleanup)
        self.src.write_credential("ag-1", "tok")
        page = self.src.wiki / "tasks" / "ag-1.md"
        page.parent.mkdir(parents=True)
        page.write_text("# ag-1\n", encoding="utf-8")
        self.before = digest_tree(self.src.path)
        self.inj = FaultInjector().register("home-copy")
        self.dst_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.dst_root, ignore_errors=True))

    def test_before_fault_target_never_created(self):
        self.inj.arm("home-copy", "before")
        dst = self.dst_root / "h1"
        with self.assertRaises(FaultInjected):
            guarded_home_copy(self.inj, "home-copy", self.src.path, dst)
        self.assertFalse(dst.exists())  # 目标不可用＝未生
        self.assertEqual(digest_tree(self.src.path), self.before)  # 源不变
        self.assertEqual(len(self.inj.hits), 1)

    def test_after_fault_target_incomplete_source_intact(self):
        self.inj.arm("home-copy", "after")
        dst = self.dst_root / "h2"
        with self.assertRaises(FaultInjected):
            guarded_home_copy(self.inj, "home-copy", self.src.path, dst)
        self.assertTrue(dst.exists())
        self.assertFalse(home_is_complete(dst))  # 在盘但不完整＝不可用
        self.assertEqual(digest_tree(self.src.path), self.before)

    def test_clean_copy_completes(self):
        dst = guarded_home_copy(self.inj, "home-copy", self.src.path,
                                self.dst_root / "h3")
        self.assertTrue(home_is_complete(dst))
        self.assertEqual(digest_tree(self.src.path), self.before)


if __name__ == "__main__":
    unittest.main()
