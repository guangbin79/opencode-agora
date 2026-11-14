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
# ag-0d2daa push 网络上界：服务侧同步不得挂死串行门（非 ff 拒/无凭据失败均秒级，300s 已宽）
_PUSH_TIMEOUT = 300


class DirtyWorktree(RuntimeError):
    """Req: F97 ensure_clean 拒绝时携带 porcelain 脏面原始记录（-z 解析后），
    门侧自动忽略判定不重跑 status、不吃异常文案。"""

    def __init__(self, records):
        super().__init__("目标仓工作区不干净——请先提交、stash 或清理后重新"
                         " post(done, commit)")
        self.records = list(records)


class Gitx:
    _SHA40 = re.compile(r"[0-9a-fA-F]{40}")

    def __init__(self, instance_id: str = None):
        # F96 实例身份：None＝legacy 形态（直接测试/旧路径，env 名零变化）；
        # 非空经 _slug 消毒后才进 worktree/分支名（ref 安全纪律单一落点）
        self.instance_id = self._slug(instance_id) if instance_id else None

    @staticmethod
    def _slug(name: str) -> str:
        # repo 名消毒为 ref 安全 slug：ref 禁字符→_、@{ 序列断开、折叠 ..、去首部 ./-（.dotfiles 类仓名可派发）
        s = name.replace("@{", "@_")  # ref 只禁 @{ 序列，{ 单独合法
        s = re.sub(r"[\x00-\x20\x7f~^:?*\[\\]", "_", s)
        return re.sub(r"\.{2,}", ".", s).lstrip(".-") or "repo"

    @staticmethod
    def _git(args, cwd, check=True, timeout=None):
        # 分支历史/diff 文本可含非 UTF-8 字节（GBK 资源混入）：降级 U+FFFD 禁崩（ag-830b1b 同族）
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r

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
        # 起基经 baseline()（ag-49312d）：本地支陈旧时落在真实远端头上，F32 自愈后 push 恒 ff
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

    def push(self, repo_dir: str, branch: str) -> tuple[str, str]:  # Req: ag-0d2daa
        """ag-0d2daa 服务侧 origin 同步：推 refs/heads/<branch>（只此一引用，不 force）。

        幂等（已同步＝up-to-date 仍 "ok"）；无 origin remote＝"none"（本地仓
        静默）；被拒输出含 non-fast-forward/fetch first＝"non-ff"；其余失败
        （含超时）＝"failed"。返回 (status, tail)——tail＝失败时 git 输出尾，
        供调用方落审计与告警（同 verify 输出尾部惯例）；只报告不重试。
        """
        if self._git(["remote", "get-url", "origin"],
                     cwd=repo_dir, check=False).returncode != 0:
            return "none", ""
        # F-1（Req: F38）：start_new_session 启动——超时 killpg 全组，ssh/传输孙进程
        # 不孤儿泄漏（gate runner 同款形态）；分类语义与返回值不变。
        proc = subprocess.Popen(["git", "push", "origin", f"refs/heads/{branch}"],
                                cwd=str(repo_dir), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace",
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=_PUSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # 超时与正常退出竞速：进程组已消失即无需补杀
            try:  # 有限宽限收尸；逃逸后代持管道时弃读，防线程挂死
                proc.communicate(timeout=2)  # F49 S-1：输出不消费，仅收尸＋排空管道
            except subprocess.TimeoutExpired:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe:
                        pipe.close()
            return "failed", f"push 超时（>{_PUSH_TIMEOUT}s）"
        if proc.returncode == 0:
            return "ok", ""
        out = ((err or "") + (out or "")).strip()
        status = "non-ff" if ("non-fast-forward" in out
                              or "fetch first" in out) else "failed"
        return status, out

    @staticmethod
    def _net(args, cwd, timeout=None):
        """网络面 git 原语（ag-0f60fd ls-remote/删除 push）：start_new_session＋
        超时 killpg 全组（push 同款 F38 纪律——ssh/传输孙进程不孤儿泄漏）。返回
        (rc, stdout, stderr) 三元——ls-remote tip 解析只吃 stdout（评审 R1：stderr
        传输噪声如 ssh 首连告警不得混进 fields[0]）；超时 rc=124 带 stderr 文案。
        timeout＝调用面短预算（None＝_PUSH_TIMEOUT 全预算；同步受理面防死 origin
        停摆，baseline fetch_timeout 同口径）。"""
        proc = subprocess.Popen(["git", *args], cwd=str(cwd),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
        tmo = timeout or _PUSH_TIMEOUT
        try:
            out, err = proc.communicate(timeout=tmo)
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
            return 124, "", f"git {' '.join(args[:2])} 超时（>{tmo:g}s）"
        return proc.returncode, (out or ""), (err or "")

    def delete_branch(self, repo_dir: str, branch: str,
                      target_branch: str) -> tuple[str, str]:  # Req: ag-0f60fd
        """ag-0f60fd merged 终局删远端任务支（守卫删除）。

        无 origin＝"none"（本地仓静默）；ls-remote 取 refs/heads/<branch> tip，
        远端无此支＝"absent"（静默幂等）；tip 本地对象库不可达＝"skip"（无从
        校验 ancestor，保守不删）；tip 不含于本地目标支＝"skip"（远端支有未并
        内容，保守不删；ag-49312d：merge-base 直比既有本地 refs——不经
        baseline()，其 best-effort fetch 违反本需求不 fetch 契约）；为真才
        push origin --delete refs/heads/<branch>（只此一引用、不 force，带
        --force-with-lease=<ref>:<tip> CAS——终闸 B1：守卫通过后远端支被第三
        方推进则拒删，不丢未并提交唯一远端副本）。失败分类同 push：non-ff/
        failed 带 git 输出尾（lease 拒含 stale info 归 failed 告警）；返回
        (status, tail)，只报告不重试。
        """
        if self._git(["remote", "get-url", "origin"],
                     cwd=repo_dir, check=False).returncode != 0:
            return "none", ""
        rc, out, err = self._net(["ls-remote", "origin", f"refs/heads/{branch}"],
                                 repo_dir)
        if rc != 0:
            return "failed", " ".join(
                x for x in (err.strip(), out.strip()) if x)
        fields = out.split()  # 仅 stdout：stderr 传输噪声不得混入 tip 解析
        if not fields:
            return "absent", ""
        tip = fields[0]
        if self._git(["cat-file", "-e", f"{tip}^{{commit}}"],
                     cwd=repo_dir, check=False).returncode != 0:
            return "skip", ""  # tip 本地不可达：对象缺失无从校验，保守不删
        # ag-49312d 直比既有本地 refs（目标支在 project_dir 本地已含合并）；
        # 不经 baseline()——其缓存过期会 best-effort fetch，违反不 fetch 契约
        anc = self._git(["merge-base", "--is-ancestor", tip, target_branch],
                        cwd=repo_dir, check=False)
        if anc.returncode != 0:
            return "skip", ""  # 含未并内容/本地目标支缺失：保守不删
        # lease CAS（终闸 B1）：远端支已非 ls-remote 所见 tip（被第三方推进）
        # 则拒删——防丢未并提交唯一远端副本
        rc, out, err = self._net(["push", "origin", "--delete",
                                  f"--force-with-lease=refs/heads/{branch}:{tip}",
                                  f"refs/heads/{branch}"], repo_dir)
        if rc == 0:
            return "ok", ""
        out = " ".join(x for x in (err.strip(), out.strip()) if x)
        status = "non-ff" if ("non-fast-forward" in out
                              or "fetch first" in out) else "failed"
        return status, out

    def has_origin(self, repo_dir: str) -> bool:  # Req: F98
        """origin remote 在位性（managed 远端优先交付的门槛判据）。"""
        return self._git(["remote", "get-url", "origin"],
                         cwd=repo_dir, check=False).returncode == 0

    def fetch_origin_branch(self, repo_dir: str, branch: str,
                            timeout: float = None) -> str:  # Req: F98
        """真 fetch origin 支进远端跟踪引用，返回精确 40 位 tip SHA；失败如实抛。

        显式 +refspec 强制刷新 refs/remotes/origin/<branch>（不依赖仓配 fetch
        refspec 在位，远端回退也同步）；无该远端支/网络病＝RuntimeError，
        绝不静默回退本地支（调用方按交付失败处置）。timeout＝同步受理面
        （dispatch）短预算——缺省＝_PUSH_TIMEOUT 全预算（门体交付面）；短预算
        超时与网络病同抛，语义不变。"""
        rc, out, err = self._net(
            ["fetch", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], repo_dir,
            timeout=timeout)
        if rc != 0:
            raise RuntimeError(
                f"git fetch origin {branch}: "
                + " ".join(x for x in (err.strip(), out.strip()) if x))
        sha = self._git(["rev-parse", f"refs/remotes/origin/{branch}"],
                        cwd=repo_dir).stdout.strip().lower()
        if not self._SHA40.fullmatch(sha):
            raise RuntimeError(
                f"git fetch origin {branch}: tip 非 40 位 SHA（{sha!r}）")
        return sha

    def push_commit(self, repo_dir: str, sha: str,
                    branch: str) -> tuple[str, str]:  # Req: F98
        """sha 级 ff-only 推送：恰推 <full-sha>:refs/heads/<branch>，绝不 force。

        分类同 push：ok（含 already up-to-date）/non-ff（他写者已推进）/
        failed（含超时与网络病）；无 origin＝none。返回 (status, tail)——
        failed 后调用方先 remote_contains 对账（推成对账不可得不重推）。"""
        if not self.has_origin(repo_dir):
            return "none", ""
        rc, out, err = self._net(["push", "origin", f"{sha}:refs/heads/{branch}"],
                                 repo_dir)
        if rc == 0:
            return "ok", ""
        tail = " ".join(x for x in (err.strip(), out.strip()) if x)
        status = "non-ff" if ("non-fast-forward" in tail
                              or "fetch first" in tail) else "failed"
        return status, tail

    def remote_contains(self, repo_dir: str, branch: str, commit: str,
                        timeout: float = None) -> bool:  # Req: F98
        """刷新远端后判 commit 是否 origin/<branch> tip 的祖先（含相等）。

        交付权威判据：push 超时/网络败后的对账与崩溃窗口补终态都走此面；
        fetch 失败如实抛——调用方按「对账不可得」处理，不猜。timeout＝恢复
        面短预算透传（Req: F100，缺省 None＝_PUSH_TIMEOUT 全预算，门体口径
        不变）。"""
        tip = self.fetch_origin_branch(repo_dir, branch, timeout=timeout)
        r = self._git(["merge-base", "--is-ancestor", commit, tip],
                      cwd=repo_dir, check=False)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        raise RuntimeError(
            f"git merge-base --is-ancestor {commit} {tip}: {r.stderr.strip()}")

    def sync_local_target(self, repo_dir: str, branch: str, sha: str,
                          expected_old) -> str:  # Req: F98
        """远端权威落地后的本地目标支尽力同步：仅当支未被任何 worktree 检出、
        expected_old 仍匹配（CAS）且 sha 构成 fast-forward 才原子推进本地 ref；
        任一守卫不过＝pending，不碰用户工作（脏/分叉本地不是交付失败，绝不清洗）。
        expected_old=None＝本地无锚（缺支/未知）恒 pending。"""
        if expected_old is None:
            return "pending"
        cur = self._git(["rev-parse", "--verify", "--quiet",
                         f"refs/heads/{branch}"], cwd=repo_dir, check=False)
        if cur.returncode != 0 or cur.stdout.strip().lower() != expected_old.lower():
            return "pending"  # 缺支/CAS 不符：本地已被他人移动
        out = self._git(["worktree", "list", "--porcelain"],
                        cwd=repo_dir).stdout
        for line in out.splitlines():
            if line.strip() == f"branch refs/heads/{branch}":
                return "pending"  # 检出面守卫：推进被检出 ref 会撕裂工作树
        anc = self._git(["merge-base", "--is-ancestor", expected_old, sha],
                        cwd=repo_dir, check=False)
        if anc.returncode != 0:
            return "pending"  # 分叉/环境病一律不碰（同步是尽力而为面）
        r = self._git(["update-ref", f"refs/heads/{branch}", sha, expected_old],
                      cwd=repo_dir, check=False)
        return "synced" if r.returncode == 0 else "pending"  # CAS 原子收口

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
        块归属：worktree 行开块、branch 行闭块（sync_local_target 同源判定）。"""
        out = self._git(["worktree", "list", "--porcelain"],
                        cwd=repo_dir).stdout
        cur = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = line.split(" ", 1)[1]
            elif cur and line.strip() == f"branch refs/heads/{branch}":
                return cur
        return None

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

    def baseline(self, repo_dir: str, branch: str = None,
                 fetch_timeout: float = None) -> str:
        """门基线 ref（ag-49312d）：best-effort fetch 后，仅本地严格落后时以 origin 作基。

        外部工作流（PR 网页合并）令主检出本地分支落后 origin：基线取远端——
        contains 认得出已落库提交、_merge_base 不把他人改动扫进任务 diff、
        build_integration 落在真实远端头上使 push 恒 ff。领先/分叉/无 origin 一律
        原样返回本地 ref：禁吞未推送的本地提交，行为退回 fetch 前现状；
        branch=None 先 symref 解析默认支（detached 主检出无从谈远端基线，原样 HEAD）。
        fetch_timeout：同步受理面（F69 accept_done）短预算——缺省＝_PUSH_TIMEOUT，
        超时降级本地 ref（同网络病口径），慢 origin 不冻单线程服务。
        """
        b = branch or self.symref(repo_dir)
        if b == "HEAD":
            return b
        local = f"refs/heads/{b}"
        cache = getattr(self, "_fetch_at", None)
        if cache is None:  # 门跑三处共用本基线：60s 内只 fetch 一次（门入口一次语义）
            cache = self._fetch_at = {}
        key = f"{repo_dir}:{b}"
        if time.time() - cache.get(key, 0) > 60:
            cache[key] = time.time()  # 先记账：超时后同门跑不再重复等待
            # "--" 防 symref 路径分支名 - 开头选项注入（显式 branch 已有 check_branch 前置）；
            # F-1（Req: F38）同款：start_new_session——超时 killpg 全组，传输孙进程不孤儿泄漏。
            # 直连 subprocess 不走 _git：网络面带超时且弃输出，不扩子类覆写的 _git 签名
            proc = subprocess.Popen(["git", "fetch", "origin", "--", b],
                                    cwd=str(repo_dir), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                proc.wait(timeout=fetch_timeout or _PUSH_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # 超时与正常退出竞速：进程组已消失即无需补杀
                try:  # 有界收尸防僵尸（push 同款惯例；DEVNULL 无管道可闭）
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                cache[key] = time.time()  # 超时也记账：同门跑不重复等待（300s 最坏只吃一次）
                return local  # 网络病静默跳过，行为退回 fetch 前现状
        if self.strictly_behind(repo_dir, b):
            return f"refs/remotes/origin/{b}"  # 严格落后：基线取远端，他人已落库提交认得出
        return local  # 领先/分叉：保持现行为

    def strictly_behind(self, repo_dir: str, branch: str) -> bool:
        """本地支是否严格落后 origin/<branch>（无远端/领先/分叉均 False）。
        不 fetch，只比对既有 refs——push 前守卫（ag-49312d：推陈旧支必 non-ff 假警报）。"""
        remote = f"refs/remotes/origin/{branch}"
        r = self._git(["rev-parse", "--verify", "--quiet", remote],
                      cwd=repo_dir, check=False)
        if r.returncode != 0:
            return False  # 无该远端分支
        if self._git(["merge-base", "--is-ancestor", f"refs/heads/{branch}", remote],
                     cwd=repo_dir, check=False).returncode != 0:
            return False  # 领先/分叉
        return r.stdout.strip() != self._git(
            ["rev-parse", f"refs/heads/{branch}"], cwd=repo_dir).stdout.strip()

    def contains(self, repo_dir: str, commit: str, branch: str = None,
                 fetch_timeout: float = None) -> bool:
        """目标基线是否已含任务提交：崩溃重排时识别合并已落，禁止重复验收后假重开。
        基线经 baseline()（ag-49312d）：本地支陈旧时认得 origin 上已落库提交。"""
        target = self.baseline(repo_dir, branch, fetch_timeout=fetch_timeout)
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
        # 目标支 ref 经 baseline()（ag-49312d）——本地陈旧时以 origin 作基，
        # 他人 PR 改动不扫进任务 diff；branch=None 先 symref 解析默认支再套用
        main = main or self._main(env)
        return self._git(["merge-base", self.baseline(main, branch), env.name],
                         cwd=main).stdout.strip()

    @staticmethod
    def _main(env: Path) -> Path:
        out = Gitx._git(["worktree", "list", "--porcelain"], cwd=env).stdout
        first = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree "))
        return Path(first)
