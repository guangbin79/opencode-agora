"""git 唯一接触点（北向访问层）：worktree 建清/ff-only 合并/原子回滚/闸体检测。

环境形态沿用全局 worktree 铁律语义：目录＝repo 同级父目录下
`<REPO>__wt-task-<tid>__<MMDDHHMM><rand4>`，分支同名，保证 `../` 相对拓扑等价；
线性历史——merge 只走 --ff-only，绝不产生 merge commit。
"""
import secrets
import string
import subprocess
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

_RAND = string.ascii_lowercase + string.digits


class Gitx:
    @staticmethod
    def _git(args, cwd, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r

    def build_env(self, project_dir: str, tid: str, branch: str = None) -> str:
        repo = Path(project_dir).resolve()
        stamp = datetime.now().strftime("%m%d%H%M")
        name = f"{repo.name}__wt-task-{tid}__{stamp}{''.join(secrets.choice(_RAND) for _ in range(4))}"
        env = repo.parent / name  # repo 同级父目录＝../ 相对拓扑等价
        # branch＝起点分支（外部工作流，如 issue/N-fix）；缺省＝仓默认支
        self._git(["worktree", "add", "-b", name, str(env)] + ([branch] if branch else []),
                  cwd=repo)
        return str(env)

    def remove_env(self, env_dir: str) -> None:
        env = Path(env_dir).resolve()
        main = self._main(env)
        self._git(["worktree", "remove", "--force", str(env)], cwd=env)
        r = self._git(["branch", "-d", env.name], cwd=main, check=False)
        if r.returncode != 0:  # 未合并分支 -d 必拒；级联清理不可半途，升 -D
            self._git(["branch", "-D", env.name], cwd=main)

    def merge_ff(self, env_dir: str, target: str, branch: str = None) -> str:
        src = Path(env_dir).resolve().name
        if branch:  # 合入指定分支（外部工作流）；缺省＝仓默认支
            self._git(["checkout", branch], cwd=target)
        env_sha = self._git(["rev-parse", src], cwd=target).stdout.strip()
        r = self._git(["merge", "--ff-only", src], cwd=target, check=False)
        # env 分支为祖先时 git 返回 0 但主干未动（Already up to date）——不算 ok
        head = self._git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        return "ok" if r.returncode == 0 and head == env_sha else "non-ff"

    def head(self, repo_dir: str, branch: str = None) -> str:
        return self._git(["rev-parse", branch or "HEAD"], cwd=repo_dir).stdout.strip()

    def rollback(self, target: str, before_sha: str) -> None:
        self._git(["reset", "--hard", before_sha], cwd=target)

    def touches_gate(self, env_dir: str, patterns) -> bool:
        env = Path(env_dir).resolve()
        names = self._diff_names(env)
        return any(fnmatch(n, p) or fnmatch(Path(n).name, p) for n in names for p in patterns)

    def _diff_names(self, env: Path) -> list[str]:
        main = self._main(env)
        branch = env.name
        base = self._git(["merge-base", branch, "HEAD"], cwd=main).stdout.strip()
        out = self._git(["diff", "--name-only", base, branch], cwd=main).stdout
        return out.splitlines()  # 换行分隔；split() 会把含空格文件名切碎

    @staticmethod
    def _main(env: Path) -> Path:
        out = Gitx._git(["worktree", "list", "--porcelain"], cwd=env).stdout
        first = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree "))
        return Path(first)
