"""Req: F126 项目级 verify 预算画像——配置解析 fail-fast＋Gate 生效预算。

覆盖任务页断言 A1/A7：verify-profiles.json schema 解析与 fail-fast（报因含
路径/key/字段/原因）、Gate 读取 profile timeout 后生效预算＝profile 值。
建单/更新优先级语义（A2-A4）在 test_core_task、投影（A5/A6）在
test_core_report 与 test_access_pages、装配缝在 test_main。
"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from agorad.main import load_verify_profiles


def canon(p):
    return str(Path(p).resolve())


class TestLoadVerifyProfiles(unittest.TestCase):
    """A1 配置解析：缺文件空；合法/非法 schema；fail-fast 报因含路径。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / ".agora" / "verify-profiles.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, payload, mode=0o600):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload if isinstance(payload, str)
                             else json.dumps(payload), encoding="utf-8")
        self.path.chmod(mode)

    def test_missing_file_empty(self):  # Req: F126
        self.assertEqual(load_verify_profiles(self.tmp), {})

    def test_valid_profile_parsed(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 5400,
                                 "note": "check ASAN cold build"}})
        self.assertEqual(load_verify_profiles(self.tmp),
                         {"/repo/hx": {"timeout": 5400,
                                       "note": "check ASAN cold build"}})

    def test_note_optional_absent(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 60}})
        self.assertEqual(load_verify_profiles(self.tmp),
                         {"/repo/hx": {"timeout": 60}})

    def test_missing_path_key_allowed(self):  # Req: F126
        # 配置可先于 clone 存在：路径不存在不拒
        self.write({"/not/cloned/yet": {"timeout": 30}})
        self.assertEqual(load_verify_profiles(self.tmp),
                         {"/not/cloned/yet": {"timeout": 30}})

    def test_bad_json_fail_fast_names_path(self):  # Req: F126
        self.write("{not json")
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_top_level_not_object_rejected(self):  # Req: F126
        self.write([1])
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_bad_timeout_rejected(self):  # Req: F126
        for bad in (0, -1, "600", 1.5, True, None, 10 ** 400):
            self.write({"/repo/hx": {"timeout": bad}})
            with self.assertRaises(ValueError) as cm:
                load_verify_profiles(self.tmp)
            self.assertIn(str(self.path), str(cm.exception))
            self.assertIn("timeout", str(cm.exception))

    def test_value_not_object_rejected(self):  # Req: F126
        self.write({"/repo/hx": 5400})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("/repo/hx", str(cm.exception))

    def test_note_over_200_chars_rejected(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 5, "note": "x" * 201}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("note", str(cm.exception))

    def test_note_non_string_rejected(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 5, "note": 9}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("note", str(cm.exception))

    def test_extra_field_rejected(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 5, "bogus": 1}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("bogus", str(cm.exception))

    def test_relative_key_rejected(self):  # Req: F126
        self.write({"repo/hx": {"timeout": 5}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("绝对路径", str(cm.exception))

    def test_duplicate_canonical_key_rejected(self):  # Req: F126
        import os
        repo = self.tmp / "proj"
        repo.mkdir()
        link = self.tmp / "plink"
        os.symlink(repo, link)
        self.write({str(repo) + "/": {"timeout": 5},
                    str(link): {"timeout": 9}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_canonical_realpath_keys_normalized(self):  # Req: F126
        import os
        repo = self.tmp / "proj"
        repo.mkdir()
        link = self.tmp / "plink"
        os.symlink(repo, link)
        self.write({str(link): {"timeout": 5}})
        self.assertEqual(load_verify_profiles(self.tmp),
                         {canon(repo): {"timeout": 5}})

    def test_wide_permission_warns_not_blocks(self):  # Req: F126
        self.write({"/repo/hx": {"timeout": 5}}, mode=0o644)
        err = io.StringIO()
        with redirect_stderr(err):
            prof = load_verify_profiles(self.tmp)
        self.assertEqual(prof, {"/repo/hx": {"timeout": 5}})  # 不阻断
        self.assertIn("verify-profiles.json", err.getvalue())  # 有告警

    def test_read_error_fail_fast_not_silent_empty(self):  # Req: F126（评审环 R1）
        # 文件在但不可读（此处＝目录形态 EISDIR）：与「缺文件＝空」不同前提，
        # 静默空画像会让全部预算无声回落 global——须 fail-fast 报路径
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_deeply_nested_json_fail_fast_with_path(self):  # Req: F126（评审环 R1）
        # 深嵌套 JSON 使 json.loads 抛 RecursionError（非 ValueError 子类）——
        # 不得逃出 ValueError 契约成裸 traceback
        self.write("[" * 100000 + "]" * 100000)
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_nul_in_key_rejected_with_context(self):  # Req: F126（评审环 R1）
        # key 内嵌 NUL：resolve 抛裸 ValueError 无路径语境——须按 schema 报因格式
        self.write({"/tmp/\x00x": {"timeout": 5}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn(str(self.path), str(cm.exception))

    def test_note_control_chars_rejected(self):  # Req: F126（评审环 R1）
        # 控制字符（NUL 等）不入任务页/投影——与 client_key isprintable 同口径
        self.write({"/repo/hx": {"timeout": 5, "note": "a\x00b"}})
        with self.assertRaises(ValueError) as cm:
            load_verify_profiles(self.tmp)
        self.assertIn("note", str(cm.exception))


class TestGateProfileBudget(unittest.TestCase):
    """A7 Gate 生效预算＝profile 值：TaskService 建单（profile 命中写入
    verify.timeout）→ gate.run FakeRunner 收到 (profile, 4×profile)。
    复用 test_core_gate 假件（FakeGitx/FakeOc/FakeHealth/FakeRunner）。"""

    def setUp(self):
        import tempfile as _tf
        from agorad.access.pages import write_task_page
        from agorad.access.store import Store
        from agorad.core.auth import Auth
        from agorad.core.gate import Gate
        from agorad.core.msg import Router
        from agorad.core.state import State
        from agorad.core.task import TaskService
        from agorad.core.verify_hygiene import find_verify_hygiene_issues
        from tests.test_core_gate import SHA, FakeGitx, FakeHealth, \
            FakeOc, FakeRunner

        self.SHA = SHA
        self.tmp = Path(_tf.mkdtemp())
        self.store = Store(self.tmp)
        self.env = self.tmp / "envs" / "t1"
        (self.env / ".review-loop").mkdir(parents=True)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.runner = FakeRunner((0, "pass"), (0, "pass"))
        self.gx = FakeGitx()
        self.gate = Gate(self.state, self.gx, self.router, self.oc,
                         lambda env: {"head": SHA, "clean": True},
                         FakeHealth(), self.auth, self.store,
                         runner=self.runner)
        self.repo = self.tmp / "repo"
        self.svc = TaskService(self.state, self.auth, self.gx, self.oc,
                               self.store, self.router, write_task_page,
                               verify_hygiene=find_verify_hygiene_issues,
                               verify_profiles={canon(self.repo): {
                                   "timeout": 5400, "note": "ASAN"}})
        self.root = self.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gate_budget_is_profile_value(self):  # Req: F126
        tid = self.svc.create("g", str(self.repo), None, self.root,
                              verify_cmd="true")
        row = self.state.get(tid)
        self.assertEqual(row["verify"]["timeout"], 5400)
        self.assertEqual(row["verify"]["timeout_source"], "profile")
        self.state.set_fields(tid, state="running", env_dir=str(self.env),
                              worker_sid="s-w1")
        self.assertEqual(self.gate.accept_done(tid, "s-w1", self.SHA),
                         "accepted")
        self.gate.run(tid)
        self.assertEqual(self.runner.budgets,
                         [(5400, 21600), (5400, 21600)])  # env＋集成同预算


if __name__ == "__main__":
    unittest.main()
