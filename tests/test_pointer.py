"""gt-32c051 AGORA_HOME 指针文件：MCP 壳启动写指针＋agorad/board env>指针>缺省 回退链。

指针＝~/.agora-home 单行纯文本（Path.home() 认 HOME，测试沙箱化），桥 MCP 配置
env → 无 env 组件统一发现；指针空白/换行 trim。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import board_tui
from agorad.main import env_config

REPO = Path(__file__).resolve().parent.parent


class PointerTest(unittest.TestCase):
    """HOME 沙箱化：指针落自建 home，绝不触碰真机 ~/.agora-home。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def _pointer(self, text):
        (self.home / ".agora-home").write_text(text, encoding="utf-8")

    # ---- a) MCP 壳启动写指针 ----

    def test_mcp_startup_writes_pointer(self):
        # Req: gt-32c051
        plaza = self.tmp / "plaza"
        env = dict(os.environ, AGORA_HOME=str(plaza), HOME=str(self.home))
        subprocess.run([sys.executable, str(REPO / "agora_mcp.py")],
                       env=env, input=b"", capture_output=True, timeout=30)
        pointer = self.home / ".agora-home"
        self.assertTrue(pointer.exists())
        self.assertEqual(pointer.read_text(encoding="utf-8"), str(plaza) + "\n")

    # ---- c) 优先级 env > 指针 > 缺省（agorad）＋ d) trim ----

    def test_env_config_priority(self):
        # Req: gt-32c051
        plaza_p, plaza_e = str(self.tmp / "plaza-p"), str(self.tmp / "plaza-e")
        self._pointer(f"  {plaza_p}  \n")  # 空白/换行夹带：兼验 d) trim
        with mock.patch.dict(os.environ, {"AGORA_HOME": plaza_e, "HOME": str(self.home)}):
            self.assertEqual(env_config()[0], plaza_e)  # env 压过指针
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("AGORA_HOME", None)
            self.assertEqual(env_config()[0], plaza_p)  # 指针回退（trim 后）
            self._pointer("")
            self.assertEqual(env_config()[0], str(Path.home() / "plaza"))  # 缺省

    def test_pointer_bad_bytes_no_crash(self):
        # Req: gt-32c051（评审环 B1：坏字节指针→缺省，不崩）
        (self.home / ".agora-home").write_bytes(b"\xff\xfe\x00binary")
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("AGORA_HOME", None)
            self.assertEqual(env_config()[0], str(Path.home() / "plaza"))

    def test_pointer_multiline_first_line(self):
        # Req: gt-32c051（评审环 B4：多行指针取首行）
        first = self.tmp / "plaza-first"
        self._pointer(f"{first}\nsecond-line\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("AGORA_HOME", None)
            self.assertEqual(env_config()[0], str(first))

    def test_mcp_bad_env_bytes_still_serves(self):
        # Req: gt-32c051（评审环 B2：坏字节 env→桥失败不阻壳，exit 0）
        env = dict(os.environ, AGORA_HOME="\udcffplaza", HOME=str(self.home))
        r = subprocess.run([sys.executable, str(REPO / "agora_mcp.py")],
                           env=env, input=b"", capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0)

    # ---- c/d) board_tui.discover 同链 ----

    def test_board_discover_pointer_trimmed(self):
        # Req: gt-32c051
        p = self.tmp / "plaza-p"
        (p / ".agora").mkdir(parents=True)
        (p / ".agora" / "service.json").write_text(json.dumps({"port": 1}), encoding="utf-8")
        (p / ".agora" / "root.token").write_text("tok\n", encoding="utf-8")
        self._pointer(f"  {p}  \n\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("AGORA_HOME", None)
            base, token = board_tui.discover()
        self.assertEqual(base, "http://127.0.0.1:1")
        self.assertEqual(token, "tok")

    def test_board_discover_env_over_pointer(self):
        # Req: gt-32c051
        self._pointer(str(self.tmp / "plaza-p") + "\n")
        with mock.patch.dict(os.environ, {"AGORA_HOME": str(self.tmp / "plaza-e"),
                                          "HOME": str(self.home)}):
            with self.assertRaises(OSError):  # env 胜出→指针未读→plaza-e 无账本文件
                board_tui.discover()


if __name__ == "__main__":
    unittest.main()
