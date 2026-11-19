"""F125 诊断通知引用优先与产物观测契约。

外层显示层截断不可控：稳定引用（home/msg/page/artifact）必须最先出现，
通知只承担摘要＋指针，完整内容永远在磁盘（state/messages 与 gate artifacts）。
锚对应任务页断言清单：A 长诊断注入引用先于正文、A2 产物在案 artifact 行、
A3 消息库与 check_mail 正文原文、C 短消息零引用噪音、D digest 头部
home/msgs_dir/ids＋#id 摘要、E 无产物不输出 artifact 行、F 产物在 home 外
绝对路径、I replay 长消息引用优先。（B gate 流、G 任务页、H board note
分别锚在 test_gate_artifacts/test_access_pages/test_board_tui。）
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.gate_artifacts import ArtifactStore
from agorad.core.msg import Router
from agorad.core.state import State
from tests.test_core_gate import SHA, FakeRunner, TestCoreGate
from tests.test_core_msg import FakeOc

LONG = "诊断正文起点 " + "x" * 300  # > _REF_LEN(240)：长诊断触发引用


class _MsgCase(unittest.TestCase):
    """Router 夹具：t1 running，dispatcher=d1 worker=w1（同 test_core_msg）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.store.append_state({
            "version": 1,
            "tasks": {
                "t1": {"state": "running", "fail_streak": 0, "title": "x",
                       "created": 0.0, "updated": 0.0,
                       "dispatcher_sid": "d1", "worker_sid": "w1"},
            },
            "mail_read": {},
        })
        self.state = State(self.store)
        self.oc = FakeOc()
        self.r = Router(self.store, self.state, self.oc)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_artifact(self, path):
        self.state.set_fields("t1", gate_artifact={
            "path": path, "phase": "verify", "rc": 1,
            "failure_kind": "content_failed", "duration_s": 2.5,
            "bytes": 99})


class TestRefFirst(_MsgCase):
    """A/A2/A3/C/E/F＋触发矩阵：注入文本 [agora] 头后引用行先于正文。"""

    def test_A_long_diagnostic_refs_before_body(self):  # Req: F125 A1/A3
        mid = self.r.post("w1", ("up", "t1"), "result", LONG)
        (sid, text), = self.oc.calls
        self.assertEqual(sid, "d1")
        header = "[agora] task t1 result from w1\n"
        self.assertTrue(text.startswith(header))
        for key in ("home=", "msg=", "page="):
            self.assertLess(len(header) - 1, text.index(key))  # 头后即引用
        self.assertIn(f"home={self.tmp}", text)
        self.assertIn(f"msg=state/messages/{mid:08d}.md", text)
        self.assertIn("page=wiki/tasks/t1.md", text)
        self.assertLess(text.index("page="), text.index(LONG[:16]))  # 引用先于正文
        (m,) = self.store.read_messages()  # A3：消息库 body 原文无引用前缀
        self.assertEqual(m["body"], LONG)
        self.assertNotIn("home=", m["body"])

    def test_A3_check_mail_returns_raw_body(self):  # Req: F125 A3
        self.oc.online = False  # 注入失败留库：补拉面返回原文
        self.r.post("w1", ("up", "t1"), "result", LONG)
        (got,) = self.r.check_mail("d1")
        self.assertEqual(got["body"], LONG)
        self.assertNotIn("msg=state/messages", got["body"])

    def test_A2_artifact_line_relative_when_in_case(self):  # Req: F125 A2
        self._set_artifact(str(self.tmp / "state/gate-artifacts/t1/d"))
        self.r.post("w1", ("up", "t1"), "result", LONG)
        text = self.oc.calls[0][1]
        self.assertIn("artifact=state/gate-artifacts/t1/d", text)
        self.assertLess(text.index("artifact="), text.index(LONG[:16]))
        head = text[:240]
        for key in ("msg=", "artifact="):  # B 口径：前 240 字符内引用可达
            self.assertIn(key, head)

    def test_C_short_milestone_no_refs(self):  # Req: F125 A5/C
        self.r.post("w1", ("up", "t1"), "milestone", "verify 超软顶——无需操作")
        (sid, text), = self.oc.calls
        self.assertEqual(text, "[agora] task t1 milestone from w1\n"
                         "verify 超软顶——无需操作")
        self.assertNotIn("home=", text)

    def test_E_no_artifact_line_without_projection(self):  # Req: F125 E
        self.r.post("w1", ("up", "t1"), "result", LONG)
        text = self.oc.calls[0][1]
        self.assertIn("page=wiki/tasks/t1.md", text)
        self.assertNotIn("artifact=", text)

    def test_F_artifact_outside_home_absolute(self):  # Req: F125 F
        self._set_artifact("/opt/elsewhere/artifact-1")
        self.r.post("w1", ("up", "t1"), "result", LONG)
        self.assertIn("artifact=/opt/elsewhere/artifact-1",
                      self.oc.calls[0][1])

    def test_trigger_critical_kind_with_marker(self):  # Req: F125 §1 触发②
        self.r.post("w1", ("up", "t1"), "question", "verify 失败")  # 短正文
        self.assertIn("msg=state/messages/", self.oc.calls[0][1])

    def test_trigger_artifact_row_with_tail(self):  # Req: F125 §1 触发③
        self._set_artifact(str(self.tmp / "state/gate-artifacts/t1/d"))
        self.r.post("w1", ("up", "t1"), "process", "输出尾部:\nboom")  # 非 _REF_KINDS
        self.assertIn("msg=state/messages/", self.oc.calls[0][1])

    def test_no_trigger_short_plain_process(self):  # Req: F125 §1 短消息零噪音
        self.r.post("w1", ("up", "t1"), "process", "普通短进度")
        self.assertNotIn("home=", self.oc.calls[0][1])


class TestDigestRefs(_MsgCase):
    """D：F42 digest 头部全文引用优先——home/msgs_dir/ids 先于 #id 摘要。"""

    def test_D_digest_header_and_id_prefixed_summaries(self):  # Req: F125 A4/D
        for i in range(5):
            self.r.post("w1", ("up", "t1"), "note", f"n{i}")
        (kind, sid, text), = self.oc.events
        self.assertEqual((kind, sid), ("silent", "d1"))
        self.assertEqual(text,
                         f"[agora] 5 条过程消息（t1）\n"
                         f"home={self.tmp}\n"
                         "msgs_dir=state/messages\n"
                         "ids=1,2,3,4,5\n"
                         "#1 n0\n#2 n1\n#3 n2\n#4 n3\n#5 n4")
        self.assertLess(text.index("msgs_dir="), text.index("ids="))
        self.assertLess(text.index("ids="), text.index("#1 n0"))


class TestReplayRefs(_MsgCase):
    """I：replay 补交注入长消息同款引用优先；补拉原文不变。"""

    def _root_msg(self, body):
        return self.store.write_message(
            {"ts": 1.0, "task": "t1", "kind": "result", "from": "w1",
             "to": "root"}, body)

    def test_I_replay_long_message_refs_first(self):  # Req: F125 I
        self._root_msg(LONG)
        n = self.r.replay_root_backlog("t1", "d1")
        self.assertEqual(n, 1)
        text = self.oc.calls[0][1]
        header = "[agora] task t1 result from w1\n"
        self.assertTrue(text.startswith(header))
        for key in ("home=", "msg=", "page="):
            self.assertLess(len(header) - 1, text.index(key))
        self.assertLess(text.index("page="), text.index(LONG[:16]))
        (m,) = [m for m in self.store.read_messages() if m["to"] == "d1"]
        self.assertEqual(m["body"], LONG)  # 副本正文原样

    def test_I_replay_pull_returns_raw(self):  # Req: F125 A3/I
        self.oc.online = False
        self._root_msg(LONG)
        self.r.replay_root_backlog("t1", "d1")
        (got,) = self.r.check_mail("d1")
        self.assertEqual(got["body"], LONG)


class TestGateFlowRefs(TestCoreGate):
    """B：长输出 verify 失败注入文本前 240 字符内 msg/artifact 可达。"""

    def setUp(self):
        super().setUp()
        self.artifacts = ArtifactStore(self.tmp / "gate-artifacts")
        self.mk_gate(artifact_store=self.artifacts)

    def test_B_verify_fail_refs_within_240(self):  # Req: F125 B
        self.runner = FakeRunner((1, "E" * 300))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed()
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        self.assertEqual(self.state.get("t1")["state"], "verify_failed")
        texts = [t for _, t in self.oc.injected if "输出尾部" in t]
        self.assertTrue(texts)
        head = texts[0][:240]
        self.assertIn("msg=state/messages/", head)
        self.assertIn("artifact=", head)

    def test_B_infra_dispatcher_refs_within_240(self):  # Req: F125 §3
        self.runner = FakeRunner((137, "Killed"))
        self.mk_gate(artifact_store=self.artifacts)
        self.seed()
        self.gate.accept_done("t1", "s-w1", SHA)
        self.gate.run("t1")
        texts = [t for sid, t in self.oc.injected
                 if sid == "disp1" and "机械失败" in t]
        self.assertTrue(texts)
        head = texts[0][:240]
        self.assertIn("msg=state/messages/", head)
        self.assertIn("artifact=", head)


if __name__ == "__main__":
    unittest.main()
