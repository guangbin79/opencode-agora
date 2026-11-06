"""T1 访问层契约：oc 对 stub opencode 的调用形状（真本地 HTTP stub，无 mock）。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from agorad.access.oc import Oc, OcUnavailable
from tests.stub_opencode import StubOpencode


class TestAccessOc(unittest.TestCase):
    def setUp(self):
        self.stub = StubOpencode()
        self.stub.start()
        self.oc = Oc(self.stub.base_url)

    def tearDown(self):
        if self.stub.httpd is not None:
            self.stub.stop()

    def test_create_returns_sid(self):  # Req: F3
        sid = self.oc.create("/tmp/env", "BOOT")
        self.assertTrue(sid)
        self.assertTrue(self.oc.alive(sid))

    def test_inject_dead_session_false(self):  # Req: F4
        sid = self.oc.create("/tmp/env", "BOOT")
        self.stub.kill(sid)
        self.assertFalse(self.oc.inject(sid, "hello"))
        self.assertTrue(self.oc.inject(self.oc.create("/e", "B"), "hi"))  # 活会话对照 True

    def test_inject_non_ses_shape_sid_false(self):  # Req: F3
        # T6 实测：真 opencode 对非 ses_ 形 sid（如 agorad 主会话位 "main"）prompt→400（非 404），
        # inject 契约同归 False（失败留库待补拉），cancel/通知链不因之 400。
        self.assertFalse(self.oc.inject("main", "hello"))

    def test_alive_unknown_sid_false(self):  # Req: F5
        self.assertFalse(self.oc.alive("no-such-sid"))

    def test_probe_returns_oc_version(self):  # Req: F3
        p = self.oc.probe()
        self.assertTrue(p["ok"])
        self.assertTrue(p["oc_version"])

    def test_stub_down_raises_unavailable(self):  # Req: F3
        self.stub.stop()
        with self.assertRaises(OcUnavailable):
            self.oc.probe()
        with self.assertRaises(OcUnavailable):
            self.oc.create("/e", "B")

    def test_non_json_200_raises_unavailable(self):  # Req: F3（T5 修正：200 text/html 同族面）
        self.stub.poison_html = True
        with self.assertRaises(OcUnavailable):
            self.oc.probe()
        with self.assertRaises(OcUnavailable):
            self.oc.create("/e", "B")

    def test_injects_recorded_in_order(self):  # Req: F4
        sid = self.oc.create("/tmp/env", "BOOT")
        for i in range(3):
            self.assertTrue(self.oc.inject(sid, f"m{i}"))
        # boot 文本随 create 走同一 prompt 通道（真 shape），先于后续 inject
        self.assertEqual(self.stub.sessions[sid]["injects"], ["BOOT", "m0", "m1", "m2"])

    def test_create_binds_directory_via_move(self):  # Req: F3（T5：POST /session 无 cwd，move 绑 env）
        sid = self.oc.create("/tmp/env-x", "BOOT")
        self.assertEqual(self.stub.sessions[sid]["directory"], "/tmp/env-x")

    def test_last_message_tail_empty_and_dead(self):  # Req: F52
        sid = self.oc.create("/e", "B")
        self.assertIsNone(self.oc.last_message(sid))  # 空会话→None
        self.stub.sessions[sid]["messages"] = [
            {"type": "user", "time": {"created": 1000}},
            {"type": "assistant", "time": {"created": 2000},
             "content": [{"type": "tool", "name": "shell",
                          "state": {"title": "pytest"}}]},
        ]
        m = self.oc.last_message(sid)  # 尾条原样（2.2.2 shape：type/time.created/assistant.content）
        self.assertEqual(m["type"], "assistant")
        self.assertEqual(m["time"]["created"], 2000)
        self.assertEqual(m["content"][0]["name"], "shell")
        self.stub.kill(sid)
        self.assertIsNone(self.oc.last_message(sid))  # 死会话 404→None（同 session 契约）

    def test_last_message_down_raises_unavailable(self):  # Req: F52
        self.stub.stop()
        with self.assertRaises(OcUnavailable):
            self.oc.last_message("ses_x")  # 服务不可达照抛（吞的决心归 health）


class TestAccessOc401Heal(unittest.TestCase):
    """401 凭据自愈（ag-dfc5f6）：opencode serve 重启轮换密码后，同一 Oc 实例
    重读 service.json 换认证面并原调用重试恰一次；二次 401/无新凭据照抛。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "opencode").mkdir(parents=True)
        self._saved_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.state)  # discover() 刷新源指向临时 service.json
        self.stub = StubOpencode()
        self.stub.password = "pw1"
        self.stub.start()
        self._write_service("pw1")
        self.oc = Oc(self.stub.base_url, "pw1")

    def tearDown(self):
        if self.stub.httpd is not None:
            self.stub.stop()
        if self._saved_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._saved_state
        self.tmp.cleanup()

    def _write_service(self, password, url=None):
        (self.state / "opencode" / "service.json").write_text(
            json.dumps({"url": url or self.stub.base_url, "password": password}))

    def _rotate(self, stub_pw, service_pw=None):
        self.stub.password = stub_pw  # 模拟 opencode serve 重启：密码即刻轮换
        self._write_service(service_pw if service_pw is not None else stub_pw)

    def test_rotation_self_heals_same_instance(self):  # Req: ag-dfc5f6
        self._rotate("pw2")
        self.assertTrue(self.oc.probe())  # 旧凭据先吃 401→重发现→对新密码放行
        sid = self.oc.create("/e", "B")  # 换面后同一实例后续调用直接走新凭据
        self.assertTrue(self.oc.inject(sid, "hi"))

    def test_rotation_stale_service_still_401(self):  # Req: ag-dfc5f6
        self._rotate("pw2", service_pw="pw1")  # service.json 未跟上轮换＝无新凭据可换
        with self.assertRaises(OcUnavailable) as cm:
            self.oc.probe()
        self.assertEqual(cm.exception.status, 401)  # 凭据对未变不重试：原 401 照抛

    def test_second_401_raises(self):  # Req: ag-dfc5f6
        self._rotate("pw2", service_pw="pw3")  # 刷新源与 stub 各持一密：换面重试仍 401
        with self.assertRaises(OcUnavailable) as cm:
            self.oc.probe()
        self.assertEqual(cm.exception.status, 401)  # 二次 401 照常上抛（重试恰一次）


if __name__ == "__main__":
    unittest.main()
