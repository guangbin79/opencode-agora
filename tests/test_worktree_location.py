"""F129 worktree 落位校验：纯判定／创建拦截／存量巡检（pytest，tmp_path 假仓）。"""
import subprocess
from pathlib import Path

import pytest

from agorad.access.gitx import Gitx, worktree_location_error
from agorad.access.wtscan import main, scan_misplaced_worktrees


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(["init", "-b", "main"], r)
    _git(["config", "user.email", "t@t"], r)
    _git(["config", "user.name", "t"], r)
    # 密闭：剥全局 excludes（本机 ~/.gitignore 可隐掉未跟踪目录，巡检假绿）
    _git(["config", "core.excludesFile", "/dev/null"], r)
    (r / "src.py").write_text("v1\n")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "init"], r)
    return r


def _worktrees(cwd):
    out = _git(["worktree", "list", "--porcelain"], cwd).stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines()
            if ln.startswith("worktree ")]


def test_sibling_location_passes(repo):  # Req: F129
    env = repo.parent / "proj__wt-ag-1a2b3c__11210000abcd"
    assert worktree_location_error(repo, env) is None


def test_nested_inside_rejected_with_canonical_path(repo):  # Req: F129
    env = repo / "sub" / "proj__wt-ag-1a2b3c__11210000abcd"
    msg = worktree_location_error(repo, env)
    assert msg is not None
    assert str(repo.parent) in msg  # 拒因必含 canonical 期望落位（repo 同级）


def test_worktree_equal_repo_rejected(repo):  # Req: F129
    assert worktree_location_error(repo, repo) is not None


def test_build_env_rejects_misplaced_worktree(repo, monkeypatch):  # Req: F129
    gx = Gitx()
    # name 含遍历成分 → env 解析进仓工作树内（落位闸须触 git 前拒）
    monkeypatch.setattr(
        gx, "_env_name", lambda _repo, _tid: f"../{repo.name}/evil__wt-x")
    with pytest.raises(RuntimeError) as ei:
        gx.build_env(str(repo), "ag-evil")
    assert str(repo.parent) in str(ei.value)
    assert _worktrees(repo) == [str(repo)]  # 零副作用：未建 worktree


def test_build_env_compliant_unchanged(repo):  # Req: F129
    env = Gitx().build_env(str(repo), "ag-1a2b3c")
    assert Path(env).parent == repo.parent  # 合规路径行为不变：仍落 repo 同级
    assert Path(env).name.startswith("proj__wt-ag-1a2b3c")
    assert env in _worktrees(repo)


def test_scan_detects_planted_worktree_dir(repo):  # Req: F129
    planted = repo / "proj__wt-ag-planted__00000000aaaa"
    planted.mkdir()
    (planted / ".git").write_text("gitdir: ../.git/worktrees/x\n")
    assert scan_misplaced_worktrees(repo) == [planted.name]


def test_scan_ignores_tracked_wt_named_dir(repo):  # Req: F129
    tracked = repo / "docs__wt-kept"
    tracked.mkdir()
    (tracked / "note.md").write_text("tracked\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "add wt-named dir"], repo)
    assert scan_misplaced_worktrees(repo) == []


def test_scan_clean_repo_cli_exit0_empty(repo, capsys):  # Req: F129
    assert scan_misplaced_worktrees(repo) == []
    assert main([str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_cli_found_exit1_prints_paths(repo, capsys):  # Req: F129
    (repo / "foo__wt-bar").mkdir()
    assert main([str(repo)]) == 1
    assert "foo__wt-bar" in capsys.readouterr().out


def test_cli_usage_error_exit2(capsys):  # Req: F129
    assert main([]) == 2
