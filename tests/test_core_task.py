"""T7 core/task 契约：保险丝/级联中止/verify 登记/重派/任务页。

真 Store/State/Auth/Router＋真 pages（tmp 夹具）；假 oc/gitx 注入；
嵌套链用 State.add 播种（平表级联＝子任务 dispatcher_sid==上级 worker_sid）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.oc import OcUnavailable
from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.msg import Router
from agorad.core.state import State
from agorad.core.task import Rejected, TaskService


class FakeGitx:
    def __init__(self):
        self.built, self.removed, self.branches, self.checked = [], [], [], []

    def check_branch(self, repo_dir, branch):
        if branch == "bad..name":
            raise RuntimeError("not a valid branch name")
        self.checked.append((repo_dir, branch))

    def build_env(self, project_dir, tid, branch=None):
        env = f"/envs/{tid}"
        self.built.append(env)
        self.branches.append(branch)
        return env

    def remove_env(self, env_dir):
        self.removed.append(env_dir)


class FakeOc:
    def __init__(self):
        self.sessions, self.injected = [], []
        self.fail_create = False
        self.detail = {}  # sid -> session 详情 dict（F28 busy 探测面）；缺 sid＝死会话
        self.down = False  # True＝oc 不可达（session 抛 OcUnavailable）

    def create(self, cwd, boot, title=None):
        if self.fail_create:
            raise OcUnavailable("stub down")
        sid = f"s{len(self.sessions) + 1}"
        self.sessions.append((sid, cwd, boot))
        return sid

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True

    def session(self, sid):
        if self.down:
            raise OcUnavailable("stub down")
        return self.detail.get(sid)


class TestCoreTask(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.gitx, self.oc = FakeGitx(), FakeOc()
        self.router = Router(self.store, self.state, self.oc)
        self.svc = TaskService(self.state, self.auth, self.gitx, self.oc,
                               self.store, self.router, write_task_page)
        self.root = self.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid, state="running", disp="disp1", worker=None, depth=1, env=None):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": env,
                             "verify": {"cmd": "", "state": "pending",
                                        "registered_by": None, "registered_at": None},
                             "verify_pending": True,  # 播种行免硬闸：闸语义归 TestVerifyGate
                             "state": state, "dispatcher_sid": disp,
                             "worker_sid": worker, "chain_depth": depth,
                             "fail_streak": 0, "created": 0.0, "updated": 0.0})
        return tid

    def events(self):
        lines = self.tmp.joinpath("events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line]

    def dispatchable(self, goal="do x", after=None):
        # 硬闸后的「可派发」＝建单原子登记 verify（快速路）
        return self.svc.create(goal, "/repo", after, self.root, verify_cmd="true")

    def dispatched_pair(self):
        """建＋派一套（dispatcher 角色 token 持有），返回 (tid, dispatcher_token, worker_token)。"""
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp, verify_cmd="true")
        return tid, disp, self.svc.dispatch(tid, disp)


def cancel_msgs(store, sid):
    return [m for m in store.read_messages()
            if m["kind"] == "cancel" and m["to"] == sid]


class TestFuses(TestCoreTask):
    def test_chain_depth_at_limit_rejected(self):  # Req: F3
        self.seed("t5", state="todo", depth=5, disp="main")
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch("t5", self.root)
        self.assertIn("chain_depth", str(cm.exception))
        self.assertEqual(self.state.get("t5")["state"], "todo")  # 拒绝零副作用

    def test_quota_rejected_then_cancel_releases(self):  # Req: F3
        for i in range(8):
            self.seed(f"b{i}", worker=f"s-w{i}", env=f"/envs/b{i}")
        tid = self.dispatchable()
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(tid, self.root)
        self.assertIn("quota", str(cm.exception))
        self.svc.cancel("b0", self.root)  # 配额按活动任务计数：cancelled 即释放
        self.svc.dispatch(tid, self.root)
        self.assertEqual(self.state.get(tid)["state"], "dispatched")

    def test_can_spawn_rejected_cleans_env_no_side_effects(self):  # Req: F3
        tid = self.dispatchable()
        self.oc.fail_create = True  # oc 名册拒 spawn（can_spawn）
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(tid, self.root)
        self.assertIn("can_spawn", str(cm.exception))
        self.assertEqual(self.state.get(tid)["state"], "todo")
        self.assertEqual(self.gitx.removed, [f"/envs/{tid}"])  # 半途环境即回收
        self.assertEqual(self.oc.sessions, [])  # 无会话无凭据

    def test_b7_after_not_terminal_rejected_then_passes(self):  # Req: F3
        a = self.dispatchable("A")
        b = self.dispatchable("B", after=a)
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(b, self.root)
        self.assertIn("after", str(cm.exception))
        self.svc.cancel(a, self.root)  # cancelled ∈ B7 终态
        self.svc.dispatch(b, self.root)
        self.assertEqual(self.state.get(b)["state"], "dispatched")

    def test_dispatch_wrong_state_rejected_and_audited(self):  # Req: F3
        self.seed("t1", state="running")
        with self.assertRaises(Rejected):
            self.svc.dispatch("t1", self.root)
        self.assertTrue(any(e["event"] == "dispatch_rejected"
                            and e.get("task") == "t1" for e in self.events()))

    def test_create_after_missing_rejected_and_audited(self):  # Req: F2
        with self.assertRaises(Rejected):
            self.svc.create("g", "/repo", "ghost", self.root)
        ev = [e for e in self.events() if e["event"] == "dispatch_rejected"]
        self.assertEqual(len(ev), 1)  # 直 raise 补审计行（F-2）
        self.assertTrue(ev[0].get("task"))
    def test_new_tid_prefix_task_and_legacy_gt_after_chain(self):  # Req: F24
        tid = self.dispatchable()
        self.assertRegex(tid, r"^task-[0-9a-f]{6}$")  # 新前缀 task-，6 hex 不变
        self.seed("gt-abc123", state="cancelled")  # 存量账本旧前缀终身有效（B7 终态）
        child = self.dispatchable(after="gt-abc123")  # 旧 id 作 after：存在性校验双形态同收
        self.svc.dispatch(child, self.root)
        self.assertEqual(self.state.get(child)["state"], "dispatched")


class TestCancel(TestCoreTask):
    def test_two_level_cascade_with_notifications(self):  # Req: F11
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", env="/envs/t2")
        self.seed("t3", disp="s-w2", worker="s-w3", env="/envs/t3")
        toks = {w: self.auth.issue(f"s-{w}", "worker") for w in ("w1", "w2", "w3")}
        n = self.svc.cancel("t1", self.root)
        self.assertEqual(n["cancelled"], 3)  # 两级级联
        for tid in ("t1", "t2", "t3"):
            self.assertEqual(self.state.get(tid)["state"], "cancelled")
        for w in ("w1", "w2", "w3"):  # 各级 worker 各收一条终止通知
            self.assertEqual(len(cancel_msgs(self.store, f"s-{w}")), 1)
        for tok in toks.values():
            self.assertIsNone(self.auth.identify(tok))  # 凭据作废
        self.assertEqual(sorted(self.gitx.removed),
                         ["/envs/t1", "/envs/t2", "/envs/t3"])  # 环境即回收
        self.assertTrue(any(e["event"] == "cancelled" for e in self.events()))

    def test_cancel_spares_merged_descendant(self):  # Req: F11
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", state="merged")
        tok2 = self.auth.issue("s-w2", "worker")
        self.svc.cancel("t1", self.root)
        self.assertEqual(self.state.get("t2")["state"], "merged")  # 中止不逆转历史
        self.assertIsNotNone(self.auth.identify(tok2))
        self.assertEqual(cancel_msgs(self.store, "s-w2"), [])
        self.assertEqual(self.gitx.removed, ["/envs/t1"])

    def test_empty_chain_idempotent_with_audit(self):  # Req: F11
        tid = self.dispatchable()
        self.svc.cancel(tid, self.root)  # todo 无链：置 cancelled
        self.assertEqual(self.state.get(tid)["state"], "cancelled")
        n = len(self.events())
        self.assertEqual(self.svc.cancel(tid, self.root)["cancelled"], 0)  # 幂等
        self.assertEqual(self.state.get(tid)["state"], "cancelled")
        self.assertEqual(len(self.events()), n + 1)  # 幂等仍留审计行

    def test_worker_token_cancel_rejected_and_audited(self):  # Req: F11
        tid, _, worker = self.dispatched_pair()
        with self.assertRaises(Rejected):
            self.svc.cancel(tid, worker)
        self.assertEqual(self.state.get(tid)["state"], "dispatched")
        self.assertTrue(any(e["event"] == "auth_denied" and e.get("task") == tid
                            for e in self.events()))

    def test_done_task_pends_and_children_still_cascade(self):  # Req: F11
        self.seed("t1", state="done", worker="s-w1", env="/envs/t1")
        self.seed("t2", disp="s-w1", worker="s-w2", env="/envs/t2")
        n = self.svc.cancel("t1", self.root)
        self.assertEqual(n["cancelled"], 1)  # done 只排队，下级照常级联
        self.assertTrue(self.state.get("t1").get("pending_cancel"))  # T8 消费
        self.assertEqual(self.state.get("t2")["state"], "cancelled")


class TestCancelBusyGate(TestCoreTask):
    """F28 防误杀忙碌闸：判例链三例误杀（worker 离线评审环中被抢先 cancel，
    env 回收连尾巴提交一起带走）。busy 信号＝health 僵态豁免同款（updated>idle）。"""

    def test_busy_rejected_without_force(self):  # Req: F28
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.oc.detail["s-w1"] = {"time": {"updated": 100.0, "idle": 50.0}}  # 回合进行中
        r = self.svc.cancel("t1", self.root)
        self.assertEqual(r["cancelled"], 0)
        self.assertEqual(r["reason"], "worker_busy")
        self.assertIn("force", r["hint"])  # 提示文案指路 force:true
        self.assertEqual(self.state.get("t1")["state"], "running")  # 行不动
        self.assertEqual(self.gitx.removed, [])  # env 不回收
        ev = [e for e in self.events() if e["event"] == "cancel_rejected_busy"]
        self.assertEqual([e.get("task") for e in ev], ["t1"])  # 专属审计行

    def test_busy_with_force_cancels_and_audits_forced(self):  # Req: F28
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.oc.detail["s-w1"] = {"time": {"updated": 100.0, "idle": 50.0}}
        r = self.svc.cancel("t1", self.root, force=True)
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual(self.state.get("t1")["state"], "cancelled")
        ev = [e for e in self.events() if e["event"] == "cancelled"
              and e.get("task") == "t1"]
        self.assertIn("forced", ev[-1]["note"])  # 强杀审计注明

    def test_idle_session_cancels_unchanged(self):  # Req: F28
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.oc.detail["s-w1"] = {"time": {"updated": 50.0, "idle": 100.0}}  # 闲置
        self.assertEqual(self.svc.cancel("t1", self.root)["cancelled"], 1)
        self.assertEqual(self.state.get("t1")["state"], "cancelled")  # 现行路径不变
        self.assertTrue(any(e["event"] == "cancelled" and e.get("task") == "t1"
                            for e in self.events()))
        self.assertEqual([e for e in self.events()
                          if e["event"] == "cancel_rejected_busy"], [])

    def test_dead_session_cancels_unchanged(self):  # Req: F28
        self.seed("t1", worker="s-w1", env="/envs/t1")  # FakeOc.detail 缺 sid＝会话死
        self.assertEqual(self.svc.cancel("t1", self.root)["cancelled"], 1)
        self.assertEqual(self.state.get("t1")["state"], "cancelled")

    def test_oc_probe_failure_fails_open_with_audit(self):  # Req: F28
        self.seed("t1", worker="s-w1", env="/envs/t1")
        self.oc.down = True  # oc 不可达：防 oc 故障卡死取消能力（fail-open）
        self.assertEqual(self.svc.cancel("t1", self.root)["cancelled"], 1)
        self.assertEqual(self.state.get("t1")["state"], "cancelled")
        ev = [e for e in self.events() if e["event"] == "cancelled"
              and e.get("task") == "t1"]
        self.assertIn("probe_failed", ev[-1]["note"])  # 放行留痕


class TestVerify(TestCoreTask):
    def test_worker_update_verify_rejected_and_audited(self):  # Req: F2
        tid, _, worker = self.dispatched_pair()
        with self.assertRaises(Rejected):
            self.svc.update_verify(tid, worker, "pytest -q")
        self.assertTrue(any(e["event"] == "auth_denied" and e.get("task") == tid
                            for e in self.events()))
        d2 = self.auth.issue("disp2", "dispatcher")
        with self.assertRaises(Rejected):  # R18：非该任务派发方亦拒
            self.svc.update_verify(tid, d2, "pytest -q")

    def test_empty_verify_cmd_rejected(self):  # Req: F2
        tid, disp, _ = self.dispatched_pair()
        with self.assertRaises(Rejected) as cm:
            self.svc.update_verify(tid, disp, "   ")
        self.assertIn("空", str(cm.exception))
        self.assertTrue(any(e["event"] == "dispatch_rejected"
                            and e.get("task") == tid for e in self.events()))
        self.assertEqual(self.state.get(tid)["verify"]["cmd"], "true")  # 原值未被空命令覆写

    def test_happy_path_post_then_register_locks(self):  # Req: F2
        tid, disp, worker = self.dispatched_pair()
        wsid, _, _ = self.oc.sessions[-1]
        self.router.post(wsid, ("up", tid), "verify",
                         "verify cmd: pytest tests/ -x")  # worker 勘察回填
        self.svc.update_verify(tid, disp, "pytest tests/ -x")
        v = self.state.get(tid)["verify"]
        self.assertEqual(v["state"], "registered")
        self.assertEqual(v["registered_by"], "disp1")
        self.assertGreater(v["registered_at"], 0)
        self.assertEqual(v["cmd"], "pytest tests/ -x")
        with self.assertRaises(Rejected):  # 登记即锁定：worker 改必拒
            self.svc.update_verify(tid, worker, "rm -rf /")


class TestVerifyGate(TestCoreTask):
    """F23 派发硬闸＋建单原子登记（gt-e103ae/gt-87f3c2/20261026-135627 同根因）。"""

    def test_create_with_verify_cmd_registers_atomically(self):  # Req: F23
        tid = self.svc.create("g", "/repo", None, self.root, verify_cmd="pytest -q")
        v = self.state.get(tid)["verify"]
        self.assertEqual(v["state"], "registered")  # 建单即登记：状态直接 registered
        self.assertEqual(v["cmd"], "pytest -q")
        self.assertEqual(v["registered_by"], "main")
        self.assertGreater(v["registered_at"], 0)
        evs = [e["event"] for e in self.events() if e.get("task") == tid]
        self.assertEqual(evs, ["task_created", "verify_registered"])  # 登记事件照落
        self.svc.dispatch(tid, self.root)  # 快速路：dispatch 一步放行
        self.assertEqual(self.state.get(tid)["state"], "dispatched")

    def test_create_rejects_empty_cmd_and_combo(self):  # Req: F23
        with self.assertRaises(Rejected) as cm:  # 空命令＝无质量闸，同 update 口径
            self.svc.create("g", "/repo", None, self.root, verify_cmd="  ")
        self.assertIn("空", str(cm.exception))
        with self.assertRaises(Rejected):  # 登记与豁免语义互斥
            self.svc.create("g", "/repo", None, self.root,
                            verify_cmd="true", verify_pending=True)

    def test_dispatch_without_verify_rejected_instant_with_audit(self):  # Req: F23
        tid = self.svc.create("g", "/repo", None, self.root)  # 无 verify 无旗
        with self.assertRaises(Rejected) as cm:
            self.svc.dispatch(tid, self.root)
        self.assertIn("verify 未登记", str(cm.exception))
        ev = [e for e in self.events()
              if e["event"] == "dispatch_rejected_verify_missing"]
        self.assertEqual(len(ev), 1)  # 专属审计行
        self.assertEqual(self.state.get(tid)["state"], "todo")  # 拒绝在派发瞬间：零副作用
        self.assertEqual(self.gitx.built, [])  # 未建环境
        self.assertEqual(self.oc.sessions, [])  # 未建会话未签凭据

    def test_incident_gt_87f3c2_shape_must_be_rejected(self):  # Req: F23
        """事故复刻证明：create 后漏 update 即 dispatch（两步舞断链）必被 403 拍回。"""
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp)  # 事故形态：少了 task_update
        with self.assertRaises(Rejected):
            self.svc.dispatch(tid, disp)
        self.assertTrue(any(e["event"] == "dispatch_rejected_verify_missing"
                            and e.get("task") == tid for e in self.events()))

    def test_verify_pending_exemption_keeps_exploration_path(self):  # Req: F23
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp, verify_pending=True)
        self.svc.dispatch(tid, disp)  # 豁免旗：未登记也可先派
        wsid, _, _ = self.oc.sessions[-1]
        self.router.post(wsid, ("up", tid), "verify", "pytest -q")  # worker 提案
        self.svc.update_verify(tid, disp, "pytest -q")  # 派发方登记照常
        self.assertEqual(self.state.get(tid)["verify"]["state"], "registered")



    def test_create_writes_task_page_and_dispatch_rerenders(self):  # Req: F2
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp, verify_cmd="true")
        page = self.tmp / "wiki" / "tasks" / f"{tid}.md"
        self.assertTrue(page.exists())  # create 产出任务页（create→page 接线）
        self.svc.dispatch(tid, disp)
        text = page.read_text(encoding="utf-8")
        self.assertIn("state: dispatched", text)  # 重渲反映当前态
        self.assertIn("disp1", text)  # 归属（dispatcher_sid）渲染

    def test_create_stores_target_branch(self):  # Req: gt-132747
        tid = self.svc.create("g", "/repo", None, self.root,
                              target_branch="issue/7")
        self.assertEqual(self.state.get(tid)["target_branch"], "issue/7")
        self.assertEqual(self.gitx.checked[-1], ("/repo", "issue/7"))  # 信任边界已校验
        tid2 = self.dispatchable()
        self.assertIsNone(self.state.get(tid2)["target_branch"])  # 缺省＝None

    def test_create_rejects_bad_target_branch(self):  # Req: gt-132747
        for bad in ("bad..name",):  # 存在性拒绝归真 gitx（test_access_gitx）
            with self.assertRaises(Rejected) as cm:
                self.svc.create("g", "/repo", None, self.root, target_branch=bad)
            self.assertIn("target_branch", str(cm.exception))  # 坏名 fail-fast 报因

    def test_dispatch_builds_env_from_target_branch(self):  # Req: gt-132747
        disp = self.auth.issue("disp1", "dispatcher")
        tid = self.svc.create("g", "/repo", None, disp,
                              target_branch="issue/7", verify_cmd="true")
        self.svc.dispatch(tid, disp)
        self.assertEqual(self.gitx.branches[-1], "issue/7")  # 起点分支透传
        tid2, disp2, _ = self.dispatched_pair()
        self.assertEqual(self.gitx.branches[-1], None)  # 缺省＝None（向后兼容）

    def test_create_seeds_queryable_row(self):  # Req: F2
        tid = self.svc.create("my goal", "/repo", None, self.root)  # 裸 create：缺省 pending
        t = self.state.snapshot()["tasks"][tid]
        self.assertEqual(t["state"], "todo")
        self.assertEqual(t["dispatcher_sid"], "main")  # 归属＝创建者会话
        self.assertEqual(t["verify"]["state"], "pending")
        self.assertFalse(t["verify_pending"])
        self.assertEqual(t["goal"], "my goal")
        self.assertEqual(t["chain_depth"], 1)

    def test_boot_carries_opening_and_shell_contract(self):  # Req: F13
        self.dispatched_pair()
        boot = self.oc.sessions[-1][2]
        self.assertIn("开工: 进 env 后首个动作 post(kind=note", boot)
        self.assertIn("shell: mcp_shell（shell_execute）按 argv 数组执行", boot)
        self.assertIn("ALLOW_COMMANDS", boot)

    def test_redispatch_revokes_old_token(self):  # Req: F3
        tid, disp, old = self.dispatched_pair()
        for _ in range(3):
            self.state.fail_streak_bump(tid)  # 连败→blocked（重派唯一合法来态）
        new = self.svc.dispatch(tid, disp)
        self.assertIsNone(self.auth.identify(old))  # 旧牌即废
        self.assertEqual(self.auth.identify(new)[1], "worker")
        self.assertEqual(self.state.get(tid)["state"], "dispatched")
        self.assertEqual(len(self.gitx.built), 1)  # 一任务一环境：重派不重建
        self.assertEqual(self.gitx.removed, [])


if __name__ == "__main__":
    unittest.main()
