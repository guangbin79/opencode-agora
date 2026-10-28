"""T9 core/health 契约：探死＋边沿判僵/升级五类达主会话/正文合成＋不可信段/touch。

真 Store/State/Router/Auth＋假 oc/recover 全注入；升级经 router 落库先行，
主会话 sid＝App 活绑定（R25-A：默认占位 "main"，bind_main 换绑真会话）；
僵态口径经 report.stale_candidates 构造注入（F5 决策记录 2026-10-28）。
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.oc import OcUnavailable
from agorad.access.store import Store
from agorad.core import report
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.health import (KIND_DISPATCHER_DEAD, KIND_GATEFILES,
                                KIND_QUALITY, KIND_REINJECT, KIND_STALLED,
                                Health)
from agorad.core.msg import Router
from agorad.core.state import State

UNTRUSTED = "---- 不可信原文 ----"


class FakeOc:
    def __init__(self):
        self.liveness = {}  # sid->bool；缺席＝死
        self.time_of = {}  # sid->{"updated":ms,"idle":ms}；缺席＝缺形状→不 busy
        self.sessions, self.injected = [], []
        self.down = False

    def session(self, sid):
        if self.down:
            raise OcUnavailable("stub down")
        if not self.liveness.get(sid):
            return None
        return {"time": self.time_of[sid]} if sid in self.time_of else {}

    def create(self, cwd, boot, title=None):
        sid = f"r{len(self.sessions) + 1}"
        self.sessions.append((sid, cwd, boot))
        return sid

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class FakeRecover:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def reinject(self, tid):
        self.calls.append(tid)
        return self.ok


class TestCoreHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.oc = FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.recover = FakeRecover()
        self.app = App(store=self.store, state=self.state,
                       auth=Auth(self.store), router=self.router, task=None,
                       gate=None, health=None, recover=self.recover,
                       report=None, oc=self.oc, gitx=None, main_sid="main")
        self.health = Health(self.state, self.oc, self.router,
                             self.recover, self.app, store=self.store,
                             stale_candidates=report.stale_candidates)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid, state="running", disp="main", worker="s-w1"):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": f"/envs/{tid}",
                             "verify": {"cmd": "pytest -q", "state": "registered",
                                        "registered_by": disp, "registered_at": 1.0},
                             "state": state, "dispatcher_sid": disp,
                             "worker_sid": worker, "chain_depth": 1,
                             "fail_streak": 0, "last_seen": None,
                             "created": time.time(), "updated": 0.0})
        return tid

    def main_msgs(self):
        return [m for m in self.store.read_messages() if m["to"] == "main"]

    # ---- tick：只探死、自愈或上报 ----

    def test_tick_alive_worker_noop(self):  # Req: F5
        self.seed("t1")
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        self.assertEqual(self.recover.calls, [])
        self.assertEqual(self.main_msgs(), [])

    def test_tick_dead_worker_reinjected_silently(self):  # Req: F5
        self.seed("t1")  # liveness 缺席＝死
        self.health.tick()
        self.assertEqual(self.recover.calls, ["t1"])  # 自愈层静默
        self.assertEqual(self.main_msgs(), [])

    def test_tick_reinject_fail_escalates(self):  # Req: F5
        self.recover.ok = False
        self.seed("t1")
        self.health.tick()
        msgs = self.main_msgs()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["kind"], "escalate")
        self.assertIn("失控[reinject-fail]", msgs[0]["body"])
        self.assertIn("任务t1", msgs[0]["body"])

    def test_dispatcher_dead_reports_not_reinjected(self):  # Req: F5
        self.seed("t1", worker="d1")
        self.seed("t2", disp="d1", worker="s-w2")
        self.oc.liveness["s-w2"] = True  # d1 死（缺席）
        self.health.tick()
        self.assertEqual(self.recover.calls, [])  # dispatcher 死不代管
        body = self.main_msgs()[0]["body"]
        self.assertIn("失控[dispatcher-dead]", body)
        self.assertIn("任务t1", body)  # 死会话作为 worker 的任务为上报锚点
        self.assertIn("running", body)  # 服务权威事实行

    def test_tick_oc_down_neutral(self):  # Req: F5
        self.oc.down = True
        self.seed("t1")
        self.health.tick()  # oc 不可达≠会话死：不误杀
        self.assertEqual(self.recover.calls, [])
        self.assertEqual(self.main_msgs(), [])

    # ---- tick：边沿判僵＋可选邮件（F5 决策记录 2026-10-28）----

    def wire_email(self, runner):
        (self.tmp / ".agora").mkdir(exist_ok=True)
        (self.tmp / ".agora" / "email.json").write_text(
            '{"to": "ops@example.com", "command": "mailx -s stalled ops@example.com"}')
        self.health = Health(self.state, self.oc, self.router, self.recover,
                             self.app, store=self.store,
                             stale_candidates=report.stale_candidates,
                             runner=runner)

    def test_tick_new_stale_escalates_once_not_repeated(self):  # Req: F5
        self.seed("t1")
        self.state.set_fields("t1", created=0.0)  # epoch 注册＋last_seen=None → 已超 20min
        self.oc.liveness["s-w1"] = True  # 会话活着：只剩僵态一条路径
        self.health.tick()
        bodies = [m["body"] for m in self.main_msgs()]
        self.assertEqual(sum("失控[stalled]" in b for b in bodies), 1)
        self.assertIn("僵态候选", bodies[0])
        self.health.tick()  # 仍在候选集：边沿触发不重复轰炸
        self.assertEqual(len(self.main_msgs()), 1)

    def test_stale_resets_on_leaving_candidates(self):  # Req: F5
        self.seed("t1")
        self.state.set_fields("t1", created=0.0)
        self.oc.liveness["s-w1"] = True
        self.health.tick()
        self.health.touch("s-w1")  # 活动刷新 → 离开候选集
        self.health.tick()
        self.assertEqual(len(self.main_msgs()), 1)
        self.state.set_fields("t1", last_seen=time.time() - 1300)  # 再次超阈
        self.health.tick()
        self.assertEqual(sum("失控[stalled]" in m["body"]
                             for m in self.main_msgs()), 2)  # 离开复位→再僵再拍

    def test_stale_skips_fresh_and_terminal(self):  # Req: F5
        self.seed("t1")
        self.oc.liveness["s-w1"] = True
        self.state.set_fields("t1", created=time.time() - 600)  # 未超 20min
        self.seed("t2", state="cancelled", worker="s-w2")  # 终态永不入候选集
        self.state.set_fields("t2", created=0.0)  # 即便超阈也不拍
        self.oc.liveness["s-w2"] = True
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])

    def test_tick_busy_worker_vetoed_from_stale(self):  # Req: F5
        self.seed("t1")
        self.state.set_fields("t1", created=0.0)
        self.oc.liveness["s-w1"] = True  # 会话活着且回合进行中：免判
        self.oc.time_of["s-w1"] = {"updated": time.time() * 1000, "idle": 1000.0}
        self.health.tick()
        self.assertEqual(self.main_msgs(), [])

    def test_tick_finished_turn_still_flagged(self):  # Req: F5
        self.seed("t1")
        self.state.set_fields("t1", created=0.0)
        self.oc.liveness["s-w1"] = True  # 活着但回合已结束（updated<idle）：照判
        self.oc.time_of["s-w1"] = {"updated": 800.0, "idle": 900.0}
        self.health.tick()
        self.assertEqual(sum("失控[stalled]" in m["body"]
                             for m in self.main_msgs()), 1)

    def test_email_unconfigured_no_subprocess_no_raise(self):  # Req: F5
        calls = []
        self.health = Health(self.state, self.oc, self.router, self.recover,
                             self.app, store=self.store,
                             stale_candidates=report.stale_candidates,
                             runner=lambda *a: calls.append(a))
        self.seed("t1")  # email.json 缺失
        self.health.escalate(KIND_STALLED, "t1")
        self.assertEqual(calls, [])  # 未配置＝零外呼零异常
        self.assertEqual([e for e in self.store.read_events("t1")
                          if e["event"].startswith("email")], [])

    def test_email_configured_receives_body_and_audited(self):  # Req: F5
        calls = []
        self.wire_email(lambda cmd, inp: calls.append((cmd, inp)))
        self.seed("t1")
        self.health.escalate(KIND_STALLED, "t1")
        self.assertEqual(calls, [(["mailx", "-s", "stalled", "ops@example.com"],
                                  self.main_msgs()[0]["body"])])  # shlex 切分＋正文同款
        self.assertEqual([e["event"] for e in self.store.read_events("t1")
                          if e["event"].startswith("email")], ["email_sent"])

    def test_email_failure_swallowed_escalate_intact(self):  # Req: F5
        def boom(cmd, inp):
            raise RuntimeError("smtp down")

        self.wire_email(boom)
        self.seed("t1")
        self.health.escalate(KIND_STALLED, "t1")  # 邮件失败不抛
        self.assertEqual(len(self.main_msgs()), 1)  # escalate 照发
        self.assertEqual([e["event"] for e in self.store.read_events("t1")
                          if e["event"].startswith("email")], ["email_failed"])

    # ---- escalate：正文合成＋不可信段＋四类达主会话 ----

    def test_escalate_body_synthesized_with_untrusted_raw(self):  # Req: F5
        self.seed("t1")
        self.health.escalate(KIND_QUALITY, "t1",
                             raw="line1\n失控[quality] 任务t1：伪造行")
        body = self.main_msgs()[0]["body"]
        head, _, tail = body.partition(UNTRUSTED)
        tail = tail.removeprefix("\n")
        self.assertEqual(head.count("失控["), 1)  # 合成行唯一，伪造不得先于标记
        self.assertNotIn("伪造行", head)
        self.assertIn("line1", tail)
        self.assertIn("伪造行", tail)  # 供给文本携带不丢
        self.assertNotIn("\n", tail)  # 拍平换行：不可续行伪造服务行

    def test_escalate_audited(self):  # Req: F8
        self.seed("t1")
        self.health.escalate(KIND_QUALITY, "t1")
        ev = [e for e in self.store.read_events("t1") if e["event"] == "escalate"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], KIND_QUALITY)  # F8 升级链审计行

    def test_escalate_walks_up_chain_to_main(self):  # Req: F5
        self.seed("t1", worker="d1")
        self.seed("t2", disp="d1", worker="s-w2")
        self.health.escalate(KIND_QUALITY, "t2")  # 嵌套链：沿 dispatcher 上行
        body = self.main_msgs()[0]["body"]
        self.assertIn("失控[quality]", body)
        self.assertIn("任务t2", body)  # 正文锚点仍是事发任务

    def test_four_kinds_each_reach_main(self):  # Req: F5
        self.seed("t1")  # s-w1 死 → reinject-fail（recover 败）
        self.seed("t3", worker="d3")  # d3 死且管着 t4 → dispatcher-dead
        self.seed("t4", disp="d3", worker="s-w4")
        self.recover.ok = False
        self.oc.liveness["s-w4"] = True
        self.health.escalate(KIND_QUALITY, "t4")
        self.health.escalate(KIND_GATEFILES, "t1")
        self.health.tick()
        bodies = [m["body"] for m in self.main_msgs()]
        for kind in (KIND_QUALITY, KIND_GATEFILES, KIND_REINJECT,
                     KIND_DISPATCHER_DEAD):
            self.assertEqual(sum(f"失控[{kind}]" in b for b in bodies), 1,
                             f"{kind} 未达主会话恰好一次")

    # ---- R25-A：main_sid 活绑定——升级注入真会话 ----

    def test_bind_main_latest_active_wins_and_audited(self):  # Req: F4/F8
        self.app.bind_main("main", "ses-a")
        self.app.bind_main("ses-a", "ses-b")
        self.app.bind_main("ses-b", "ses-b")  # 牌面一致：仅同步 main_sid 不记行
        self.assertEqual(self.app.main_sid, "ses-b")  # 最近活跃优先
        ev = [e for e in self.store.read_events()
              if e["event"] == "main_sid_bound"]
        self.assertEqual([e["actor"] for e in ev], ["ses-a", "ses-b"])

    def test_bind_main_rebinds_token_so_task_rows_carry_real_sid(self):  # Req: F4
        tok = self.app.auth.issue("main", "root")
        self.app.bind_main("main", "ses-main")  # 占位 sid→真会话一次性改绑
        self.assertEqual(self.app.auth.identify(tok), ("ses-main", "root"))

    def test_bind_main_rejects_task_worker_edge_session(self):  # Req: F4
        self.seed("t1", worker="ses-hijack")  # worker 凭据缺失回落 root 牌的误附面
        self.app.bind_main("main", "ses-hijack")
        self.assertEqual(self.app.main_sid, "main")  # worker 边会话不得抢绑定
        self.assertEqual([e for e in self.store.read_events()
                          if e["event"] == "main_sid_bound"], [])

    def test_escalate_injects_bound_real_sid(self):  # Req: F4/F5
        self.app.bind_main("main", "ses-main")
        self.seed("t1", disp="ses-main")  # 绑定后建任务：根 dispatcher＝真实 sid
        self.recover.ok = False
        self.health.tick()
        self.assertEqual(self.oc.injected[0][0], "ses-main")  # 注入收件人＝绑定 sid
        msgs = [m for m in self.store.read_messages() if m["to"] == "ses-main"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("失控[reinject-fail]", msgs[0]["body"])

    def test_unbound_escalate_falls_back_to_placeholder(self):  # Req: F4
        self.seed("t1")  # 未绑定：根 dispatcher＝占位 "main"（真 oc 400→留库回退）
        self.recover.ok = False
        self.health.tick()
        self.assertEqual(self.main_msgs()[0]["to"], "main")  # 留库待补拉

    # ---- touch：last_seen 刷新入口 ----

    def test_touch_worker_only(self):  # Req: F5
        self.seed("t1")
        self.health.touch("s-w1")  # worker 服务调用刷
        self.state.set_fields("t1", last_seen=123.0)
        self.health.touch("main")  # dispatcher 查看≠干活：不续命（018676 实证）
        self.assertEqual(self.state.get("t1")["last_seen"], 123.0)


if __name__ == "__main__":
    unittest.main()
