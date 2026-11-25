"""ag-d03e0e done 拒收可见化：锚校验同步报错＋拒因落消息面（v2 live 栈）。

事故（HXNativeApp 任务 ag-d03e0e）：worker 三次 post(done) 被 F111 锚门拒收
（双件锚缺 .review-base），但 API 返回 200 成功形状——worker 零感知、自信
「已发送」；拒因只进 fail_streak 与审计，任务消息面零痕迹，派发方翻
tasks.jsonl 破案约 1 小时。本文件钉住修复后的两条硬性质：
①受理段拒收＝同步 MCP 报错（HTTP 400，拒因原文含 .review-base——与
「done 仅任务 worker」身份拒同档，MCP 调用失败＋拒因清晰）；
②拒因原文持久化为任务消息（from=agorad/gate、kind=note、to=worker），
消息库/check_mail 可查——fail_streak 不再是唯一痕迹。
两栈各钉：v2（httpd live 栈）同步报错＋落面；v3（serverv3 栈）落面。
"""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from agorad.main import assemble
from agorad.transport.httpd import make_server
from tests.stub_opencode import StubOpencode
from tests.test_transport_httpd import SHA, FakeGitx, call, ok_runner
from tests.test_v3_server import _ChainBase


def _text(m):
    return (m.body.decode("utf-8", "replace")
            if isinstance(m.body, (bytes, bytearray)) else str(m.body))


class DoneRejectVisibilityTest(unittest.TestCase):
    """真 httpd＋StubOpencode＋假 gitx；read_anchor 恒 None＝F111 双件锚缺
    （.review-base 缺席形态）——accept_done 锚三检必拒。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = StubOpencode()
        self.stub.start()
        self.gitx = FakeGitx(self.tmp)
        self.app = assemble(self.tmp, oc_url=self.stub.base_url,
                            gitx=self.gitx,
                            read_anchor=lambda env: None,
                            runner=ok_runner)
        self.root = self.app.auth.issue("main", "root")
        self.httpd = make_server(self.app)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.tid = self.create_task()
        self.wtok = self.dispatch_task(self.tid)
        self.wsid = self.app.state.get(self.tid)["worker_sid"]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def create_task(self):
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo",
                      "verify_base_exempt": True}, self.root)
        self.assertEqual(st, 200)
        return r["task"]

    def dispatch_task(self, tid):
        st, _ = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": "true"}, self.root)
        self.assertEqual(st, 200)
        st, _ = call(self.port, "POST", "/task_dispatch", {"task": tid},
                     self.root)
        self.assertEqual(st, 200)
        return self.app.auth.token_for(self.app.state.get(tid)["worker_sid"])

    def post_done(self):
        return call(self.port, "POST", "/post",
                    {"task": self.tid, "kind": "done", "body": SHA}, self.wtok)

    # ---- 断言面 ----

    def test_anchor_reject_is_synchronous_error(self):  # A2 同步拒因
        st, r = self.post_done()
        self.assertEqual(st, 400)  # 同步报错：MCP 调用失败（非 200 result）
        self.assertIn("rejected:anchor", r["error"])
        self.assertIn(".review-base", r["error"])  # 拒因原文点名缺失件
        self.assertEqual(self.app.state.get(self.tid)["state"], "running")

    def test_anchor_reject_reason_persisted_as_message(self):  # A3 拒因落面
        st, _ = self.post_done()
        self.assertEqual(st, 400)
        hits = [m for m in self.app.store.read_messages()
                if m["kind"] == "note" and m["from"] == "agorad"
                and m["to"] == self.wsid
                and "rejected:anchor" in m["body"]
                and ".review-base" in m["body"]]
        self.assertTrue(hits, "拒因原文未落任务消息面")
        # 连败照计（锚族拒收既有语义不变）——但不再是唯一痕迹（上一断言）
        self.assertEqual(self.app.state.get(self.tid)["fail_streak"], 1)


class V3DoneRejectPersistTest(_ChainBase):
    """A3 的 v3 面钉子（A 册 W1）：_deny 六拒统一出口的拒因落库行为——
    v3 回归（_msg 未接线/异常被吞）时此处必须红，不以 v2 绿为代偿。"""

    def test_done_reject_reason_persisted_as_message(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        self.call("post", {"task": tid, "kind": "question", "body": "开工"},
                  wtok)  # worker 调用即开工→running
        s, b = self.call("post", {"task": tid, "kind": "done", "body": "zz"},
                         wtok)
        self.assertEqual(s, 400)  # 同步报错（非 200 result）
        self.assertIn("rejected:body-format", b["error"])
        hits = [m for m in self.stack.storage.messages_all()
                if m.task_id == tid and m.kind == "note"
                and m.from_sid == "gate" and m.to_sid == wsid
                and "rejected:body-format" in _text(m)]
        self.assertTrue(hits, "v3 拒因原文未落任务消息面")


if __name__ == "__main__":
    unittest.main()
