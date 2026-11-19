"""F121 unpublished 拆分出版 hunk 级对账：patch_equivalent_on_origin 在
完整 commit patch-id 未命中时回退 per-hunk patch-id 全覆盖对账——交付补丁
被拆进 origin 窗口多个 commit（ag-50ef74 双文件实证）不再误报未出版。

祖先噪音/rename 对称/rebase 漂移零回归用例在 test_access_gitx.py
（TestPublishedOnOrigin，verify 命令同跑）。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agorad.access.gitx import Gitx


class TestPatchSplitEquivalence(unittest.TestCase):
    """真 git、零网络（update-ref 伪造 origin 跟踪 ref）。Req: F121"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        self._git(["init", "-b", "main"], cwd=self.repo)
        self._git(["config", "user.email", "t@t"], cwd=self.repo)
        self._git(["config", "user.name", "t"], cwd=self.repo)
        self.gx = Gitx()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "mainw.py").write_text(
            "a1\na2\na3\na4\na5\n")
        (self.repo / "src" / "rmc.py").write_text("r1\nr2\nr3\nr4\nr5\n")
        self._git(["add", "-A"])
        self._git(["commit", "-qm", "init"])
        self.base = self._git(["rev-parse", "HEAD"]).stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, args, cwd=None, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd or self.repo),
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r

    def _commit(self, files, msg):
        for name, content in files.items():
            (self.repo / name).write_text(content)
        self._git(["add", "-A"])
        self._git(["commit", "-m", msg])
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    def _set_origin(self, sha):
        self._git(["update-ref", "refs/remotes/origin/main", sha])

    def _delivery_two_files(self):
        """work 支交付：双文件各一 hunk（嵌套路径，钉 -r 递归面）。"""
        self._git(["checkout", "-q", "-b", "work"])
        recorded = self._commit(
            {"src/mainw.py": "a1\na2\nA3\na4\na5\n",
             "src/rmc.py": "r1\nr2\nR3\nr4\nr5\n"}, "delivery")
        self._git(["checkout", "-q", "main"])
        return recorded

    def test_split_publish_two_files_true(self):  # A1
        # ag-50ef74 复刻：mainw hunk 混进大 commit、rmc hunk 单独出版——
        # 完整 patch-id 必缺席，hunk 级全覆盖应 True
        recorded = self._delivery_two_files()
        self._commit({"src/mainw.py": "a1\na2\nA3\na4\na5\n",
                      "noise.py": "noise\n"}, "big commit")
        self._commit({"src/rmc.py": "r1\nr2\nR3\nr4\nr5\n"}, "rmc hunk")
        self._set_origin(self._git(["rev-parse", "HEAD"]).stdout.strip())
        self.assertTrue(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))

    def test_split_publish_same_file_two_hunks_true(self):  # A2
        base20 = "".join(f"l{i}\n" for i in range(1, 21))
        hunk1 = base20.replace("l2\n", "L2\n", 1)
        both = hunk1.replace("l18\n", "L18\n", 1)
        self._commit({"src/mainw.py": base20}, "20-line base")
        self._git(["checkout", "-q", "-b", "work"])
        recorded = self._commit({"src/mainw.py": both}, "two hunks one file")
        self._git(["checkout", "-q", "main"])
        self._commit({"src/mainw.py": hunk1}, "publish hunk1")
        self._commit({"src/mainw.py": both}, "publish hunk2")
        self._set_origin(self._git(["rev-parse", "HEAD"]).stdout.strip())
        self.assertTrue(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))

    def test_partial_publish_false(self):  # A3
        # 两个 hunk 只出版 mainw 一个：rmc hunk 缺席 → False（禁 majority）
        recorded = self._delivery_two_files()
        self._commit({"src/mainw.py": "a1\na2\nA3\na4\na5\n"},
                     "only mainw hunk")
        self._set_origin(self._git(["rev-parse", "HEAD"]).stdout.strip())
        self.assertFalse(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))

    def test_hunk_in_unrelated_path_no_match(self):  # A4
        # 交付 a.py 的 hunk；origin 只出版无关路径 b.py 的同内容变更——
        # 合成 patch 含文件路径头，不得跨文件误配
        (self.repo / "a.py").write_text("a1\na2\na3\na4\na5\n")
        self._git(["add", "-A"])
        self._git(["commit", "-qm", "add a.py"])
        self._git(["checkout", "-q", "-b", "work"])
        recorded = self._commit({"a.py": "a1\na2\nA3\na4\na5\n"}, "delivery")
        self._git(["checkout", "-q", "main"])
        self._commit({"b.py": "a1\na2\nA3\na4\na5\n"}, "same hunk other path")
        self._set_origin(self._git(["rev-parse", "HEAD"]).stdout.strip())
        self.assertFalse(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))

    def test_full_patch_id_hit_still_true(self):  # A6
        # 完整 patch-id 命中路径仍直接 True（0b07cc9 语义优先，hunk 只是
        # fallback）；verbatim cherry-pick 出版＝完整等价
        self._git(["checkout", "-q", "-b", "work"])
        recorded = self._commit({"src/mainw.py": "a1\na2\nA3\na4\na5\n"},
                                "delivery")
        self._git(["checkout", "-q", "main"])
        self._commit({"other.py": "x"}, "parallel")  # 改父基：sha 必异
        self._git(["cherry-pick", recorded])
        self._set_origin(self._git(["rev-parse", "HEAD"]).stdout.strip())
        self.assertTrue(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))

    def test_no_tracking_ref_and_empty_patch_false(self):  # G/A7 边界
        self._git(["checkout", "-q", "-b", "work"])
        recorded = self._commit({"src/mainw.py": "a1\na2\nA3\na4\na5\n"},
                                "delivery")
        # 无跟踪 ref：出版无从证实 → False
        self.assertFalse(self.gx.patch_equivalent_on_origin(
            str(self.repo), recorded, "main"))
        # 空补丁（空提交）：无内容可对账，保守 False
        self._git(["commit", "--allow-empty", "-m", "empty"])
        empty = self._git(["rev-parse", "HEAD"]).stdout.strip()
        self._set_origin(self.base)
        self.assertFalse(self.gx.patch_equivalent_on_origin(
            str(self.repo), empty, "main"))
        # merge 提交：diff-tree -p 补丁面为空，保守 False
        self._git(["checkout", "-q", "main"])
        self._git(["commit", "--allow-empty", "-m", "advance main"])
        self._git(["merge", "--no-ff", "-m", "merge work", "work"])
        merged = self._git(["rev-parse", "HEAD"]).stdout.strip()
        self._set_origin(merged)
        self.assertFalse(self.gx.patch_equivalent_on_origin(
            str(self.repo), merged, "main"))


class TestHunkPatchParser(unittest.TestCase):
    """纯解析器 Gitx._hunk_patches：diff-tree -p 输出 → per-hunk 合成 patch。
    Req: F121

    合成 patch＝文件头（diff --git..首个 @@ 前，路径入哈希）+单 hunk 正文；
    commit sha 前言行、文件头均不得算入 hunk 正文。
    """

    def test_split_keeps_header_and_excludes_preamble(self):
        diff = (
            "deadbeef0123456789abcdef0123456789abcdef01\n"  # commit 前言行
            "diff --git a/src/x.py b/src/x.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/x.py\n"
            "+++ b/src/x.py\n"
            "@@ -1,5 +1,5 @@\n"
            " a1\n"
            "-a3\n"
            "+A3\n"
            "@@ -11,5 +11,5 @@\n"
            " b1\n"
            "-b3\n"
            "+B3\n"
            "diff --git a/y.py b/y.py\n"
            "old mode 100644\n"
            "new mode 100755\n")
        patches = Gitx._hunk_patches(diff)
        self.assertEqual(len(patches), 2)
        for p in patches:
            lines = p.splitlines()
            self.assertTrue(lines[0].startswith("diff --git "))
            self.assertEqual(sum(l.startswith("@@ -") for l in lines), 1)
            self.assertNotIn("deadbeef", p)  # commit 前言行不混入
        # 纯 mode change 文件（y.py）无 hunk：不产生合成 patch
        self.assertTrue(all("y.py" not in p for p in patches))

    def test_empty_and_header_only_diff_yields_nothing(self):
        self.assertEqual(Gitx._hunk_patches(""), [])
        self.assertEqual(Gitx._hunk_patches("deadbeef\n"), [])
        binary = ("diff --git a/bin b/bin\n"
                  "Binary files /dev/null and b/bin differ\n")
        self.assertEqual(Gitx._hunk_patches(binary), [])


if __name__ == "__main__":
    unittest.main()
