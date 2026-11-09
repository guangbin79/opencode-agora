"""main 装配消费面（设计 §3/§4.8/R20）：service.json 落 .agora/、env 缺省、root 牌行固定位。

service.json 路径与 env 缺省是②期 lift/T5 拉起链路（F6 服务重启恢复）的探测契约；
root 牌 stdout 行前缀是 T4 install 捕获解析（F1 主会话 root 牌）的固定接口。
"""
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agorad.main import assemble, audit_root_poison, env_config, issue_root, \
    load_env_file, main, quick_limits_from_env, verify_ceiling_from_env, \
    verify_timeout_from_env

ROOT = Path(__file__).resolve().parent.parent


class TestEnvConfig(unittest.TestCase):
    def test_home_defaults_to_plaza(self):  # Req: F6
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("agorad.main.discover", return_value=(None, None)):
            home, url, _pw = env_config()
        self.assertEqual(home, str(Path.home() / "plaza"))
        self.assertIsNone(url)  # 无 service.json、无 env 显式 → None（main 报错）

    def test_explicit_home_wins(self):  # Req: F6
        with mock.patch.dict(os.environ, {"AGORA_HOME": "/tmp/x"}), \
             mock.patch("agorad.main.discover", return_value=(None, None)):
            home, _, _pw = env_config()
        self.assertEqual(home, "/tmp/x")

    def test_oc_discovery_from_service_json(self):  # Req: F6（T5：AGORA_OC_URL 发现来源实证）
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("agorad.main.discover",
                        return_value=("http://127.0.0.1:49374", "pw")):
            url, password = env_config()[1:]
        self.assertEqual(url, "http://127.0.0.1:49374")
        self.assertEqual(password, "pw")

    def test_oc_url_missing_exits_2_and_names_it(self):  # Req: F6
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("agorad.main.discover", return_value=(None, None)):
            import io
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                rc = main()
        self.assertEqual(rc, 2)
        self.assertIn("AGORA_OC_URL", err.getvalue())


class TestVerifyTimeoutConfig(unittest.TestCase):
    def test_default_and_positive_fraction(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(verify_timeout_from_env(), 300)
        with mock.patch.dict(os.environ, {"AGORA_VERIFY_TIMEOUT": "0.25"}, clear=True):
            self.assertEqual(verify_timeout_from_env(), 0.25)

    def test_invalid_values_fail_fast(self):
        for raw in ("", "abc", "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(raw=raw), \
                 mock.patch.dict(os.environ, {"AGORA_VERIFY_TIMEOUT": raw}, clear=True):
                with self.assertRaises(ValueError) as cm:
                    verify_timeout_from_env()
                self.assertIn("AGORA_VERIFY_TIMEOUT", str(cm.exception))


class TestVerifyCeilingConfig(unittest.TestCase):
    def test_unset_returns_none_and_value_parses(self):  # Req: F36
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(verify_ceiling_from_env())  # 未设＝None（缺省 4×软顶）
        with mock.patch.dict(os.environ, {"AGORA_VERIFY_CEILING": "9600"}, clear=True):
            self.assertEqual(verify_ceiling_from_env(), 9600.0)

    def test_invalid_values_fail_fast(self):  # Req: F36
        for raw in ("", "abc", "0", "-1", "nan", "inf"):
            with self.subTest(raw=raw), \
                 mock.patch.dict(os.environ, {"AGORA_VERIFY_CEILING": raw}, clear=True):
                with self.assertRaises(ValueError) as cm:
                    verify_ceiling_from_env()
                self.assertIn("AGORA_VERIFY_CEILING", str(cm.exception))


class TestEnvFileDefaults(unittest.TestCase):
    """agorad.env 旋钮缺省层（ag-fc67bc）：加载/优先级/缺文件/坏行容错。"""

    def test_load_priority_and_read_points(self):  # Req: ag-fc67bc
        tmp = tempfile.mkdtemp()
        try:
            d = Path(tmp) / ".agora"
            d.mkdir(parents=True)
            (d / "agorad.env").write_text(
                "# 注释\nAGORA_VERIFY_TIMEOUT=3600\n"
                "AGORA_VERIFY_CEILING=\"14400\"\n"
                "\n坏行无等号\n=空键\nexport AGORA_VERIFY_TIMEOUT=1\n"
                "AGORA_HOME=/elsewhere\nAGORA_HEALTH_INTERVAL=60\n"
                "AGORA_HEALTH_INTERVAL=99\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGORA_HOME": tmp}, clear=True):
                self.assertEqual(load_env_file(), {
                    "AGORA_VERIFY_TIMEOUT": "3600",
                    "AGORA_VERIFY_CEILING": "14400",
                    "AGORA_HEALTH_INTERVAL": "60"})
                self.assertNotIn("export AGORA_VERIFY_TIMEOUT", os.environ)
                self.assertEqual(os.environ["AGORA_HOME"], tmp)  # 保留键不被重定向
                # 读取点自动受益（原纯读 os.environ 的事故面）
                self.assertEqual(verify_timeout_from_env(), 3600.0)
                self.assertEqual(verify_ceiling_from_env(), 14400.0)
            with mock.patch.dict(os.environ,
                                 {"AGORA_HOME": tmp, "AGORA_VERIFY_TIMEOUT": "600"},
                                 clear=True):
                load_env_file()
                self.assertEqual(verify_timeout_from_env(), 600.0)  # 显式 env 优先
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_file_silent(self):  # Req: ag-fc67bc
        with mock.patch.dict(os.environ, {"AGORA_HOME": "/nonexistent-agora-fx"},
                             clear=True):
            self.assertEqual(load_env_file(), {})
            self.assertNotIn("AGORA_VERIFY_TIMEOUT", os.environ)


class TestQuickLimitsConfig(unittest.TestCase):
    """F65 quick 包络旋钮：缺省/env 覆盖/坏值 fail-fast（VERIFY_TIMEOUT 同款）。"""

    def test_defaults_and_override(self):  # Req: F65
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(quick_limits_from_env(), (3, 30))
        with mock.patch.dict(os.environ, {"AGORA_QUICK_MAX_FILES": "5",
                                          "AGORA_QUICK_MAX_LINES": "40"},
                             clear=True):
            self.assertEqual(quick_limits_from_env(), (5, 40))

    def test_invalid_values_fail_fast(self):  # Req: F65
        for name, raw in (("AGORA_QUICK_MAX_FILES", "0"),
                          ("AGORA_QUICK_MAX_FILES", "-1"),
                          ("AGORA_QUICK_MAX_FILES", "abc"),
                          ("AGORA_QUICK_MAX_LINES", "0"),
                          ("AGORA_QUICK_MAX_LINES", "2.5")):
            with self.subTest(name=name, raw=raw), \
                 mock.patch.dict(os.environ, {name: raw}, clear=True):
                with self.assertRaises(ValueError) as cm:
                    quick_limits_from_env()
                self.assertIn(name, str(cm.exception))


class TestAssembleWiring(unittest.TestCase):
    def test_gate_gets_ceiling_and_default_roster_path(self):  # Req: F36
        tmp = tempfile.mkdtemp()
        try:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw",
                           verify_ceiling=9600.0)
            self.assertEqual(app.gate._verify_ceiling, 9600.0)  # 装配透传
            self.assertEqual(app.gate._roster_path,
                             str(Path(tmp) / ".agora" / "ignore.roster"))
            app2 = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw")
            self.assertIsNone(app2.gate._verify_ceiling)  # 未设＝None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestIssueRoot(unittest.TestCase):
    """ag-588eb8：启动 root 签发面收窄（裁决 A·新装防御）——root 牌只属 daemon
    常量身份 sid="root"，占位/客户端会话 sid 不进 root 签发。"""

    def test_bootstrap_issues_root_to_daemon_identity(self):  # ag-588eb8
        tmp = tempfile.mkdtemp()
        try:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw")
            tok = issue_root(app)
            self.assertIsNotNone(tok)
            self.assertEqual(app.auth.identify(tok), ("root", "root"))
            self.assertIsNone(app.auth.token_for("main"))  # 占位 sid 零 root 落名
            self.assertIsNone(app.auth.token_for("ses-client"))  # 客户端 sid 同
            self.assertIsNone(issue_root(app))  # 已有 root 不重签（rotate 语义保留）
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGateQuickWiring(unittest.TestCase):
    def test_gate_gets_quick_limits(self):  # Req: F65
        tmp = tempfile.mkdtemp()
        try:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw",
                           quick_max_files=5, quick_max_lines=40)
            self.assertEqual(app.gate._quick_max_files, 5)  # 装配透传
            self.assertEqual(app.gate._quick_max_lines, 40)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStartupRootPoisonAudit(unittest.TestCase):
    """ag-588eb8 裁决 A：启动维护只审计不撤销——会话 sid 持 root 的存量条目
    计数入账（events 一行），真 root（sid=root）与 worker 在飞牌分毫不动
    （撤销唯一 root 即断 CLI/enforce 面；窗口自愈归 cred_issue 换牌面）。"""

    def test_audit_counts_poisoned_roots_without_revoking(self):  # ag-588eb8
        tmp = tempfile.mkdtemp()
        try:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw")
            tok_daemon = app.auth.issue("root", "root")  # 真 root（daemon 身份）
            tok_poison = app.auth.issue("ses-poison", "root")  # 会话 sid 污染
            tok_worker = app.auth.issue("ses-worker", "worker")  # 在飞 worker
            self.assertEqual(audit_root_poison(app), 1)
            toks = app.store.read_ephemeral("tokens")
            self.assertEqual(len(toks), 3)  # 零撤销：账本原样
            self.assertEqual(app.auth.identify(tok_daemon), ("root", "root"))
            self.assertEqual(app.auth.identify(tok_poison), ("ses-poison", "root"))
            self.assertEqual(app.auth.identify(tok_worker), ("ses-worker", "worker"))
            ev = [e for e in app.store.read_events()
                  if e["event"] == "root_poison_audit"]
            self.assertEqual([(e["actor"], e["note"]) for e in ev],
                             [("agorad", "poisoned=1")])  # 审计一行
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_audit_silent_when_ledger_clean(self):  # ag-588eb8
        tmp = tempfile.mkdtemp()
        try:
            app = assemble(tmp, oc_url="http://127.0.0.1:9", oc_password="pw")
            app.auth.issue("ses-w", "worker")
            self.assertEqual(audit_root_poison(app), 0)
            self.assertEqual([e for e in app.store.read_events()
                              if e["event"] == "root_poison_audit"], [])  # 零污染零新行
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBoot(unittest.TestCase):
    """真进程拉起（oc 不可达不挡服务）：stdout 与 service.json 落位即消费契约。"""

    def test_service_json_under_dot_agora_and_root_token_line(self):  # Req: F6
        tmp = Path(tempfile.mkdtemp())
        p = None
        try:
            env = dict(os.environ, AGORA_HOME=str(tmp),
                       AGORA_OC_URL="http://127.0.0.1:9", AGORA_PORT="0")
            p = subprocess.Popen(
                [sys.executable, "agorad/main.py"], cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            lines, deadline = [], time.time() + 20
            while time.time() < deadline and p.poll() is None:
                r, _, _ = select.select([p.stdout], [], [], 1.0)
                if not r:
                    continue
                line = p.stdout.readline()
                if not line:
                    break
                lines.append(line)
                if "agorad ready" in line:
                    break
            self.assertTrue(
                any(l.startswith("AGORA_ROOT_TOKEN: ") for l in lines), lines)
            sj = json.loads((tmp / ".agora" / "service.json").read_text())
            self.assertIn("port", sj)
        finally:
            if p and p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
            if p:
                p.stdout.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
