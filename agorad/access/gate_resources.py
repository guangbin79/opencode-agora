"""F116 跨进程 gate 资源闸：repo 互斥锁＋AGORA_HOME 全局槽闸.

锁语义（Req: F116）：F112 车道只约束单实例——多 agorad 实例/同机其他
gate/checker 进程仍可能对同仓叠跑重验证（HXNativeApp checker 叠跑被 oomd
杀）。本闸两级：repo 锁（canonical project_dir 一仓一锁，跨进程互斥恒 1，
覆盖 Gate.run 全过程）＋host 全局闸（同一 AGORA_HOME 全部 repo gate 共享
N 槽，AGORA_GATE_MAX_CONCURRENT 可配缺省 1，只约束异仓并行数）。取序恒
repo→slot：repo 锁持有者只等槽、槽持有者不再等任何锁——无环不死锁。
flock 随进程退出由内核回收，崩溃/被杀不留死锁（F96 交付锁同口径）；等待＝
阻塞 flock 内核唤醒（零轮询零 CPU）。repo 锁文件落 git common dir
（worktree 归主 .git、多实例同仓共享同一把锁）；git 不可解析（非 git 目录/
测试假路径）回退 $AGORA_HOME/.agora/gate/repo-<sha256(realpath)>.lock——
同 home 内仍互斥。全局槽文件恒在 home 下。
shortcut: 槽全忙时等待者固定阻塞 0 号槽——N>1 且 0 号槽长占时，1..N-1 号
槽空转期间等待者可能多等一个持有周期（各持有者受门看门狗硬顶约束，有界）；
N=1 无此面。需要严格公平再换牌传递式信号量。
access 包纪律：仅 stdlib，不 import 兄弟模块。
"""
import fcntl
import hashlib
import os
import subprocess
import time
from pathlib import Path

REPO_LOCK_NAME = "agora-gate.lock"


def _flock(path, blocking):
    """独占 flock 一个锁文件；非阻塞遇忙返回 None，其余 OSError 如实上抛。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if blocking
                    else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    except OSError:
        fh.close()  # ENOLCK/EIO 等：未持锁就地关闭防泄漏再上抛
        raise
    return fh


def _unlock(fh):
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()  # close 亦释放：异常路径双保险（RepoDeliveryLock 同款）


class GateHandle:
    """一次 gate 占用的全部锁描述（repo＋slot）；release 逆序全释放。

    进程死亡即内核回收全部 flock——崩溃不留死锁；等待者醒后经 Gate.run
    状态前置检查（非 done 直退）即时让位，锁面不残留。
    """

    def __init__(self, repo, limit):
        self.repo = repo
        self.limit = limit
        self.slot = None
        self.waited = 0.0
        self.acquired = None      # time.monotonic()（held 时长口径）
        self.acquired_at = None   # time.time()（任务行投影口径）
        self._fhs = []

    def release(self):
        while self._fhs:
            _unlock(self._fhs.pop())


class GateResources:
    """跨进程资源闸：acquire(repo_dir)＝repo 锁→全局槽（恒此序）。

    多线程/多对象共用同一实例安全：每次 acquire 返回独立 GateHandle，
    无实例级共享可变态（F112 多车道线程并发取放）。同进程两次 open 得
    独立文件描述，flock 互斥按 open file description 生效——跨线程/跨
    进程/跨描述符同口径（F96 同款）。
    """

    def __init__(self, home, max_concurrent=1):
        if isinstance(max_concurrent, bool) \
                or not isinstance(max_concurrent, int) or max_concurrent < 1:
            raise ValueError(
                f"max_concurrent 须为正整数（收到 {max_concurrent!r}）")
        self._gate_dir = Path(home) / ".agora" / "gate"
        self._max = max_concurrent

    @property
    def max_concurrent(self):
        return self._max

    def _repo_lock_path(self, repo_dir):
        """canonical 键：git common dir 优先（worktree/多实例同仓共享），
        git 失败（非 git 目录/cwd 不存在/无 git）回退 home 下 realpath 键。"""
        try:
            r = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                               cwd=str(repo_dir), capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass  # cwd 缺失/无 git/挂死（病态 .git/FUSE）——回退 realpath 键
            # （10s 上界：本调用先于门看门狗起表，无界即门线程楔死面）
        else:
            if r.returncode == 0:
                p = Path(r.stdout.strip())
                if not p.is_absolute():
                    p = Path(repo_dir) / p
                return p / REPO_LOCK_NAME
        key = hashlib.sha256(
            os.path.realpath(str(repo_dir)).encode("utf-8")).hexdigest()[:16]
        return self._gate_dir / f"repo-{key}.lock"

    def acquire(self, repo_dir):
        """阻塞取两级资源；返回 GateHandle（waited/slot/limit 供观测）。

        等待者至多持自己的 repo 锁再等槽——repo 锁无他人竞争方（同仓
        任务本就须串行），槽持有者绝不再等锁：无环。槽扫描先非阻塞遍历
        0..N-1，全忙才阻塞 0 号槽（醒来即持有，见模块头 shortcut）。
        """
        start = time.monotonic()
        repo_fh = _flock(self._repo_lock_path(repo_dir), blocking=True)
        handle = GateHandle(repo_dir, self._max)
        handle._fhs.append(repo_fh)
        try:
            for i in range(self._max):
                slot_fh = _flock(self._gate_dir / f"slot-{i}.lock",
                                 blocking=False)
                if slot_fh is not None:
                    handle.slot = i
                    handle._fhs.append(slot_fh)
                    break
            else:
                handle.slot = 0
                handle._fhs.append(
                    _flock(self._gate_dir / "slot-0.lock", blocking=True))
        except BaseException:
            handle.release()
            raise
        handle.waited = time.monotonic() - start
        handle.acquired = time.monotonic()
        handle.acquired_at = time.time()
        return handle
