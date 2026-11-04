"""T1 访问层契约：oc 对 stub opencode 的调用形状（真本地 HTTP stub，无 mock）。"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
