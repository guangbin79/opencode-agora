"""Req: F99 verify 卫生纯静态检查器契约：ABSOLUTE_CD/PROJECT_PATH 阻断、GLOBAL_STATE 告警、依赖段与引号字面量放行。

纯文本分析（ag-df192c 假红先例：绝对 cd/主仓绝对路径绕开任务 env）。
夹具全部走临时目录，不依赖真实 HOME。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from agorad.core.verify_hygiene import find_verify_hygiene_issues


class VerifyHygieneTest(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.project = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        for d in (self.home, self.project):
            for name in os.listdir(d):
                os.unlink(os.path.join(d, name))
            os.rmdir(d)

    def codes(self, cmd, project=None, home="unset"):
        kwargs = {} if home == "unset" else {"home": home}
        return [f["code"] for f in
                find_verify_hygiene_issues(cmd, project or self.project, **kwargs)]

    def test_absolute_cd_blocks(self):  # Req: F99
        out = find_verify_hygiene_issues("cd /tmp && pytest -q", self.project)
        self.assertEqual([f["code"] for f in out], ["ABSOLUTE_CD"])
        self.assertTrue(out[0]["blocking"])
        self.assertEqual(out[0]["fragment"], "/tmp")

    def test_absolute_cd_quoted_blocks(self):  # Req: F99
        out = find_verify_hygiene_issues("cd '/var/log'", self.project)
        self.assertEqual([f["code"] for f in out], ["ABSOLUTE_CD"])

    def test_absolute_pushd_blocks(self):  # Req: F99
        out = find_verify_hygiene_issues("pushd /opt/wherever; make", self.project)
        self.assertEqual([f["code"] for f in out], ["ABSOLUTE_CD"])
        self.assertTrue(out[0]["blocking"])

    def test_project_script_blocks(self):  # Req: F99
        cmd = f"{self.project}/scripts/run.sh --all"
        out = find_verify_hygiene_issues(cmd, self.project)
        self.assertEqual([f["code"] for f in out], ["PROJECT_PATH"])
        self.assertTrue(out[0]["blocking"])

    def test_source_project_path_blocks(self):  # Req: F99
        for cmd in (f"source {self.project}/env.sh",
                    f". {self.project}/env.sh"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.codes(cmd), ["PROJECT_PATH"])

    def test_home_expansion_project_blocks(self):  # Req: F99
        project = os.path.join(self.home, "repo")
        for cmd in ("ls $HOME/repo/src", "cat ${HOME}/repo/env",
                    "python3 ~/repo/tools/lint.py"):
            with self.subTest(cmd=cmd):
                out = find_verify_hygiene_issues(cmd, project, home=self.home)
                self.assertEqual([f["code"] for f in out], ["PROJECT_PATH"])

    def test_pythonpath_project_blocks(self):  # Req: F99
        cmd = f"PYTHONPATH={self.project}/src:$PATH python3 -m pytest"
        self.assertEqual(self.codes(cmd), ["PROJECT_PATH"])

    def test_project_venv_allowed(self):  # Req: F99
        for dep in ("venv", ".venv", "node_modules", "site-packages"):
            cmd = f"{self.project}/{dep}/bin/tool run"
            with self.subTest(dep=dep):
                self.assertEqual(self.codes(cmd), [])

    def test_external_repo_allowed(self):  # Req: F99
        self.assertEqual(self.codes("/opt/other-repo/bin/tool --x"), [])

    def test_relative_cd_allowed(self):  # Req: F99
        self.assertEqual(self.codes("cd server && pytest -q"), [])

    def test_quoted_grep_literal_allowed(self):  # Req: F99
        cmd = f"grep '{self.project}/agorad' README.md"
        self.assertEqual(self.codes(cmd), [])

    def test_home_config_warns_not_blocks(self):  # Req: F99
        out = find_verify_hygiene_issues(
            "cp conf $HOME/.config/app.conf", self.project, home=self.home)
        self.assertEqual([f["code"] for f in out], ["GLOBAL_STATE"])
        self.assertFalse(out[0]["blocking"])
        for d in (".local/state", ".local/share"):
            cmd = f"cp data ~/{d}/app"
            with self.subTest(d=d):
                out = find_verify_hygiene_issues(cmd, self.project, home=self.home)
                self.assertEqual([f["code"] for f in out], ["GLOBAL_STATE"])

    def test_same_token_not_duplicated(self):  # Req: F99
        cmd = f"cp {self.project}/a.txt {self.project}/a.txt"
        out = find_verify_hygiene_issues(cmd, self.project)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "PROJECT_PATH")

    def test_findings_follow_command_order(self):  # Req: F99
        cmd = f"cd /abs && cat {self.project}/f.txt"
        self.assertEqual(self.codes(cmd), ["ABSOLUTE_CD", "PROJECT_PATH"])

    def test_home_default_from_env(self):  # Req: F99
        with patch.dict(os.environ, {"HOME": self.home}):
            out = find_verify_hygiene_issues(
                "ls $HOME/.local/state/app", self.project)
        self.assertEqual([f["code"] for f in out], ["GLOBAL_STATE"])

    def test_finding_keys_fixed(self):  # Req: F99
        out = find_verify_hygiene_issues("cd /tmp", self.project)
        self.assertEqual(set(out[0]), {"code", "fragment", "hint", "blocking"})
        self.assertTrue(out[0]["hint"])  # hint 非空可读


if __name__ == "__main__":
    unittest.main()
