"""F96 仓级跨进程交付锁：Git common dir 下 flock 锁文件，只护最终 ref 推进互斥。

锁语义（需求裁定）：仅最终交付（final ref advancement/push）持锁——verify 全程
绝不持锁，防长任务把同仓其他实例整个冻住；flock 随进程退出由内核回收，崩溃不
留死锁。锁文件路径取 `git rev-parse --git-common-dir`（worktree 场景解析到主
.git），多实例同仓共享同一把锁。access 包纪律：仅 stdlib，不 import 兄弟模块
（Gitx 经动态 import 取用——静态分层扫描声明的豁免面）。
"""
import fcntl
import subprocess
from pathlib import Path

LOCK_NAME = "agora-delivery.lock"


class RepoDeliveryLock:
    """仓级交付锁 CM：阻塞持锁（with）与非阻塞探测（acquire(blocking=False)）。

    同进程两次 open 得独立文件描述，flock 互斥按 open file description 生效，
    跨进程/跨描述符同口径。
    """

    def __init__(self, repo_dir):
        self.path = self._lock_path(repo_dir)
        self._fh = None

    @staticmethod
    def _lock_path(repo_dir):
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                           cwd=str(repo_dir), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git rev-parse --git-common-dir: {r.stderr.strip()}")
        p = Path(r.stdout.strip())
        if not p.is_absolute():
            p = Path(repo_dir) / p
        return p / LOCK_NAME

    def acquire(self, blocking=True):
        """取锁：成功 True；非阻塞遇冲突 False（blocking 形态等待）。"""
        if self._fh is not None:
            raise RuntimeError("delivery lock already held by this handle")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX if blocking
                        else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        except OSError:
            fh.close()  # ENOLCK/EIO 等：句柄未落 self._fh，就地关闭防泄漏再如实上抛
            raise
        self._fh = fh
        return True

    def release(self):
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()  # close 亦释放：异常路径双保险

    def __enter__(self):
        self.acquire()  # CM 形态＝阻塞持锁（最终交付短临界区）
        return self

    def __exit__(self, *exc):
        self.release()  # 异常路径同样释放
        return False
