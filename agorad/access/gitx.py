"""git 唯一接触点（北向访问层）：worktree 建清/ff-only 合并/原子回滚/闸体检测。

环境形态沿用全局 worktree 铁律语义：目录＝repo 同级父目录下
`<REPO>__wt-task-<tid>__<MMDDHHMM><rand4>`（REPO 经 _slug 消毒为 ref 安全名），分支同名，保证 `../` 相对拓扑等价；
线性历史——merge 只走 --ff-only，绝不产生 merge commit。
"""
import secrets
import re
import string
import shutil
import subprocess
import time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

_RAND = string.ascii_lowercase + string.digits
# rmtree 撞写重试参数：worker 评审环进程合并后仍可能往 env/.review-loop/ 写文件
_RMT_TRIES = 4
_RMT_WAIT = 0.5


class Gitx:
    @staticmethod
    def _slug(name: str) -> str:
        # repo 名消毒为 ref 安全 slug：ref 禁字符→_、@{ 序列断开、折叠 ..、去首部 ./-（.dotfiles 类仓名可派发）
        s = name.replace("@{", "@_")  # ref 只禁 @{ 序列，{ 单独合法
        s = re.sub(r"[\x00-\x20\x7f~^:?*\[\\]", "_", s)
        return re.sub(r"\.{2,}", ".", s).lstrip(".-") or "repo"

    @staticmethod
    def _git(args, cwd, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r

    def build_env(self, project_dir: str, tid: str, branch: str = None) -> str:
        repo = Path(project_dir).resolve()
        stamp = datetime.now().strftime("%m%d%H%M")
        name = f"{self._slug(repo.name)}__wt-task-{tid}__{stamp}{''.join(secrets.choice(_RAND) for _ in range(4))}"
        env = repo.parent / name  # repo 同级父目录＝../ 相对拓扑等价
        # branch＝起点分支（外部工作流，如 issue/N-fix）；缺省＝仓默认支。
        # 起点全限定 refs/heads/<branch>：裸名解析 tag 优先（同名 tag 遮蔽致错基线）
        self._git(["worktree", "add", "-b", name, str(env)]
                  + ([f"refs/heads/{branch}"] if branch else []), cwd=repo)
        return str(env)

    def remove_env(self, env_dir: str) -> None:
        env = Path(env_dir).resolve()
        try:
            main = self._main(env)  # 最先取：注销后 env 不再是 worktree，_main 会失败
        except (RuntimeError, FileNotFoundError):  # 含 env 已整体消失（kill 窗口残留）
            main = env.parent / env.name.partition("__wt-task-")[0]
        if not main.exists():  # slug≠原始仓名（.dotfiles 类）：重建失真，宁抛不盲删
            raise RuntimeError(f"env 回收失败：主仓定位失败（{main} 不存在）: {env}")
        if env.exists():  # env 已删净仅剩分支：跳过目录清理，直落分支级联
            self._git(["worktree", "remove", "--force", str(env)], cwd=env, check=False)
            for _ in range(_RMT_TRIES):  # 评审环并发写撞 rmtree：重试收敛，删不掉禁假账
                shutil.rmtree(env, ignore_errors=True)
                if not env.exists():
                    break
                time.sleep(_RMT_WAIT)
            if env.exists():  # 重试走完仍残留＝并发写入未停，如实上抛（分支留给重试收敛）
                raise RuntimeError(f"env 目录删除失败（并发写入未停）: {env}")
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
        # 全限定 refs/heads/<branch>：裸名 rev-parse tag 优先，同名 tag 会给出错误回滚点
        return self._git(["rev-parse", f"refs/heads/{branch}" if branch else "HEAD"],
                         cwd=repo_dir).stdout.strip()

    def check_branch(self, repo_dir: str, branch: str) -> None:
        """信任边界校验：引用名语法（防 - 开头选项注入）＋分支存在（防裸 SHA detached 丢提交）。"""
        self._git(["check-ref-format", "--branch", branch], cwd=repo_dir)
        r = self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                      cwd=repo_dir, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"分支不存在: {branch}")

    def symref(self, repo_dir: str) -> str:
        return self._git(["rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=repo_dir).stdout.strip()

    def checkout(self, repo_dir: str, branch: str) -> bool:
        try:  # 门毕还原分支位；失败不掩盖门结果，回 False 交调用方留痕
            self._git(["checkout", branch], cwd=repo_dir)
            return True
        except RuntimeError:
            return False

    def rollback(self, target: str, before_sha: str) -> None:
        self._git(["reset", "--hard", before_sha], cwd=target)

    def touches_gate(self, env_dir: str, patterns, branch: str = None) -> bool:
        env = Path(env_dir).resolve()
        names = self._diff_names(env, branch)
        return any(fnmatch(n, p) or fnmatch(Path(n).name, p) for n in names for p in patterns)

    def diff_check(self, env_dir: str, branch: str = None) -> str:
        """任务 diff 机械检查（gt-83185d R3）：merge-base..env 跑 `git diff --check`
        （冲突标记/空白错误）——docs 在 OCR 评审排除面，gate 机械兜底。
        返回告警文本，空＝干净。"""
        env = Path(env_dir).resolve()
        main = self._main(env)
        base = self._merge_base(env, branch)
        r = self._git(["diff", "--check", base, env.name], cwd=main, check=False)
        return r.stdout.strip() if r.returncode else ""

    def _diff_names(self, env: Path, branch: str = None) -> list[str]:
        main = self._main(env)
        base = self._merge_base(env, branch)
        out = self._git(["diff", "--name-only", base, env.name], cwd=main).stdout
        return out.splitlines()  # 换行分隔；split() 会把含空格文件名切碎

    def _merge_base(self, env: Path, branch: str = None) -> str:
        main = self._main(env)
        src = env.name
        if branch:  # 外部支起点：基点＝与目标支的分叉点，外部支自有提交不计入任务 diff
            return self._git(["merge-base", f"refs/heads/{branch}", src],
                             cwd=main).stdout.strip()
        return self._git(["merge-base", src, "HEAD"], cwd=main).stdout.strip()

    @staticmethod
    def _main(env: Path) -> Path:
        out = Gitx._git(["worktree", "list", "--porcelain"], cwd=env).stdout
        first = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree "))
        return Path(first)
