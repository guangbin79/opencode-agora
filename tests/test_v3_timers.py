"""Req: F145 — v3 timer 服务（opencode-agora v3 P5-T14，设计 §6）。

断言清单（任务页 ag-e75bc8，逐条机械判定）：
1 契约校验：after_seconds/due_at 互斥、30 天上限、body 1-2000 可打印、
  每任务 ≤16 pending、client_key 幂等（同键同载荷返原 timer；异载荷 409）。
2 权限：worker 限本任务建且仅取消自建；dispatcher 归属终审；root 放行；
  越权拒＋auth_denied 审计。
3 角色寻址：target 仅 worker|dispatcher；触发时解析当前 sid（换号跟随）；
  worker 目标仅 dispatched/running/verify_failed/reopened 触发，done/
  blocked/todo 保持 pending/overdue；任务终态自动取消＋审计（幂等）。
4 两级扫描：timer 30s（next due 提前醒，时钟注入验证）；每批 ≤32 按
  due/id 升序；overdue 启动即补发分批；时钟前跳只发一次、回拨不重发。
5 触发事务：pending→firing＋message（dedup=timer id 唯一）＋receipt＋event
  同事务→fired→Outbox 在线注入。三崩溃窗：firing 前回滚零残留；消息提交
  后按 timer_id 补记 fired 不产第二消息；在线未落账 at-least-once 重投。
6 取消竞态：pending 可取消；firing/fired 409 不伪造取消；终态自动取消幂等。
7 /v3 端点真接线（catalog 描述同步）；legacy 面零新增；响应含 timer
  id/due_ms/status。
8 观测：board(task)（/v3 面）pending/overdue/recent 投影；doctor overdue
  摘要（/v3 面）；timer 消息信封带 timer id。
9 缺 worker：保持 overdue 可见、扫描零数据库写入、不误 escalate。
10 索引验证：(status,due_ms) 命中无全表 SCAN；v6 迁移补列；v3-only 面
  不扰差分（equivalence 归全量验收）。
"""
import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request

from agorad import compose, contracts, serverv3
from agorad.domain import Task, Timer, TimerState, timer_transition
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from agorad.timersvc import TimerService
from tests import v3_harness as harness

DISPATCHER, WORKER, WORKER2, ROOT = ("ses-d1", "ses-w1", "ses-w2", "root")
FIRING_MSG_WINDOW = ("timer_firing", "after")  # 崩窗2：消息已提交、fired 未落


def mk_task(tid="ag-t01", **over):
    base = dict(id=tid, state="running", title="t", goal="g", root_sid=ROOT,
                dispatcher_sid=DISPATCHER, worker_sid=WORKER, created_ms=1,
                updated_ms=2)
    base.update(over)
    return Task(**base)


class OcStub:
    """OC 桩（test_v3_outbox 同款单测内联形态）。"""

    def __init__(self, mode=None):
        self.calls, self.mode = [], mode

    def _rec(self, channel, sid, text):
        self.calls.append((channel, sid, text))
        if self.mode == "raise":
            raise RuntimeError("oc down")
        return self.mode != "false"

    def inject(self, sid, text):
        return self._rec("inject", sid, text)

    def inject_silent(self, sid, text):
        return self._rec("silent", sid, text)

    def inject_notify(self, sid, text):
        return self._rec("notify", sid, text)


class FakeClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


class _SvcBase(unittest.TestCase):
    """服务级基座：真 Storage＋真 Messaging（OC 桩）＋注入钟＋failpoints。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = (harness.FaultInjector()
                         .register("uow").register("dispatch")
                         .register("timer_fire").register("timer_firing"))
        self.clock = FakeClock()
        self.oc = OcStub()
        self.storage = Storage(self.home.state / "agora.db",
                               injector=self.injector)
        self.addCleanup(self.storage.close)
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.svc = TimerService(self.storage, self.messaging,
                                clock=self.clock, injector=self.injector)

    # ---- 助手 ----

    def seed_task(self, tid="ag-t01", **over):
        with self.storage.uow():
            self.storage.upsert_task(mk_task(tid, **over))
        return tid

    def create(self, tid="ag-t01", target="worker", after=60, body="醒一醒",
               key=None, due_at=None, actor=DISPATCHER, role="dispatcher"):
        params = {"task": tid, "target": target}
        if after is not None:
            params["after_seconds"] = after
        if due_at is not None:
            params["due_at"] = due_at
        if body is not None:
            params["body"] = body
        if key is not None:
            params["client_key"] = key
        return self.svc.create(actor, role, params)

    def events(self):
        return [e.event for e in self.storage.events_all()]

    def messages(self):
        return self.storage.messages_all()


class TestContractValidation(_SvcBase):  # 断言1
    def setUp(self):
        super().setUp()
        self.seed_task()

    def _err(self, code, fn):
        with self.assertRaises(contracts.ContractError) as cm:
            fn()
        return cm.exception.code == code

    def test_after_and_due_are_mutually_exclusive(self):
        self.assertTrue(self._err(
            "invalid_argument", lambda: self.create(after=5, due_at=99)))
        self.assertTrue(self._err(
            "invalid_argument", lambda: self.create(after=None, due_at=None)))

    def test_30day_cap(self):
        cap = 30 * 86400
        self.assertTrue(self._err("invalid_argument",
                                  lambda: self.create(after=cap + 1)))
        self.assertTrue(self._err("invalid_argument",
                                  lambda: self.create(
                                      after=None,
                                      due_at=self.clock.ms
                                      + (cap + 60) * 1000)))
        r = self.create(after=cap)  # 恰上限放行
        self.assertEqual(r["status"], "pending")

    def test_after_must_be_positive(self):
        self.assertTrue(self._err("invalid_argument",
                                  lambda: self.create(after=0)))
        self.assertTrue(self._err("invalid_argument",
                                  lambda: self.create(after=-5)))

    def test_due_in_past_is_legal_immediately_overdue(self):
        r = self.create(after=None, due_at=self.clock.ms - 1000)
        self.assertLessEqual(r["due_ms"], self.clock.ms)

    def test_body_bounds(self):
        for bad in ("", "x" * 2001, "a\nb"):
            self.assertTrue(self._err(
                "invalid_argument",
                lambda bad=bad: self.create(body=bad)), bad)
        r = self.create(body="x" * 2000)  # 恰上限
        self.assertIn("timer", r)

    def test_max_16_pending_per_task(self):
        for i in range(16):
            self.create(after=60 + i)
        self.assertTrue(self._err("conflict", lambda: self.create(after=1)))
        self.svc.cancel(DISPATCHER, "dispatcher",
                        self.storage.timers_for_task("ag-t01")[0].id)
        self.create(after=1)  # 释放一格后可再建

    def test_client_key_idempotent_same_payload_returns_original(self):
        a = self.create(after=120, key="k1")
        self.clock.advance(5_000)
        b = self.create(after=120, key="k1")
        self.assertEqual(a["timer"], b["timer"])  # 同键同载荷返原 timer
        self.assertEqual(len(self.storage.timers_all()), 1)
        c = self.create(after=None, due_at=self.clock.ms + 120_000, key="k2")
        self.assertNotEqual(a["timer"], c["timer"])

    def test_client_key_conflict_on_different_payload(self):
        self.create(after=120, key="k1")
        for kw in ({"after": 121}, {"target": "dispatcher"},
                   {"body": "别的"}):
            self.assertTrue(self._err(
                "conflict", lambda kw=kw: self.create(key="k1", **kw)), kw)
        # due_at 载荷同键同值幂等；异值 409
        base = self.clock.ms + 300_000
        self.create(after=None, due_at=base, key="k3")
        self.create(after=None, due_at=base, key="k3")
        self.assertTrue(self._err("conflict", lambda: self.create(
            after=None, due_at=base + 1, key="k3")))

    def test_unknown_target_rejected_by_contract(self):
        from agorad.contracts import validate_params
        with self.assertRaises(contracts.ContractError):
            validate_params("timer_create", {
                "task": "ag-t01", "target": "root", "after_seconds": 5})

    def test_task_must_exist(self):
        self.assertTrue(self._err("not_found",
                                  lambda: self.create(tid="ag-nope")))


class TestPermissions(_SvcBase):  # 断言2
    def setUp(self):
        super().setUp()
        self.seed_task()
        self.seed_task("ag-t02", worker_sid=WORKER2,
                       dispatcher_sid="ses-d2")

    def _denied(self, fn):
        with self.assertRaises(contracts.ContractError) as cm:
            fn()
        return cm.exception.code == "forbidden"

    def test_create_matrix(self):
        self.assertIn("timer", self.create(actor=ROOT, role="root"))
        self.assertIn("timer", self.create(actor=DISPATCHER,
                                           role="dispatcher"))
        self.assertIn("timer", self.create(actor=WORKER, role="worker"))
        self.assertTrue(self._denied(lambda: self.create(
            tid="ag-t02", actor=WORKER, role="worker")))  # 他人任务拒
        self.assertTrue(self._denied(lambda: self.create(
            tid="ag-t02", actor=DISPATCHER, role="dispatcher")))

    def test_denial_audited(self):
        self._denied(lambda: self.create(tid="ag-t02", actor=WORKER,
                                         role="worker"))
        evs = [e for e in self.storage.events_all()
               if e.event == "auth_denied"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].actor, WORKER)
        self.assertEqual(evs[0].detail["tool"], "timer_create")

    def test_cancel_matrix(self):
        tid_by_worker = self.create(actor=WORKER, role="worker")["timer"]
        tid_by_root = self.create(actor=ROOT, role="root",
                                  after=90)["timer"]
        r = self.svc.cancel(WORKER, "worker", tid_by_worker)  # 自建可取消
        self.assertEqual(r["status"], "cancelled")
        self.assertTrue(self._denied(lambda: self.svc.cancel(  # 非自建拒
            WORKER, "worker", tid_by_root)))
        self.assertIn("cancelled", self.svc.cancel(  # dispatcher 归属终审
            DISPATCHER, "dispatcher", tid_by_root)["status"])
        other = self.create(tid="ag-t02", actor="ses-d2",
                            role="dispatcher")["timer"]
        self.assertTrue(self._denied(lambda: self.svc.cancel(
            DISPATCHER, "dispatcher", other)))  # 非本任务派发方拒


class TestTargetAddressing(_SvcBase):  # 断言3
    def setUp(self):
        super().setUp()
        self.seed_task()

    def test_target_enum_only_worker_dispatcher(self):
        self.assertIn("timer", self.create(target="dispatcher"))

    def test_fire_resolves_current_worker_sid_rebind_follows(self):
        self.create(after=10)
        with self.storage.uow():  # 换号：worker_sid 现值即目标（设计 §6）
            self.storage.upsert_task(mk_task("ag-t01", worker_sid=WORKER2))
        self.clock.advance(20_000)
        self.assertEqual(self.svc.scan(), 1)
        self.assertEqual(self.messages()[0].to_sid, WORKER2)

    def test_worker_target_state_gate(self):
        for state, fires in (("dispatched", True), ("running", True),
                             ("verify_failed", True), ("reopened", True),
                             ("done", False), ("blocked", False),
                             ("todo", False)):
            home = harness.TempAgoraHome()
            self.addCleanup(home.cleanup)
            storage = Storage(home.state / "agora.db")
            self.addCleanup(storage.close)
            clock = FakeClock()
            msg = MessagingService(storage, OcStub(), clock=clock)
            svc = TimerService(storage, msg, clock=clock)
            with storage.uow():
                storage.upsert_task(mk_task(
                    state=state, worker_sid=(WORKER if state != "todo"
                                             else None)))
            svc.create(DISPATCHER, "dispatcher",
                       {"task": "ag-t01", "target": "worker",
                        "after_seconds": 1})
            clock.advance(2_000)
            self.assertEqual(svc.scan(), 1 if fires else 0, state)
            if not fires:
                self.assertEqual(  # 保持 pending/overdue、零消息
                    storage.timers_all()[0].state, TimerState.PENDING)
                self.assertEqual(storage.messages_all(), [])

    def test_dispatcher_target_fires_to_dispatcher(self):
        self.create(target="dispatcher", after=10)
        self.clock.advance(20_000)
        self.assertEqual(self.svc.scan(), 1)
        self.assertEqual(self.messages()[0].to_sid, DISPATCHER)

    def test_task_terminal_autocancels_with_audit(self):
        tasksvc = TaskService(self.storage, None, None,
                              messaging=self.messaging, home=str(self.home.path),
                              clock=self.clock, timers=self.svc)
        for to in ("merged", "closed"):  # 终态两条链（closed＝merged 后续跳）
            self.create(after=10 ** 6)
            self.create(after=10 ** 6, target="dispatcher")
            with self.storage.uow():
                self.storage.upsert_task(mk_task(state="done"))
            tasksvc.transition("ag-t01", "merged", actor="gate")
            if to == "closed":
                tasksvc.transition("ag-t01", "closed", actor="gate")
            rows = self.storage.timers_for_task("ag-t01")
            self.assertTrue(rows)
            self.assertTrue(all(x.state is TimerState.CANCELLED
                                for x in rows), to)
        self.assertEqual(self.events().count("timer_autocancelled"), 2)

    def test_explicit_cancel_path_autocancels_too(self):
        tasksvc = TaskService(self.storage, None, None,
                              messaging=self.messaging, home=str(self.home.path),
                              clock=self.clock, timers=self.svc)
        self.create(after=10 ** 6)
        tasksvc.cancel("ag-t01", DISPATCHER, "dispatcher", "不需要了")
        self.assertEqual(self.storage.timers_all()[0].state,
                         TimerState.CANCELLED)
        self.assertIn("timer_autocancelled", self.events())


class TestScan(_SvcBase):  # 断言4
    def setUp(self):
        super().setUp()
        self.seed_task()

    def test_batch_32_ordered_by_due_then_id(self):
        for t, tid in enumerate(("ag-t01", "ag-t02", "ag-t03")):
            self.seed_task(tid)
            for i in range(16):  # 每任务 ≤16 pending——批 32 跨任务聚合
                self.create(tid=tid, after=10 + t * 16 + i)
        self.clock.advance(60_000)
        self.assertEqual(self.svc.scan(), 32)  # 每批 ≤32
        self.assertEqual(len(self.messages()), 32)
        dues = [m.ts_ms for m in self.messages()]
        fired = sorted((x.due_ms, x.id) for x in self.storage.timers_all()
                       if x.state is TimerState.FIRED)
        self.assertEqual([d for d, _ in fired],
                         sorted(d for d, _ in fired))  # due 升序
        self.assertEqual(self.svc.scan(), 16)  # 尾批收完
        self.assertEqual(len(self.messages()), 48)

    def test_overdue_startup_catchup_in_batches(self):
        for t in range(5):
            tid = f"ag-t{t}"
            self.seed_task(tid)
            for i in range(14):
                self.create(tid=tid, after=t + i + 1)
        self.clock.advance(100_000)
        self.assertEqual(self.svc.catch_up(), 70)
        self.assertEqual(len(self.messages()), 70)

    def test_next_wait_seconds_early_wake(self):
        self.assertEqual(self.svc.next_wait_seconds(), 30.0)  # 无 pending
        self.create(after=5)
        self.assertAlmostEqual(self.svc.next_wait_seconds(), 5.0)
        self.clock.advance(6_000)
        self.assertEqual(self.svc.next_wait_seconds(), 0.0)  # 已 overdue

    def test_forward_jump_fires_once_rollback_no_refire(self):
        self.create(after=10)
        self.clock.advance(30_000)
        self.assertEqual(self.svc.scan(), 1)
        self.assertEqual(self.svc.scan(), 0)  # 前跳后扫描不再发
        self.clock.advance(-10 ** 6)  # 回拨
        self.assertEqual(self.svc.scan(), 0)  # 不重发
        self.assertEqual(len(self.messages()), 1)


class TestFireTransaction(_SvcBase):  # 断言5
    def setUp(self):
        super().setUp()
        self.seed_task()

    def _timer(self):
        return self.storage.timers_all()[0]

    def test_composite_commit_before_oc(self):
        self.create(after=10)
        self.clock.advance(20_000)
        self.oc.calls.clear()
        self.svc.scan()
        # 先提交后注入：OC 调用时点 timer/firing/message 已可读
        self.assertTrue(self.oc.calls)
        self.assertEqual(self._timer().state, TimerState.FIRED)

    def test_crash_before_firing_zero_residue(self):
        self.create(after=10)
        self.clock.advance(20_000)
        self.injector.arm("timer_fire", "before")
        with self.assertRaises(harness.FaultInjected):
            self.svc.scan()
        self.assertEqual(self._timer().state, TimerState.PENDING)
        self.assertEqual(self.messages(), [])
        self.assertNotIn("timer_fired", self.events())

    def test_crash_after_message_commit_reconciles_fired_no_second(self):
        self.create(after=10)
        self.clock.advance(20_000)
        self.injector.arm(*FIRING_MSG_WINDOW)
        with self.assertRaises(harness.FaultInjected):
            self.svc.scan()
        self.assertEqual(self._timer().state, TimerState.FIRING)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.svc.scan(), 0)  # 对账轮无新触发
        self.assertEqual(self._timer().state, TimerState.FIRED)  # 补记 fired
        self.assertEqual(len(self.messages()), 1)  # 不产第二消息
        self.assertIn("timer_reconciled", self.events())

    def test_online_result_unsettled_at_least_once(self):
        self.create(after=10)
        self.clock.advance(20_000)
        self.injector.arm("dispatch", "after")  # OC 已回、receipt 未落账
        with self.assertRaises(harness.FaultInjected):
            self.svc.scan()
        msg = self.messages()[0]
        receipt = self.storage.get_receipt(msg.id)
        self.assertEqual(receipt.status, "inflight")  # 未落账
        self.svc.reconcile_firing()  # 对账：fired 与投递解耦
        self.assertEqual(self._timer().state, TimerState.FIRED)
        self.clock.advance(31_000)  # lease 过期
        pulled = self.messaging.pull(WORKER)  # at-least-once：补拉送达
        self.assertEqual([m.id for m in pulled], [msg.id])
        self.assertEqual(len(self.messages()), 1)

    def test_dedup_key_is_timer_id_unique(self):
        self.create(after=10)
        self.clock.advance(20_000)
        self.svc.scan()
        msg = self.messages()[0]
        self.assertEqual(msg.dedup_key, self._timer().id)
        self.assertEqual(self.storage.message_for_timer(
            self._timer().id).id, msg.id)


class TestCancelRaces(_SvcBase):  # 断言6
    def setUp(self):
        super().setUp()
        self.seed_task()

    def test_pending_cancellable(self):
        r = self.create(after=60)
        out = self.svc.cancel(DISPATCHER, "dispatcher", r["timer"])
        self.assertEqual(out, {"timer": r["timer"], "due_ms": r["due_ms"],
                               "status": "cancelled"})
        self.assertEqual(self.storage.timers_all()[0].state,
                         TimerState.CANCELLED)

    def test_firing_and_fired_rejected_not_faked(self):
        r = self.create(after=60)
        with self.storage.uow():  # 直推 firing（事务内态）
            self.storage.upsert_timer(timer_transition(
                self.storage.get_timer(r["timer"]), TimerState.FIRING))
        with self.assertRaises(contracts.ContractError) as cm:
            self.svc.cancel(DISPATCHER, "dispatcher", r["timer"])
        self.assertEqual(cm.exception.code, "conflict")
        with self.storage.uow():  # 直推 fired
            self.storage.upsert_timer(timer_transition(
                self.storage.get_timer(r["timer"]), TimerState.FIRED,
                now_ms=self.clock()))
        with self.assertRaises(contracts.ContractError) as cm:
            self.svc.cancel(DISPATCHER, "dispatcher", r["timer"])
        self.assertEqual(cm.exception.code, "conflict")

    def test_cancelled_idempotent_ok(self):
        r = self.create(after=60)
        self.svc.cancel(DISPATCHER, "dispatcher", r["timer"])
        out = self.svc.cancel(DISPATCHER, "dispatcher", r["timer"])
        self.assertEqual(out["status"], "cancelled")

    def test_autocancel_idempotent_zero_writes_second_time(self):
        self.create(after=60)
        with self.storage.uow():
            n1 = self.svc.cancel_task_timers_uow(
                "ag-t01", self.clock(), "task_merged")
            n2 = self.svc.cancel_task_timers_uow(  # 幂等：无 pending 零写
                "ag-t01", self.clock(), "task_merged")
        self.assertEqual((n1, n2), (1, 0))
        self.assertEqual(self.events().count("timer_autocancelled"), 1)

    def test_missing_timer_not_found(self):
        with self.assertRaises(contracts.ContractError) as cm:
            self.svc.cancel(DISPATCHER, "dispatcher", "tm-nope")
        self.assertEqual(cm.exception.code, "not_found")


class _HttpBase(unittest.TestCase):
    """HTTP 级基座（test_v3_server 同款装配，压缩版）。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.clock = FakeClock()
        self.oc = _ServerOc()
        self.stack = compose.V3Stack(
            self.home.path, oc=self.oc, gitx=_GitxNothing(), runner=_run_noop,
            clock=self.clock)
        self.stack.start()
        self.addCleanup(self.stack.close)
        self.server = serverv3.make_server_v3(self.stack)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)
        self.root = self.stack.root_token

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None, token=None, method="POST"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Agora-Token"] = token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/{path}",
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def mk(self, goal="g"):
        s, b = self.call("task_create", {"goal": goal,
                                         "project_dir": "/repo",
                                         "verify_cmd": "true"}, self.root)
        self.assertEqual(s, 200, b)
        return b["task"]


class _ServerOc:
    def create(self, env, boot, title=None, model=None):
        return WORKER

    def inject(self, sid, text):
        return True

    def inject_silent(self, sid, text):
        return True

    def inject_notify(self, sid, text):
        return True

    def session(self, sid):
        return {"model": None}

    def alive(self, sid):
        return True

    def probe(self):
        return {}

    def models(self):
        return []


def _run_noop(env_dir, cmd, *, timeout=None, ceiling=None, on_soft=None):
    return 0, ""


class _GitxNothing:
    def symref(self, repo_dir):
        return "main"

    def env_on_disk(self, env_dir):
        return True


class TestV3Endpoints(_HttpBase):  # 断言7
    def test_timer_create_cancel_wired_with_id_due_status(self):
        tid = self.mk()
        s, b = self.call("v3/timer_create",
                         {"task": tid, "target": "dispatcher",
                          "after_seconds": 30, "body": "到点提醒"}, self.root)
        self.assertEqual(s, 200, b)
        self.assertEqual(sorted(b), ["due_ms", "status", "timer"])
        self.assertEqual(b["status"], "pending")
        self.assertEqual(b["due_ms"], self.clock.ms + 30_000)
        s, b2 = self.call("v3/timer_cancel", {"timer_id": b["timer"]},
                          self.root)
        self.assertEqual((s, b2["status"]), (200, "cancelled"))

    def test_approval_placeholder_still_501(self):
        s, b = self.call("v3/approval_request",
                         {"business_action": "x"}, self.root)
        self.assertEqual((s, b["error"]["code"]),
                         (501, "not_implemented"))

    def test_catalog_description_synced(self):
        s, b = self.call("v3/tools", method="GET")
        tools = {t["name"]: t for t in b["tools"]}
        for name in ("timer_create", "timer_cancel"):
            self.assertIn(name, tools)
            self.assertIn("定时器", tools[name]["description"])
            self.assertTrue(tools[name]["inputSchema"]["required"])

    def test_legacy_face_has_no_timer_routes(self):
        tid = self.mk()
        for name in ("timer_create", "timer_cancel"):
            s, b = self.call(name, {"task": tid, "target": "worker",
                                    "after_seconds": 5}
                             if name == "timer_create" else {"timer_id": "x"},
                             self.root)
            self.assertEqual((s, b), (404, {"error": "not found"}), name)


class TestObservability(_HttpBase):  # 断言8
    def test_board_detail_timer_projection_on_v3_face_only(self):
        tid = self.mk()
        s, b = self.call("v3/board", {"task": tid}, self.root)
        self.assertEqual(s, 200, b)
        self.assertEqual(sorted(b["timers"]), ["overdue", "pending", "recent"])
        self.assertEqual(b["timers"]["pending"], [])
        s, r = self.call("v3/timer_create",
                         {"task": tid, "target": "dispatcher",
                          "after_seconds": 10}, self.root)
        self.assertEqual(s, 200, r)
        self.clock.advance(30_000)
        s, b = self.call("v3/board", {"task": tid}, self.root)
        self.assertEqual(len(b["timers"]["overdue"]), 1)
        self.assertEqual(b["timers"]["pending"], b["timers"]["overdue"])
        s, legacy = self.call("board", {"task": tid}, self.root)  # legacy 面
        self.assertEqual(s, 200, legacy)
        self.assertNotIn("timers", legacy)  # v2 兼容形状零扰动

    def test_recent_history_projection(self):
        tid = self.mk()
        s, r = self.call("v3/timer_create",
                         {"task": tid, "target": "dispatcher",
                          "after_seconds": 5}, self.root)
        self.assertEqual(s, 200, r)
        self.clock.advance(10_000)
        self.stack.timers.scan()
        s, b = self.call("v3/board", {"task": tid}, self.root)
        self.assertEqual(b["timers"]["pending"], [])
        self.assertEqual(b["timers"]["recent"][0]["id"], r["timer"])
        self.assertEqual(b["timers"]["recent"][0]["status"], "fired")
        self.assertIsNotNone(b["timers"]["recent"][0]["fired_ms"])

    def test_doctor_overdue_summary_on_v3_face(self):
        tid = self.mk()
        s, b = self.call("v3/doctor", {}, self.root)
        self.assertEqual(s, 200, b)
        self.assertEqual(b["timers_overdue"], [])
        s, r = self.call("v3/timer_create",
                         {"task": tid, "target": "dispatcher",
                          "after_seconds": 5}, self.root)
        self.clock.advance(205_000)
        s, b = self.call("v3/doctor", {}, self.root)
        self.assertEqual(b["timers_overdue"], [[r["timer"], 200]])
        s, legacy = self.call("doctor", {}, self.root)  # legacy 面零扰动
        self.assertEqual(s, 200, legacy)
        self.assertNotIn("timers_overdue", legacy)

    def test_message_envelope_carries_timer_id(self):
        tid = self.mk()
        s, r = self.call("v3/timer_create",
                         {"task": tid, "target": "dispatcher",
                          "after_seconds": 5, "body": "该收尾了"}, self.root)
        self.assertEqual(s, 200, r)
        self.clock.advance(10_000)
        self.assertEqual(self.stack.timers.scan(), 1)
        msg = [m for m in self.stack.storage.messages_all()
               if m.kind == "timer"][0]
        body = bytes(msg.body).decode("utf-8")
        self.assertIn(f"[agora] timer {r['timer']}", body)  # 信封带 timer id
        self.assertIn("该收尾了", body)


class TestMissingWorker(_SvcBase):  # 断言9
    def test_no_worker_sid_stays_overdue_zero_writes_no_escalate(self):
        self.seed_task(state="todo", worker_sid=None)  # 未派发无 worker
        self.create(after=1)
        self.clock.advance(10_000)
        before_events = self.events()
        before_messages = self.messages()
        timer = self.storage.timers_all()[0]
        self.assertEqual(self.svc.scan(), 0)  # 零触发
        after = self.storage.timers_all()[0]
        self.assertEqual((after.id, after.state, after.due_ms),
                         (timer.id, TimerState.PENDING, timer.due_ms))
        self.assertEqual(self.events(), before_events)  # 零数据库写入
        self.assertEqual(self.messages(), before_messages)
        self.assertNotIn("escalate", self.events())  # 不误 escalate
        self.assertEqual(self.svc.projection("ag-t01")["overdue"],
                         [{"id": timer.id, "due_ms": timer.due_ms}])
        self.assertEqual(self.svc.overdue_rows(),
                         [[timer.id, (self.clock.ms - timer.due_ms) // 1000]])
        with self.storage.uow():  # 派发后恢复触发（环境补齐即收敛）
            self.storage.upsert_task(mk_task(state="dispatched"))
        self.assertEqual(self.svc.scan(), 1)
        self.assertEqual(self.messages()[0].to_sid, WORKER)


class TestIndexesAndSchema(_SvcBase):  # 断言10
    def test_due_scan_hits_index_no_table_scan(self):
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        detail = " | ".join(row[3] for row in reader.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM timers "
            "WHERE status='pending' AND due_ms<=? "
            "ORDER BY due_ms, id LIMIT 32", (self.clock.ms,)))
        self.assertNotIn("SCAN", detail)
        self.assertIn("INDEX", detail)

    def test_v6_columns_present_and_roundtrip(self):
        reader = self.storage.open_reader()
        self.addCleanup(reader.close)
        cols = {r[1] for r in reader.execute("PRAGMA table_info(timers)")}
        self.assertTrue({"body", "created_by", "client_key"} <= cols)
        self.assertEqual(
            reader.execute("PRAGMA user_version").fetchone()[0], 6)
        self.seed_task()
        self.create(after=5, body="正文", key="k")
        tm = self.storage.timers_all()[0]
        self.assertEqual((tm.body, tm.created_by, tm.client_key),
                         ("正文", DISPATCHER, "k"))

    def test_v5_upgrade_adds_columns(self):
        db = self.home.state / "agora.db"
        self.storage.close()  # 让位迁移者
        conn = sqlite3.connect(db)
        conn.executescript(
            "DROP TABLE timers;"  # 换装 v5 旧形状（无 F145 三列）
            "PRAGMA user_version=5;"
            "CREATE TABLE timers(id TEXT PRIMARY KEY,"
            " task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,"
            " target_role TEXT NOT NULL DEFAULT 'worker',"
            " due_ms INTEGER NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " created_ms INTEGER, fired_ms INTEGER);")
        conn.close()
        upgraded = Storage(db)
        self.addCleanup(upgraded.close)
        reader = upgraded.open_reader()
        self.addCleanup(reader.close)
        cols = {r[1] for r in reader.execute("PRAGMA table_info(timers)")}
        self.assertTrue({"body", "created_by", "client_key"} <= cols)


if __name__ == "__main__":
    unittest.main()
