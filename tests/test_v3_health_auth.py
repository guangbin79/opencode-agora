"""Req: F138 — v3 health/auth 应用服务（opencode-agora v3 P3-T10）。

断言清单（任务页 ag-99f70c，逐条机械判定）：1 Auth 基面（签发＝secret 文件
先落盘 0600 后 DB 提交、崩溃窗只留孤儿文件＋启动清扫、identify hash 常时
比较、revoke/revoke_session/rotate_root、root 牌钉死 sid=root）；2 血统
（封顶 16 FIFO、same_lineage、换号旧 sid 入血统、嵌套 dispatcher_sid 覆写、
终态行冻结 TOCTOU）；3 bind_worker（已占 sid 拒绑、活动行复检、嵌套所有权
波及）；4 bind_dispatcher（root 牌前判愈合、oc alive 探测、daemon 行恒不
愈合、oc 不可达免判）；5 角色矩阵（19 工具×三角色全叉乘，Contracts 角色声
明为准）；6 判僵（20min 阈、busy/waiting_on/in_gate/gate_resource.waiting
免判、边沿复位再拍、×4 退避封顶 24h、env-gone 每丢失周期恰一次）；7
wait_scan（终态清 waiting_on＋审计＋双通知、CAS 并发换登记不抹）；8
escalate（六类 quiet 走 notify＋邮件缝、正文权威合成、供给文本拍平不可
伪造、沿 dispatcher_sid 上行至根）；9 nudge（自检注入恰一次、失败不重试
不阻断）；10 delivery_watch（awaiting_external 超龄 external-stale、merged
未出版 unpublished（gitx Protocol 投影）、退避同口径）；11 last_seen 批量
窗（节流合并写、崩溃最多丢一窗——时钟注入）；12 recover 协作钩（reinject
决策可桩化、oc 不可达轮整体免判不误杀）。FaultInjector/临时 home 复用
v3_harness；消息面接真 MessagingService；touch 面接真 TaskService。
"""
import dataclasses
import json
import os
import stat
import unittest

from agorad import domain
from agorad.authsvc import TASK_DOMAIN, AuthService, _TASK_ROW_OPTIONAL
from agorad.contracts import CATALOG, ContractError
from agorad.domain import Credential, DeliveryState, GateState, VerifySpec
from agorad.gitstore import GitStore
from agorad.healthsvc import (CAP_MS, GAP0_MS, KIND_DISPATCHER_DEAD,
                              KIND_ENV_GONE, KIND_EXT_STALE, KIND_REINJECT,
                              KIND_STALLED, KIND_UNPUBLISHED,
                              QUIET_ESCALATES, HealthService)
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from tests import v3_harness as harness

DISPATCHER, ROOT, WORKER = "ses-d1", "ses-root", "ses-w1"
MIN = 60_000


class OcStub:
    """oc 桩：session→dict|None（None＝死）；inject 可预置失败；alive 探测。"""

    def __init__(self):
        self.sessions = {}      # sid -> dict | None(死)
        self.injects = []       # (sid, text)
        self.inject_fails = False
        self.alive_raises = False

    def session(self, sid):
        return self.sessions.get(sid, {"time": {"updated": 0, "idle": 1}})

    def inject(self, sid, text):
        self.injects.append((sid, text))
        return not self.inject_fails

    def inject_silent(self, sid, text):
        return True

    def inject_notify(self, sid, text):
        self.injects.append(("notify", sid, text))
        return True

    def alive(self, sid):
        if self.alive_raises:
            raise RuntimeError("oc down")
        s = self.sessions.get(sid)
        return s is not None


class PubGitxStub:
    """gitx 出版对账桩：publication/published_on_origin/patch 等价/内容
    落定（blob 落定·行包含性）可预置。"""

    def __init__(self):
        self.pub = {"origin_ref": "refs/heads/v3-runtime", "branch":
                    "v3-runtime", "ahead": 2}
        self.published = False
        self.patch_eq = False
        self.blob_settled = False
        self.lines_settled = False
        self.raises = False

    def publication(self, repo_dir, branch=None):
        if self.raises:
            raise RuntimeError("refs sick")
        return self.pub

    def published_on_origin(self, repo_dir, commit, branch):
        return self.published

    def patch_equivalent_on_origin(self, repo_dir, commit, branch):
        return self.patch_eq

    def blob_settled_on_origin(self, repo_dir, commit, branch):
        return self.blob_settled

    def lines_settled_on_origin(self, repo_dir, commit, branch):
        return self.lines_settled


class FakeClock:
    def __init__(self, ms=1_000_000_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


class RecoverStub:
    """reinject 决策桩：记录调用；result 可预置 True/None/False。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def reinject(self, tid, expect_wsid=None):
        self.calls.append((tid, expect_wsid))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = (harness.FaultInjector()
                         .register("uow").register("dispatch"))
        self.storage = Storage(self.home.state / "agora.db",
                               injector=self.injector)
        self.addCleanup(self.storage.close)
        self.clock = FakeClock()
        self.oc = OcStub()
        self.gitx = PubGitxStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.emails = []  # [(tid, body)]

        def emailer(tid, body):
            self.emails.append((tid, body))
            return True

        self.email_fn = emailer
        self.tasksvc = TaskService(
            self.storage, self._gitx_stub(), self.oc,
            messaging=self.messaging, home=str(self.home.path),
            clock=self.clock)
        self.auth = AuthService(self.storage, home=str(self.home.path),
                                clock=self.clock, oc=self.oc)
        self.health = HealthService(
            self.storage, self.oc, self.messaging, clock=self.clock,
            recover=None, tasks=self.tasksvc, emailer=emailer,
            gitx=self.gitx)

    @staticmethod
    def _gitx_stub():
        class _G:
            def env_on_disk(self, env):
                return True
        return _G()

    def seed(self, tid, **over):
        row = dict(id=tid, state="todo", title=tid, goal="g",
                   project_dir="/repo", target_branch="v3-runtime",
                   dispatcher_sid=DISPATCHER, root_sid=DISPATCHER,
                   chain_depth=1, created_ms=self.clock.ms,
                   updated_ms=self.clock.ms,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=DISPATCHER,
                                     registered_at_ms=1))
        row.update(over)
        with self.storage.uow():
            self.storage.upsert_task(domain.Task(**row))
        return tid

    def put_cred(self, sid, token, task_id=None, lineage=()):
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=sid, token=token, task_id=task_id, lineage=lineage,
                created_ms=self.clock.ms))

    def events(self, tid, name=None):
        evs = [e for e in self.storage.events_for_task(tid)
               if name is None or e.event == name]
        return [e.event for e in evs]

    def escalate_kinds(self, tid):
        return [e.detail.get("kind") for e in
                self.storage.events_for_task(tid) if e.event == "escalate"]

    def nudges(self):
        return [e for e in self.oc.injects if len(e) == 2]  # 直注面（非 notify 通道）

    def bodies(self, sid):
        out = []
        for m in self.storage.messages_for_sid(sid):
            b = (m.body.decode("utf-8", "replace")
                 if isinstance(m.body, (bytes, bytearray, memoryview))
                 else str(m.body))
            out.append((m.kind, b))
        return out


class TestAuthBasics(_Base):  # 断言1
    def test_issue_worker_file_first_0600_then_db(self):
        tid = self.seed("ag-t1", state="running", worker_sid=WORKER)
        token, path = self.auth.issue(WORKER, "worker", task_id=tid)
        self.assertEqual(_mode(path), 0o600)  # 无「先建后 chmod」窗
        self.assertEqual(json.loads(
            open(path, encoding="utf-8").read())["token"], token)
        self.assertIsNotNone(self.storage.get_credential(WORKER))
        self.assertEqual(self.auth.identify(token), (WORKER, "worker"))

    def test_issue_crash_window_orphan_file_then_sweep(self):
        tid = self.seed("ag-t1", state="running", worker_sid=WORKER)
        live_tid = self.seed("ag-live", state="running", worker_sid="ses-wl")
        self.auth.issue("ses-wl", "worker", task_id=live_tid)  # 在册活件
        self.injector.arm("uow", "before")
        with self.assertRaises(Exception):
            self.auth.issue(WORKER, "worker", task_id=tid)  # 崩在 DB 提交前
        self.assertTrue((self.home.credentials / f"{tid}.token").exists())
        self.assertIsNone(self.storage.get_credential(WORKER))  # DB 零残留
        self.injector.disarm("uow")
        swept = AuthService(self.storage, home=str(self.home.path),
                            clock=self.clock, oc=self.oc).sweep()
        self.assertEqual(swept, 1)  # 只清孤儿：活件留、孤儿除
        self.assertFalse((self.home.credentials / f"{tid}.token").exists())
        self.assertTrue((self.home.credentials / f"{live_tid}.token").exists())

    def test_sweep_clears_terminal_task_file(self):  # F43 终态牌文件即清
        tid = self.seed("ag-t1", state="closed")
        token, path = self.auth.issue("ses-w9", "worker", task_id=tid)
        self.assertEqual(self.auth.sweep(), 1)
        self.assertFalse((self.home.credentials / f"{tid}.token").exists())
        self.assertEqual(self.auth.identify(token), ("ses-w9", "worker"))

    def test_identify_unknown_and_role_faces(self):
        self.assertIsNone(self.auth.identify("nope"))
        self.assertIsNone(self.auth.identify(""))
        self.assertIsNone(self.auth.identify(None))
        tok_d, _ = self.auth.issue("ses-disp1", "dispatcher")
        self.assertEqual(self.auth.identify(tok_d), ("ses-disp1", "dispatcher"))
        tok_r, _ = self.auth.issue("ses-any", "root")  # 钉死 daemon 身份
        self.assertEqual(self.auth.identify(tok_r), ("root", "root"))

    def test_issue_validation(self):
        with self.assertRaises(ContractError):
            self.auth.issue("ses-x", "bogus")
        with self.assertRaises(ContractError):
            self.auth.issue("ses-x", "worker")  # worker 牌须携签发任务
        with self.assertRaises(ContractError):
            self.auth.issue("bad/sid", "dispatcher")

    def test_issue_rejects_root_sid_for_non_root_and_traversal(self):
        with self.assertRaises(ContractError):  # R1 C3：非 root 角色签 root sid＝提权
            self.auth.issue("root", "dispatcher")
        tid = self.seed("ag-tv", state="running", worker_sid=WORKER)
        with self.assertRaises(ContractError):  # R1 C4：worker task_id 路径穿越
            self.auth.issue(WORKER, "worker", task_id="../evil")
        with self.assertRaises(ContractError):  # 控制字符 sid（防伪造服务行）
            self.auth.issue("ses-bad\nx", "dispatcher")

    def test_rebind_root_target_and_same_sid_guards(self):
        # R1 C1/C2：rebind 新 sid＝root 拒（角色按 sid 派生＝提权面）
        self.put_cred(WORKER, "tok-w", task_id="ag-t1")
        self.assertIsNone(self.auth.rebind(WORKER, "root"))
        self.assertEqual(self.auth.identify("tok-w"),
                         (WORKER, "worker"))  # 原Praça面不动
        # R1 B3：同 sid rebind 不自灭牌
        self.assertEqual(self.auth.rebind(WORKER, WORKER), "tok-w")
        self.assertEqual(self.auth.identify("tok-w"), (WORKER, "worker"))
        # R1 C2：bind_worker 自报 caller=root 拒绑
        tok_root, _ = self.auth.issue(ROOT, "root")
        self.seed("ag-bw", state="running", worker_sid=WORKER)
        self.assertEqual(self.auth.bind_worker(WORKER, "root"), WORKER)
        self.assertEqual(self.storage.get_task("ag-bw").worker_sid, WORKER)
        self.assertEqual(self.auth.identify(tok_root), ("root", "root"))

    def test_bind_worker_credential_occupancy_recheck(self):
        # R1 B4：caller 已持牌（无任务行的 fresh dispatcher）——事务内拒绑
        self.seed("ag-oc", state="running", worker_sid="ses-old")
        self.put_cred("ses-held", "tok-h")
        self.assertEqual(self.auth.bind_worker("ses-old", "ses-held"),
                         "ses-old")
        self.assertEqual(self.storage.get_task("ag-oc").worker_sid, "ses-old")
        self.assertEqual(self.storage.get_credential("ses-held").token,
                         "tok-h")  # 占位者牌不被覆写

    def test_rotate_root_crash_window_sweeps_back_to_db(self):
        # R1 B1/C8：文件先落、DB 崩 → 孤儿新文件；sweep 按 DB 权威对账回写
        old, _ = self.auth.issue(ROOT, "root")
        self.injector.arm("uow", "before")
        with self.assertRaises(Exception):
            self.auth.rotate_root()  # 崩在 DB 提交前（文件已写新牌）
        self.injector.disarm("uow")
        self.assertEqual(self.auth.identify(old), ("root", "root"))  # DB 旧牌续用
        AuthService(self.storage, home=str(self.home.path),
                    clock=self.clock, oc=self.oc).sweep()
        import json as _json
        back = _json.loads((self.home.agora / "root.token")
                           .read_text(encoding="utf-8"))["token"]
        cred = self.storage.get_credential("root")
        self.assertEqual(back, cred.token)  # 文件回写 DB 权威牌
        self.assertEqual(self.auth.identify(back), ("root", "root"))

    def test_sweep_bad_json_orphan_removed_without_crash(self):
        # R1 B5/C10＋R2：合法非对象 JSON 与非字符串牌值不再炸清扫，按孤儿清
        (self.home.credentials / "ag-junk.token").write_text(
            "[1, 2]", encoding="utf-8")
        (self.home.credentials / "ag-unhashable.token").write_text(
            '{"token": ["a"]}', encoding="utf-8")
        swept = AuthService(self.storage, home=str(self.home.path),
                            clock=self.clock, oc=self.oc).sweep()
        self.assertEqual(swept, 2)
        self.assertFalse((self.home.credentials / "ag-junk.token").exists())
        self.assertFalse(
            (self.home.credentials / "ag-unhashable.token").exists())

    def test_revoke_and_revoke_session(self):
        token, _ = self.auth.issue(WORKER, "worker", task_id=self.seed("ag-t"))
        self.assertTrue(self.auth.revoke(token))
        self.assertIsNone(self.auth.identify(token))
        self.assertFalse(self.auth.revoke(token))  # 再撤＝False
        tok2, _ = self.auth.issue("ses-d2", "dispatcher")
        self.assertTrue(self.auth.revoke_session("ses-d2"))
        self.assertFalse(self.auth.revoke_session("ses-d2"))

    def test_rotate_root_revokes_old_pins_root_sid(self):
        old, _ = self.auth.issue(ROOT, "root")
        new, path = self.auth.rotate_root()
        self.assertEqual(_mode(path), 0o600)
        self.assertIsNone(self.auth.identify(old))  # 旧 root 牌即废
        self.assertEqual(self.auth.identify(new), ("root", "root"))  # 钉死
        with self.storage.open_reader() as r, r:  # 无任务行事件经读者断言
            rows = r.execute("SELECT event, detail_json FROM events "
                             "WHERE event='root_rotated'").fetchall()
        self.assertEqual(rows[0][0], "root_rotated")
        self.assertEqual(json.loads(rows[0][1]), {"revoked": 1})


class TestLineage(_Base):  # 断言2
    def test_rebind_moves_old_sid_into_lineage(self):
        self.put_cred(WORKER, "tok-1", task_id="ag-t1")
        token = self.auth.rebind(WORKER, "ses-w2")
        self.assertEqual(token, "tok-1")  # token 本体不变
        self.assertIsNone(self.storage.get_credential(WORKER))
        cred = self.storage.get_credential("ses-w2")
        self.assertEqual(cred.lineage, (WORKER,))
        self.assertTrue(self.auth.same_lineage("ses-w2", WORKER))
        self.assertFalse(self.auth.same_lineage(WORKER, "ses-w2"))
        self.assertEqual(self.auth.identify("tok-1"),
                         ("ses-w2", "worker"))  # 角色随 task_id 保持

    def test_lineage_cap_16_fifo(self):
        self.put_cred("s0", "tok")
        sid = "s0"
        for i in range(1, 21):  # 20 连换：s0..s19 都曾用名
            sid = f"s{i}"
            self.auth.rebind(f"s{i - 1}", sid)
        cred = self.storage.get_credential(sid)
        self.assertEqual(len(cred.lineage), 16)  # 封顶 16
        self.assertEqual(cred.lineage, tuple(f"s{i}" for i in range(4, 20)))
        self.assertFalse(self.auth.same_lineage(sid, "s3"))  # 最旧出局
        self.assertTrue(self.auth.same_lineage(sid, "s4"))  # FIFO 保最近

    def test_bind_worker_nested_overwrite_and_terminal_freeze(self):
        old, new = "ses-old", "ses-new"
        self.seed("ag-p", state="running", worker_sid=old)
        self.seed("ag-c-open", state="todo", dispatcher_sid=old,
                  root_sid=DISPATCHER, chain_depth=2)
        self.seed("ag-c-done", state="running", dispatcher_sid=old,
                  root_sid=DISPATCHER, chain_depth=2)
        self.seed("ag-c-frozen", state="merged", dispatcher_sid=old,
                  root_sid=DISPATCHER, chain_depth=2)
        self.put_cred(old, "tok-x", task_id="ag-p")
        self.assertEqual(self.auth.bind_worker(old, new), new)
        self.assertEqual(self.storage.get_task("ag-p").worker_sid, new)
        self.assertEqual(self.storage.get_task("ag-c-open").dispatcher_sid, new)
        self.assertEqual(self.storage.get_task("ag-c-done").dispatcher_sid, new)
        self.assertEqual(  # 终态行冻结不覆写（TOCTOU 复检语义）
            self.storage.get_task("ag-c-frozen").dispatcher_sid, old)
        for tid in ("ag-p", "ag-c-open", "ag-c-done"):
            self.assertEqual(self.events(tid, "worker_rebound").count(
                "worker_rebound"), 1)
        self.assertEqual(self.events("ag-c-frozen", "worker_rebound"), [])
        self.assertEqual(self.storage.get_credential(new).lineage, (old,))


class TestBindWorker(_Base):  # 断言3
    def test_occupied_caller_rejected(self):
        self.seed("ag-a", state="running", worker_sid="ses-old")
        self.seed("ag-b", state="running", worker_sid="ses-occ")
        self.put_cred("ses-old", "tok", task_id="ag-a")
        self.assertEqual(  # 已占 sid 拒绑（防跨任务冒充）
            self.auth.bind_worker("ses-old", "ses-occ"), "ses-old")
        self.assertEqual(self.storage.get_task("ag-a").worker_sid, "ses-old")
        self.assertIsNotNone(self.storage.get_credential("ses-old"))

    def test_no_active_row_no_rebind(self):
        self.seed("ag-a", state="merged", worker_sid="ses-old")
        self.put_cred("ses-old", "tok", task_id="ag-a")
        self.assertEqual(self.auth.bind_worker("ses-old", "ses-n1"), "ses-old")
        self.assertIsNotNone(self.storage.get_credential("ses-old"))

    def test_active_rows_rechecked_terminal_skipped(self):
        self.seed("ag-live", state="running", worker_sid="ses-old")
        self.seed("ag-dead", state="cancelled", worker_sid="ses-old")
        self.auth.bind_worker("ses-old", "ses-n2")
        self.assertEqual(self.storage.get_task("ag-live").worker_sid, "ses-n2")
        self.assertEqual(  # 终态行 worker 字段同样冻结
            self.storage.get_task("ag-dead").worker_sid, "ses-old")

    def test_rebind_without_credential_still_rebinds_rows(self):
        self.seed("ag-a", state="running", worker_sid="ses-old")
        self.assertEqual(self.auth.bind_worker("ses-old", "ses-n3"), "ses-n3")
        self.assertEqual(self.storage.get_task("ag-a").worker_sid, "ses-n3")


class TestBindDispatcher(_Base):  # 断言4
    def test_dead_dispatcher_healed(self):
        old, heal = "ses-d-old", "ses-d-new"
        self.seed("ag-a", state="running", dispatcher_sid=old, root_sid=old)
        self.put_cred(old, "tok-d")
        self.oc.sessions[old] = None  # 死号
        self.auth.bind_dispatcher("root", heal,
                                  self.storage.get_task("ag-a"))
        t = self.storage.get_task("ag-a")
        self.assertEqual(t.dispatcher_sid, heal)
        self.assertEqual(self.events("ag-a"), ["dispatcher_rebind"])
        cred = self.storage.get_credential(heal)
        self.assertEqual((cred.token, cred.lineage), ("tok-d", (old,)))

    def test_heal_target_root_sid_refused_no_clobber(self):
        # R1 C1：治愈目标＝daemon 身份 → 拒；真 root 牌不可被改绑覆写
        old = "ses-d-old"
        self.seed("ag-a", state="running", dispatcher_sid=old, root_sid=old)
        self.put_cred(old, "tok-dead")
        tok_root, _ = self.auth.issue(ROOT, "root")
        self.oc.sessions[old] = None
        self.auth.bind_dispatcher("root", "root",
                                  self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, old)
        self.assertEqual(self.auth.identify(tok_root), ("root", "root"))
        # 死号牌原地不动（未被顶成 root 牌）；行未改绑＝下一轮可重试愈合
        self.assertEqual(self.auth.identify("tok-dead"),
                         ("ses-d-old", "dispatcher"))

    def test_alive_dispatcher_not_healed(self):
        old = "ses-d-old"
        self.seed("ag-a", state="running", dispatcher_sid=old, root_sid=old)
        self.oc.sessions[old] = {"time": {"updated": 1, "idle": 0}}
        self.auth.bind_dispatcher("root", ROOT, self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, old)

    def test_oc_unreachable_exempt(self):
        old = "ses-d-old"
        self.seed("ag-a", state="running", dispatcher_sid=old, root_sid=old)
        self.oc.sessions[old] = None
        self.oc.alive_raises = True  # oc 不可达≠死：免判
        self.auth.bind_dispatcher("root", ROOT, self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, old)

    def test_daemon_row_never_healed(self):
        self.seed("ag-a", state="running", dispatcher_sid="root",
                  root_sid="root")
        self.auth.bind_dispatcher("root", ROOT, self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, "root")

    def test_guards(self):
        old = "ses-d-old"
        self.seed("ag-a", state="running", dispatcher_sid=old, root_sid=old)
        self.oc.sessions[old] = None
        self.auth.bind_dispatcher(  # 非 root 牌不动
            "dispatcher", ROOT, self.storage.get_task("ag-a"))
        self.seed("ag-b", state="running", worker_sid=ROOT)  # caller 已占
        self.auth.bind_dispatcher("root", ROOT, self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, old)
        with self.storage.uow():  # 仅终态行不动
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-a"), state="cancelled"))
        self.auth.bind_dispatcher("root", "ses-free",
                                  self.storage.get_task("ag-a"))
        self.assertEqual(self.storage.get_task("ag-a").dispatcher_sid, old)


class TestRoleMatrix(_Base):  # 断言5
    def test_cross_product_per_contracts(self):
        self.assertEqual(len(CATALOG), 20)  # 19 服务工具＋sleep
        for tool, spec in CATALOG.items():
            self.assertTrue(self.auth.may("root", tool), tool)
            self.assertEqual(self.auth.may("worker", tool),
                             "worker" in spec.roles, tool)
            allowed = "dispatcher" in spec.roles
            if tool in TASK_DOMAIN:  # task 域单列：须有任务行（归属终审在服务）
                self.assertEqual(self.auth.may("dispatcher", tool,
                                               task_exists=False),
                                 False, tool)
                self.assertEqual(self.auth.may("dispatcher", tool,
                                               task_exists=True),
                                 allowed, tool)
            else:
                self.assertEqual(self.auth.may("dispatcher", tool), allowed,
                                 tool)
        self.assertFalse(self.auth.may("worker", "board"))
        self.assertFalse(self.auth.may("dispatcher", "enforce"))
        self.assertFalse(self.auth.may("root", "bogus_tool"))
        self.assertTrue(self.auth.may("worker", "message_post"))
        self.assertTrue(self.auth.may("worker", "sleep"))
        # R2：成员资格子集锚——CATALOG.ownership 漂移致闸面静默收窄/放宽即红
        self.assertLessEqual({"task_update", "task_cancel", "task_deliver",
                              "task_reverify", "task_wait"}, TASK_DOMAIN)
        self.assertFalse(_TASK_ROW_OPTIONAL & TASK_DOMAIN)


class TestStaleScan(_Base):  # 断言6
    def stale_task(self, tid="ag-st", age=25 * MIN, **over):
        over.setdefault("state", "running")
        over.setdefault("worker_sid", WORKER)
        over.setdefault("last_seen_ms", self.clock.ms - age)
        t = self.seed(tid, **over)
        self.oc.sessions[WORKER] = {"time": {"updated": 0, "idle": 1}}
        return t

    def test_fires_after_threshold_with_nudge(self):
        self.stale_task()
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-st"), [KIND_STALLED])
        self.assertEqual(len(self.emails), 1)  # quiet 走邮件缝
        self.assertEqual(len(self.nudges()), 1)  # 自检注入恰一次
        self.assertIn("僵态自检", self.nudges()[0][1])
        posted = self.bodies(DISPATCHER)
        self.assertTrue(any(k == "escalate" and "失控[stalled]" in b
                            for k, b in posted))

    def test_under_threshold_no_fire(self):
        self.stale_task(age=15 * MIN)
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-st"), [])

    def test_busy_round_exempt_then_refires_when_idle(self):
        self.stale_task()
        self.oc.sessions[WORKER] = {"time": {"updated": 9, "idle": 1}}
        self.health.tick()  # busy 回合免判
        self.assertEqual(self.escalate_kinds("ag-st"), [])
        self.oc.sessions[WORKER] = {"time": {"updated": 1, "idle": 9}}
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-st"), [KIND_STALLED])

    def test_waiting_on_and_gate_faces_exempt(self):
        dep = self.seed("ag-dep", state="running")
        self.stale_task(waiting_on=dep)
        self.seed("ag-g1", state="done", worker_sid="ses-wg",
                  gate=GateState(in_gate=True),
                  last_seen_ms=self.clock.ms - 30 * MIN)
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-st"), [])
        self.assertEqual(self.escalate_kinds("ag-g1"), [])

    def test_gate_busy_done_queued_exempt(self):
        # ag-b49329：他任务持门＝done 串行排队，正常态免判；门闲滞留照拍
        self.seed("ag-holder", state="done", worker_sid="ses-wh",
                  gate=GateState(in_gate=True))
        self.stale_task("ag-queued", state="done", worker_sid="ses-wq")
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-queued"), [])
        with self.storage.uow():  # 门空闲：done 滞留仍候选（无人消费＝真异常）
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-holder"),
                gate=GateState(in_gate=False)))
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-queued"), [KIND_STALLED])

    def test_gate_resource_waiting_exempt(self):
        # F116 gate_resource 系瞬态投影不落库（§3）——经内存 Task 直调判僵面
        tasks = {t.id: t for t in self.storage.tasks_all()}
        tasks["ag-q"] = domain.Task(
            id="ag-q", state="running", worker_sid="ses-wq",
            created_ms=self.clock.ms - 30 * MIN,
            gate=GateState(gate_resource={"waiting": True}))
        self.health._stale_scan(tasks, set())
        self.assertEqual(self.escalate_kinds("ag-q"), [])

    def test_edge_no_refire_and_reset_refire_with_backoff(self):
        tid = self.stale_task()
        self.health.tick()
        self.health.tick()  # 仍僵：边沿内不重拍
        self.assertEqual(self.escalate_kinds(tid), [KIND_STALLED])
        with self.storage.uow():  # 活动：离开候选集→复位
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task(tid), last_seen_ms=self.clock.ms))
        self.health.tick()
        with self.storage.uow():  # 再僵
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task(tid),
                last_seen_ms=self.clock.ms - 25 * MIN))
        self.clock.advance(GAP0_MS - 1_000)  # 退避窗内：不拍不记
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_STALLED])
        self.clock.advance(2_000)  # 越窗到期：重试拍第 2 次
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_STALLED] * 2)
        self.assertIn("第 2 次升级",
                      self.bodies(DISPATCHER)[-1][1])

    def test_esc_book_rebuilt_from_events_after_restart(self):
        # R1 A4：重启退避账经事件面重建——退避窗不因重启归零提前重拍
        tid = self.stale_task()
        self.health.tick()
        self.clock.advance(GAP0_MS // 2)  # 窗内重启
        fresh = HealthService(self.storage, self.oc, self.messaging,
                              clock=self.clock, tasks=self.tasksvc,
                              emailer=lambda *a: True)
        fresh.tick()  # 重建账 n=1：仍在退避窗内不拍
        self.assertEqual(self.escalate_kinds(tid), [KIND_STALLED])
        self.clock.advance(GAP0_MS)  # 越窗到期：第 2 次（streak 连续）
        fresh.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_STALLED] * 2)
        self.assertIn("第 2 次升级", self.bodies(DISPATCHER)[-1][1])

    def test_backoff_progression_and_cap(self):
        self.assertEqual(self.health._gap(1), GAP0_MS)      # 首档 1200s
        self.assertEqual(self.health._gap(2), GAP0_MS * 4)  # ×4
        self.assertEqual(self.health._gap(3), GAP0_MS * 16)
        self.assertEqual(self.health._gap(30), CAP_MS)      # 封顶 24h

    def test_env_gone_once_per_loss_cycle(self):
        tid = self.stale_task(env_dir="/nonexistent-env-x")
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_ENV_GONE])
        self.assertEqual(self.nudges(), [])  # env 已失无从自纠：免自检
        self.health.tick()  # 持续丢失：不重拍
        self.assertEqual(self.escalate_kinds(tid), [KIND_ENV_GONE])
        env = self.home.path / "env-restore"
        env.mkdir()  # 恢复在盘：回常规 stalled 边沿
        with self.storage.uow():
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task(tid), env_dir=str(env)))
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid),
                         [KIND_ENV_GONE, KIND_STALLED])
        env.rmdir()  # 新丢失周期：env-gone 再拍一次
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid),
                         [KIND_ENV_GONE, KIND_STALLED, KIND_ENV_GONE])


class TestWaitScan(_Base):  # 断言7
    def test_dependency_terminal_resolves_with_dual_notify(self):
        self.seed("ag-dep", state="merged")
        self.seed("ag-w", state="running", worker_sid=WORKER)
        with self.storage.uow():
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-w"), waiting_on="ag-dep"))
        self.health.tick()
        self.assertIsNone(self.storage.get_task("ag-w").waiting_on)
        self.assertIn("wait_resolved", self.events("ag-w"))
        worker_msgs = self.bodies(WORKER)
        disp_msgs = self.bodies(DISPATCHER)
        self.assertTrue(any(k == "process" and "受控等待解除" in b
                            and "ag-dep" in b for k, b in worker_msgs))
        self.assertTrue(any(k == "result" and "受控等待解除" in b
                            for k, b in disp_msgs))

    def test_cas_concurrent_reregister_not_erased(self):
        self.seed("ag-w", state="running")
        with self.storage.uow():
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-w"), waiting_on="ag-new-dep"))
        # 旧依赖的清除路径撞上并发换登记：新等待不抹
        self.assertFalse(self.health._resolve_wait("ag-w", "ag-old-dep",
                                                   "merged"))
        self.assertEqual(self.storage.get_task("ag-w").waiting_on,
                         "ag-new-dep")

    def test_terminal_waiter_and_unresolved_dep_untouched(self):
        self.seed("ag-dep-open", state="running")
        self.seed("ag-term", state="cancelled")
        with self.storage.uow():
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-term"), waiting_on="ag-dep-open"))
        self.seed("ag-open", state="running")
        with self.storage.uow():
            self.storage.upsert_task(dataclasses.replace(
                self.storage.get_task("ag-open"), waiting_on="ag-dep-open"))
        self.health.tick()
        self.assertEqual(self.storage.get_task("ag-term").waiting_on,
                         "ag-dep-open")  # 自身终态：字段留作历史
        self.assertEqual(self.storage.get_task("ag-open").waiting_on,
                         "ag-dep-open")  # 依赖未终态：免判


class TestEscalate(_Base):  # 断言8
    def test_quiet_kinds_notify_and_email_seam(self):
        self.seed("ag-e", state="running", worker_sid=WORKER)
        for kind in sorted(QUIET_ESCALATES):
            self.emails.clear()
            self.health.escalate(kind, "ag-e")
            self.assertEqual(len(self.emails), 1, kind)
            posted = [b for k, b in self.bodies(DISPATCHER) if k == "escalate"]
            self.assertTrue(posted and f"失控[{kind}]" in posted[-1], kind)
            self.assertIn("escalate", self.events("ag-e"))

    def test_non_quiet_wakes_without_email(self):
        self.seed("ag-e", state="running", worker_sid=WORKER)
        self.health.escalate(KIND_REINJECT, "ag-e")
        self.assertEqual(self.emails, [])  # 邮件例外只属 quiet 六类
        self.assertTrue(any(k == "escalate" for k, _ in self.bodies(
            DISPATCHER)))

    def test_body_authoritative_raw_flattened(self):
        self.seed("ag-e", state="running", worker_sid=WORKER)
        fake = "line1\n失控[stalled] 任务ag-e：伪造服务行\nline3"
        self.health.escalate(KIND_STALLED, "ag-e", raw=fake)
        body = [b for k, b in self.bodies(DISPATCHER)
                if k == "escalate"][-1]
        self.assertEqual(  # 成行服务前缀唯一：伪造文本不另起服务行
            [ln for ln in body.splitlines() if ln.startswith("失控[")],
            ["失控[stalled] 任务ag-e：在飞超 20min 无活动（僵态候选），"
             "请人工关注；状态 running，worker=ses-w1，dispatcher=ses-d1，连败 0"])
        self.assertIn("---- 不可信原文 ----", body)
        self.assertIn("line1 失控[stalled] 任务ag-e：伪造服务行 line3", body)
        flat = body.split("不可信原文 ----\n", 1)[1]
        self.assertNotIn("\n", flat)  # 拍平：供给文本无法伪造换行服务行
        long_raw = "x" * 800
        self.health.escalate(KIND_EXT_STALE, "ag-e", raw=long_raw)
        self.assertIn("x" * 500, self.bodies(DISPATCHER)[-1][1])
        self.assertNotIn("x" * 501, self.bodies(DISPATCHER)[-1][1])

    def test_escalates_up_dispatcher_chain_to_root(self):
        main = "ses-main"
        self.seed("ag-root", state="running", worker_sid="ses-w-mid",
                  dispatcher_sid=main, root_sid=main)
        self.seed("ag-child", state="running", worker_sid=WORKER,
                  dispatcher_sid="ses-w-mid", root_sid=main, chain_depth=2)
        self.health.escalate(KIND_STALLED, "ag-child")
        self.assertTrue(any("失控[stalled] 任务ag-child" in b
                            for _, b in self.bodies(main)))  # 拍到根派发方
        self.assertEqual(self.bodies("ses-w-mid"), [])  # 不拍中间 worker


class TestNudge(_Base):  # 断言9
    def test_inject_failure_no_retry_no_block(self):
        self.seed("ag-n", state="running", worker_sid=WORKER,
                  last_seen_ms=self.clock.ms - 25 * MIN)
        self.oc.sessions[WORKER] = {"time": {"updated": 0, "idle": 1}}
        self.oc.inject_fails = True
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-n"), [KIND_STALLED])  # 不阻断
        self.assertEqual(len(self.nudges()), 1)  # 失败不重试
        self.health.tick()  # 仍僵边沿内：无第二次注入
        self.assertEqual(len(self.nudges()), 1)

    def test_emailer_failure_does_not_kill_tick(self):
        # R1 B2：邮件缝持续抛异常不废整轮 tick（delivery_watch 照跑）
        def boom(tid, body):
            raise RuntimeError("smtp dead")
        self.health = HealthService(
            self.storage, self.oc, self.messaging, clock=self.clock,
            tasks=self.tasksvc, emailer=boom, gitx=self.gitx,
            stale_after_ms=10 ** 12)
        self.seed("ag-x", state="done", worker_sid=WORKER,
                  last_seen_ms=self.clock.ms,
                  updated_ms=self.clock.ms - 31 * MIN,
                  delivery=DeliveryState(mode="external_branch",
                                         status="awaiting_external",
                                         commit="c" * 40))
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-x"), [KIND_EXT_STALE])

    def test_nudge_without_worker(self):
        self.seed("ag-n", state="dispatched",
                  last_seen_ms=self.clock.ms - 25 * MIN)
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-n"), [KIND_STALLED])
        self.assertEqual(self.nudges(), [])
        self.assertFalse(self.health.nudge_worker("ag-n"))


class TestDeliveryWatch(_Base):  # 断言10
    def setUp(self):
        super().setUp()
        self.health = HealthService(  # 判僵阈拉到不可达：时钟前跳只服务退避
            self.storage, self.oc, self.messaging, clock=self.clock,
            tasks=self.tasksvc, emailer=self.email_fn, gitx=self.gitx,
            stale_after_ms=10 ** 12)

    def test_external_stale_with_backoff(self):
        self.seed("ag-x", state="done", worker_sid=WORKER,
                  last_seen_ms=self.clock.ms,  # 新活：不混入判僵
                  updated_ms=self.clock.ms - 31 * MIN,
                  delivery=DeliveryState(mode="external_branch",
                                         status="awaiting_external",
                                         commit="c" * 40))
        self.oc.sessions[WORKER] = {"time": {"updated": 0, "idle": 1}}
        self.health.tick()
        self.assertEqual(self.escalate_kinds("ag-x"), [KIND_EXT_STALE])
        self.assertEqual(len(self.emails), 1)
        self.assertIn("awaiting_external", self.bodies(DISPATCHER)[-1][1])
        self.health.tick()  # 退避窗内不重拍
        self.assertEqual(self.escalate_kinds("ag-x"), [KIND_EXT_STALE])
        self.clock.advance(GAP0_MS + 1_000)
        self.health.tick()  # 同口径 ×4 退避到期重拍
        self.assertEqual(self.escalate_kinds("ag-x"),
                         [KIND_EXT_STALE] * 2)

    def test_unpublished_fires_only_when_really_unpublished(self):
        self.seed("ag-u", state="merged",
                  updated_ms=self.clock.ms - 61 * MIN,
                  commit="a" * 40,
                  delivery=DeliveryState(mode="managed", status="delivered",
                                         commit="a" * 40))
        self.health.tick()  # sha 与补丁均不在 origin：拍
        self.assertEqual(self.escalate_kinds("ag-u"), [KIND_UNPUBLISHED])
        self.assertEqual(len(self.emails), 1)

    def test_unpublished_reconciliations_suppress(self):
        for name, over in (("published", {"published": True}),
                           ("patch_eq", {"patch_eq": True})):
            self.gitx.published = over.get("published", False)
            self.gitx.patch_eq = over.get("patch_eq", False)
            tid = self.seed(f"ag-u-{name}", state="closed",
                            updated_ms=self.clock.ms - 61 * MIN,
                            commit="a" * 40,
                            delivery=DeliveryState(mode="managed",
                                                   status="delivered",
                                                   commit="a" * 40))
            self.health.tick()
            self.assertEqual(self.escalate_kinds(tid), [], name)

    def test_unpublished_content_settled_twins_suppress(self):
        # ag-b0efd4：rebase 孪生残留两形态（上下文漂移/纯二进制）——第三层
        # 内容落定（blob 落定/行包含性）任一命中即视同已出版：不拍且落
        # content-settled 审计行（判定层随行）
        for layer, attr in (("blob", "blob_settled"),
                            ("lines", "lines_settled")):
            tid = self.seed(f"ag-u-{layer}", state="closed",
                            updated_ms=self.clock.ms - 61 * MIN,
                            commit="a" * 40,
                            delivery=DeliveryState(mode="managed",
                                                   status="delivered",
                                                   commit="a" * 40))
            setattr(self.gitx, attr, True)
            self.health.tick()
            self.health.tick()  # 驻留任务每轮重探：审计边沿触发只落一次
            self.assertEqual(self.escalate_kinds(tid), [], layer)
            evs = [e for e in self.storage.events_for_task(tid)
                   if e.event == "content-settled"]
            self.assertEqual([e.detail.get("layer") for e in evs], [layer])
            setattr(self.gitx, attr, False)
            self.health.tick()  # 判定回落（窗口滑出形态）：边沿重臂
            setattr(self.gitx, attr, True)
            self.health.tick()  # 再命中：重留审计行（真边沿，非一次性闩锁）
            evs = [e for e in self.storage.events_for_task(tid)
                   if e.event == "content-settled"]
            self.assertEqual([e.detail.get("layer") for e in evs],
                             [layer, layer])
            setattr(self.gitx, attr, False)

    def test_unpublished_escalation_stops_when_content_settles(self):
        # 自愈（ag-b0efd4 判据 4）：退避窗已过旧口径必重拍，内容落定后
        # 下轮探针判定即停
        tid = self.seed("ag-u-heal", state="merged",
                        updated_ms=self.clock.ms - 61 * MIN,
                        commit="a" * 40,
                        delivery=DeliveryState(mode="managed",
                                               status="delivered",
                                               commit="a" * 40))
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_UNPUBLISHED])
        self.gitx.lines_settled = True
        self.clock.advance(GAP0_MS * 4)  # 退避窗已过：无判定必重拍
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_UNPUBLISHED])  # 即停
        self.assertIn("content-settled", self.events(tid))
        self.gitx.published = False
        self.gitx.patch_eq = False
        self.gitx.raises = True  # 对账不可得：本轮免判（下轮重试）
        tid = self.seed("ag-u-sick", state="closed",
                        updated_ms=self.clock.ms - 61 * MIN,
                        commit="a" * 40,
                        delivery=DeliveryState(mode="managed",
                                               status="delivered",
                                               commit="a" * 40))
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [])


class TestDeliveryWatchOutage(_Base):  # 断言10 评审环 R1 B8 锚
    def setUp(self):
        super().setUp()
        self.health = HealthService(
            self.storage, self.oc, self.messaging, clock=self.clock,
            tasks=self.tasksvc, emailer=self.email_fn, gitx=self.gitx,
            stale_after_ms=10 ** 12)

    def merged_task(self, tid):
        return self.seed(tid, state="merged",
                         updated_ms=self.clock.ms - 61 * MIN,
                         commit="a" * 40,
                         delivery=DeliveryState(mode="managed",
                                                status="delivered",
                                                commit="a" * 40))

    def test_transient_outage_keeps_backoff_book(self):
        tid = self.merged_task("ag-o")
        self.health.tick()  # 首发 n=1
        self.gitx.raises = True
        self.clock.advance(GAP0_MS * 4)  # 对账不可得窗口：保账不清
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid), [KIND_UNPUBLISHED])
        self.gitx.raises = False
        self.clock.advance(GAP0_MS * 4)  # 恢复后按重建账续退避（第 2 次）
        self.health.tick()
        self.assertEqual(self.escalate_kinds(tid),
                         [KIND_UNPUBLISHED] * 2)
        self.assertIn("第 2 次升级", self.bodies(DISPATCHER)[-1][1])


class TestLastSeenWindow(_Base):  # 断言11
    def test_touch_batched_by_window(self):
        self.seed("ag-t", state="running", worker_sid=WORKER)
        self.health.touch(WORKER)  # 首刷（锚 0→立即持久）
        self.assertEqual(self.storage.get_task("ag-t").last_seen_ms,
                         self.clock.ms)
        for _ in range(3):
            self.clock.advance(1_000)
            self.health.touch(WORKER)  # 窗内：只进内存不落库
        self.assertEqual(self.storage.get_task("ag-t").last_seen_ms,
                         self.clock.ms - 3_000)  # 三次调用零独立写事务
        self.clock.advance(2_000)  # 距上次持久 ≥ 窗口：合并落一次
        self.health.touch(WORKER)
        self.assertEqual(self.storage.get_task("ag-t").last_seen_ms,
                         self.clock.ms)

    def test_crash_loses_at_most_one_window(self):
        self.seed("ag-t", state="running", worker_sid=WORKER)
        self.health.touch(WORKER)
        flushed = self.clock.ms
        self.clock.advance(4_000)  # 窗内未持久候选
        self.health.touch(WORKER)
        # 崩溃：内存态全失，新实例只见已持久值
        fresh = HealthService(self.storage, self.oc, self.messaging,
                              clock=self.clock, tasks=TaskService(
                                  self.storage, self._gitx_stub(), self.oc,
                                  home=str(self.home.path),
                                  clock=self.clock))
        seen = self.storage.get_task("ag-t").last_seen_ms
        self.assertEqual(seen, flushed)
        self.assertLess(seen - flushed, 5_000)  # 丢的 ≤ 一个窗口


class TestRecoverHook(_Base):  # 断言12
    def dead_worker_task(self, tid="ag-r", state="running", wsid=WORKER):
        t = self.seed(tid, state=state, worker_sid=wsid)
        self.oc.sessions[wsid] = None
        return t

    def health_with(self, recover):
        return HealthService(self.storage, self.oc, self.messaging,
                             clock=self.clock, recover=recover,
                             tasks=self.tasksvc, emailer=lambda *a: True,
                             gitx=self.gitx)

    def test_reinject_true_silent_self_heal(self):
        self.dead_worker_task()
        stub = RecoverStub(True)
        h = self.health_with(recover=stub)
        h.tick()
        self.assertEqual(self.escalate_kinds("ag-r"), [])  # 静默自愈
        self.assertEqual(stub.calls, [("ag-r", WORKER)])  # 决策接口被桩化调用

    def test_reinject_none_yields_no_false_alarm(self):
        self.dead_worker_task()
        h = self.health_with(recover=RecoverStub(None))
        h.tick()
        self.assertEqual(self.escalate_kinds("ag-r"), [])  # 身份易主让位

    def test_reinject_false_escalates(self):
        self.dead_worker_task()
        h = self.health_with(recover=RecoverStub(False))
        h.tick()
        self.assertEqual(self.escalate_kinds("ag-r"), [KIND_REINJECT])
        self.assertEqual(self.emails, [])  # 非 quiet：无邮件

    def test_dispatcher_dead_when_managing_inflight_children(self):
        self.dead_worker_task()  # ag-r worker=WORKER（死）
        self.seed("ag-child", state="dispatched", dispatcher_sid=WORKER,
                  root_sid=DISPATCHER, chain_depth=2)
        h = self.health_with(recover=RecoverStub(False))
        h.tick()
        self.assertEqual(self.escalate_kinds("ag-r"),
                         [KIND_DISPATCHER_DEAD])

    def test_reinject_hook_raising_counts_as_failure(self):
        # R1 B2：钩异常＝失败口径（升级 reinject-fail），不废整轮 tick
        self.dead_worker_task()
        h = self.health_with(recover=RecoverStub(RuntimeError("boom")))
        h.tick()
        self.assertEqual(self.escalate_kinds("ag-r"), [KIND_REINJECT])

    def test_oc_unreachable_round_wholly_exempt(self):
        self.seed("ag-r", state="running", worker_sid=WORKER,
                  last_seen_ms=self.clock.ms - 25 * MIN)
        self.seed("ag-x", state="done", worker_sid="ses-wx",
                  last_seen_ms=self.clock.ms - 25 * MIN)
        class _Down:
            def session(self, sid):
                raise RuntimeError("oc down")
            def inject(self, sid, text):
                return True
        HealthService(self.storage, _Down(), self.messaging,
                      clock=self.clock, emailer=lambda *a: True).tick()
        self.assertEqual(self.escalate_kinds("ag-r"), [])  # 不误杀
        self.assertEqual(self.escalate_kinds("ag-x"), [])  # 僵态整体免判
        self.assertEqual(self.storage.get_task("ag-r").state, "running")


class TestAuthGitPort(unittest.TestCase):
    """Req: F151——AuthService 零改动跑 GitStore 后端（不复制 SQLite 套件：
    择签发/识别/撤销/轮换/改绑/清扫代表路径＋重启重放与 secret 文件对账）。"""

    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self.injector = harness.FaultInjector().register("uow")
        for fp in ("write-object", "write-tree", "commit-tree",
                   "update-ref", "memory-publish"):  # GitStore 提交面 failpoints
            self.injector.register(fp)
        self.clock = FakeClock()
        self.oc = OcStub()
        # 单实例贯穿用例（GitStore 跨实例不自动跟随——CAS 单写者面）
        self.gitstore = GitStore(self.home.state, injector=self.injector)

    def auth(self, store=None):
        return AuthService(store or self.gitstore, home=str(self.home.path),
                           clock=self.clock, oc=self.oc)

    def seed(self, tid, **over):
        row = dict(id=tid, state="todo", title=tid, goal="g",
                   project_dir="/repo", target_branch="v3-runtime",
                   dispatcher_sid=DISPATCHER, root_sid=DISPATCHER,
                   chain_depth=1, created_ms=self.clock.ms,
                   updated_ms=self.clock.ms,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=DISPATCHER,
                                     registered_at_ms=1))
        row.update(over)
        with self.gitstore.uow():
            self.gitstore.upsert_task(domain.Task(**row))
        return tid

    def test_issue_identify_token_for_revoke_and_restart_replay(self):
        svc = self.auth()
        tid = self.seed("ag-g1", state="running", worker_sid=WORKER)
        tok_w, path_w = svc.issue(WORKER, "worker", task_id=tid)
        tok_d, path_d = svc.issue("ses-dx", "dispatcher")
        tok_r, path_r = svc.issue(ROOT, "root")
        self.assertEqual(_mode(path_w), 0o600)  # secret 文件铁则同口径
        for path in (path_w, path_d, path_r):
            self.assertEqual(_mode(path), 0o600)
        self.assertEqual(svc.identify(tok_w), (WORKER, "worker"))
        self.assertEqual(svc.identify(tok_d), ("ses-dx", "dispatcher"))
        self.assertEqual(svc.identify(tok_r), ("root", "root"))
        self.assertEqual(svc.token_for(WORKER), tok_w)
        self.assertTrue(svc.revoke(tok_d))
        self.assertIsNone(svc.identify(tok_d))
        self.assertFalse(svc.revoke(tok_d))  # 再撤＝False
        self.assertTrue(svc.revoke_session(WORKER))
        self.assertIsNone(svc.identify(tok_w))
        # 重启重放：新仓库实例从外部 secret 文件水合，牌面续用
        svc2 = self.auth(GitStore(self.home.state))
        self.assertEqual(svc2.identify(tok_r), ("root", "root"))
        self.assertEqual([c.sid for c in svc2._creds], ["root"])

    def test_rotate_root_revokes_old_and_replays_after_restart(self):
        svc = self.auth()
        old, _ = svc.issue(ROOT, "root")
        new, path = svc.rotate_root()
        self.assertIsNone(svc.identify(old))  # 旧 root 牌即废
        self.assertEqual(svc.identify(new), ("root", "root"))
        self.assertEqual(json.loads(open(path, encoding="utf-8").read())
                         ["token"], new)
        self.assertIn("root_rotated",
                      [e.event for e in self.gitstore.events_all()])
        svc2 = self.auth(GitStore(self.home.state))  # 重启：外部文件水合
        self.assertEqual(svc2.identify(new), ("root", "root"))
        self.assertIsNone(svc2.identify(old))

    def test_rebind_dispatcher_lineage_keeps_secret_at_old_key(self):
        svc = self.auth()
        token, _ = svc.issue("ses-d1", "dispatcher")
        self.assertEqual(svc.rebind("ses-d1", "ses-d2"), token)
        self.assertEqual(svc.identify(token), ("ses-d2", "dispatcher"))
        self.assertTrue(svc.same_lineage("ses-d2", "ses-d1"))
        self.assertFalse(svc.same_lineage("ses-d1", "ses-d2"))
        # 文件留在旧键位（服务不移文件）；GitStore 按同摘要继用既录路径
        self.assertTrue((self.home.credentials / "ses-d1.token").exists())
        self.assertFalse((self.home.credentials / "ses-d2.token").exists())
        svc2 = self.auth()  # 重启重放：改绑牌照常水合
        self.assertEqual(svc2.identify(token), ("ses-d2", "dispatcher"))
        self.assertEqual(svc2.token_for("ses-d2"), token)

    def test_bind_worker_rebind_and_task_row_overwrite(self):
        svc = self.auth()
        self.seed("ag-bw", state="running", worker_sid="ses-wold")
        token, _ = svc.issue("ses-wold", "worker", task_id="ag-bw")
        self.assertEqual(svc.bind_worker("ses-wold", "ses-wnew"), "ses-wnew")
        self.assertEqual(self.gitstore.get_task("ag-bw").worker_sid,
                         "ses-wnew")
        cred = self.gitstore.get_credential("ses-wnew")
        self.assertEqual((cred.token, cred.lineage), (token, ("ses-wold",)))
        self.assertIn("worker_rebound",
                      [e.event for e in self.gitstore.events_all()])
        self.assertEqual(svc.identify(token), ("ses-wnew", "worker"))

    def test_sweep_secret_file_reconciliation_on_git(self):
        svc = self.auth()
        live_tid = self.seed("ag-live", state="running", worker_sid="ses-wl")
        dead_tid = self.seed("ag-dead", state="closed", worker_sid="ses-wd")
        svc.issue("ses-wl", "worker", task_id=live_tid)  # 在册活件：留
        # 终态任务牌文件：清（F43）
        (self.home.credentials / f"{dead_tid}.token").write_text(
            json.dumps({"token": "tok-dead"}) + "\n", encoding="utf-8")
        # 孤儿（DB 无同牌）：清
        (self.home.credentials / "ag-junk.token").write_text(
            json.dumps({"token": "tok-junk"}) + "\n", encoding="utf-8")
        # 孤儿 root.token（无 root 牌）：清
        (self.home.agora / "root.token").write_text(
            json.dumps({"token": "tok-orphan"}) + "\n", encoding="utf-8")
        self.assertEqual(svc.sweep(), 3)
        self.assertTrue((self.home.credentials / f"{live_tid}.token").exists())
        for name in (f"{dead_tid}.token", "ag-junk.token"):
            self.assertFalse((self.home.credentials / name).exists())
        self.assertFalse((self.home.agora / "root.token").exists())


if __name__ == "__main__":
    unittest.main()
