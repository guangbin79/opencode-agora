"""F96 仓级跨进程交付锁：gitlock 上下文管理器（真实 git/flock，tmp 夹具，零 mock）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agorad.access.gitlock import LOCK_NAME, RepoDeliveryLock

ROOT = Path(__file__).resolve().parent.parent

# 独立进程探测脚本：rc=0＝非阻塞取锁成功，rc=1＝锁被他方持有
_PROBE = (
    "import sys\n"
    "from agorad.access.gitlock import RepoDeliveryLock\n"
    "sys.exit(0 if RepoDeliveryLock(sys.argv[1]).acquire(blocking=False) else 1)\n"
)


def _init_repo(path):
    path.mkdir()
    (path / "src.py").write_text("v1\n")
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-m", "init"]):
        subprocess.run(["git", *args], cwd=str(path), check=True,
                       capture_output=True)


class TestRepoDeliveryLock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        _init_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe_acquired(self):
        r = subprocess.run([sys.executable, "-c", _PROBE, str(self.repo)],
                           cwd=str(ROOT), capture_output=True)
        self.assertIn(r.returncode, (0, 1), r.stderr.decode()[:400])
        return r.returncode == 0

    def test_lock_file_under_git_common_dir(self):  # Req: F96
        lock = RepoDeliveryLock(self.repo)
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                                cwd=str(self.repo), capture_output=True,
                                text=True, check=True).stdout.strip()
        base = Path(common) if os.path.isabs(common) else self.repo / common
        self.assertEqual(lock.path, base / LOCK_NAME)  # 锁文件在 Git common dir 下

    def test_second_process_blocked_then_freed(self):  # Req: F96
        lock = RepoDeliveryLock(self.repo)
        with lock:
            self.assertFalse(self._probe_acquired())  # 独立进程观锁被占
        self.assertTrue(self._probe_acquired())  # CM 退出即释放

    def test_lock_released_on_exception(self):  # Req: F96
        lock = RepoDeliveryLock(self.repo)
        with self.assertRaises(RuntimeError):
            with lock:
                raise RuntimeError("boom")
        self.assertTrue(self._probe_acquired())  # 异常路径可靠释放

    def test_nonblocking_probe_in_process(self):  # Req: F96
        a, b = RepoDeliveryLock(self.repo), RepoDeliveryLock(self.repo)
        self.assertTrue(a.acquire(blocking=False))
        self.assertFalse(b.acquire(blocking=False))  # 第二句柄非阻塞不可得
        a.release()
        self.assertTrue(b.acquire(blocking=False))  # 释放后可再得
        b.release()
        self.assertTrue(RepoDeliveryLock(self.repo).acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
