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


class Gitx:
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

    def build_env(self, project_dir: str, tid: str, branch: str = None) -> str:
        repo = Path(project_dir).resolve()
        stamp = datetime.now().strftime("%m%d%H%M")
        name = f"{self._slug(repo.name)}__wt-{tid}__{stamp}{''.join(secrets.choice(_RAND) for _ in range(4))}"
        env = repo.parent / name  # repo 同级父目录＝../ 相对拓扑等价
        # branch＝起点分支（外部工作流，如 issue/N-fix）；缺省＝仓默认支。
        # 起点全限定 refs/heads/<branch>：裸名解析 tag 优先（同名 tag 遮蔽致错基线）
        self._git(["worktree", "add", "-b", name, str(env)]
                  + ([f"refs/heads/{branch}"] if branch else []), cwd=repo)
        return str(env)

    def build_integration(self, target_dir: str, integration_dir: str,
                          branch: str = None) -> str:
        """在目标分支当前 HEAD 建 detached 集成环境，供试合并与集成 verify。

        复跑先收敛 kill 窗口留下的目录/注册元数据；detached 避免与主检出争用分支。
        """
        target = Path(target_dir).resolve()
        integration = Path(integration_dir).resolve()
        self.remove_integration(str(integration), str(target))
        # 起基经 baseline()（ag-49312d）：本地支陈旧时落在真实远端头上，F32 自愈后 push 恒 ff
        self._git(["worktree", "add", "--detach", str(integration),
                   self.baseline(target, branch)], cwd=target)
        return str(integration)

    def remove_integration(self, integration_dir: str, target_dir: str) -> None:
        """回收 detached 集成环境；不删任务分支，幂等处理 kill 窗口残留。"""
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
        self._git(["worktree", "prune"], cwd=target, check=False)

    def ensure_clean(self, repo_dir: str) -> None:
        """主检出写前硬闸：任何已跟踪或未跟踪变更都拒绝，防合并/回滚伤用户工作。"""
        r = self._git(["status", "--porcelain", "--untracked-files=normal"], cwd=repo_dir)
        if r.stdout.strip():
            raise RuntimeError("目标仓工作区不干净——请先提交、stash 或清理后重新 post(done, commit)")

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
        # 目录消失收敛，分支删除亦被其挡（used by worktree）——先 prune 再级联删支
        self._git(["worktree", "prune"], cwd=main, check=False)
        r = self._git(["branch", "-d", env.name], cwd=main, check=False)
        if r.returncode != 0:  # 未合并分支 -d 必拒；级联清理不可半途，升 -D
            self._git(["branch", "-D", env.name], cwd=main)

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

    def rebase_onto(self, env_dir: str, integration: str) -> str:
        """F32 门内自愈：integration（detached at 目标 HEAD）内把 env 分支提交
        rebase --onto 目标 HEAD 重放（mb=merge-base），返回重放后 sha R。
        detached 零临时分支：不碰 env 分支、不碰目标检出；等价提交全 drop 时
        R＝目标 HEAD（no-op 形态）。分类（F29 纪律）：冲突（sequencer 态在位）
        → abort 后回 "conflict"；其余非零（对象损坏等环境病）如实 RuntimeError。"""
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
                    self._git(["rebase", "--abort"], cwd=integration, check=False)
                    return "conflict"
            raise RuntimeError(f"git rebase --onto {onto} {mb}: "
                               f"rc={r.returncode}: {r.stderr.strip()}")
        return self._git(["rev-parse", "HEAD"], cwd=integration).stdout.strip()

    def baseline(self, repo_dir: str, branch: str = None) -> str:
        """门基线 ref（ag-49312d）：best-effort fetch 后，仅本地严格落后时以 origin 作基。

        外部工作流（PR 网页合并）令主检出本地分支落后 origin：基线取远端——
        contains 认得出已落库提交、_merge_base 不把他人改动扫进任务 diff、
        build_integration 落在真实远端头上使 push 恒 ff。领先/分叉/无 origin 一律
        原样返回本地 ref：禁吞未推送的本地提交，行为退回 fetch 前现状；
        branch=None 先 symref 解析默认支（detached 主检出无从谈远端基线，原样 HEAD）。
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
                proc.wait(timeout=_PUSH_TIMEOUT)
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

    def contains(self, repo_dir: str, commit: str, branch: str = None) -> bool:
        """目标基线是否已含任务提交：崩溃重排时识别合并已落，禁止重复验收后假重开。
        基线经 baseline()（ag-49312d）：本地支陈旧时认得 origin 上已落库提交。"""
        target = self.baseline(repo_dir, branch)
        r = self._git(["merge-base", "--is-ancestor", commit, target],
                      cwd=repo_dir, check=False)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
        raise RuntimeError(f"git merge-base --is-ancestor {commit} {target}: {r.stderr.strip()}")

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
