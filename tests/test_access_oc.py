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

    def test_injects_recorded_in_order(self):  # Req: F4
        sid = self.oc.create("/tmp/env", "BOOT")
        for i in range(3):
            self.assertTrue(self.oc.inject(sid, f"m{i}"))
        self.assertEqual(self.stub.sessions[sid]["injects"], ["m0", "m1", "m2"])


if __name__ == "__main__":
    unittest.main()
