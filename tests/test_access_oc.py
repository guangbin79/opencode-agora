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

    def test_inject_notify_synthetic_resume_true(self):  # Req: F76
        """notify 通道（gt-5bc312 勘误）＝synthetic resume:true——落合成消息并调度
        回合（弹消息通知、无选择框）；死会话/非 ses 形同 inject 契约归 False。"""
        sid = self.oc.create("/e", "B")
        self.assertTrue(self.oc.inject_notify(sid, "wake up"))
        self.assertEqual(self.stub.sessions[sid]["synthetics"], [("wake up", True)])
        self.stub.kill(sid)
        self.assertFalse(self.oc.inject_notify(sid, "again"))  # 死会话 404→False
        self.assertFalse(self.oc.inject_notify("main", "x"))  # 非 ses 形 400→False

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

    def test_messages_envelope_tolerant_and_skips_non_dict(self):  # Req: F57
        sid = self.oc.create("/e", "B")
        self.stub.sessions[sid]["messages"] = [
            {"type": "user"},
            {"type": "assistant", "tokens": {"input": 1}},
        ]
        self.assertEqual([m["type"] for m in self.oc.messages(sid)],
                         ["user", "assistant"])  # 信封 {data:[...]}：dict 取 data
        self.stub.msg_raw = True  # 真机裸数组形状拨杆（F56 /config 热修同族）
        self.stub.sessions[sid]["messages"] = ["junk", {"type": "user"}]
        self.assertEqual(self.oc.messages(sid), [{"type": "user"}])  # 非 dict 元素守卫跳过
        self.stub.kill(sid)
        self.assertEqual(self.oc.messages(sid), [])  # 死会话 404→[]（同 last_message 契约）

    def test_prompt_carries_model_when_set(self):  # Req: F55
        frag = {"providerID": "zai-coding-plan", "modelID": "glm-5.3"}
        sid = self.oc.create("/e", "BOOT", model="zai-coding-plan/glm-5.3")
        self.assertTrue(self.oc.inject(sid, "m", model="zai-coding-plan/glm-5.3"))
        # model 是 prompt 逐回合参数：boot 与 inject 各携一次
        self.assertEqual(self.stub.sessions[sid]["prompt_models"], [frag, frag])

    def test_omitted_model_keeps_body_free_of_model(self):  # Req: F55
        sid = self.oc.create("/e", "BOOT")
        self.assertTrue(self.oc.inject(sid, "m"))
        self.assertEqual(self.stub.sessions[sid]["prompt_models"],
                         [None, None])  # 防「恒带键」回归：缺省 body 不得带 model 键

    def test_boot_failure_deletes_created_session(self):  # Req: F55
        self.stub.fail_prompt = True  # 坏 model 串→prompt 400：半建会话须清理不残留
        with self.assertRaises(OcUnavailable):
            self.oc.create("/e", "BOOT", model="bad/model")
        self.assertEqual(self.stub.sessions, {})

    def test_models_merges_agent_and_config_bindings(self):  # Req: F56
        # 键名归一两侧照实测：/config 侧读 info.model/info.agents.*.model 的 "model" 键，
        # /agent 侧读 model 的 "id" 键；合并去重排序＝缺省 model＋agents 绑定投影
        self.assertEqual(self.oc.models(),
                         ["zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"])

    def test_models_tolerates_config_envelope_shape(self):  # Req: F56（热修信封宽容分支）
        # 双形状宽容回归钉：信封 {location, data:[...]} 也吃（stub 缺省回裸数组后此分支无既有覆盖）
        self.stub.config_docs = {"location": "stub", "data": self.stub.config_docs}
        self.assertEqual(self.oc.models(),
                         ["zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"])

    def test_models_skips_non_dict_agent_model(self):  # Req: F56（R1 评审补：model 字段漂移守卫）
        # /agent 元素 model 漂移成非 dict（本热修所防形状族）：跳过不炸，其余绑定照常收录
        self.stub.agents = [{"id": "build", "model": "zai-coding-plan/glm-5.3-flash"},
                            {"id": "plan", "model": {"providerID": "zai-coding-plan",
                                                     "id": "glm-5.3"}}]
        self.assertEqual(self.oc.models(), ["zai-coding-plan/glm-5.3",
                                            "zai-coding-plan/glm-5.3-flash"])

    def test_models_policy_filters_providers(self):  # Req: F56
        self.stub.config_docs = [{
            "type": "document", "path": "opencode.json",
            "info": {"policies": [{"action": "provider.use",
                                   "resource": "zai-coding-plan", "effect": "deny"},
                                  {"action": "provider.use",
                                   "resource": "kimi-code-plan-cn", "effect": "allow"}]}}]
        self.stub.agents = [{"id": "build", "model": {"providerID": "zai-coding-plan",
                                                      "id": "glm-5.3-flash"}}]
        # deny 剔除优先于 allow 白名单：绑了 zai 也被剔
        self.assertNotIn("zai-coding-plan/glm-5.3-flash", self.oc.models())

    def test_models_unavailable_raises(self):  # Req: F56
        self.stub.stop()
        with self.assertRaises(OcUnavailable):
            self.oc.models()  # 任一端点不可达照抛（吞的决心归 doctor 调用方）


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

    def test_peer_healed_still_retries(self):  # Req: ag-dfc5f6（评审 R2：并发 401 他线程先换面）
        self._rotate("pw2")
        req, face = self.oc._req("/info")  # 本请求持旧凭据面对（并发窗：换面尚未落）
        self.oc._swap(self.stub.base_url, "pw2")  # 他线程先换面＝实例当前面已新
        with self.assertRaises(OcUnavailable):
            self.oc._do(req)  # 旧凭据请求照吃 401
        self.assertTrue(self.oc._refresh(face))  # 判据对比失败请求的面（非当前面）→ 仍判可重试
        self.assertTrue(self.oc.probe())  # 重试放行


if __name__ == "__main__":
    unittest.main()
