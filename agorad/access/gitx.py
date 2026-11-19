"""git 唯一接触点（北向访问层）：worktree 建清/ff-only 合并/集成隔离/闸体检测。

环境形态沿用全局 worktree 铁律语义：目录＝repo 同级父目录下
`<REPO>__wt-<tid>__<MMDDHHMM><rand4>`（REPO 经 _slug 消毒为 ref 安全名），分支同名，保证 `../` 相对拓扑等价；
线性历史——merge 只走 --ff-only，绝不产生 merge commit；rebase_onto（F32）
仅门内 detached 干净同步自愈，不碰 env 分支与目标检出。
"""
import os
import secrets
import re
import string
import shutil
import signal
import subprocess
import time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

_RAND = string.ascii_lowercase + string.digits
# rmtree 撞写重试参数：worker 评审环进程合并后仍可能往 env/.review-loop/ 写文件
_RMT_TRIES = 4
_RMT_WAIT = 0.5


class DirtyWorktree(RuntimeError):
    """Req: F97 ensure_clean 拒绝时携带 porcelain 脏面原始记录（-z 解析后），
    门侧自动忽略判定不重跑 status、不吃异常文案。"""

    def __init__(self, records):
        super().__init__("目标仓工作区不干净——请先提交、stash 或清理后重新"
                         " post(done, commit)")
        self.records = list(records)


class Gitx:
    _SHA40 = re.compile(r"[0-9a-fA-F]{40}")

    def __init__(self, instance_id: str = None, spawn_tracker=None):
        # F96 实例身份：None＝legacy 形态（直接测试/旧路径，env 名零变化）；
        # 非空经 _slug 消毒后才进 worktree/分支名（ref 安全纪律单一落点）
        self.instance_id = self._slug(instance_id) if instance_id else None
        # P0-1（ag-9c4cbb）：门路径 spawn 登记簿（与 Gate 同实例注入）——
        # 门线程内 _git 子进程组可被看门狗整组收杀；None＝不登记（直接测试）
        self._tracker = spawn_tracker

    @staticmethod
    def _slug(name: str) -> str:
        # repo 名消毒为 ref 安全 slug：ref 禁字符→_、@{ 序列断开、折叠 ..、去首部 ./-（.dotfiles 类仓名可派发）
        s = name.replace("@{", "@_")  # ref 只禁 @{ 序列，{ 单独合法
        s = re.sub(r"[\x00-\x20\x7f~^:?*\[\\]", "_", s)
        return re.sub(r"\.{2,}", ".", s).lstrip(".-") or "repo"

    def _git(self, args, cwd, check=True, timeout=None, input_data=None):
        """git plumbing 统一执行点。P0-2（ag-9c4cbb）：start_new_session 自立
        进程组＋超时 killpg 全组——孤儿孙进程（ssh/传输）不再持管道挂死
        communicate()（旧 subprocess.run(timeout=) 只杀直接子进程的 wedge 形态）；
        超时按 gitx 失败契约抛 RuntimeError。spawn_tracker 在位时登记 pgid，
        归门看门狗兜底收杀（timeout=None 的挂死亦可达）。"""
        proc = subprocess.Popen(["git", *args], cwd=str(cwd),
                                stdin=subprocess.PIPE
                                if input_data is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
        if self._tracker is not None:
            self._tracker.add(proc.pid)
        try:
            try:
                out, err = proc.communicate(input=input_data, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # 超时与正常退出竞速：进程组已消失即无需补杀
                try:  # 有限宽限收尸；逃逸后代持管道时弃读，防线程挂死
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()
                raise RuntimeError(
                    f"git {' '.join(args)}: 超时（>{timeout:g}s）——已终止进程组"
                ) from None
            if check and proc.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)}: {err.strip()}")
            return subprocess.CompletedProcess(
                ["git", *args], proc.returncode, stdout=out, stderr=err)
        finally:
            if self._tracker is not None:
                self._tracker.discard(proc.pid)

    @classmethod
    def _check_sha(cls, sha: str) -> str:  # Req: F96
        # 信任边界：显式 SHA 构造器——malformed 一律在触 git 前拒（大写归一小写）
        if not isinstance(sha, str) or not cls._SHA40.fullmatch(sha):
            raise ValueError(
                f"base_sha 须为完整 40 位十六进制 commit SHA（收到 {sha!r}）")
        return sha.lower()

    def _env_name(self, repo: Path, tid: str) -> str:
        # F96：实例属主成分插在 tid 之后；None＝legacy 名（存量形态零变化）
        iid = f"-{self.instance_id}" if self.instance_id else ""
        stamp = datetime.now().strftime("%m%d%H%M")
        return (f"{self._slug(repo.name)}__wt-{tid}{iid}__{stamp}"
                f"{''.join(secrets.choice(_RAND) for _ in range(4))}")

    def _build_env(self, project_dir: str, tid: str, startpoint: list) -> str:
        repo = Path(project_dir).resolve()
        name = self._env_name(repo, tid)
        env = repo.parent / name  # repo 同级父目录＝../ 相对拓扑等价
        self._git(["worktree", "add", "-b", name, str(env), *startpoint], cwd=repo)
        return str(env)

    def build_env(self, project_dir: str, tid: str, branch: str = None) -> str:
        # branch＝起点分支（外部工作流，如 issue/N-fix）；缺省＝仓默认支。
        # 起点全限定 refs/heads/<branch>：裸名解析 tag 优先（同名 tag 遮蔽致错基线）
        return self._build_env(
            project_dir, tid, [f"refs/heads/{branch}"] if branch else [])

    def build_env_at(self, project_dir: str, tid: str, branch: str = None,
                     base_sha: str = None) -> str:  # Req: F96
        """从完整 40 位 SHA 建实例属主任务 env/分支（权威定基，不经分支 ref 解析）。

        branch 仅保留签名对齐（起点已由 base_sha 钉死），不参与起点解析。
        """
        return self._build_env(project_dir, tid, [self._check_sha(base_sha)])

    def env_on_disk(self, env_dir: str) -> bool:  # Req: F71
        """记录 env 是否仍在盘（dispatch 重派自愈判据；空值＝无 env 视不在盘）。"""
        return bool(env_dir) and Path(env_dir).is_dir()

    def build_integration(self, target_dir: str, integration_dir: str,
                          branch: str = None) -> str:
        """在目标分支当前 HEAD 建 detached 集成环境，供试合并与集成 verify。

        复跑先收敛 kill 窗口留下的目录/注册元数据；detached 避免与主检出争用分支。
        """
        target = Path(target_dir).resolve()
        # 起基经 baseline()（F109 本地权威）：恒为本地支 ref，不 fetch、不以 origin 作基
        return self._build_integration(
            target, Path(integration_dir).resolve(),
            self.baseline(target, branch))

    def build_integration_at(self, target_dir: str, integration_dir: str,
                             base_sha: str = None) -> str:  # Req: F96
        """从完整 40 位 SHA 建 detached 集成环境（权威定基，不经 baseline 解析）。"""
        return self._build_integration(
            Path(target_dir).resolve(), Path(integration_dir).resolve(),
            self._check_sha(base_sha))

    def _build_integration(self, target: Path, integration: Path,
                           startpoint: str) -> str:
        self.remove_integration(str(integration), str(target))
        self._git(["worktree", "add", "--detach", str(integration), startpoint],
                  cwd=target)
        return str(integration)

    def remove_integration(self, integration_dir: str, target_dir: str) -> None:
        """回收 detached 集成环境；不删任务分支，幂等处理 kill 窗口残留。

        F96：热路径零全局 prune——残留注册按精确路径注销，绝不动同仓其他
        实例/任务的在册条目。"""
        integration = Path(integration_dir).resolve()
        target = Path(target_dir).resolve()
        if integration.exists():
            self._git(["worktree", "remove", "--force", str(integration)],
                      cwd=target, check=False)
            for _ in range(_RMT_TRIES):
                shutil.rmtree(integration, ignore_errors=True)
                if not integration.exists():
                    break
                time.sleep(_RMT_WAIT)
            if integration.exists():
                raise RuntimeError(f"integration 目录删除失败（并发写入未停）: {integration}")
        self._unregister_worktree(target, integration)

    def delivery_lock(self, repo_dir: str):  # Req: F96
        """仓级跨进程交付锁 CM（只护最终 ref 推进互斥；verify 期间绝不持有）。

        兄弟模块经动态 import 取用——access 包静态分层仅许 stdlib
        （test_architecture 声明动态 import 不在扫描范围），gitlock 零 agorad 依赖。"""
        from importlib import import_module
        return import_module("agorad.access.gitlock").RepoDeliveryLock(repo_dir)

    def ensure_clean(self, repo_dir: str, exempt=()) -> None:
        """主检出写前硬闸：任何已跟踪或未跟踪变更都拒绝，防合并/回滚伤用户工作。

        exempt＝clean-ignore 数据层豁免（ag-f5aa4d 产品裁定，有意打破旧「全净
        口径不变」设计）：门防的是代码任务污染主检出，声明路径上的数据层心跳
        不再拦门；条目按仓根锚定的目录前缀语义匹配（`data/` 豁免 data 本体与
        整棵子树，非子串匹配）。缺省空＝逐条目全计，与旧口径行为等价。status
        用 -z 机读解析：路径含空格/引号/非 ASCII 不撕裂；rename/copy 条目的
        第二段（orig 路径）非工作树条目，不评脏面。"""
        r = self._git(["status", "--porcelain", "-z", "--untracked-files=normal"],
                      cwd=repo_dir)
        bases = [b for b in (e.rstrip("/") for e in exempt) if b]
        dirty, skip = [], False
        for rec in r.stdout.split("\0"):
            if not rec:
                continue
            if skip:  # rename/copy 的 orig 路径段：非工作树条目，不评脏面
                skip = False
                continue
            skip = rec[0] in "RC"
            path = rec[3:]
            if not any(path == b or path.startswith(b + "/") for b in bases):
                dirty.append(rec)
        if dirty:
            raise DirtyWorktree(dirty)

    def clean_ignore(self, repo_dir: str, env_value: str = "") -> list[str]:
        """clean-ignore 声明解析（ag-f5aa4d）：仓根 .agora/clean-ignore（一行一条，
        # 注释/空行丢弃）∪ 部署 env AGORA_CLEAN_IGNORE（逗号分隔，调用点透传），
        文件在前 env 在后，保序去重。缺文件/不可读/非 UTF-8＝空声明（F35 名册
        解析同款容错）。.agora/ 自身无条件免计（ag-0f60fd 自豁免）：声明文件
        落在名册自家目录，其存在不得把 .agora/ 变成未跟踪脏面；两层皆缺＝
        仅剩该自豁免，其余全净口径不变。"""
        try:
            lines = (Path(repo_dir) / ".agora" / "clean-ignore").read_text(
                encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        entries = [ln.strip() for ln in lines
                   if ln.strip() and not ln.strip().startswith("#")]
        entries += [p.strip() for p in env_value.split(",") if p.strip()]
        return list(dict.fromkeys([".agora/", *entries]))  # 自豁免恒在＋保序去重

    def apply_ignore_roster(self, repo_dir: str, patterns) -> None:  # Req: F35
        """F35：名册幂等写入 info/exclude——空名册零动作（不建文件）；
        `git rev-parse --git-path` 定位（worktree 场景指向主 .git），相对路径按
        repo_dir 解析；缺行补写、不重排不删既有条目；既有文本无尾换行先补换行
        防粘连，追加后以换行结尾。既有内容按字节读写零解码（git 该文件本按字节
        处理；文本模式遇非 UTF-8 既有字节会抛解码错、errors=replace 会改写损坏）。"""
        patterns = list(patterns)
        if not patterns:
            return
        r = self._git(["rev-parse", "--git-path", "info/exclude"], cwd=repo_dir)
        p = Path(r.stdout.strip())
        if not p.is_absolute():
            p = Path(repo_dir) / p
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_bytes() if p.exists() else b""
        have = set(existing.splitlines())
        missing = [pat for pat in patterns if pat.encode("utf-8") not in have]
        if not missing:  # 幂等：全在册零追加（不重排不删既有行）
            return
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        p.write_bytes(existing + "\n".join(missing).encode("utf-8") + b"\n")

    def remove_env(self, env_dir: str) -> None:
        env = Path(env_dir).resolve()
        try:
            main = self._main(env)  # 最先取：注销后 env 不再是 worktree，_main 会失败
        except (RuntimeError, FileNotFoundError):  # 含 env 已整体消失（kill 窗口残留）
            # rpartition 锚最后一个 __wt-：仓名自身含该字面量时 partition 会提前切错（终闸 B 册 probe）
            main = env.parent / env.name.rpartition("__wt-")[0]
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
        # kill 窗口/外部删除留下的 worktree 注册元数据（.git/worktrees/<name>）不随
        # 目录消失收敛，分支删除亦被其挡（used by worktree）——按精确路径注销当前
        # 任务的注册后级联删支（F96：全局 prune 会顺带摘其他实例的在册条目，禁用）
        self._unregister_worktree(main, env)
        r = self._git(["branch", "-d", env.name], cwd=main, check=False)
        if r.returncode != 0:  # 未合并分支 -d 必拒；级联清理不可半途，升 -D
            self._git(["branch", "-D", env.name], cwd=main)

    def _unregister_worktree(self, main: Path, wt: Path) -> None:  # Req: F96
        """精确路径注销 worktree 注册元数据（替代全局 prune 的跨实例误伤面）。

        注册项＝<common-dir>/worktrees/<admin>/gitdir（内容＝该 worktree 的
        .git 文件路径）；仅当其指向恰为 wt 时摘除，绝不按前缀/名字匹配。"""
        common = Path(self._git(["rev-parse", "--git-common-dir"],
                                cwd=main).stdout.strip())
        if not common.is_absolute():
            common = main / common
        admin_root = common / "worktrees"
        if not admin_root.is_dir():
            return
        for admin in list(admin_root.iterdir()):
            try:
                ref = (admin / "gitdir").read_text(
                    encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if ref and os.path.realpath(str(Path(ref).parent)) == str(wt):
                shutil.rmtree(admin, ignore_errors=True)

    def materialize_checkout(self, tree_dir: str, source_dir: str) -> str:  # Req: ag-0d2daa
        """检出后依赖物化（submodule gitlink 预置＋LFS 实体还原）：零网络零凭据。

        submodule：HEAD 树 gitlink pin 级本地填充，参考源＝source_dir 主检出的
        同名 submodule 检出（--reference alternates 覆盖 pin 不在源 refs 可达面的
        场景）。url 用一次性 `-c` 覆写——worktree 里 `git config` 落共享 config
        会改写主检出（实证），禁用；`protocol.file` 放开为 submodule 间接 clone
        本地路径的必要位。源缺 pin／缺 .gitmodules 映射：跳过留报告不阻断其余；
        git 机械病如实上抛。只铺顶层（libs/* 级），嵌套层归 verify 自理。
        LFS：worktree 与主检出共享 `<common>/.git/lfs` 对象库（实证），`git lfs
        checkout` 即零网络还原；缺对象 rc=0 静默留指针（实证）——逐文件复查
        marker，残留如实入报告，绝不联网 fetch。git-lfs 缺席＝跳过留痕。
        返回人读报告（空串＝无依赖物零动作）；审计由调用方落。
        """
        tree = Path(tree_dir).resolve()
        parts = [self._seed_submodules(tree, Path(source_dir).resolve()),
                 self._materialize_lfs(tree)]
        return "; ".join(p for p in parts if p)

    def _seed_submodules(self, tree: Path, src_root: Path) -> str:
        entries = []
        for line in self._git(["ls-files", "--stage"], cwd=tree).stdout.splitlines():
            mode, sha, _stage, path = line.split(None, 3)  # <mode> <sha> <stage>\t<path>
            if mode == "160000":
                entries.append((path, sha))
        if not entries:
            return ""
        if not (tree / ".gitmodules").is_file():
            return ("submodule skipped all: gitlink 无 .gitmodules 映射（"
                    + ", ".join(p for p, _ in entries) + "）")
        names = {}
        r = self._git(["config", "--file", ".gitmodules", "--get-regexp",
                       r"^submodule\..*\.path$"], cwd=tree, check=False)
        for line in r.stdout.splitlines():
            key, _, val = line.partition(" ")
            names[val.strip()] = key[len("submodule."):-len(".path")]
        report = []
        for path, sha in entries:
            name = names.get(path)
            if not name:
                report.append(f"submodule skipped {path}: 无 .gitmodules 映射")
                continue
            src = src_root / path
            if not (src / ".git").exists():
                report.append(f"submodule skipped {path}: 参考源未检出（{src}）")
                continue
            if self._git(["cat-file", "-e", f"{sha}^{{commit}}"],
                         cwd=src, check=False).returncode != 0:
                report.append(f"submodule skipped {path}: pin {sha[:12]} 不在参考源")
                continue
            self._git(["-c", f"submodule.{name}.url={src}",
                       "-c", "protocol.file.allow=always",
                       "submodule", "update", "--reference", str(src),
                       "--", path], cwd=tree)
            got = self._git(["rev-parse", "HEAD"], cwd=tree / path).stdout.strip()
            if got != sha:  # 预检已过仍错位＝机械病，宁抛不交残树
                raise RuntimeError(
                    f"submodule {path} 预置校验败：HEAD@{got[:12]} ≠ pin {sha[:12]}")
            report.append(f"submodule seeded {path}@{sha[:12]}")
        return "; ".join(report)

    def _materialize_lfs(self, tree: Path) -> str:
        r = self._git(["lfs", "ls-files"], cwd=tree, check=False)
        if r.returncode != 0:
            if "is not a git command" in r.stderr:  # git 报错文案随 locale 变体（R2）
                return "lfs: git-lfs 不可用，跳过"
            return f"lfs: ls-files 失败（rc={r.returncode}）: {r.stderr.strip()[:200]}"
        if not r.stdout.strip():
            return ""
        self._git(["lfs", "checkout"], cwd=tree, check=False)  # 缺对象 rc=0 留指针（实证）
        rr = self._git(["lfs", "ls-files"], cwd=tree, check=False)
        if rr.returncode != 0:  # 复查败＝未知态，宁报失败不假绿（R2）
            return (f"lfs: 复查 ls-files 失败（rc={rr.returncode}）: "
                    f"{rr.stderr.strip()[:200]}")
        after = rr.stdout.splitlines()
        # 行形＝<oid> <marker> <path>：marker `-`＝工作文件仍是指针（实证 3.6.1）
        missing = [parts[2] for ln in after
                   if len(parts := ln.split(None, 2)) == 3 and parts[1] == "-"]
        total = len(after)
        if not missing:
            return f"lfs: {total} tracked, all materialized"
        shown = ", ".join(missing[:50]) + (
            f" …共 {len(missing)} 个" if len(missing) > 50 else "")  # R2：报告面截断
        return (f"lfs: {total - len(missing)}/{total} materialized, "
                f"missing: {shown}")

    def merge_ff(self, env_dir: str, target: str, branch: str = None) -> str:
        src = Path(env_dir).resolve().name
        sha = self._git(["rev-parse", src], cwd=target).stdout.strip()
        return self._ff(target, src, sha, branch)

    def ff_sha(self, target: str, sha: str, branch: str = None) -> str:
        """sha 级 ff 推进（F32 rebase 后 R 推进）：源＝重放 sha，零临时分支，语义同 merge_ff。"""
        return self._ff(target, sha, sha, branch)

    def _ff(self, target: str, src: str, sha: str, branch: str = None) -> str:
        if branch:  # 合入指定分支（外部工作流）；缺省＝仓默认支
            self._git(["checkout", branch], cwd=target)
        head = self._git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        # F29 分类先于合并：merge rc 混杂脏工作区拒绝等环境失败，曾把 FF 可并
        # 分支误拒 non-ff（task-bbf257 R2-R4）。祖先判定才是唯一真判据——
        # 主干已超前（env 为真祖先）＝真 non-ff；祖先成立而 merge 仍败＝环境病，
        # 如实上抛禁伪装 non-ff（merge-base 自身 rc=128 病同此，禁并入 non-ff）。
        anc = self._git(["merge-base", "--is-ancestor", head, src],
                        cwd=target, check=False)
        if anc.returncode == 1:
            return "non-ff"
        if anc.returncode != 0:
            raise RuntimeError(
                f"git merge-base --is-ancestor {head} {src}: {anc.stderr.strip()}")
        r = self._git(["merge", "--ff-only", src], cwd=target, check=False)
        new_head = self._git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        if r.returncode != 0 or new_head != sha:
            detail = r.stderr.strip() or r.stdout.strip()  # already-up-to-date 走 stdout
            raise RuntimeError(f"git merge --ff-only {src}: rc={r.returncode}, "
                               f"head@{new_head} vs expected@{sha}: {detail}")
        return "ok"  # head==sha（目标支已在提交上）亦达此：幂等推进

    def local_branch_sha(self, repo_dir: str, branch: str) -> str:  # Req: F100
        """本地钉死目标支 tip 解析（no-origin 权威定基）：refs/heads/<branch>
        精确 40 位 SHA；缺支/malformed 如实 RuntimeError——绝不静默回退仓默认支。"""
        r = self._git(["rev-parse", "--verify", f"refs/heads/{branch}"],
                      cwd=repo_dir, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"本地目标分支不存在: refs/heads/{branch}"
                               f"（{r.stderr.strip()}）")
        sha = r.stdout.strip().lower()
        if not self._SHA40.fullmatch(sha):
            raise RuntimeError(f"本地目标分支 tip 非 40 位 SHA"
                               f"（refs/heads/{branch}: {sha!r}）")
        return sha

    def publication(self, repo_dir: str, branch: str = None) -> dict:  # P1b ag-454fbe
        """origin 出版投影（零网络，refs-only）：本地权威交付后 merged≠已出版
        origin——target_ref=本地目标支 tip、origin_ref=origin/<branch> 跟踪 ref
        tip（无跟踪 ref＝None）、ahead=rev-list origin..local 计数（无跟踪
        ref＝0）、note=as of last fetch（读数截至上次 fetch）。branch=None＝
        symref 解析默认支；本地缺支如实 RuntimeError（调用方观测面省略字段）。"""
        if branch is None:
            branch = self.symref(repo_dir)

        def _ref(ref):
            r = self._git(["rev-parse", "--verify", "--quiet", ref],
                          cwd=repo_dir, check=False)
            return r.stdout.strip().lower() if r.returncode == 0 else None

        out = {"branch": branch,
               "target_ref": _ref(f"refs/heads/{branch}"),
               "origin_ref": _ref(f"refs/remotes/origin/{branch}"),
               "note": "as of last fetch"}
        if out["target_ref"] is None:  # 本地缺支投影无意义：如实抛（调用方省略字段，detached-HEAD 同盖）
            raise RuntimeError(f"本地缺支: refs/heads/{branch}")
        c = (self._git(["rev-list", "--count",
                        f"refs/remotes/origin/{branch}..refs/heads/{branch}"],
                       cwd=repo_dir, check=False)
             if out["origin_ref"] and out["target_ref"] else None)
        out["ahead"] = int(c.stdout.strip()) if c is not None \
            and c.returncode == 0 else 0
        return out

    def published_on_origin(self, repo_dir: str, commit: str,
                            branch: str) -> bool:
        """commit 是否已出版到 origin（零网络 refs-only，as of last fetch）：
        merge-base --is-ancestor <commit> refs/remotes/origin/<branch>。
        rc=0 True、rc=1 False；无跟踪 ref→False（无法证实已出版，告警与否
        归调用方）；其他非零如实 RuntimeError（调用方本轮免判）。"""
        commit = self._check_sha(commit)  # malformed 在触 git 前拒（F96 同源）
        ref = f"refs/remotes/origin/{branch}"
        probe = self._git(["rev-parse", "--verify", "--quiet", ref],
                          cwd=repo_dir, check=False)
        if probe.returncode != 0:
            return False  # 无跟踪 ref：出版无从证实
        anc = self._git(["merge-base", "--is-ancestor", commit, ref],
                        cwd=repo_dir, check=False)
        if anc.returncode == 1:
            return False
        if anc.returncode != 0:
            raise RuntimeError(
                f"git merge-base --is-ancestor {commit} {ref}: "
                f"{anc.stderr.strip()}")
        return True

    def patch_equivalent_on_origin(self, repo_dir: str, commit: str,
                                   branch: str) -> bool:
        """commit 自身补丁是否已内容在场于 origin（零网络 refs-only，as of
        last fetch）——两层对账（F121）：①完整层＝交付补丁号（diff-tree -p
        经 patch-id --stable）∈ origin 窗口补丁集合→True；②完整未命中时退
        hunk 级全覆盖：交付 diff 拆 per-hunk 合成 patch（文件头+单 hunk），
        其 patch-id 全部 ∈ 窗口 hunk 集合→True——交付补丁被拆进窗口多个
        commit 出版（拆分/混合出版，ag-50ef74 双文件实证）不再误报未出版。
        窗口＝merge-base..ref（无共同祖先退 ref 全史），经
        rev-list|diff-tree -p --stdin 收集——两侧同为 diff-tree -p -r
        --no-renames --root 补丁面，porcelain（log -p）的改名检测不对称
        与消息文本均不混入。防假阳性：origin hunk 集只来自窗口内 commit；
        交付 hunk 须全覆盖（禁 majority）；合成 patch 含文件路径头，无关
        路径同内容 hunk 不互配。只对账交付自身补丁：范围级 cherry 对账会被
        祖先补丁变基后的 patch-id 漂移毒化而整体误判未出版（ag-87bb0a 看门
        狗误报实证）；交付自身补丁变基漂移则 patch-id 天然失效，属另案。
        空补丁或无 hunk 面（空提交/合并提交/纯 mode change/二进制）＝无从
        hunk 对账保守 False（完整 patch-id 命中除外）；无跟踪 ref→False
        （出版无从证实）；其他非零如实 RuntimeError（调用方免判）。commit
        已原样在 ref 上（ancestor）时窗口不含自身亦判 False——先经
        published_on_origin 证真再谓本方法（health.py 现行调用序）。"""
        commit = self._check_sha(commit)
        ref = f"refs/remotes/origin/{branch}"
        probe = self._git(["rev-parse", "--verify", "--quiet", ref],
                          cwd=repo_dir, check=False)
        if probe.returncode != 0:
            return False
        patch = self._git(["diff-tree", "-p", "-r", "--no-renames",
                           "--root", commit],
                          cwd=repo_dir, check=False)
        if patch.returncode != 0:
            raise RuntimeError(
                f"git diff-tree -p {commit}: {patch.stderr.strip()}")
        mine = self._patch_ids(repo_dir, patch.stdout)
        if not mine:  # 空补丁（空提交/合并提交）：无内容可对账，保守未出版
            return False
        mb = self._git(["merge-base", commit, ref], cwd=repo_dir,
                       check=False)
        if mb.returncode not in (0, 1):  # rc=1＝无共同祖先；其余＝环境病如实抛
            raise RuntimeError(
                f"git merge-base {commit} {ref}: {mb.stderr.strip()}")
        window = f"{mb.stdout.strip()}..{ref}" if mb.returncode == 0 else ref
        revs = self._git(["rev-list", window], cwd=repo_dir, check=False)
        if revs.returncode != 0:
            raise RuntimeError(f"git rev-list {window}: {revs.stderr.strip()}")
        # 窗口补丁流用 diff-tree（plumbing）而非 log -p（porcelain）：后者
        # 缺省改名检测会让改名交付的补丁面与交付侧不对称，永配不上对
        win = self._git(["diff-tree", "-p", "-r", "--no-renames",
                         "--root", "--stdin"],
                        cwd=repo_dir, check=False, input_data=revs.stdout)
        if win.returncode != 0:
            raise RuntimeError(
                f"git diff-tree -p --stdin: {win.stderr.strip()}")
        if mine & self._patch_ids(repo_dir, win.stdout):
            return True  # 完整 commit patch-id 命中（0b07cc9 语义优先）
        hunks = self._split_hunk_patch_ids(repo_dir, patch.stdout)
        if not hunks:  # 纯 mode change/二进制等无 hunk 面：无从 hunk 对账
            return False
        return hunks <= self._split_hunk_patch_ids(repo_dir, win.stdout)

    def _patch_ids(self, repo_dir: str, patch_text: str) -> set:
        """patch 文本流（diff-tree/log -p 输出）→ patch-id 集合（--stable）。"""
        r = self._git(["patch-id", "--stable"], cwd=repo_dir, check=False,
                      input_data=patch_text)
        if r.returncode != 0:
            raise RuntimeError(f"git patch-id --stable: {r.stderr.strip()}")
        return {p[0] for ln in r.stdout.splitlines() if (p := ln.split())}

    @staticmethod
    def _hunk_patches(diff_text: str) -> list[str]:  # Req: F121
        """diff-tree -p 输出 → per-hunk 合成 patch 列表（纯解析，零 git）。

        合成 patch＝文件头（diff --git .. 首个 @@ 前：路径随 patch-id 入
        哈希，无关路径同内容 hunk 不互配）+单 hunk 正文。diff-tree 的
        commit sha 前言行与 index/mode 行不入 hunk；无 hunk 文件（纯 mode
        change/二进制占位）不产出。hunk 只认 @@ - 起始行：正文行恒带
        ' '/'+'/'-'/'\\' 前缀，不会与文件头/前言行混淆。
        """
        out, header, cur = [], None, None
        for ln in diff_text.splitlines():
            if ln.startswith("diff --git "):
                if cur is not None:
                    out.append("\n".join(header + cur))
                header, cur = [ln], None
            elif ln.startswith("@@ -"):
                if cur is not None:
                    out.append("\n".join(header + cur))
                cur = [ln] if header is not None else None
            elif header is None:
                continue
            elif cur is not None:
                cur.append(ln)
            else:
                header.append(ln)
        if cur is not None:
            out.append("\n".join(header + cur))
        return out

    def _split_hunk_patch_ids(self, repo_dir: str, diff_text: str) -> set:
        """diff 文本 → per-hunk patch-id 集（Req: F121）。单一稳定流一次
        patch-id：每合成 patch 前置唯一 "commit <40hex>" 行作分段边界
        （patch-id 按该前言行分段，不按 diff --git）——免每 hunk 一进程的
        O(N×H) 洪泛；实证与逐 hunk 独立进程结果一致、index 行不入哈希
        （hunk id 对 rebase 漂移稳定）。"""
        patches = self._hunk_patches(diff_text)
        if not patches:
            return set()
        stream = "".join(f"commit {i:040x}\n{p}\n"
                         for i, p in enumerate(patches))
        return self._patch_ids(repo_dir, stream)

    def advance_local_target(self, repo_dir: str, branch: str, sha: str,
                             expected_old) -> str:  # Req: F100
        """本地权威目标支推进（调用方持仓级交付锁）：CAS＋ff-only 双守卫。

        先重读 tip 要求 expected_old 匹配（他写者已推进/分叉＝non-ff，整链
        重试口径）；支未被任何 worktree 检出＝update-ref 原子 CAS 推进；被
        检出＝仅在该检出内 merge --ff-only（干净且仍在 expected_old）——脏
        ＝dirty（保全 WIP）、机械拒绝＝failed；绝不清洗/换支/stash/force，
        绝不重写历史。返回 synced/non-ff/failed/dirty。expected_old=None＝
        无从 CAS，按 non-ff 交调用方重取基线。"""
        sha = self._check_sha(sha)  # malformed 在触 git 前拒（F96 信任边界同源）
        if expected_old is None:
            return "non-ff"
        cur = self._git(["rev-parse", "--verify", "--quiet",
                         f"refs/heads/{branch}"], cwd=repo_dir, check=False)
        if cur.returncode != 0:
            return "non-ff"  # 建单后被删：非机械病，重取基线口径
        tip = cur.stdout.strip().lower()
        if tip != expected_old.lower():
            return "non-ff"  # CAS：他写者已推进/分叉
        anc = self._git(["merge-base", "--is-ancestor", tip, sha],
                        cwd=repo_dir, check=False)
        if anc.returncode == 1:
            return "non-ff"  # sha 不构成 fast-forward（分叉）
        if anc.returncode != 0:
            return "failed"  # 环境病（对象损坏等）
        wt = self._holding_worktree(repo_dir, branch)
        if wt is not None:
            gd = self._git(["rev-parse", "--absolute-git-dir"], cwd=repo_dir,
                           check=False)
            if gd.returncode == 0 and os.path.realpath(wt) \
                    == os.path.realpath(gd.stdout.strip()):
                # ag-4c6136 repo-tool 伪 worktree：.git 符号链接布局下
                # porcelain 把 gitdir 本身报为持支检出，gitdir 内 status/
                # merge 必拒——解析真实检出替换之，双验不过保持 failed
                cand = self._repo_tool_checkout(os.path.realpath(wt), tip)
                if cand is None:
                    return "failed"
                wt = cand
        if wt is None:  # 未检出：ref 级原子 CAS 推进（不撕裂任何工作树）
            r = self._git(["update-ref", f"refs/heads/{branch}", sha,
                           expected_old], cwd=repo_dir, check=False)
            if r.returncode == 0:
                return "synced"
            moved = self._git(["rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], cwd=repo_dir,
                              check=False)
            # CAS 窗口落空（tip 已非快照值）＝non-ff；其余＝机械病
            return ("non-ff" if moved.returncode == 0
                    and moved.stdout.strip().lower() != tip else "failed")
        # 被检出：仅在该检出内推进——从仓本级改被检出 ref 会撕裂工作树
        if self._git(["status", "--porcelain", "-z",
                      "--untracked-files=normal"], cwd=wt,
                     check=False).stdout:
            return "dirty"  # 用户 WIP 在场：保全现场零接触
        head = self._git(["rev-parse", "HEAD"], cwd=wt, check=False)
        if head.returncode != 0:
            return "failed"
        if head.stdout.strip().lower() == sha:
            return "synced"  # 幂等：已在交付位
        if head.stdout.strip().lower() != tip:
            return "non-ff"  # 检出漂移（mid-rebase 等）：不碰
        r = self._git(["merge", "--ff-only", sha], cwd=wt, check=False)
        now = self._git(["rev-parse", "HEAD"], cwd=wt, check=False)
        if (r.returncode == 0 and now.returncode == 0
                and now.stdout.strip().lower() == sha):
            return "synced"
        return "failed"  # 干净同位仍拒＝占用/机械病（index.lock 等）：不清洗不强制

    def _holding_worktree(self, repo_dir: str, branch: str):  # Req: F100
        """refs/heads/<branch> 被哪个 worktree 检出（None＝未检出）。porcelain
        块归属：worktree 行开块、branch 行闭块（advance_local_target 同源判定）。"""
        out = self._git(["worktree", "list", "--porcelain"],
                        cwd=repo_dir).stdout
        cur = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = line.split(" ", 1)[1]
            elif cur and line.strip() == f"branch refs/heads/{branch}":
                return cur
        return None

    def _repo_tool_checkout(self, gitdir: str, tip: str):
        """ag-4c6136 repo-tool 检出解析：gitdir 形如 <root>/.repo/projects/
        <name>.git → 候选检出 <root>/<name>；仅当 readlink -f 候选/.git
        实体即本 gitdir 且候选 HEAD 钉在 CAS 快照 tip 才接受；任何不匹配
        返回 None（调用方保持 failed——不猜）。"""
        marker = "/.repo/projects/"
        i = gitdir.find(marker)
        if i <= 0 or not gitdir.endswith(".git"):
            return None
        cand = gitdir[:i] + "/" + gitdir[i + len(marker):-len(".git")]
        try:
            if os.path.realpath(f"{cand}/.git") != gitdir:
                return None
        except OSError:
            return None
        head = self._git(["rev-parse", "HEAD"], cwd=cand, check=False)
        if head.returncode != 0 or head.stdout.strip().lower() != tip:
            return None
        return cand

    def rebase_onto(self, env_dir: str, integration: str,
                    conflicts: list = None) -> str:
        """F32 门内自愈：integration（detached at 目标 HEAD）内把 env 分支提交
        rebase --onto 目标 HEAD 重放（mb=merge-base），返回重放后 sha R。
        detached 零临时分支：不碰 env 分支、不碰目标检出；等价提交全 drop 时
        R＝目标 HEAD（no-op 形态）。分类（F29 纪律）：冲突（sequencer 态在位）
        → abort 后回 "conflict"；其余非零（对象损坏等环境病）如实 RuntimeError。
        conflicts（Req: F105）非 None 时，abort 前以 index 未合并路径回填
        冲突文件清单（diff --name-only --diff-filter=U，machine-readable）；
        既有双参调用零变化。"""
        integration = Path(integration).resolve()
        src = Path(env_dir).resolve().name
        onto = self._git(["rev-parse", "HEAD"], cwd=integration).stdout.strip()
        mb = self._git(["merge-base", "HEAD", src], cwd=integration).stdout.strip()
        self._git(["checkout", "--detach", src], cwd=integration)
        r = self._git(["rebase", "--onto", onto, mb], cwd=integration, check=False)
        if r.returncode != 0:
            # 冲突态判据：per-worktree sequencer 目录在位＝rebase 停在冲突——
            # merge backend 停 rebase-merge、apply backend 停 rebase-apply，两态
            # 目录皆缺＝环境病，禁并入冲突（分类先于动作）
            for state_dir in ("rebase-merge", "rebase-apply"):
                p = self._git(["rev-parse", "--git-path", state_dir],
                              cwd=integration).stdout.strip()
                if p and (integration / p).exists():
                    if conflicts is not None:  # Req: F105 abort 前取证（abort 即失态）
                        u = self._git(["diff", "--name-only", "--diff-filter=U",
                                       "-z"], cwd=integration, check=False)
                        if u.returncode == 0:  # 取证面尽力而为：不逆转冲突分类
                            conflicts.extend(x for x in u.stdout.split("\0") if x)
                    self._git(["rebase", "--abort"], cwd=integration, check=False)
                    return "conflict"
            raise RuntimeError(f"git rebase --onto {onto} {mb}: "
                               f"rc={r.returncode}: {r.stderr.strip()}")
        return self._git(["rev-parse", "HEAD"], cwd=integration).stdout.strip()

    def baseline(self, repo_dir: str, branch: str = None) -> str:
        """门基线 ref（F109 本地权威）：恒为本地支 ref——不 fetch、不以 origin 作基。

        F108 起交付权威恒为本地钉死支，origin 同步是派发方职责；本地支落后
        origin 属派发方侧状态，门体零感知零网络。branch=None 先 symref 解析
        默认支（detached 主检出原样 HEAD）。
        """
        b = branch or self.symref(repo_dir)
        return "HEAD" if b == "HEAD" else f"refs/heads/{b}"

    def contains(self, repo_dir: str, commit: str, branch: str = None) -> bool:
        """目标基线是否已含任务提交：崩溃重排时识别合并已落，禁止重复验收后假重开。
        基线经 baseline()（F109 本地权威）：恒查本地支 ref，零网络。"""
        target = self.baseline(repo_dir, branch)
        r = self._git(["merge-base", "--is-ancestor", commit, target],
                      cwd=repo_dir, check=False)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        raise RuntimeError(f"git merge-base --is-ancestor {commit} {target}: {r.stderr.strip()}")

    def is_ancestor(self, repo_dir: str, base: str, commit: str) -> bool:
        """raw 祖先判定（ag-1ae0f1 空集判据 a）：base 是否 commit 的祖先——
        env HEAD 相对任务起点 base 有前进即真。不经 baseline() 不 fetch（两端
        均为具体 sha，基线解析无意义）；rc 0/1 分类同 contains，其余病如实抛。"""
        r = self._git(["merge-base", "--is-ancestor", base, commit],
                      cwd=repo_dir, check=False)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        raise RuntimeError(f"git merge-base --is-ancestor {base} {commit}: {r.stderr.strip()}")

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

    def default_branch(self, repo_dir: str) -> str:  # ag-b5f2e9
        """仓默认支（建单缺省父支 detached/解析失败的回退面）：origin/HEAD
        本地声明（clone 地道）优先，次取 init.defaultBranch 配置，兜底 main
        ——只读本地引用/配置，零网络；返回名不担保存在，不存在时由派发
        local_base 拒因点名暴露。终闸 C 册：两路由输出不可信（origin/HEAD
        可伪造/config 值零校验，前导 - 与换行实测可注入），逐路由过 check_branch
        同款校验，不过即弃走下一路由。"""
        r = self._git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                      cwd=repo_dir, check=False)
        name = r.stdout.strip()
        if r.returncode == 0 and name.startswith("origin/"):
            branch = name[len("origin/"):]
            if self._git(["check-ref-format", "--branch", branch],
                         cwd=repo_dir, check=False).returncode == 0:
                return branch
        r = self._git(["config", "--get", "init.defaultBranch"],
                      cwd=repo_dir, check=False)
        if r.returncode == 0 and r.stdout.strip():
            branch = r.stdout.strip()
            if self._git(["check-ref-format", "--branch", branch],
                         cwd=repo_dir, check=False).returncode == 0:
                return branch
        return "main"

    def checkout(self, repo_dir: str, branch: str) -> bool:
        try:  # 门毕还原分支位；失败不掩盖门结果，回 False 交调用方留痕
            self._git(["checkout", branch], cwd=repo_dir)
            return True
        except RuntimeError:
            return False

    def touches_gate(self, env_dir: str, patterns, branch: str = None) -> bool:
        env = Path(env_dir).resolve()
        names = self._diff_names(env, branch)
        return any(fnmatch(n, p) or fnmatch(Path(n).name, p) for n in names for p in patterns)

    def diff_check(self, env_dir: str, branch: str = None) -> str:
        """任务 diff 机械检查（gt-83185d R3）：merge-base..env 跑 `git diff --check`
        （冲突标记/空白错误）——docs 在 OCR 评审排除面，gate 机械兜底。
        返回告警文本，空＝干净；git 自身失败（非零退出且无告警输出）宁抛 RuntimeError。"""
        env = Path(env_dir).resolve()
        main = self._main(env)
        base = self._merge_base(env, branch, main)
        r = self._git(["diff", "--check", base, env.name], cwd=main, check=False)
        warn = r.stdout.strip()
        if r.returncode and not warn:  # 检查告警必带输出；空＝git 自身失败，宁抛不假绿
            raise RuntimeError(f"git diff --check(rc={r.returncode}): {r.stderr.strip()}")
        return warn

    def _diff_names(self, env: Path, branch: str = None) -> list[str]:
        main = self._main(env)
        base = self._merge_base(env, branch, main)
        out = self._git(["diff", "--name-only", base, env.name], cwd=main).stdout
        return out.splitlines()  # 换行分隔；split() 会把含空格文件名切碎

    def diff_stats(self, env_dir: str, branch: str = None) -> tuple[int, int, str]:
        """F65 quick 包络读数：merge-base..env numstat——（文件数, 增删行合计, 明细）。

        同 _diff_names 的 merge-base 纪律（基线经 baseline()，外部支自有提交
        不计入）；二进制条目（"-\t-" 占位）按 0 行计、文件数照计。"""
        env = Path(env_dir).resolve()
        main = self._main(env)
        base = self._merge_base(env, branch, main)
        out = self._git(["diff", "--numstat", base, env.name], cwd=main).stdout
        files = lines = 0
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            lines += (int(parts[0]) if parts[0].isdigit() else 0) \
                + (int(parts[1]) if parts[1].isdigit() else 0)
        return files, lines, out.strip()

    def _merge_base(self, env: Path, branch: str = None,
                    main: Path = None) -> str:  # main 可注入：调用方已定位免重复子进程
        # 外部支起点：基点＝与目标支的分叉点，外部支自有提交不计入任务 diff；
        # 目标支 ref 经 baseline()（F109 本地权威）——恒为本地支 ref，零网络；
        # branch=None 先 symref 解析默认支再套用
        main = main or self._main(env)
        return self._git(["merge-base", self.baseline(main, branch), env.name],
                         cwd=main).stdout.strip()

    def _main(self, env: Path) -> Path:
        out = self._git(["worktree", "list", "--porcelain"], cwd=env).stdout
        first = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree "))
        return Path(first)
