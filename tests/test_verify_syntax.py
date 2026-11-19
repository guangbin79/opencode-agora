"""Req: F123 verify/review 登记命令 shell 语法预检（/bin/sh -n 无执行）。

事实修正（任务页）：gate 生产 runner 是 subprocess.Popen(cmd, shell=True)，
true && true 等按 shell 语义执行——合法 &&/||/|/;/相对 cd/多行 shell 必须
放行；预检只拒真语法错误，方言与生产一致（/bin/sh -n -c）。预检器自身
不可用（/bin/sh 缺失/OSError/超时）＝fail-closed 拒登记，不静默放行。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agorad.core.verify_hygiene as vh
from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.gate import _subprocess_runner, _with_shell_exports
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import Rejected, TaskService
from agorad.core.verify_hygiene import find_verify_hygiene_issues
from tests.test_core_task import FakeGitx, FakeOc


class SyntaxGateTest(unittest.TestCase):
    """A 系：create verify_cmd/review_cmd、update verify_cmd 三入口语法预检
    （经既有 F99 卫生闸聚合——三入口同一判定面）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.gitx, self.oc = FakeGitx(), FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.svc = TaskService(self.state, self.auth, self.gitx, self.oc,
                               self.store, self.router, write_task_page,
                               verify_hygiene=find_verify_hygiene_issues)
        self.root = self.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def events(self):
        lines = self.tmp.joinpath("events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line]

    def rejected(self, **kwargs):
        with self.assertRaises(Rejected) as cm:
            self.svc.create("g", "/repo", None, self.root, **kwargs)
        return str(cm.exception)

    def test_valid_shell_operators_register(self):  # Req: F123 A1
        """合法 &&/||/管道/;/相对 cd/多行 heredoc 均登记成功（不误拒）。"""
        for cmd in ("true && true", "true || false", "printf a | cat",
                    "cd sub; pytest", "cat <<'EOF'\nline\nEOF"):
            with self.subTest(cmd=cmd):
                tid = self.svc.create("g", "/repo", None, self.root,
                                      verify_cmd=cmd)
                self.assertEqual(self.state.get(tid)["verify"]["cmd"], cmd)

    def test_create_unclosed_quote_rejected(self):  # Req: F123 A2
        msg = self.rejected(verify_cmd='echo "unclosed')
        self.assertIn("SHELL_SYNTAX", msg)
        self.assertIn("/bin/sh -n -c", msg)  # hint 带复现指引
        self.assertEqual(self.state.snapshot()["tasks"], {})  # 零任务行
        names = [e["event"] for e in self.events()]
        self.assertEqual(names.count("verify_hygiene_rejected"), 1)

    def test_create_eof_terminated_heredoc_allowed(self):  # Req: F123 A2-3 裁定
        """EOF 隐式终止 heredoc 放行：/bin/sh -n 与运行时均 rc=0（预检唯一
        权威＝/bin/sh -n -c，与生产方言一致，不发明更严格 shell 子集）。"""
        tid = self.svc.create("g", "/repo", None, self.root,
                              verify_cmd="run <<EOF\nbody")
        self.assertEqual(self.state.get(tid)["verify"]["cmd"],
                         "run <<EOF\nbody")

    def test_create_structural_error_rejected(self):  # Req: F123 A2
        self.rejected(verify_cmd="if true then fi")

    def test_update_rejects_syntax_error_old_verify_intact(self):  # Req: F123 A5
        tid = self.svc.create("g", "/repo", None, self.root,
                              verify_cmd="true")
        with self.assertRaises(Rejected) as cm:
            self.svc.update_verify(tid, self.root, 'echo "open')
        self.assertIn("SHELL_SYNTAX", str(cm.exception))
        row = self.state.get(tid)["verify"]
        self.assertEqual(row["cmd"], "true")  # 旧 verify 原样保留
        self.assertEqual(row["state"], "registered")

    def test_review_cmd_entry_rejects(self):  # Req: F123 A5
        msg = self.rejected(review_cmd="cd sub && echo 'open")
        self.assertIn("SHELL_SYNTAX", msg)

    def test_rejection_zero_side_effects(self):  # Req: F123 A6
        """拒绝发生在 set_fields 前：verify 未落值、状态未变、fail_streak 不变。"""
        tid = self.svc.create("g", "/repo", None, self.root,
                              verify_cmd="true")
        self.state.set_fields(tid, fail_streak=2)
        with self.assertRaises(Rejected):
            self.svc.update_verify(tid, self.root, "if true then fi")
        t = self.state.get(tid)
        self.assertEqual(t["state"], "todo")  # 状态未变
        self.assertEqual(t["verify"]["cmd"], "true")  # 未落值
        self.assertEqual(t["fail_streak"], 2)  # 连败不动
        self.assertEqual(
            [e["event"] for e in self.events()].count("verify_registered"), 1)

    def test_prechecker_unavailable_fail_closed(self):  # Req: F123 A3
        """/bin/sh 缺失/超时等预检器自身异常＝fail-closed 拒登记，不静默放行。"""
        for boom in (OSError("No such file: /bin/sh"),
                     subprocess.TimeoutExpired("/bin/sh", 1)):
            with self.subTest(boom=type(boom).__name__):
                with patch.object(vh.subprocess, "run", side_effect=boom):
                    msg = self.rejected(verify_cmd="true")
                self.assertIn("SHELL_SYNTAX_PRECHECK", msg)  # 预检器不可用
                self.assertIn("预检器", msg)


class ShellSyntaxCheckerTest(unittest.TestCase):
    """纯检查器契约：SHELL_SYNTAX finding 形状＋静态检查语义与顺序保持。"""

    def setUp(self):
        self.project = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def test_blocks_unclosed_quote(self):  # Req: F123 A2
        out = find_verify_hygiene_issues('echo "open', self.project)
        self.assertEqual([f["code"] for f in out], ["SHELL_SYNTAX"])
        self.assertTrue(out[0]["blocking"])

    def test_finding_shape_and_fragment_truncated(self):  # Req: F123 A2
        cmd = "echo 'open\n" + "x" * 200  # 多行长命令：fragment 安全截断
        out = find_verify_hygiene_issues(cmd, self.project)
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertEqual(set(f), {"code", "fragment", "hint", "blocking"})
        self.assertEqual(f["code"], "SHELL_SYNTAX")
        self.assertNotIn("\n", f["fragment"])  # 审计行单行安全
        self.assertLessEqual(len(f["fragment"]), 100)
        self.assertTrue(f["fragment"].startswith("echo"))

    def test_allows_multiline_heredoc(self):  # Req: F123 A1
        out = find_verify_hygiene_issues(
            "cat <<'EOF'\nbody line\nEOF\necho done", self.project)
        self.assertEqual(out, [])

    def test_static_checks_semantics_and_order_preserved(self):  # Req: F123
        """语法过关后既有 ABSOLUTE_CD/PROJECT_PATH 判定与顺序零变化。"""
        cmd = f"cd /tmp && cat {self.project}/f.txt"
        out = find_verify_hygiene_issues(cmd, self.project)
        self.assertEqual([f["code"] for f in out],
                         ["ABSOLUTE_CD", "PROJECT_PATH"])


class ShellExportsHelperTest(unittest.TestCase):
    """_with_shell_exports 契约：export 前缀全链生效（VAR=x cmd 前缀赋值
    只作用于首条命令——`cmd1 && cmd2` 形态下 cmd2 读不到，禁用）。"""

    def test_export_prefix_form(self):  # Req: F123
        self.assertEqual(
            _with_shell_exports("cd x && pytest",
                                {"AGORA_REVIEW_BASE": "abc123"}),
            "export AGORA_REVIEW_BASE=abc123; cd x && pytest")

    def test_empty_env_passthrough(self):  # Req: F123
        self.assertEqual(_with_shell_exports("pytest -q", {}), "pytest -q")

    def test_rejects_unsafe_name_or_value(self):  # Req: F123
        for env in ({"BAD-NAME": "v"}, {"AGORA_X": "a;b"},
                    {"AGORA_X": "a\nb"}, {"AGORA_X": ""}):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    _with_shell_exports("cmd", env)

    def test_whole_chain_visibility_real_shell(self):  # Req: F123
        """真实 shell 语义：&& 右支（第二条命令）能读到注入变量。"""
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _with_shell_exports(
                'true && printf %s "$AGORA_TEST_BASE"',
                {"AGORA_TEST_BASE": "seen"})
            rc, out = _subprocess_runner(tmp, cmd, timeout=10, ceiling=20)
        self.assertEqual((rc, out), (0, "seen"))


if __name__ == "__main__":
    unittest.main()
