"""worktree 错位存量巡检 CLI（Req: F129）。

列出仓工作树内未跟踪的 '*__wt-*' 目录（错位 worktree 存量）：
`python -m agorad.access.wtscan <repo>`——发现逐行打印 exit 1、零发现
无输出 exit 0、用法/仓错 stderr＋exit 2。落位校验真源在
gitx.worktree_location_error（创建闸同源）；本模块纯 stdlib 自持
（access 孤岛铁律），git 接触仅一处只读探针（ls-files，与
main.payload_drift_note 同豁免形态——不经 Gitx 因兄弟 import 被 F2 分层禁）。
"""
import subprocess
import sys
from fnmatch import fnmatch

_WT_PATTERN = "*__wt-*"


def scan_misplaced_worktrees(repo_path):
    """仓工作树内未跟踪 '*__wt-*' 目录清单（相对 posix 路径）。
    未跟踪口径＝git ls-files --others --directory --exclude-standard
    （.gitignore 命中不计）。shortcut: --directory 折叠列表层——错位
    worktree 嵌在另一全未跟踪目录之内时不展开，巡检升级时再补。"""
    r = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", "--others",
         "--directory", "--exclude-standard"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files: {r.stderr.strip()}")
    return [p[:-1] for p in r.stdout.splitlines()
            if p.endswith("/") and fnmatch(p[:-1].rsplit("/", 1)[-1],
                                           _WT_PATTERN)]


def main(argv=None):
    """CLI 面：python -m agorad.access.wtscan <repo>。发现→逐行打印＋
    exit 1；零发现→无输出 exit 0；用法/仓错→stderr＋exit 2。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m agorad.access.wtscan <repo>", file=sys.stderr)
        return 2
    try:
        found = scan_misplaced_worktrees(argv[0])
    except (RuntimeError, subprocess.SubprocessError) as e:
        print(str(e), file=sys.stderr)
        return 2
    for path in found:
        print(path)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
