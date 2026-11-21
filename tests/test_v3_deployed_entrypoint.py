"""Req: F158 — GitStore Phase 3 部署入口（spec gitstore-phase3-deployed-entrypoint）。

agorad/main.py 显式 env 选择（AGORA_API_VERSION=3 / AGORA_BACKEND=sqlite|git）：
缺省 v2/SQLite 零变化；未知值先于绑端口/建存储 fail-closed；v3 生产装配
（真 Oc/Gitx/runner/anchor/exporter）＋service.json backend 投影；install.sh
显式透传＋双 ready 行＋v3 root.token（authsvc JSON 形）接管；`agora` CLI
发现面两形 root.token 同读。隔离纪律：install 测试一律临时 HOME/AGORA_HOME，
不触真实 ~/.config/opencode、插件、bin 与 plaza。
"""
import importlib.machinery
import importlib.util
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from agorad.main import api_version_from_env, backend_from_env, main

ROOT = Path(__file__).resolve().parent.parent

_loader = importlib.machinery.SourceFileLoader(
    "agora_cli_deployed", str(ROOT / "agora"))
_spec = importlib.util.spec_from_loader("agora_cli_deployed", _loader)
agora_cli = importlib.util.module_from_spec(_spec)
_loader.exec_module(agora_cli)

_ENV_KEYS = ("AGORA_PORT", "AGORA_OC_URL", "AGORA_OC_PASSWORD",
             "AGORA_API_VERSION", "AGORA_BACKEND")


def _stub_bin(base):
    """临时 no-op 工具位（opencode/bun）：install.sh 依赖检查只 command -v
    从不执行它们；真实运行冒烟归 agorad 进程测试——隔离安装不依赖宿主 CLI
    （gate verify 子进程 PATH 无 opencode 的环境差异面，派发方裁定）。"""
    b = Path(base) / "stubbin"
    b.mkdir(parents=True, exist_ok=True)
    for name in ("opencode", "bun"):
        p = b / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    return str(b)


def _clean_env(**extra):
    """服务态 env 继承消毒（test_install.sh 同 juris：只认显式注入）。"""
    env = dict(os.environ)
    for k in _ENV_KEYS:
        env.pop(k, None)
    env.update(extra)
    return env


def _read_root_token(home):
    """root.token 两形同读：v3 authsvc JSON / v2 install 纯文本。"""
    raw = (Path(home) / ".agora" / "root.token").read_text(
        encoding="utf-8").strip()
    if raw.startswith("{"):
        return json.loads(raw)["token"]
    return raw


def _scrub(lines):
    """失败消息抹牌行（评审环 C1：真牌不入断言输出/转录）。"""
    return ["AGORA_ROOT_TOKEN: <scrubbed>" if l.startswith("AGORA_ROOT_TOKEN: ")
            else l for l in lines]


def _get_health(port, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"X-Agora-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def _boot_daemon(script, home, ready_marker, timeout=30.0, extra_env=None):
    """真进程拉起（oc 不可达不挡服务）：读到 ready 行返回 (proc, lines, port)。"""
    env = _clean_env(AGORA_HOME=str(home), AGORA_OC_URL="http://127.0.0.1:9",
                     AGORA_PORT="0", PYTHONDONTWRITEBYTECODE="1")
    if extra_env:  # 消毒后显式重注入（版本/后端选择只认显式）
        env.update(extra_env)
    p = subprocess.Popen([sys.executable, str(script)], cwd=str(ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, start_new_session=True)
    lines, port = [], None
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and p.poll() is None:
            if not select.select([p.stdout], [], [], 0.5)[0]:
                continue
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            if ready_marker in line:
                for part in line.split():
                    if part.startswith("port="):
                        port = int(part[5:])
                return p, lines, port
        raise AssertionError(f"未等到 {ready_marker!r}：{_scrub(lines)}")
    except BaseException:
        _stop_daemon(p)
        raise


def _stop_daemon(p):
    if p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except OSError:
            p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(p.pid), 9)
            p.wait(timeout=5)
    if p.stdout:
        p.stdout.close()


def _run_install(home, plaza, extra_env=None, path_prefix=()):
    env = _clean_env(HOME=str(home), AGORA_HOME=str(plaza))
    if path_prefix:
        env["PATH"] = os.pathsep.join(
            [*path_prefix, env.get("PATH", os.defpath)])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(ROOT / "install.sh")], env=env,
                          capture_output=True, text=True, timeout=180)


class TestAgoradV3Entrypoint(unittest.TestCase):
    """env 选择显式化＋fail-closed＋缺省 v2 零变化＋v2/v3 真进程冒烟。"""

    def test_defaults_v2_sqlite_unchanged(self):  # Req: F158
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api_version_from_env(), 2)
            self.assertEqual(backend_from_env(2), "sqlite")

    def test_v3_and_backend_selected_explicitly(self):  # Req: F158
        with mock.patch.dict(os.environ, {"AGORA_API_VERSION": "3"},
                             clear=True):
            self.assertEqual(api_version_from_env(), 3)
            self.assertEqual(backend_from_env(3), "sqlite")
        with mock.patch.dict(os.environ, {"AGORA_API_VERSION": "3",
                                          "AGORA_BACKEND": "git"}, clear=True):
            self.assertEqual(backend_from_env(3), "git")

    def _assert_fail_closed_zero_artifacts(self, env, needle):
        home = Path(tempfile.mkdtemp(prefix="agora-fc-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        import io
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**env, "AGORA_HOME": str(home)},
                             clear=True), \
             mock.patch("sys.stderr", err), \
             mock.patch("agorad.main.discover", return_value=(None, None)):
            rc = main()
        self.assertEqual(rc, 2)
        self.assertIn(needle, err.getvalue())
        for rel in ("agorad.lock", "state", ".agora"):
            self.assertFalse((home / rel).exists(), rel)  # 零动土：锁/存储/服务面

    def test_unknown_api_version_fails_closed(self):  # Req: F158
        self._assert_fail_closed_zero_artifacts(
            {"AGORA_API_VERSION": "9"}, "AGORA_API_VERSION")

    def test_unknown_backend_fails_closed(self):  # Req: F158
        self._assert_fail_closed_zero_artifacts(
            {"AGORA_API_VERSION": "3", "AGORA_BACKEND": "xyz"},
            "AGORA_BACKEND")

    def test_backend_without_v3_rejected_not_silently_ignored(self):  # Req: F158
        # v2 面无后端概念：显式 AGORA_BACKEND＋非 v3＝伪装成功面，fail-closed
        self._assert_fail_closed_zero_artifacts(
            {"AGORA_BACKEND": "git"}, "AGORA_BACKEND")

    def _main_v3_rc(self, home, needle):
        import io
        err = io.StringIO()
        with mock.patch.dict(os.environ, {
                "AGORA_HOME": str(home), "AGORA_API_VERSION": "3",
                "AGORA_BACKEND": "git",
                "AGORA_OC_URL": "http://127.0.0.1:9"}, clear=True), \
             mock.patch("sys.stderr", err):
            rc = main()
        return rc, err.getvalue()

    def test_foreign_ledger_fails_clean_not_traceback(self):  # Req: F158
        # GitStore split-brain 拒开是 StorageError 族（非 ValueError）——
        # 入口须收成 rc=2＋stderr 报因，不许裸 traceback 退 1
        home = Path(tempfile.mkdtemp(prefix="agora-fl-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "state").mkdir(parents=True)
        (home / "state" / "agora.db").write_bytes(b"foreign")
        rc, err = self._main_v3_rc(home, "外来账本")
        self.assertEqual(rc, 2)
        self.assertIn("外来账本", err)

    def test_oserror_env_failures_exit_clean(self):  # Req: F158（评审环 B-F1）
        # git 缺席/坏权限/盘满等环境面（OSError 族）＝干净 rc=2 报因，非 traceback；
        # 夹具：state 路径被文件占位 → mkdir FileExistsError
        home = Path(tempfile.mkdtemp(prefix="agora-os-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "state").write_text("occupied", encoding="utf-8")
        rc, err = self._main_v3_rc(home, "")
        self.assertEqual(rc, 2)
        self.assertTrue(err.strip())

    def test_bad_verify_profiles_fail_before_storage_artifacts(self):  # Req: F158
        # v2 纪律：旋钮配置坏＝先于存储动土失败（不留半建 agora.db/journal）
        home = Path(tempfile.mkdtemp(prefix="agora-vp-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / ".agora").mkdir(parents=True)
        (home / ".agora" / "verify-profiles.json").write_text(
            "{ bad json", encoding="utf-8")
        rc, err = self._main_v3_rc(home, "verify-profiles")
        self.assertEqual(rc, 2)
        self.assertTrue(err.strip())  # compose 压缩版报因不含路径——非空即可
        for name in ("agora.db", "journal.git"):
            self.assertFalse((home / "state" / name).exists(), name)

    def test_v3_sqlite_service_metadata_and_health(self):  # Req: F158
        home = Path(tempfile.mkdtemp(prefix="agora-v3-ep-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        p = None
        try:
            p, lines, _port = _boot_daemon(
                ROOT / "agorad" / "main.py", home, "agorad-v3 ready",
                extra_env={"AGORA_API_VERSION": "3"})
            tok_lines = [i for i, l in enumerate(lines)
                         if l.startswith("AGORA_ROOT_TOKEN: ")]
            self.assertEqual(len(tok_lines), 1)
            self.assertLess(tok_lines[0],
                            next(i for i, l in enumerate(lines)
                                 if "agorad-v3 ready" in l))  # 牌行先于 ready
            sj = json.loads((home / ".agora" / "service.json").read_text())
            self.assertEqual((sj["api_version"], sj["storage_version"],
                              sj["backend"]), (3, 3, "sqlite"))
            self.assertIn("port", sj)  # v2 旧字段保留（CLI/插件发现面）
            self.assertTrue(_get_health(sj["port"], _read_root_token(home))["ok"])
        finally:
            if p:
                _stop_daemon(p)

    def test_v2_default_boot_unchanged(self):  # Req: F158
        home = Path(tempfile.mkdtemp(prefix="agora-v2-ep-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        p = None
        try:
            p, lines, _port = _boot_daemon(
                ROOT / "agorad" / "main.py", home, "agorad ready")
            self.assertTrue(any(l.startswith("AGORA_ROOT_TOKEN: ")
                                for l in lines))
            self.assertFalse(any("agorad-v3" in l for l in lines))
            sj = json.loads((home / ".agora" / "service.json").read_text())
            self.assertNotIn("api_version", sj)  # v2 service.json 形状零变化
            # v2 账本＝JSONL 面（agora.db 是 v3-sqlite 工件）：缺省启动零 SQLite
            self.assertFalse((home / "state" / "agora.db").exists())
            self.assertFalse((home / "state" / "journal.git").exists())
        finally:
            if p:
                _stop_daemon(p)

    def test_cli_discovery_reads_v3_service_json_and_both_token_forms(self):  # Req: F158
        home = Path(tempfile.mkdtemp(prefix="agora-cli-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        ag = home / ".agora"
        ag.mkdir(parents=True)
        (ag / "service.json").write_text(json.dumps(
            {"pid": 1, "port": 45678, "api_version": 3,
             "storage_version": 3, "backend": "git"}), encoding="utf-8")
        (ag / "root.token").write_text(
            json.dumps({"token": "tok-v3-json"}) + "\n", encoding="utf-8")
        base, token = agora_cli.discover(str(home))
        self.assertEqual((base, token),
                         ("http://127.0.0.1:45678", "tok-v3-json"))
        import board_tui  # 评审环 A1：同形发现面（agora-board 控制台脚本）
        self.assertEqual(board_tui.discover(str(home))[1], "tok-v3-json")
        (ag / "root.token").write_text("tok-v2-plain\n", encoding="utf-8")
        self.assertEqual(agora_cli.discover(str(home))[1], "tok-v2-plain")
        with self.assertRaises(ValueError):  # 对象缺 token 键＝坏形归口 ValueError
            (ag / "root.token").write_text('{"no_token": 1}',
                                           encoding="utf-8")
            agora_cli.discover(str(home))


class TestInstallGitV3Deployment(unittest.TestCase):
    """隔离 install（临时 HOME/AGORA_HOME＋显式 v3+git）：装载荷/插件/CLI、
    ready 行、root.token、/health、journal.git 在位且零 SQLite 工件；随后以
    安装载荷真拉起应答 /health。"""

    def setUp(self):
        fix = Path(tempfile.mkdtemp(prefix="agora-inst-v3-"))
        self.addCleanup(shutil.rmtree, fix, ignore_errors=True)
        self.fix, self.home, self.plaza = fix, fix / "home", fix / "plaza"
        self.home.mkdir(parents=True)
        self.path_prefix = (_stub_bin(fix),)

    def test_isolated_v3_git_install_and_payload_launch(self):  # Req: F158
        r = _run_install(self.home, self.plaza,
                         {"AGORA_API_VERSION": "3", "AGORA_BACKEND": "git"},
                         path_prefix=self.path_prefix)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        # 安装载荷指纹（P2-13）＋插件/CLI 在位
        pay = self.plaza / ".agora" / "agorad"
        self.assertTrue((pay / "main.py").is_file())
        pv = (pay / "PAYLOAD_VERSION").read_text(encoding="utf-8").splitlines()
        self.assertEqual(pv[0], str(ROOT))
        self.assertEqual(pv[1], subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip())
        self.assertTrue(
            (self.home / ".config/opencode/plugins/agora/index.ts").is_file())
        self.assertTrue((self.home / ".local/bin/agora").exists())
        # v3 ready 行（双 ready 行契约）＋牌行取牌即抹
        log = (self.plaza / ".agora" / "install-agorad.log").read_text()
        self.assertIn("agorad-v3 ready", log)
        self.assertNotIn("AGORA_ROOT_TOKEN: ", log)
        # service.json v3 投影
        sj = json.loads((self.plaza / ".agora" / "service.json").read_text())
        self.assertEqual((sj["api_version"], sj["storage_version"],
                          sj["backend"]), (3, 3, "git"))
        # root.token：在位、0600、JSON 形可解析（不外泄内容）
        tokfile = self.plaza / ".agora" / "root.token"
        self.assertTrue(tokfile.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(tokfile).st_mode), 0o600)
        token = _read_root_token(self.plaza)
        self.assertTrue(token)
        # git 后端：journal.git 在位、零 SQLite 工件
        self.assertTrue((self.plaza / "state" / "journal.git").is_dir())
        for name in ("agora.db", "agora.db-wal", "agora.db-shm"):
            self.assertFalse((self.plaza / "state" / name).exists(), name)
        # 安装载荷真拉起：复用既有 root.token（无新牌行）应答 /health
        before = tokfile.read_bytes()
        env = _clean_env(AGORA_HOME=str(self.plaza),
                         AGORA_API_VERSION="3", AGORA_BACKEND="git",
                         AGORA_OC_URL="http://127.0.0.1:4096", AGORA_PORT="0",
                         PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, str(pay / "main.py")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, start_new_session=True)
        try:
            lines, deadline = [], time.time() + 30
            while time.time() < deadline and p.poll() is None:
                if not select.select([p.stdout], [], [], 0.5)[0]:
                    continue
                line = p.stdout.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
                if "agorad-v3 ready" in line:
                    break
            self.assertTrue(any("agorad-v3 ready" in l for l in lines),
                            _scrub(lines))
            self.assertFalse(
                [l for l in lines if l.startswith("AGORA_ROOT_TOKEN: ")])
            sj2 = json.loads(
                (self.plaza / ".agora" / "service.json").read_text())
            self.assertEqual(sj2["backend"], "git")
            h = _get_health(sj2["port"], _read_root_token(self.plaza))
            self.assertTrue(h["ok"])
            self.assertEqual(h.get("api_version"), 3)
            self.assertEqual(tokfile.read_bytes(), before)  # 牌复用不轮换
            for name in ("agora.db", "agora.db-wal", "agora.db-shm"):
                self.assertFalse((self.plaza / "state" / name).exists(), name)
        finally:
            _stop_daemon(p)


class TestInstallDefaultV2Unchanged(unittest.TestCase):
    """无显式 env 的既有安装契约零变化（特征级：不二次长跑 daemon）。"""

    def setUp(self):
        fix = Path(tempfile.mkdtemp(prefix="agora-inst-v2-"))
        self.addCleanup(shutil.rmtree, fix, ignore_errors=True)
        self.fix, self.home, self.plaza = fix, fix / "home", fix / "plaza"
        self.home.mkdir(parents=True)
        self.path_prefix = (_stub_bin(fix),)

    def test_default_install_launch_contract(self):  # Req: F158
        r = _run_install(self.home, self.plaza, path_prefix=self.path_prefix)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        log = (self.plaza / ".agora" / "install-agorad.log").read_text()
        self.assertIn("agorad ready", log)  # v2 ready 行（非 -v3）
        self.assertNotIn("agorad-v3", log)
        sj = json.loads((self.plaza / ".agora" / "service.json").read_text())
        self.assertIn("port", sj)
        self.assertNotIn("api_version", sj)
        # v2 缺省＝JSONL 账本：零 v3 工件（agora.db/journal.git 皆 sqlite/git 后端面）
        self.assertFalse((self.plaza / "state" / "agora.db").exists())
        self.assertFalse((self.plaza / "state" / "journal.git").exists())
        tok = (self.plaza / ".agora" / "root.token").read_text(
            encoding="utf-8").strip()
        self.assertTrue(tok and not tok.startswith("{"))
        # 源级锁：透传不添缺省（缺省注入＝静默改行为面）
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        for marker in (":-2}", ":-3}", ":-sqlite}", ":-git}"):
            self.assertNotIn(marker, text, marker)

    def test_v3_via_agorad_env_keeps_json_root_token(self):  # Req: F158
        # agorad.env 层选 v3（旋钮免疫 supervisor 重生的文档化用途）：daemon
        # 实跑 v3 时 install 不得以纯文本覆写 authsvc 的 JSON 形 root.token
        # （判定须按 daemon 实际形态＝ready 行，非壳 env）
        ag = self.plaza / ".agora"
        ag.mkdir(parents=True)
        (ag / "agorad.env").write_text("AGORA_API_VERSION=3\n",
                                       encoding="utf-8")
        r = _run_install(self.home, self.plaza, path_prefix=self.path_prefix)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        log = (ag / "install-agorad.log").read_text()
        self.assertIn("agorad-v3 ready", log)  # daemon 经 env 文件跑 v3
        raw = (ag / "root.token").read_text(encoding="utf-8").strip()
        self.assertTrue(raw.startswith("{"))
        self.assertTrue(json.loads(raw)["token"])


if __name__ == "__main__":
    unittest.main()
