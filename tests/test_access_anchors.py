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

    def _write_base(self, data: bytes):
        (self.tmp / ".review-loop" / ".review-base").write_bytes(data)

    def test_anchor_matches_head(self):
        self._write_marker((self.sha + "\n").encode())
        self._write_base((self.sha + "\n").encode())
        self.assertEqual(read_anchor(str(self.tmp)),
                         {"head": self.sha, "base": self.sha, "clean": True})

    def test_stale_or_missing_anchor_is_none(self):
        self.assertIsNone(read_anchor(str(self.tmp)))  # 缺锚
        self._write_marker(b"deadbeef\n")
        self._write_base((self.sha + "\n").encode())
        self.assertIsNone(read_anchor(str(self.tmp)))  # 落后

    def test_missing_review_base_is_none(self):  # Req: F111
        """旧单件锚（仅 .last-reviewed）：链根不可证——拒收，重建锚。"""
        self._write_marker((self.sha + "\n").encode())
        self.assertIsNone(read_anchor(str(self.tmp)))

    def test_empty_review_base_is_none(self):  # Req: F111
        self._write_marker((self.sha + "\n").encode())
        self._write_base(b"  \n")
        self.assertIsNone(read_anchor(str(self.tmp)))

    def test_corrupt_anchor_bytes_degrade_to_none_not_raise(self):  # Req: ag-e96140
        """锚文件含坏字节：降级失配回 None（done 拒收 fail-closed），不抛解码崩。"""
        self._write_marker(b"\xd6\xd0\xce\xc4")  # GBK 字节
        self._write_base((self.sha + "\n").encode())
        self.assertIsNone(read_anchor(str(self.tmp)))

    def test_short_sha_base_and_marker_equivalent_to_full(self):  # Req: ag-71dd8b
        """ag-4d17ea：评审环工具写短 sha——rev-parse 归一后与全 sha 等价。"""
        short = self.sha[:8]
        self._write_marker((short + "\n").encode())
        self._write_base(short.encode())  # 无换行变体同收
        self.assertEqual(read_anchor(str(self.tmp)),
                         {"head": self.sha, "base": self.sha, "clean": True})

    def test_ambiguous_or_unknown_short_sha_is_none(self):  # Req: ag-71dd8b
        """歧义/未知短 sha：rev-parse 失败＝不可证 → None。"""
        self._write_marker((self.sha + "\n").encode())
        self._write_base(b"1234abcd\n")  # 不在库内的 8 位前缀
        self.assertIsNone(read_anchor(str(self.tmp)))

    def test_garbage_base_is_none(self):  # Req: ag-71dd8b
        """垃圾内容（非 sha 文本）：rev-parse 失败 → None。"""
        self._write_marker((self.sha + "\n").encode())
        self._write_base(b"not-a-sha!!\n")
        self.assertIsNone(read_anchor(str(self.tmp)))

    def test_symbolic_ref_in_anchor_is_none(self):  # Req: ag-71dd8b
        """HEAD/分支名解析到当前 tip——若放行，"锚落后"检测恒真（评审环洞）。"""
        self._write_marker(b"HEAD\n")
        self._write_base((self.sha + "\n").encode())
        self.assertIsNone(read_anchor(str(self.tmp)))  # 标记为符号引用
        self._write_marker(b"main\n")
        self.assertIsNone(read_anchor(str(self.tmp)))  # 标记为分支名
        self._write_marker((self.sha + "\n").encode())
        self._write_base(b"main\n")
        self.assertIsNone(read_anchor(str(self.tmp)))  # 链根为分支名


if __name__ == "__main__":
    unittest.main()
