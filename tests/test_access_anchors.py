"""T5 访问层契约：anchors 评审环锚读取（真实 git，tmp 夹具，零 mock）。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agorad.access.anchors import read_anchor


class TestAccessAnchors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._git(["init", "-q", "-b", "main"], cwd=self.tmp)
        self._git(["config", "user.email", "t@t"], cwd=self.tmp)
        self._git(["config", "user.name", "t"], cwd=self.tmp)
        (self.tmp / "f").write_text("v")
        self._git(["add", "-A"], cwd=self.tmp)
        self._git(["commit", "-q", "-m", "init"], cwd=self.tmp)
        self.sha = self._git(["rev-parse", "HEAD"], cwd=self.tmp).stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _git(args, cwd):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)

    def _write_marker(self, data: bytes):
        d = self.tmp / ".review-loop"
        d.mkdir(exist_ok=True)
        (d / ".last-reviewed").write_bytes(data)

    def test_anchor_matches_head(self):
        self._write_marker((self.sha + "\n").encode())
        self.assertEqual(read_anchor(str(self.tmp)),
                         {"head": self.sha, "clean": True})

    def test_stale_or_missing_anchor_is_none(self):
        self.assertIsNone(read_anchor(str(self.tmp)))  # 缺锚
        self._write_marker(b"deadbeef\n")
        self.assertIsNone(read_anchor(str(self.tmp)))  # 落后

    def test_corrupt_anchor_bytes_degrade_to_none_not_raise(self):  # Req: ag-e96140
        """锚文件含坏字节：降级失配回 None（done 拒收 fail-closed），不抛解码崩。"""
        self._write_marker(b"\xd6\xd0\xce\xc4")  # GBK 字节
        self.assertIsNone(read_anchor(str(self.tmp)))


if __name__ == "__main__":
    unittest.main()
