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

from agorad.main import env_config, main

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
