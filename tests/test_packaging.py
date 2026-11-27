"""F31 PyPI 分发契约：pyproject 源级锁＋四入口可导入＋installer 定位纯函数。

shortcut: wheel 构建冒烟走网络（pip 拉 setuptools）不入套件，防 flaky——
构建链证据由任务页验收节的人工 venv 冒烟承担，套件只锁源级契约。
"""
import re
import unittest
from pathlib import Path

import agorad.installer
import agorad.main
import agora_mcp
import board_tui

REPO = Path(__file__).resolve().parent.parent


class PyprojectContractTest(unittest.TestCase):
    """pyproject 契约（Req: F31）；py3.10 无 tomllib——文本包含断言，随 test_install.sh 源级锁风格。"""

    def setUp(self):
        self.text = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    def test_name(self):
        self.assertIn('name = "opencode-agora"', self.text)  # Req: F31

    def test_requires_python(self):
        self.assertIn('requires-python = ">=3.10"', self.text)

    def test_scripts(self):
        for line in (
            'agorad = "agorad.main:main"',
            'agora-board = "board_tui:main"',
            'agora-mcp = "agora_mcp:main"',
            'agora-install = "agorad.installer:main"',
        ):
            self.assertIn(line, self.text)  # Req: F31

    def test_data_files_keys(self):
        for key in (
            '"share/opencode-agora"',
            '"share/opencode-agora/agora-plugin"',
            '"share/opencode-agora/agora-plugin/core"',
            '"share/opencode-agora/skills/agora-dispatch"',
            '"share/opencode-agora/skills/agora-ops"',
        ):
            self.assertIn(f"{key} =", self.text)  # Req: F31；core 键 Req: F176

    def test_plugin_data_files_match_repo(self):  # Req: F176
        """数据面清单↔仓内非测试 ts 盘点同步锁（防排除规则漂移静默漏装——
        F176 平铺迁 core/ 时由 OCR round1 实证漂移可静默漏装）。"""
        listed = sorted(re.findall(r'"(agora-plugin/[^"\n]+\.ts)"', self.text))
        on_disk = sorted(
            str(p.relative_to(REPO))
            for p in REPO.glob("agora-plugin/**/*.ts")
            if not p.name.endswith(".test.ts"))
        self.assertEqual(listed, on_disk)

    def test_version_attr(self):
        self.assertIn('attr = "agorad.main._VERSION"', self.text)


class EntryPointsTest(unittest.TestCase):
    """四 entry callable 可导入（Req: F31）。"""

    def test_entries_callable(self):
        for mod in (agorad.main, agora_mcp, board_tui, agorad.installer):
            self.assertTrue(callable(mod.main), mod.__name__)


class FindShareDirTest(unittest.TestCase):
    """find_share_dir 命中与落空（Req: F31）。"""

    def test_hit(self):
        hit = agorad.installer.find_share_dir(["../../../share/opencode-agora/install.sh"])
        self.assertEqual(str(hit), "share/opencode-agora/install.sh")

    def test_miss(self):
        self.assertIsNone(
            agorad.installer.find_share_dir(["opencode_agora-0.1.0.dist-info/METADATA"]))
