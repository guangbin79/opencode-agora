"""T5 访问层契约：gitx worktree 建清/ff-only/回滚/闸体检测（真实 git，tmp 夹具，零 mock）。"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import agorad.access.gitx as gitx_mod
from agorad.access.gitx import Gitx


class TestAccessGitx(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        self._init_repo(self.repo)
        self.gx = Gitx()

    def _init_repo(self, path):
        path.mkdir()
        self._git(["init", "-b", "main"], cwd=path)
        self._git(["config", "user.email", "t@t"], cwd=path)
        self._git(["config", "user.name", "t"], cwd=path)
        self._commit(path, {"src.py": "v1"}, "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _git(args, cwd, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r

    def _commit(self, cwd, files, msg):
        for name, content in files.items():
            path = cwd / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self._git(["add", "-A"], cwd=cwd)
        self._git(["commit", "-m", msg], cwd=cwd)
        return self._git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def _head(self, cwd):
        return self._git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def _worktrees(self, cwd):
        out = self._git(["worktree", "list", "--porcelain"], cwd=cwd).stdout
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def test_env_sibling_of_repo_and_ff_only(self):  # Req: F1
        env = self.gx.build_env(self.repo, "gt-x1")
        self.assertEqual(Path(env).parent, self.repo.parent)  # repo 同级父目录，../ 拓扑等价
        self.assertRegex(
            Path(env).name,
            re.escape(self.repo.name) + r"__wt-gt-x1__\d{8}[0-9a-z]{4}$",
        )
        self._commit(self.repo, {"src.py": "v2"}, "advance main")  # 主干已推进
        before = self._head(self.repo)
        self.assertEqual(self.gx.merge_ff(env, str(self.repo)), "non-ff")
        self.assertEqual(self._head(self.repo), before)  # 主干未被污染

    def test_dot_repo_name_ref_safe_env(self):  # Req: F1
        dot = self.tmp / ".dotfiles"
        self._init_repo(dot)
        env = self.gx.build_env(str(dot), "gt-d1")
        self.assertEqual(Path(env).parent, dot.parent)  # 仓同级父目录
        self._git(["check-ref-format", "--branch", Path(env).name], cwd=self.tmp)  # 构造名恒过 ref 语法
        self.gx.remove_env(env)
        self.assertEqual(self._worktrees(dot), [str(dot)])  # 级联干净
        self.assertEqual(self._git(["branch", "--list", Path(env).name], cwd=dot).stdout.strip(), "")
        # 消毒规则钉死：非法名修正、合法名恒等、..折叠、全空兜底
        self.assertEqual(Gitx._slug(".dotfiles"), "dotfiles")
        self.assertEqual(Gitx._slug("proj"), "proj")
        self.assertEqual(Gitx._slug("a..b"), "a.b")
        self.assertEqual(Gitx._slug("--"), "repo")
        self.assertEqual(Gitx._slug("env{prod}"), "env{prod}")  # { 单独合法，恒等
        self.assertEqual(Gitx._slug("a@{b"), "a@_b")  # 只断 @{ 序列

    def test_merge_ok_fast_forward(self):  # Req: F7
        env = self.gx.build_env(self.repo, "gt-x2")
        env_sha = self._commit(Path(env), {"src.py": "v2\nfeature"}, "task work")
        before = (self.repo / "src.py").read_bytes()
        self.assertEqual(self.gx.merge_ff(env, str(self.repo)), "ok")
        self.assertEqual(self._head(self.repo), env_sha)  # ff：主干 HEAD＝env 分支头，无 merge commit
        self.assertEqual((self.repo / "src.py").read_bytes(), b"v2\nfeature")
        self.assertNotEqual(before, b"v2\nfeature")

    def test_merge_ff_dirty_worktree_raises_not_nonff(self):  # Req: F29
        env = self.gx.build_env(self.repo, "gt-f29")
        self._commit(Path(env), {"src.py": "v1\ntask"}, "task work")  # FF 可并
        (self.repo / "src.py").write_text("v1\nlocal dirty")  # 主干同文件未提交修改
        before = self._head(self.repo)
        with self.assertRaises(RuntimeError) as cm:
            self.gx.merge_ff(env, str(self.repo))
        self.assertIn("local changes", str(cm.exception))  # 环境病如实上抛禁伪装 non-ff
        self.assertEqual(self._head(self.repo), before)  # 主干未动

    def test_merge_ff_target_at_env_head_is_ok(self):  # Req: F29
        env = self.gx.build_env(self.repo, "gt-f2a")  # env 头＝主干头（无任务提交）
        self.assertEqual(self.gx.merge_ff(env, str(self.repo)), "ok")  # 幂等续走集成 verify

    def test_merge_ff_ancestor_probe_env_failure_raises_not_nonff(self):  # Req: F29
        class BoomGitx(Gitx):  # merge-base 自身病（rc=128 对象损坏类）宁抛不伪装 non-ff
            def _git(self, args, cwd, check=True):
                if "--is-ancestor" in args:
                    return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
                return Gitx._git(args, cwd, check)

        env = BoomGitx().build_env(self.repo, "gt-f2b")
        self._commit(Path(env), {"src.py": "v1\ntask"}, "task work")
        with self.assertRaises(RuntimeError) as cm:
            BoomGitx().merge_ff(env, str(self.repo))
        self.assertIn("bad object", str(cm.exception))

    def test_rebase_onto_clean_replay_env_branch_untouched(self):  # Req: F32
        """F32 干净同步：重放成功、env 分支 sha 不动、R 踩在新基线上可 ff 推进。"""
        env = self.gx.build_env(self.repo, "gt-f32a")
        env_sha = self._commit(Path(env), {"feat.py": "task"}, "task work")
        self._commit(self.repo, {"src.py": "v2"}, "advance main")  # 主干超前＝真 non-ff
        integration = Path(self.tmp / "gate-a")
        self.gx.build_integration(str(self.repo), str(integration))
        self.assertEqual(self.gx.merge_ff(env, str(integration)), "non-ff")
        r = self.gx.rebase_onto(env, str(integration))
        self.assertNotEqual(r, env_sha)  # R＝重放新提交
        self.assertEqual(self._head(integration), r)  # integration 停在 R
        self.assertEqual((integration / "feat.py").read_text(), "task")
        self.assertEqual((integration / "src.py").read_text(), "v2")  # 重放在新基线上
        self.assertEqual(self._head(Path(env)), env_sha)  # env 分支 sha 不动
        self.assertEqual(self.gx.ff_sha(str(self.repo), r), "ok")  # R 可 ff 推进
        self.assertEqual(self._head(self.repo), r)
        self.assertTrue(self.gx.contains(str(self.repo), r))

    def test_rebase_onto_conflict_aborts_classifies_and_cleans(self):  # Req: F32
        """冲突分类：abort＋回 conflict；env 分支与主干不动；回收后无 sequencer 残留。"""
        env = self.gx.build_env(self.repo, "gt-f32b")
        env_sha = self._commit(Path(env), {"src.py": "task edit"}, "task edit")
        self._commit(self.repo, {"src.py": "main edit"}, "main edit")  # 同文件真冲突
        before = self._head(self.repo)
        integration = Path(self.tmp / "gate-b")
        self.gx.build_integration(str(self.repo), str(integration))
        self.assertEqual(self.gx.merge_ff(env, str(integration)), "non-ff")
        self.assertEqual(self.gx.rebase_onto(env, str(integration)), "conflict")
        self.assertEqual(self._head(Path(env)), env_sha)  # env 分支不动
        self.assertEqual(self._head(self.repo), before)  # 主干不动
        state = self._git(["rev-parse", "--git-path", "rebase-merge"],
                          cwd=integration).stdout.strip()
        self.assertFalse((integration / state).exists())  # abort 已收敛冲突态
        self.gx.remove_integration(str(integration), str(self.repo))
        self.assertNotIn(str(integration), self._worktrees(self.repo))  # 回收无残留

    def test_rebase_onto_apply_backend_conflict_still_classified(self):  # Req: F32
        """apply backend（rebase.backend=apply）冲突停 rebase-apply：同判 conflict 不误报环境病。"""
        self._git(["config", "rebase.backend", "apply"], cwd=self.repo)  # integration 共享主仓 config
        env = self.gx.build_env(self.repo, "gt-f32e")
        self._commit(Path(env), {"src.py": "task edit"}, "task edit")
        self._commit(self.repo, {"src.py": "main edit"}, "main edit")  # 同文件真冲突
        integration = Path(self.tmp / "gate-e")
        self.gx.build_integration(str(self.repo), str(integration))
        self.assertEqual(self.gx.merge_ff(env, str(integration)), "non-ff")
        self.assertEqual(self.gx.rebase_onto(env, str(integration)), "conflict")
        apply_dir = self._git(["rev-parse", "--git-path", "rebase-apply"],
                              cwd=integration).stdout.strip()
        self.assertFalse((integration / apply_dir).exists())  # abort 已收敛

    def test_rebase_onto_environment_disease_raises_not_conflict(self):  # Req: F32
        class BoomGitx(Gitx):  # rebase 自身病（rc=128 对象损坏类）宁抛不并入冲突
            def _git(self, args, cwd, check=True):
                if "rebase" in args and "--onto" in args:
                    return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
                return Gitx._git(args, cwd, check)

        env = BoomGitx().build_env(self.repo, "gt-f32c")
        self._commit(Path(env), {"src.py": "v1\ntask"}, "task work")
        self._commit(self.repo, {"src.py": "v2"}, "advance main")
        integration = Path(self.tmp / "gate-c")
        BoomGitx().build_integration(str(self.repo), str(integration))
        with self.assertRaises(RuntimeError) as cm:
            BoomGitx().rebase_onto(env, str(integration))
        self.assertIn("bad object", str(cm.exception))

    def test_rebase_onto_all_equivalent_drops_to_target_head(self):  # Req: F32
        """等价提交边界：全部 drop→R＝目标 HEAD，ff 推进＝幂等（零增量 merged）。"""
        env = self.gx.build_env(self.repo, "gt-f32d")
        self._commit(Path(env), {"src.py": "v2"}, "same change")
        self._commit(self.repo, {"src.py": "v2"}, "advance main")  # 上游已含同变更
        integration = Path(self.tmp / "gate-d")
        self.gx.build_integration(str(self.repo), str(integration))
        self.assertEqual(self.gx.merge_ff(env, str(integration)), "non-ff")
        r = self.gx.rebase_onto(env, str(integration))
        self.assertEqual(r, self._head(self.repo))  # no-op 形态：R＝目标 HEAD
        self.assertEqual(self.gx.ff_sha(str(self.repo), r), "ok")  # 幂等推进
        self.assertEqual(self._head(self.repo), r)

    def test_remove_env_cascades_branch(self):  # Req: F1
        env = self.gx.build_env(self.repo, "gt-x3")
        self._commit(Path(env), {"src.py": "dirty"}, "unmerged work")  # 未合并分支也须级联清掉
        self.gx.remove_env(env)
        self.assertEqual(self._worktrees(self.repo), [str(self.repo)])  # worktree list 干净
        self.assertEqual(self._git(["branch", "--list", Path(env).name], cwd=self.repo).stdout.strip(), "")

    def test_remove_env_converges_on_deregistered_shell(self):  # Req: gt-2e4829
        env = Path(self.gx.build_env(self.repo, "gt-x8"))
        self._git(["worktree", "remove", "--force", str(env)],
                  cwd=self.repo)  # 模拟半途注销：git 已注销、目录残留
        (env / ".review-loop").mkdir(parents=True)  # 重建残留壳：评审环并发写重建
        (env / ".review-loop" / "anchor.json").write_text("{}")
        self.gx.remove_env(str(env))  # 幂等收敛不抛
        self.assertFalse(env.exists())  # 目录消失
        self.assertEqual(self._worktrees(self.repo), [str(self.repo)])
        self.assertEqual(self._git(["branch", "--list", env.name],
                                   cwd=self.repo).stdout.strip(), "")  # 分支清掉

    def test_remove_env_fallback_anchors_last_wt_when_repo_name_has_literal(self):  # Req: F24
        repo = self.tmp / "proj__wt-x"  # 仓名自身含 __wt-：partition 提前切错，rpartition 锚真边界
        self._init_repo(repo)
        env = Path(self.gx.build_env(repo, "gt-y1"))
        self._git(["worktree", "remove", "--force", str(env)],
                  cwd=repo)  # 注销使 _main 失败，逼兜底解析
        self.gx.remove_env(str(env))
        self.assertFalse(env.exists())
        self.assertEqual(self._worktrees(repo), [str(repo)])
        self.assertEqual(self._git(["branch", "--list", env.name],
                                   cwd=repo).stdout.strip(), "")

    def test_remove_env_raises_when_delete_blocked(self):  # Req: gt-2e4829
        env = Path(self.gx.build_env(self.repo, "gt-x9"))
        (env / ".review-loop").mkdir()
        (env / ".review-loop" / "anchor.json").write_text("{}")
        (env / ".review-loop").chmod(0o500)  # 同用户删不掉其中文件：模拟并发写入未停
        orig_wait = gitx_mod._RMT_WAIT
        gitx_mod._RMT_WAIT = 0  # 重试不睡：测试提速
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.gx.remove_env(str(env))
            self.assertIn("并发写入未停", str(cm.exception))  # 禁假账：如实上抛
            self.assertTrue(env.exists())  # 残留如实存在
        finally:
            (env / ".review-loop").chmod(0o700)
            gitx_mod._RMT_WAIT = orig_wait

    def test_remove_env_converges_when_env_gone(self):  # Req: gt-2e4829
        env = Path(self.gx.build_env(self.repo, "gt-ya"))
        self._git(["worktree", "remove", "--force", str(env)],
                  cwd=self.repo)  # kill 窗口残留：目录已删净、分支仍在
        self.gx.remove_env(str(env))  # 不抛：跳过目录清理直落分支级联
        self.assertEqual(self._worktrees(self.repo), [str(self.repo)])
        self.assertEqual(self._git(["branch", "--list", env.name],
                                   cwd=self.repo).stdout.strip(), "")

    def test_remove_env_prunes_ghost_registration(self):  # Req: task-bb4ad2 gitx prune
        env = Path(self.gx.build_env(self.repo, "gt-yc"))
        shutil.rmtree(env)  # 外部删除（kill 窗口/手清）：git 仍持注册元数据
        self.assertEqual(len(self._worktrees(self.repo)), 2)
        self.gx.remove_env(str(env))  # 目录已不在：worktree remove 不可达，靠 prune 收敛
        self.assertEqual(self._worktrees(self.repo), [str(self.repo)])
        self.assertEqual(self._git(["branch", "--list", env.name],
                                   cwd=self.repo).stdout.strip(), "")

    def test_remove_env_raises_when_main_unresolvable(self):  # Req: gt-2e4829
        dot = self.tmp / ".dotfiles"  # slug≠原始仓名：重建主仓失真
        self._init_repo(dot)
        env = Path(self.gx.build_env(str(dot), "gt-yb"))
        self._git(["worktree", "remove", "--force", str(env)], cwd=dot)
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.gx.remove_env(str(env))
            self.assertIn("主仓定位失败", str(cm.exception))  # 宁抛不盲删
        finally:
            self._git(["branch", "-D", env.name], cwd=dot)

    def test_integration_side_effects_never_touch_target(self):
        env = self.gx.build_env(self.repo, "gt-gate1")
        env_sha = self._commit(Path(env), {"src.py": "task"}, "task work")
        target_sha = self._head(self.repo)
        target_bytes = (self.repo / "src.py").read_bytes()
        integration = Path(self.tmp / "integration")

        self.gx.build_integration(str(self.repo), str(integration))
        self.assertEqual(self._head(integration), target_sha)  # 从目标 HEAD 隔离起步
        self.assertFalse(self.gx.contains(str(self.repo), env_sha))
        self.assertEqual(self.gx.merge_ff(env, str(integration)), "ok")
        (integration / "verify.side-effect").write_text("test garbage")

        self.assertEqual(self._head(self.repo), target_sha)  # 试合并不推进目标
        self.assertEqual((self.repo / "src.py").read_bytes(), target_bytes)
        self.assertFalse((self.repo / "verify.side-effect").exists())
        self.gx.remove_integration(str(integration), str(self.repo))
        self.assertFalse(integration.exists())
        self.assertNotIn(str(integration), self._worktrees(self.repo))

        self.assertEqual(self.gx.merge_ff(env, str(self.repo)), "ok")  # 全绿后才最终推进
        self.assertTrue(self.gx.contains(str(self.repo), env_sha))

    def test_build_integration_converges_ghost_registration(self):
        integration = Path(self.tmp / "integration-ghost")
        self.gx.build_integration(str(self.repo), str(integration))
        shutil.rmtree(integration)  # kill 留下注册元数据
        self.gx.build_integration(str(self.repo), str(integration))  # 重排先 prune 再建
        self.assertEqual(self._head(integration), self._head(self.repo))
        self.gx.remove_integration(str(integration), str(self.repo))
        self.assertEqual(self._worktrees(self.repo), [str(self.repo)])

    def test_ensure_clean_rejects_tracked_and_untracked_changes(self):
        self.gx.ensure_clean(str(self.repo))
        untracked = self.repo / "local.note"
        untracked.write_text("keep me")
        with self.assertRaises(RuntimeError) as cm:
            self.gx.ensure_clean(str(self.repo))
        self.assertIn("工作区不干净", str(cm.exception))
        self.assertEqual(untracked.read_text(), "keep me")  # 拒绝而非清掉用户文件
        untracked.unlink()
        (self.repo / "src.py").write_text("uncommitted")
        with self.assertRaises(RuntimeError):
            self.gx.ensure_clean(str(self.repo))

    def test_target_branch_start_and_merge(self):  # Req: gt-132747
        self._git(["checkout", "-b", "issue/7"], cwd=self.repo)
        issue_sha = self._commit(self.repo, {"issue.txt": "i1"}, "issue work")
        self._git(["checkout", "main"], cwd=self.repo)
        main_sha = self._commit(self.repo, {"src.py": "v2"}, "advance main")
        env = self.gx.build_env(self.repo, "gt-b1", "issue/7")
        self.assertEqual(self._head(Path(env)), issue_sha)  # 起点＝指定分支头
        env_sha = self._commit(Path(env), {"feat.txt": "f"}, "task work")
        self.assertEqual(self.gx.head(str(self.repo), "issue/7"), issue_sha)
        integration = str(self.tmp / "issue-integration")
        self.gx.build_integration(str(self.repo), integration, "issue/7")
        self.assertEqual(self._head(Path(integration)), issue_sha)  # 指定支为集成基点
        self.assertEqual(self.gx.merge_ff(env, integration), "ok")
        self.assertEqual(self.gx.head(str(self.repo), "issue/7"), issue_sha)  # 目标支仍未动
        self.assertFalse(self.gx.contains(str(self.repo), env_sha, "issue/7"))
        self.gx.remove_integration(integration, str(self.repo))
        self.assertEqual(self.gx.merge_ff(env, str(self.repo), "issue/7"), "ok")
        self.assertTrue(self.gx.contains(str(self.repo), env_sha, "issue/7"))
        self.assertEqual(self._git(["rev-parse", "issue/7"],
                                   cwd=self.repo).stdout.strip(), env_sha)  # 合入指定支
        self.assertEqual(self._git(["rev-parse", "main"],
                                   cwd=self.repo).stdout.strip(), main_sha)  # 仓默认支不动

    def test_tag_shadowed_branch_resolves_branch(self):  # Req: gt-132747
        self._git(["branch", "v1"], cwd=self.repo)
        main_sha = self._head(self.repo)
        self._git(["checkout", "v1"], cwd=self.repo)
        tag_sha = self._commit(self.repo, {"tag.txt": "t"}, "tagged commit")
        self._git(["tag", "v1"], cwd=self.repo)  # 同名 tag 指旧提交
        self._git(["commit", "-q", "--amend", "-m", "tagged"], cwd=self.repo)
        branch_tip = self._git(["rev-parse", "refs/heads/v1"],
                               cwd=self.repo).stdout.strip()
        self.assertNotEqual(branch_tip, self._git(["rev-parse", "v1"],
                                                  cwd=self.repo).stdout.strip())
        self.assertEqual(self.gx.head(str(self.repo), "v1"), branch_tip)  # 回滚点＝分支头非 tag
        env = self.gx.build_env(self.repo, "gt-b3", "v1")
        self.assertEqual(self._head(Path(env)), branch_tip)  # 起点＝分支头非 tag

    def test_touches_gate_branch_base_excludes_external_commits(self):  # Req: gt-132747
        self._git(["checkout", "-b", "issue/9"], cwd=self.repo)
        fork = self._commit(self.repo, {"pyproject.toml": "[x]"}, "external gate change")
        self._git(["checkout", "main"], cwd=self.repo)
        env = self.gx.build_env(self.repo, "gt-b4", "issue/9")
        self._commit(Path(env), {"feat.txt": "f"}, "task work only")
        self.assertFalse(self.gx.touches_gate(env, ["pyproject.toml"],
                                              "issue/9"))  # 外部支提交不计入任务 diff
        self._commit(Path(env), {"Makefile": "all:"}, "task touches gate")
        self.assertTrue(self.gx.touches_gate(env, ["Makefile"], "issue/9"))

    def test_diff_check_flags_conflict_markers(self):  # Req: gt-83185d R3
        env = self.gx.build_env(self.repo, "gt-dc1")
        self._commit(Path(env), {"c.py": "a = 1\n<<<<<<< HEAD\n"}, "conflicted")
        warn = self.gx.diff_check(env)
        self.assertIn("c.py", warn)
        self.assertIn("leftover conflict marker", warn)  # git --check 冲突标记文案
        self._commit(Path(env), {"c.py": "a = 1\n"}, "cleanup")
        self.assertEqual(self.gx.diff_check(env), "")  # 清理后端到端干净

    def test_diff_check_git_failure_raises_not_clean(self):  # Req: gt-83185d R3
        class BoomGitx(Gitx):  # git 自身失败（坏对象，exit 128 无输出）宁抛不假绿
            def _git(self, args, cwd, check=True):
                if "--check" in args:
                    return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
                return Gitx._git(args, cwd, check)

        gx = BoomGitx()
        env = gx.build_env(self.repo, "gt-dc2")
        self._commit(Path(env), {"ok.py": "x"}, "clean work")
        with self.assertRaises(RuntimeError):
            gx.diff_check(env)

    def test_check_branch_symref_checkout(self):  # Req: gt-132747
        self._git(["checkout", "-b", "issue/8"], cwd=self.repo)
        self.assertEqual(self.gx.symref(str(self.repo)), "issue/8")
        self.gx.checkout(str(self.repo), "main")
        self.assertEqual(self.gx.symref(str(self.repo)), "main")  # 还原位成立
        self.assertIsNone(self.gx.check_branch(str(self.repo), "issue/8"))  # 语法＋存在
        for bad in ("-bad", "a..b", "ghost"):
            with self.assertRaises(RuntimeError, msg=bad):
                self.gx.check_branch(str(self.repo), bad)  # 选项注入/语法坏/不存在

    def test_target_branch_missing_fails_clear(self):  # Req: gt-132747
        with self.assertRaises(RuntimeError) as cm:
            self.gx.build_env(self.repo, "gt-b2", "no/such")
        self.assertIn("no/such", str(cm.exception))  # 报因含分支名

    def test_touches_gate_matches_task_diff(self):  # Req: F7
        env = self.gx.build_env(self.repo, "gt-x5")
        self._commit(Path(env), {"Makefile": "all:"}, "touch gate")
        self.assertTrue(self.gx.touches_gate(env, ["Makefile"]))
        env2 = self.gx.build_env(self.repo, "gt-x6")
        self._commit(Path(env2), {"src.py": "v9"}, "touch code only")
        self.assertFalse(self.gx.touches_gate(env2, ["Makefile", "pyproject.toml"]))


    def test_touches_gate_handles_spaces_in_names(self):  # Req: F7
        env = self.gx.build_env(self.repo, "gt-x7")
        self._commit(Path(env), {"my file.conf": "k=v"}, "touch spaced name")
        self.assertTrue(self.gx.touches_gate(env, ["my file.conf"]))
        self.assertFalse(self.gx.touches_gate(env, ["file.conf"]))  # 碎片不得误报

    def test_apply_ignore_roster_idempotent_keeps_lines_and_status_clean(self):  # Req: F35
        """F35 名册 exclude 机制化：无尾换行接写不粘连＋幂等零重排＋既有行保留；
        名册路径不进 status，ensure_clean 放行未跟踪名册目录。"""
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.log")  # 预置无尾换行
        self.gx.apply_ignore_roster(str(self.repo), [".req-gate/", "*.log"])
        self.assertEqual(exclude.read_text(), "*.log\n.req-gate/\n")  # 补换行＋追加＋尾换行
        self.gx.apply_ignore_roster(str(self.repo), [".req-gate/", "*.log"])  # 幂等
        self.assertEqual(exclude.read_text(), "*.log\n.req-gate/\n")  # 零追加不重排
        (self.repo / ".req-gate").mkdir()
        (self.repo / ".req-gate" / "s1.md").write_text("gate log")
        self.assertEqual(self._git(["status", "--porcelain"],
                                   cwd=self.repo).stdout.strip(), "")  # 名册路径不进 status
        self.gx.ensure_clean(str(self.repo))  # 全净谓词放行（不抛）

    def test_apply_ignore_roster_empty_patterns_zero_action(self):  # Req: F35
        """空名册零动作：缺文件也不建 exclude（缺名册＝现状全净语义）。"""
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.unlink(missing_ok=True)
        self.gx.apply_ignore_roster(str(self.repo), [])
        self.assertFalse(exclude.exists())

    def test_apply_ignore_roster_non_utf8_exclude_preserved(self):  # Req: F35
        """F35 round-1 评审修复：既有 exclude 含非 UTF-8 字节——字节级无损读写，
        名册行照常追加（文本模式抛 UnicodeDecodeError、errors=replace 会改写损坏）。"""
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(b"*.log\n# \xd6\xd0\xce\xc4")  # 无尾换行＋GBK 注释字节
        self.gx.apply_ignore_roster(str(self.repo), [".req-gate/"])
        self.assertEqual(exclude.read_bytes(),
                         b"*.log\n# \xd6\xd0\xce\xc4\n.req-gate/\n")

    # ---- ag-0d2daa gate env 供给：submodule 预置＋服务侧 push（ag-0d2daa） ----

    _SUB_URL = "ssh://git@127.0.0.1:1/t/sub.git"  # 无凭据假源：一旦真拉取即秒拒

    def _init_with_submodule(self):
        """主仓＋libs/sub（.gitmodules url＝无凭据 ssh 假源，主检出本地填充）——
        镜像 gate 无凭据视角。返回 (main, sub, pin1, pin2)。"""
        sub = self.tmp / "subsrc"
        sub.mkdir()
        self._git(["init", "-b", "master"], cwd=sub)
        self._git(["config", "user.email", "t@t"], cwd=sub)
        self._git(["config", "user.name", "t"], cwd=sub)
        (sub / "api.h").write_text("v1\n")
        self._git(["add", "-A"], cwd=sub)
        self._git(["commit", "-m", "c1"], cwd=sub)
        pin1 = self._head(sub)
        (sub / "impl.h").write_text("impl\n")
        self._git(["add", "-A"], cwd=sub)
        self._git(["commit", "-m", "c2"], cwd=sub)
        pin2 = self._head(sub)
        main = self.tmp / "projm"
        main.mkdir()
        self._git(["init", "-b", "master"], cwd=main)
        self._git(["config", "user.email", "t@t"], cwd=main)
        self._git(["config", "user.name", "t"], cwd=main)
        (main / "app.txt").write_text("app\n")
        self._git(["add", "-A"], cwd=main)
        self._git(["commit", "-m", "m1"], cwd=main)
        self._git(["-c", "protocol.file.allow=always", "submodule", "add",
                   str(sub), "libs/sub"], cwd=main)
        self._git(["config", "--file", ".gitmodules",
                   "submodule.libs/sub.url", self._SUB_URL], cwd=main)
        self._git(["add", ".gitmodules"], cwd=main)
        self._git(["commit", "-m", "m2"], cwd=main)
        self._git(["submodule", "init"], cwd=main)  # 主检出本地填充（有凭据常态模拟）
        self._git(["config", "submodule.libs/sub.url", str(sub)], cwd=main)
        self._git(["-c", "protocol.file.allow=always", "submodule", "update"], cwd=main)
        self._git(["config", "submodule.libs/sub.url", self._SUB_URL], cwd=main)
        return main, sub, pin1, pin2

    def test_seed_submodules_populates_env_at_pin(self):  # Req: ag-0d2daa
        """检出后 submodule 非空：env 填至 gitlink pin，verify 自带 submodule 句
        成 no-op（ssh 假 url 下零拉取）；主检出 config 与工作区零被动。"""
        main, sub, pin1, pin2 = self._init_with_submodule()
        env = self.gx.build_env(main, "gt-s1")
        report = self.gx.seed_submodules(env, main)
        self.assertIn(f"seeded libs/sub@{pin2[:12]}", report)
        self.assertTrue((Path(env) / "libs" / "sub" / "impl.h").exists())
        self.assertEqual(self._head(Path(env) / "libs" / "sub"), pin2)
        self.assertEqual(self._git(["status", "--porcelain"], cwd=env).stdout, "")
        self._git(["submodule", "update", "--init", "--recursive"], cwd=env)  # no-op 不炸
        self.assertEqual(self._git(["config", "submodule.libs/sub.url"],
                                   cwd=main).stdout.strip(), self._SUB_URL)  # url 未被改写
        self.assertEqual(self._git(["status", "--porcelain"], cwd=main).stdout, "")

    def test_seed_submodules_detached_integration_tree(self):  # Req: ag-0d2daa
        """gate 集成树（detached worktree）同机制覆盖：pin 落位、树全净。"""
        main, sub, pin1, pin2 = self._init_with_submodule()
        integ = self.tmp / "integ"
        self._git(["worktree", "add", "--detach", str(integ), "HEAD"], cwd=main)
        report = self.gx.seed_submodules(str(integ), main)
        self.assertIn("seeded libs/sub", report)
        self.assertEqual(self._head(integ / "libs" / "sub"), pin2)
        self.assertEqual(self._git(["status", "--porcelain"], cwd=integ).stdout, "")

    def test_seed_submodules_skips_pin_missing_in_reference(self):  # Req: ag-0d2daa
        """参考源缺 pin：跳过留报告不阻断、不落残树（后续 verify 如实显症状）。"""
        main, sub, pin1, pin2 = self._init_with_submodule()
        (sub / "new.h").write_text("v3\n")
        self._git(["add", "-A"], cwd=sub)
        self._git(["commit", "-q", "-m", "c3"], cwd=sub)
        pin3 = self._head(sub)  # 参考源对象库此后不再更新＝缺失的新 pin
        self._git(["update-index", "--add", "--cacheinfo",
                   f"160000,{pin3},libs/sub"], cwd=main)
        self._git(["commit", "-q", "-m", "bump"], cwd=main)
        env = self.gx.build_env(main, "gt-s2")
        report = self.gx.seed_submodules(env, main)
        self.assertIn(f"skipped libs/sub: pin {pin3[:12]} 不在参考源", report)
        self.assertFalse((Path(env) / "libs" / "sub" / "api.h").exists())  # 树未被半拉子填充
        self.assertEqual(self._git(["status", "--porcelain"], cwd=env).stdout, "")

    def test_seed_submodules_reference_covers_unreachable_pin(self):  # Req: ag-0d2daa
        """pin 仅存参考源对象库（refs 不可达）：--reference alternates 接住。"""
        main, sub, pin1, pin2 = self._init_with_submodule()
        mod = main / "libs" / "sub"
        self._git(["checkout", "--detach", "-q"], cwd=mod)
        (mod / "extra.h").write_text("x\n")
        self._git(["add", "-A"], cwd=mod)
        self._git(["-c", "user.email=t@t", "-c", "user.name=t",
                   "commit", "-q", "-m", "orphan"], cwd=mod)
        pin3 = self._head(mod)
        self._git(["checkout", "-q", pin2], cwd=mod)  # 还原主检出位（对象库留 pin3）
        self._git(["update-index", "--add", "--cacheinfo",
                   f"160000,{pin3},libs/sub"], cwd=main)
        self._git(["commit", "-q", "-m", "bump-orphan"], cwd=main)
        env = self.gx.build_env(main, "gt-s3")
        report = self.gx.seed_submodules(env, main)
        self.assertIn(f"seeded libs/sub@{pin3[:12]}", report)
        self.assertTrue((Path(env) / "libs" / "sub" / "extra.h").exists())
        self.assertEqual(self._git(["status", "--porcelain"], cwd=env).stdout, "")

    def test_seed_submodules_no_gitlinks_zero_action(self):  # Req: ag-0d2daa
        env = self.gx.build_env(self.repo, "gt-s4")
        self.assertEqual(self.gx.seed_submodules(env, self.repo), "")

    def test_push_ok_advances_origin(self):  # Req: ag-0d2daa
        origin = self.tmp / "origin.git"
        self._git(["init", "--bare", "-b", "main", str(origin)], cwd=self.repo)
        self._git(["remote", "add", "origin", str(origin)], cwd=self.repo)
        self._commit(self.repo, {"src.py": "v2"}, "advance")
        self.assertEqual(self.gx.push(str(self.repo), "main"), "ok")
        self.assertEqual(self._head(origin), self._head(self.repo))
        self.assertEqual(self.gx.push(str(self.repo), "main"), "ok")  # 幂等：已同步仍 ok

    def test_push_non_ff_classified_not_forced(self):  # Req: ag-0d2daa
        origin = self.tmp / "origin.git"
        self._git(["init", "--bare", "-b", "main", str(origin)], cwd=self.repo)
        self._git(["remote", "add", "origin", str(origin)], cwd=self.repo)
        self._commit(self.repo, {"src.py": "v2"}, "advance")
        self.assertEqual(self.gx.push(str(self.repo), "main"), "ok")
        peer = self.tmp / "peer"
        self._git(["clone", "-q", str(origin), str(peer)], cwd=self.tmp)
        self._git(["config", "user.email", "t@t"], cwd=peer)
        self._git(["config", "user.name", "t"], cwd=peer)
        self._commit(peer, {"src.py": "v3"}, "peer advance")
        self._git(["push", "-q", "origin", "main"], cwd=peer)  # 远端被第三方推进
        local = self._commit(self.repo, {"src.py": "v2-local"}, "local advance")
        self.assertEqual(self.gx.push(str(self.repo), "main"), "non-ff")
        self.assertEqual(self._head(self.repo), local)  # 本地合并未被 force 回滚

    def test_push_none_without_origin(self):  # Req: ag-0d2daa
        self.assertEqual(self.gx.push(str(self.repo), "main"), "none")

    def test_push_failure_classified(self):  # Req: ag-0d2daa
        self._git(["remote", "add", "origin", str(self.tmp / "nope.git")],
                  cwd=self.repo)
        self.assertEqual(self.gx.push(str(self.repo), "main"), "failed")


if __name__ == "__main__":
    unittest.main()
