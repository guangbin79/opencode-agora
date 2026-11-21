"""ag-0e6ca3 env 回收作用域与审计锚（0f7d7f 事故：仓外 glob rm+prune 冒名）。

三断言（任务页断言清单）：
1) 同前缀多兄弟 env 回收 A 只动 A——目录/注册/分支三点全精确；
2) 每次 remove_env 调用恰产一条含任务 id＋resolve 后路径＋调用点标签的
   env_removed 审计事件（v2 漏斗行为锚＋源扫描锁死 agorad 全调用点走漏斗）；
3) _unregister_worktree 在 gitdir 指向兄弟路径时不误摘（F96 行为锁定）。
真实 git 临时仓夹具（同 test_access_gitx 口径），零 mock 服务装配。
"""
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from agorad.access.gitx import Gitx
import agorad.core.gate as gate_mod
import agorad.core.task as task_mod

AGORAD = Path(__file__).resolve().parent.parent / "agorad"


class _RealGitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        self._git(["init", "-b", "main"], cwd=self.repo)
        self._git(["config", "user.email", "t@t"], cwd=self.repo)
        self._git(["config", "user.name", "t"], cwd=self.repo)
        self._commit(self.repo, {"src.py": "v1"}, "init")
        self.gx = Gitx()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _git(args, cwd, check=True):
        r = subprocess.run(["git", *args], cwd=str(cwd),
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r

    def _commit(self, cwd, files, msg):
        for name, content in files.items():
            (cwd / name).write_text(content)
        self._git(["add", "-A"], cwd=cwd)
        self._git(["commit", "-m", msg], cwd=cwd)

    def _branch_exists(self, name):
        return self._git(["show-ref", "--verify", "--quiet",
                          f"refs/heads/{name}"], cwd=self.repo,
                         check=False).returncode == 0

    def _registered(self, env_dir):
        out = self._git(["worktree", "list", "--porcelain"],
                        cwd=self.repo).stdout
        return f"worktree {env_dir}" in out


class TestSiblingScope(_RealGitBase):
    def test_recycle_a_touches_only_a(self):  # Req: ag-0e6ca3 断言1
        a = self.gx.build_env(str(self.repo), "ag-a")
        b = self.gx.build_env(str(self.repo), "ag-b")
        c = self.gx.build_env(str(self.repo), "ag-c")
        # 同前缀家族在盘：proj__wt-ag-{a,b,c}__*（0f7d7f 事故同构命名面）
        for env in (a, b, c):
            self.assertTrue(Path(env).name.startswith("proj__wt-ag-"))
        self.gx.remove_env(a)
        self.assertFalse(Path(a).exists())  # A 目录回收
        self.assertFalse(self._branch_exists(Path(a).name))  # A 分支级联
        self.assertFalse(self._registered(a))  # A 注册注销
        for env in (b, c):  # B/C 三点零变化
            self.assertTrue(Path(env).exists())
            self.assertTrue(self._branch_exists(Path(env).name))
            self.assertTrue(self._registered(env))


class TestUnregisterExactPath(_RealGitBase):
    def test_sibling_gitdir_never_dropped(self):  # Req: ag-0e6ca3 断言3（F96 锁定）
        a = self.gx.build_env(str(self.repo), "ag-a")
        b = self.gx.build_env(str(self.repo), "ag-b")
        admin_root = self.repo / ".git" / "worktrees"
        # ghost 注册：gitdir 指向兄弟 A 的路径（仿手工/残留错位注册）
        ghost = admin_root / "ghost"
        ghost.mkdir()
        (ghost / "gitdir").write_text(f"{a}/.git\n")
        self.gx._unregister_worktree(self.repo, Path(b).resolve())
        self.assertFalse((admin_root / Path(b).name).exists())  # B 精确摘除
        self.assertTrue((admin_root / Path(a).name).exists())  # A 注册不动
        self.assertTrue(ghost.exists())  # 指向兄弟路径的注册不误摘


class _RecStore:
    def __init__(self):
        self.events = []

    def append_event(self, ev):
        self.events.append(ev)


class _FakeGitx:
    def __init__(self):
        self.calls = []

    def remove_env(self, env_dir):
        self.calls.append(env_dir)


class TestReclaimAudit(unittest.TestCase):
    def _gate_ns(self, store, gx):
        ns = SimpleNamespace(_store=store, _gitx=gx, _pages=None)
        ns._audit = types.MethodType(gate_mod.Gate._audit, ns)  # 真 _audit 钩
        return ns

    def _stub(self, cls, *args):
        store, gx = _RecStore(), _FakeGitx()
        ns = (self._gate_ns(store, gx) if cls is gate_mod.Gate
              else SimpleNamespace(_store=store, _gitx=gx))
        cls._reclaim_env(ns, *args)
        return store, gx

    def test_gate_funnel_one_audited_event(self):  # Req: ag-0e6ca3 断言2
        env = "/tmp/nonexistent-env-x"
        store, gx = self._stub(gate_mod.Gate, "ag-a", env, "gate:succeed")
        self.assertEqual(gx.calls, [env])  # remove_env 恰调一次
        self.assertEqual(len(store.events), 1)  # 恰产一条审计
        ev = store.events[0]
        self.assertEqual(ev["event"], "env_removed")
        self.assertEqual(ev["task"], "ag-a")  # 含任务 id
        self.assertEqual(ev["env"], str(Path(env).resolve()))  # resolve 后路径
        self.assertEqual(ev["site"], "gate:succeed")  # 调用点标签

    def test_task_funnel_one_audited_event(self):  # Req: ag-0e6ca3 断言2
        env = "/tmp/nonexistent-env-y"
        store, gx = self._stub(task_mod.TaskService, "ag-b", env, "sid1",
                               "task:cancel_one")
        self.assertEqual(gx.calls, [env])
        self.assertEqual(len(store.events), 1)
        ev = store.events[0]
        self.assertEqual((ev["event"], ev["task"], ev["site"]),
                         ("env_removed", "ag-b", "task:cancel_one"))
        self.assertEqual(ev["env"], str(Path(env).resolve()))

    def test_audit_precedes_removal_even_on_failure(self):  # Req: ag-0e6ca3
        # 回收抛也不丢审计（0f7d7f 定谳痛点：先落账再动手）
        store = _RecStore()
        gx = _FakeGitx()
        gx.remove_env = lambda env: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            gate_mod.Gate._reclaim_env(
                self._gate_ns(store, gx), "ag-c", "/tmp/z", "gate:tail_cancel")
        self.assertEqual([e["event"] for e in store.events], ["env_removed"])

    def test_audit_failure_never_blocks_removal(self):  # Req: ag-0e6ca3
        # B 册终闸锚：审计面病（uow 竞争/IO 病）不得拦断回收、不得外逃
        # （拦断＝凭据已删/终态已落而 env 无痕滞留——0f7d7f 式孤儿复发）
        for cls, args in ((gate_mod.Gate, ("ag-a", "/tmp/x", "gate:succeed")),
                          (task_mod.TaskService,
                           ("ag-b", "/tmp/y", "sid1", "task:cancel_one"))):
            store, gx = _RecStore(), _FakeGitx()
            store.append_event = lambda ev: (_ for _ in ()).throw(
                RuntimeError("audit io"))
            if cls is gate_mod.Gate:
                ns = self._gate_ns(store, gx)
            else:
                ns = SimpleNamespace(_store=store, _gitx=gx)
            cls._reclaim_env(ns, *args)  # 不抛
            self.assertEqual(len(gx.calls), 1)  # 回收照常执行

    def test_all_remove_env_calls_go_through_audited_funnel(self):
        """源扫描：agorad 内每个 self._gitx.remove_env( 直连只许出现在
        _reclaim_env 漏斗体内——新增调用点绕漏斗即红（覆盖全部调用点）。"""
        offenders, funnels = [], 0
        for py in sorted(AGORAD.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            cur = None
            for ln in py.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s+def\s+(\w+)", ln)
                if m:
                    cur = m.group(1)
                if "self._gitx.remove_env(" in ln:
                    if cur != "_reclaim_env":
                        offenders.append(f"{py.name}: def {cur}: {ln.strip()}")
                    else:
                        funnels += 1
        self.assertEqual(offenders, [])
        self.assertGreaterEqual(funnels, 2)  # gate/task 漏斗俱在（main 无 tasksvc/gateverify）


if __name__ == "__main__":
    unittest.main()
