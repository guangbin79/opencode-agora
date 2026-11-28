"""Req: F137 — v3 交付执行体（opencode-agora v3 P3-T9b）。

断言清单（任务页 ag-18d819，逐条机械判定）：1 交付前复验（推进目标支前
复跑集成 verify——复用 GateVerifyService 执行面；集成红保持 reopened 质量
口径绝不带病推进）；2 ff-only 线性历史（合并仅 ff-only；干净 non-ff 竞争按
attempt 预算换基重排重验 ≤3 次；耗尽→retry_exhausted→done→blocked 专用边
不计 fail_streak 不弹 worker）；3 rebase 冲突（内容冲突终局 failed/
rebase_conflict＋conflict_files；同基假冲突 F105 最新基重建再 rebase 恰
一次 attempt 不变；同基禁盲试；基不可得径直终局）；4 managed 本地 CAS
（advance 前置 expected-old；他写者推进=non_ff 重试；脏/占用=local_target_
dirty/busy 终局；绝不 force/stash/换支/清洗）；5 幻影守卫（推进后终检
contains 才 merged；G1 空集 rebase；崩溃残留已含直收口/未含重排恰一次绝不
重复推进）；6 external_branch（绿后 awaiting_external 驻留不合不推不回收；
task_deliver delivered→merged→closed 全链＋回收；failed→blocked；non_ff
祖先预判 diverged/协议矛盾拒零改写；stale 拒）；7 out_of_band（done body
作交付引用、零 Git 交付动作、直接收口）；8 quick 包络（F65/F67 合并前
merge-base 累计 diff 超阈值按 verify 红既有路径退回）；9 干净检查（主检出
脏终局分类；全根级未跟踪隐藏整目录自动入册同轮重试 F97；混合脏面零部分
豁免）；10 交付轮残值（重派/新轮/确认面 reset conflict_files/attempt/
base/error）；11 崩溃恢复（failed+done 只补 blocked 绝不自动重试；
awaiting_external 残留直通；恢复探针零网络；FaultInjector 崩溃窗零半态）；
12 架构守卫（test_architecture.py 分层锁＋SQL 圈禁自动覆盖；gitx/runner
调用点必在 UoW 外——桩内逐调用断言 _depth）。

gitx/锚/runner 按桩约定注入；消息面接真 MessagingService；streak/deliver
确认面接真 TaskService（task_deliver 单源复用的活证）。
"""
import unittest

from agorad import domain
from agorad.contracts import ContractError
from agorad.domain import (Credential, DeliveryState, VerifySpec,
                           dispatch_secret_key)
from agorad.gatedelivery import (GateDeliveryService, _append_roster,
                                 _auto_ignore_patterns, dirty_face_detail,
                                 dirty_face_warning)
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from tests import v3_harness as harness

DISPATCHER, WORKER = "ses-d1", "ses-w1"
BRANCH, TIP, C, E = "v3-runtime", "b" * 40, "c" * 40, "e" * 40
TIP2, R = "2" * 40, "9" * 40
# 断言4 安全面：交付循环可触 gitx 方法白名单（无 force/clean/stash/checkout 面）
# ag-9a680b 增补两只读取数面（dirty 三形细分：porcelain 脏面＋碰撞 diff）
SAFE_OPS = {"contains", "is_ancestor", "head", "local_branch_sha",
            "build_integration_at", "merge_ff", "rebase_onto",
            "materialize_checkout", "advance_local_target", "delivery_lock",
            "remove_integration", "remove_env", "status_records",
            "diff_paths"}


class LockStub:
    def __init__(self):
        self.available = True
        self.acquires = 0

    def acquire(self, blocking=False):
        if self.available:
            self.acquires += 1
            return True
        return False

    def release(self):
        pass


class GitxDeliveryStub:
    """gitx 交付桩：tip/可达集/祖先对可预置；ff/rebase/advance 结果按调用序
    脚本化；逐调用断言身处 UoW 外（断言12）。"""

    def __init__(self, storage=None):
        self.storage = storage
        self.tip = TIP                     # 本地钉死支 tip（他写者可推进）
        self.contained = set()             # {(commit, branch)}
        self.ancestors = set()             # {(ancestor, descendant)}
        self.merge_ff_result = "ok"
        self.rebase_script = []            # 逐次弹出："ok"|"conflict"
        self.rebase_head = None            # rebase 后 integration 头（None＝不动）
        self.rebase_files = ["src/x.py"]   # conflict 时的未合并路径清单
        self.advance_script = []           # 逐次弹出："synced"|"non-ff"|"dirty"|"failed"
        self.sync_marks_contained = True   # synced 是否置可达集（G2 幻影用）
        self.status_records_value = []
        self.diff_paths_value = []         # ag-9a680b 碰撞面注入（base..deliver）
        self.roster_applied = []
        self.diff = (0, 0, "")
        self.lock = LockStub()
        self.fail_local_sha = None
        self.on_rebase = None              # rebase 前副作用钩（模拟基漂移）
        self.calls = []
        self.removed_envs = set()
        self._gate_head = None

    def _ok(self):
        if self.storage is not None and self.storage._depth:
            raise AssertionError("gitx 调用点身处 UoW 写事务内（§4 禁子进程）")

    def _rec(self, name, *a):
        self._ok()
        self.calls.append((name, *a))

    def contains(self, repo, commit, branch=None):
        self._rec("contains", commit, branch or BRANCH)
        return (commit, branch or BRANCH) in self.contained

    def is_ancestor(self, repo, base, commit):
        self._rec("is_ancestor", base, commit)
        return (base, commit) in self.ancestors

    def head(self, repo, branch=None):
        self._rec("head", repo, branch)
        if branch is not None:
            return self.tip
        if str(repo).endswith("__gate"):
            if self._gate_head is None:
                self._gate_head = self.tip
            return self._gate_head
        return C  # env HEAD＝done sha（锚三检一致口径）

    def symref(self, repo):
        return BRANCH

    def local_branch_sha(self, repo_dir, branch):
        self._rec("local_branch_sha", repo_dir, branch)
        if self.fail_local_sha is not None:
            raise self.fail_local_sha
        return self.tip

    def build_integration(self, target, integration, branch=None):
        self._rec("build_integration", integration, branch)
        self._gate_head = self.tip

    def build_integration_at(self, target, integration, base_sha=None):
        self._rec("build_integration_at", integration, base_sha)
        self._gate_head = base_sha or self.tip

    def merge_ff(self, env_dir, target, branch=None):
        self._rec("merge_ff", env_dir, target)
        if self.merge_ff_result == "ok":
            self._gate_head = C  # ff 直通＝env 头
        return self.merge_ff_result

    def rebase_onto(self, env_dir, integration, branch=None, conflicts=None):
        self._rec("rebase_onto", env_dir, integration)
        if self.on_rebase is not None:
            self.on_rebase()
        st = self.rebase_script.pop(0) if self.rebase_script else "ok"
        if st == "conflict":
            if conflicts is not None:
                conflicts.extend(self.rebase_files)
            return "conflict"
        if self.rebase_head is not None:
            self._gate_head = self.rebase_head
        return st

    def materialize_checkout(self, tree, source):
        self._rec("materialize_checkout", tree, source)
        return ""

    def advance_local_target(self, repo_dir, branch, sha, expected_old):
        self._rec("advance_local_target", repo_dir, branch, sha, expected_old)
        st = self.advance_script.pop(0) if self.advance_script else "synced"
        if st == "synced":
            if self.sync_marks_contained:
                self.contained.add((sha, branch))
            self.tip = sha
        return st

    def delivery_lock(self, repo_dir):
        self._rec("delivery_lock", repo_dir)
        return self.lock

    def diff_stats(self, env_dir, branch=None):
        self._rec("diff_stats", env_dir, branch)
        return self.diff

    def status_records(self, repo_dir):
        self._rec("status_records", repo_dir)
        return list(self.status_records_value)

    def diff_paths(self, repo_dir, a, b):
        self._rec("diff_paths", repo_dir, a, b)
        return list(self.diff_paths_value)

    def apply_ignore_roster(self, repo_dir, patterns):
        self._rec("apply_ignore_roster", repo_dir, list(patterns))
        self.roster_applied.extend(patterns)

    def env_on_disk(self, env_dir):
        return True

    def remove_integration(self, integration, target):
        self._rec("remove_integration", integration)

    def remove_env(self, env_dir):
        self._rec("remove_env", env_dir)
        self.removed_envs.add(env_dir)

    def ops(self):
        return {c[0] for c in self.calls}


class RunnerStub:
    """runner 桩：按调用序脚本化 (rc, out)；调用点必在 UoW 外（断言12）。"""

    def __init__(self, script=(), storage=None):
        self.script = list(script)
        self.calls = []
        self.storage = storage

    def __call__(self, env_dir, cmd, *, timeout=None, ceiling=None,
                 on_soft=None):
        if self.storage is not None and self.storage._depth:
            raise AssertionError("runner 调用点身处 UoW 写事务内（§4 禁子进程）")
        self.calls.append({"env": env_dir, "cmd": cmd, "timeout": timeout,
                           "ceiling": ceiling})
        return self.script.pop(0) if self.script else (0, "")

    def integration_cmds(self):
        return [c for c in self.calls if "AGORA_INTEGRATION_BASE" in c["cmd"]]


class OcStub:
    def __init__(self):
        self.injects = []

    def create(self, env, boot, title=None, model=None):
        return WORKER

    def inject(self, sid, text, model=None):
        self.injects.append(("wake", sid, text))
        return True

    def inject_silent(self, sid, text):
        self.injects.append(("silent", sid, text))
        return True

    def inject_notify(self, sid, text):
        self.injects.append(("notify", sid, text))
        return True


class FakeClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


def _delivery(mode="managed", **over):
    d = DeliveryState(mode=mode, target_branch=BRANCH)
    return domain.dataclasses.replace(d, **over) if over else d


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
        self.gitx = GitxDeliveryStub(storage=self.storage)
        self.oc = OcStub()
        self.messaging = MessagingService(
            self.storage, self.oc, home=str(self.home.path),
            clock=self.clock, injector=self.injector)
        self.tasksvc = TaskService(
            self.storage, self.gitx, self.oc, messaging=self.messaging,
            home=str(self.home.path), clock=self.clock)
        self.runner = RunnerStub(storage=self.storage)
        self.gate = GateDeliveryService(
            self.storage, self.gitx, self.runner, messaging=self.messaging,
            tasksvc=self.tasksvc,
            anchors=lambda env: self._anchor_rows.get(env),
            artifacts=None, home=str(self.home.path),
            clock=self.clock, injector=self.injector, verify_timeout=300,
            lock_wait_s=0)
        self._anchor_rows = {}

    def set_anchor(self, tid, head=C, base=E, clean=True):
        if head is not None:
            self._anchor_rows[f"/tmp/envs/{tid}"] = {"head": head,
                                                     "base": base,
                                                     "clean": clean}

    def seed(self, tid, state="done", delivery="managed", deliverable=None,
             delivery_extra=None, **over):
        dl = {"managed": _delivery(),
              "external": _delivery(mode="external_branch"),
              "none": None}[delivery]
        if dl is not None and delivery_extra:
            dl = domain.dataclasses.replace(dl, **delivery_extra)
        row = dict(id=tid, state=state, title=tid, goal="g",
                   project_dir="/repo", target_branch=BRANCH,
                   env_dir=f"/tmp/envs/{tid}", env_base=E,
                   dispatcher_sid=DISPATCHER, root_sid=DISPATCHER,
                   worker_sid=WORKER, chain_depth=1, created_ms=1,
                   updated_ms=1, commit=C,
                   verify=VerifySpec(cmd="true", state="registered",
                                     registered_by=DISPATCHER,
                                     registered_at_ms=1),
                   deliverable=deliverable, delivery=dl)
        row.update(over)
        task = domain.Task(**row)
        with self.storage.uow():
            self.storage.upsert_task(task)
        return tid

    def green_script(self, probe_rc=1, n=1):
        """全绿交付一轮：探针红→env 绿→交付循环集成绿×n 次。"""
        self.runner.script = ([(probe_rc, "base-out")] if probe_rc is not None
                              else []) + [(0, "env-ok")] + [(0, "int-ok")] * n

    def events(self, tid):
        return [e.event for e in self.storage.events_for_task(tid)]

    def messages(self, sid):
        return self.storage.messages_for_sid(sid)

    @staticmethod
    def text(m):
        return (m.body.decode("utf-8", "replace")
                if isinstance(m.body, (bytes, bytearray)) else str(m.body))

    def put_credential(self, tid):
        self.home.write_credential(tid, "tok")
        with self.storage.uow():
            self.storage.put_credential(Credential(
                sid=WORKER, token="tok", task_id=tid))

    def requeue(self, tid, commit=C):
        """reopened→running→accept_done 再入门（worker 续修路）。"""
        self.tasksvc.transition(tid, "running", actor=WORKER)
        self.set_anchor(tid, head=commit)
        self.gitx.contained.discard((commit, BRANCH))
        return self.gate.accept_done(tid, WORKER, commit)

    def advance_args(self):
        return [(c[3], c[4]) for c in self.gitx.calls
                if c[0] == "advance_local_target"]


class TestManagedGreen(_Base):  # 断言1/2/4
    def test_green_advances_after_integration_rerun(self):
        tid = self.seed("ag-m1")
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit), ("closed", C))
        d = t.delivery
        self.assertEqual((d.status, d.local_status, d.commit, d.base,
                          d.attempt, d.error, d.conflict_files),
                         ("delivered", "synced", C, TIP, 1, None, None))
        self.assertEqual(t.fail_streak, 0)  # 全绿清连败
        # 断言1：推进目标支前复跑集成 verify（runner 序先于 advance）
        self.assertEqual(len(self.runner.integration_cmds()), 1)
        self.assertEqual(self.advance_args(), [(C, TIP)])  # ff 直通＋expected-old
        self.assertIn("merged", self.events(tid))
        self.assertIn("closed", self.events(tid))
        self.assertTrue(any("local=synced" in self.text(m)
                            for m in self.messages(DISPATCHER)))

    def test_green_revokes_credential_and_reclaims_env(self):
        tid = self.seed("ag-m2")
        self.put_credential(tid)
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertIsNone(self.storage.get_credential(WORKER))
        self.assertFalse(self.home.credentials.joinpath(f"{tid}.token").exists())
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)

    def test_green_revokes_per_dispatch_staging_secret_too(self):
        # Req: F152——门侧终局撤牌双键清理（gateverify 与 tasksvc 同款）：
        # per-dispatch staging 文件随撤牌同清，不留死牌明文至下次 sweep
        tid = self.seed("ag-m2b")
        self.put_credential(tid)
        stg = self.home.write_credential(
            dispatch_secret_key(tid, "tok")[:-len(".token")], "tok")
        self.assertTrue(stg.is_file())
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertIsNone(self.storage.get_credential(WORKER))
        self.assertFalse(stg.exists())  # staging 键位随撤牌同清
        self.assertFalse(self.home.credentials.joinpath(f"{tid}.token").exists())

    def test_integration_red_keeps_quality_reopened(self):
        tid = self.seed("ag-m3")
        self.runner.script = [(1, ""), (0, ""), (1, "INT-FAIL")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("reopened", 1))
        self.assertEqual(t.delivery.status, "pending")  # 零交付面改写
        self.assertEqual(self.advance_args(), [])  # 绝不带病推进
        self.assertTrue(any("集成 verify 失败" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_non_ff_retries_then_exhausts_to_blocked(self):
        tid = self.seed("ag-m4")
        self.green_script(n=3)
        self.set_anchor(tid)
        self.gitx.advance_script = ["non-ff", "non-ff", "non-ff"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((t.state, d.status, d.error, d.attempt),
                         ("blocked", "failed", "retry_exhausted", 3))
        self.assertEqual(t.fail_streak, 0)  # 专用边不计连败
        self.assertEqual([m for m in self.messages(WORKER)
                          if m.kind == "process"], [])  # 不弹 worker
        self.assertTrue(any("交付失败（retry_exhausted）" in self.text(m)
                            for m in self.messages(DISPATCHER)))
        ev = self.events(tid)
        self.assertEqual(ev.count("delivery_nonff"), 3)
        self.assertEqual(ev.count("delivery_failed"), 1)
        self.assertEqual(len(self.runner.integration_cmds()), 3)  # 每次重验
        self.assertEqual(self.advance_args(), [(C, TIP)] * 3)  # expected-old 前置

    def test_non_ff_recovers_on_third_attempt(self):
        tid = self.seed("ag-m5")
        self.green_script(n=3)
        self.set_anchor(tid)
        self.gitx.advance_script = ["non-ff", "non-ff"]
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertEqual(self.storage.get_task(tid).delivery.attempt, 3)

    def test_writer_advance_between_verify_and_publish_retries(self):
        tid = self.seed("ag-m6")
        self.set_anchor(tid)
        self.runner.script = [(1, ""), (0, ""), (0, ""), (0, ""), (0, "")]
        self.runner.storage = self.storage
        orig = self.runner.__call__

        def guarded(env, cmd, **kw):  # 首次集成 verify 后他写者推进本地 tip
            r = orig(env, cmd, **kw)
            if "AGORA_INTEGRATION_BASE" in cmd and self.gitx.tip == TIP:
                self.gitx.tip = TIP2
            return r
        self.gate._runner = guarded
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertIn("delivery_nonff", self.events(tid))  # 锁内重读落空
        self.assertEqual(self.advance_args(), [(C, TIP2)])  # 重试以新基 CAS

    def test_dirty_and_busy_terminal(self):
        tid = self.seed("ag-m7")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error, t.fail_streak),
                         ("blocked", "local_target_dirty", 0))
        self.assertNotIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # env 留现场
        tid = self.seed("ag-m8")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["failed"]
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_target_busy")

    # ---- ag-9a680b：local_target_dirty 三形细分（umbrella reason 不变） ----

    def _run_dirty_face(self, records, incoming):
        """脏面三形公共夹具：advance 拒 dirty → blocked 终局＋明细 note。"""
        tid = self.seed("ag-df")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.gitx.status_records_value = list(records)
        self.gitx.diff_paths_value = list(incoming)
        self.assertEqual(self.gate.run(tid), "blocked")
        return tid

    def _delivery_failed_note(self, tid):
        return [e.detail["note"] for e in self.storage.events_for_task(tid)
                if e.event == "delivery_failed"][0]

    def test_dirty_untracked_only_disposition(self):  # (a)
        tid = self._run_dirty_face(
            ["?? linux-arm64-out/", "?? .req-gate/ag-x.md"],
            ["src/agorad/a.py", "tests/test_a.py"])
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error, t.fail_streak),
                         ("blocked", "local_target_dirty", 0))
        self.assertEqual(self.advance_args(), [(C, TIP)])  # 无整链重试
        body = "\n".join(self.text(m) for m in self.messages(DISPATCHER))
        self.assertIn("untracked_only", body)
        self.assertIn("linux-arm64-out/", body)   # 路径清单入通知面
        self.assertIn(".req-gate/ag-x.md", body)
        self.assertIn("safe disposition: append paths to .git/info/exclude "
                      "(local-only ignore) or mv them out (byte-preserving "
                      "quarantine) - never rm; tracked changes always require "
                      "human adjudication", body)  # 处置句完整不被 200 帽截断
        self.assertIn("untracked_only", self._delivery_failed_note(tid))

    def test_dirty_tracked_requires_human_adjudication(self):  # (b)
        # 记录用真 porcelain 形状（' M '＝unstaged modified，3 字符前缀）
        tid = self._run_dirty_face([" M src/a.py", "?? out/"], [])
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error),
                         ("blocked", "local_target_dirty"))
        body = "\n".join(self.text(m) for m in self.messages(DISPATCHER))
        self.assertIn("tracked_dirty", body)
        self.assertIn("requires human adjudication", body)
        self.assertIn("src/a.py", body)  # 冒犯路径清单
        self.assertIn("另有 1 条未跟踪路径随行: out/", body)  # 混合面一并点名

    def test_dirty_untracked_collision_never_non_ff(self):  # (c)
        tid = self._run_dirty_face(
            ["?? linux-arm64-out/build.o"],
            ["linux-arm64-out/build.o", "src/agorad/a.py"])
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error),
                         ("blocked", "local_target_dirty"))  # 撞面归 dirty 家族
        self.assertEqual(self.advance_args(), [(C, TIP)])  # 恰一次：绝不 non-ff 整链重试
        self.assertNotIn("delivery_nonff", self.events(tid))  # gt-fc6fb2 缺陷面
        body = "\n".join(self.text(m) for m in self.messages(DISPATCHER))
        self.assertIn("untracked_collision", body)
        self.assertIn("linux-arm64-out/build.o", body)  # git 拒并文件名入明细
        self.assertIn("would be overwritten by merge", body)
        self.assertIn("never rm", body)

    def test_dirty_face_probe_sick_degrades_formless(self):
        # 取数病（真件 RuntimeError）：reason 不变、明细如实降级（形未知不
        # 猜），绝不逆转终局——warn/detail 面 fail-open 只降明细不降拒因
        tid = self.seed("ag-dfp")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        orig = self.gitx.status_records

        def boom(repo_dir):
            raise RuntimeError("porcelain down")
        self.gitx.status_records = boom
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_target_dirty")
        self.assertIn("unknown", self._delivery_failed_note(tid))
        self.assertIn("dirty_face_probe_failed", self.events(tid))
        self.gitx.status_records = orig  # 复原防泄漏后续用例

    def test_dirty_empty_records_names_other_worktree(self):
        # 主检出 porcelain 全净而 advance 报 dirty（脏面在持有支的其他
        # worktree）——如实降级不猜形，绝不按零路径伪装 untracked_only
        tid = self._run_dirty_face([], [])
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_target_dirty")
        self.assertIn("unknown", self._delivery_failed_note(tid))

    def test_git_surface_whitelisted(self):
        tid = self.seed("ag-m9")
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertLessEqual(self.gitx.ops(), SAFE_OPS)  # 无 force/清洗/换支面

    def test_managed_without_branch_terminal(self):
        tid = self.seed("ag-m10", target_branch=None,
                        delivery_extra=dict(target_branch=None))
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_base")


class TestRebaseConflict(_Base):  # 断言3
    def _conflict_setup(self, tid):
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"

    def _rebase_calls(self):
        return [c for c in self.gitx.calls if c[0] == "rebase_onto"]

    def test_same_base_conflict_terminal_with_files(self):
        tid = self.seed("ag-c1")
        self._conflict_setup(tid)
        self.gitx.rebase_script = ["conflict"]  # tip 未动＝同基
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((d.status, d.error, d.conflict_files, d.attempt),
                         ("failed", "rebase_conflict", ["src/x.py"], 1))
        self.assertEqual(len(self._rebase_calls()), 1)  # 同基禁盲试
        self.assertIn("rebase_conflict_same_base", self.events(tid))
        self.assertTrue(any("冲突文件: src/x.py" in self.text(m)
                            for m in self.messages(DISPATCHER)))

    def test_false_conflict_heals_once_attempt_unchanged(self):
        tid = self.seed("ag-c2")
        self._conflict_setup(tid)
        self.gitx.rebase_script = ["conflict", "ok"]
        self.gitx.rebase_head = R
        self.gitx.on_rebase = lambda: setattr(self.gitx, "tip", TIP2)
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit, t.rebased_from),
                         ("closed", R, C))  # 重放先落账
        d = t.delivery
        self.assertEqual((d.status, d.attempt, d.base, d.commit),
                         ("delivered", 1, TIP2, R))  # attempt 不变（F105）
        ev = self.events(tid)
        self.assertIn("rebase_conflict_retry", ev)
        self.assertEqual(len(self._rebase_calls()), 2)  # 重建后恰一次
        self.assertEqual(self.advance_args(), [(R, TIP2)])

    def test_healed_round_second_conflict_terminal(self):
        tid = self.seed("ag-c3")
        self._conflict_setup(tid)
        self.gitx.rebase_script = ["conflict", "conflict"]
        self.gitx.on_rebase = lambda: setattr(self.gitx, "tip", TIP2)
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.delivery.status, t.delivery.error,
                          t.delivery.conflict_files),
                         ("failed", "rebase_conflict", ["src/x.py"]))
        self.assertEqual(len(self._rebase_calls()), 2)

    def test_base_unavailable_on_refresh_terminal(self):
        tid = self.seed("ag-c4")
        self._conflict_setup(tid)
        self.gitx.rebase_script = ["conflict"]
        self.gitx.on_rebase = lambda: setattr(
            self.gitx, "fail_local_sha", RuntimeError("ref gone"))
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.delivery.status, t.delivery.error),
                         ("failed", "rebase_conflict"))
        self.assertIn("rebase_conflict_base_unavailable", self.events(tid))


class TestPhantomGuard(_Base):  # 断言5
    def test_g1_empty_rebase_bounces_without_advance(self):
        tid = self.seed("ag-g1")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_script = ["ok"]  # 重放零增量：head 仍在基位
        self.gitx.rebase_head = None      # _gate_head 不动＝r==before
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("reopened", 1))
        self.assertIn("empty_rebase_noop", self.events(tid))
        self.assertEqual(self.advance_args(), [])
        self.assertTrue(any("无新增提交" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_g2_synced_without_contains_blocks_merge(self):
        tid = self.seed("ag-g2")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.sync_marks_contained = False  # advance 假成功：目标支未含
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status),
                         ("reopened", "pending"))  # 绝不 merged
        self.assertIn("phantom_merge_blocked", self.events(tid))
        self.assertTrue(any("无新增提交" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_g2_probe_sick_is_gate_crash_not_quality(self):
        # 探针病如实上抛走门内异常面 reopened（非质量败不冤 worker 计连败）
        tid = self.seed("ag-g4")
        self.green_script()
        self.set_anchor(tid)
        orig_contains = self.gitx.contains

        def sick_contains(repo, commit, branch=None):
            if commit == C and branch == BRANCH:
                raise RuntimeError("git sick")
            return orig_contains(repo, commit, branch)
        self.gitx.contains = sick_contains
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak, t.delivery.status),
                         ("reopened", 0, "pending"))
        self.assertNotIn("phantom_merge_blocked", self.events(tid))

    def test_external_landed_locally_still_hands_off(self):
        # external 权威在外部派发方——本地支已含 commit 也不走 landed 直收
        tid = self.seed("ag-g5", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.contained.add((C, BRANCH))  # 本地支已含（如外部已推回）
        self.assertEqual(self.gate.run(tid), "awaiting_external")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status), ("done",
                                                        "awaiting_external"))

    def test_crash_after_advance_settles_exactly_once(self):
        tid = self.seed("ag-g3")
        self.green_script()
        self.set_anchor(tid)
        self.put_credential(tid)
        self.injector.arm("publish", "after")  # advance 已落地、落账前崩溃
        self.assertEqual(self.gate.run(tid), "reopened")  # 门内崩弹回（捕获面）
        self.assertEqual(self.storage.get_task(tid).state, "reopened")
        self.assertEqual(len(self.advance_args()), 1)
        self.injector.disarm("publish")
        # 崩溃残留：目标支已含落账 commit——重受理由 vacuous 放行（prev==C），
        # 重排后 landed 探针经 _finish_delivered 直收口（投影一并落账——
        # 基类 _land 不写投影，假 pending 残值不得泄漏）
        self.tasksvc.transition(tid, "running", actor=WORKER)
        self.set_anchor(tid, head=C)
        self.assertEqual(self.gate.accept_done(tid, WORKER, C), "accepted")
        self.runner.script = []  # landed 直收：零重验
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")
        d = t.delivery
        self.assertEqual((d.status, d.local_status, d.commit),
                         ("delivered", "synced", C))  # 投影终态一致
        self.assertEqual(len(self.advance_args()), 1)  # 绝不重复推进


class TestExternal(_Base):  # 断言6
    def test_green_hands_off_awaiting_external(self):
        tid = self.seed("ag-e1", delivery="external")
        self.put_credential(tid)
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "awaiting_external")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "done")  # 驻留：不合不推
        d = t.delivery
        self.assertEqual((d.status, d.commit, d.base, d.attempt,
                          d.local_status, d.error, d.conflict_files),
                         ("awaiting_external", C, TIP, 1, "not_required",
                          None, None))
        self.assertEqual(self.advance_args(), [])  # 零目标支动作
        self.assertNotIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # env 保留
        self.assertIsNotNone(self.storage.get_credential(WORKER))  # 凭据保留
        self.assertIn("awaiting_external", self.events(tid))
        self.assertTrue(any("handoff 就绪" in self.text(m)
                            for m in self.messages(DISPATCHER)))

    def test_non_ff_diverged_terminal_without_verify_burn(self):
        tid = self.seed("ag-e2", delivery="external")
        self.runner.script = [(1, ""), (0, "")]
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((t.state, d.status, d.error),
                         ("blocked", "failed", "diverged"))
        self.assertEqual(self.runner.integration_cmds(), [])  # 不烧集成 verify
        self.assertFalse([c for c in self.gitx.calls
                          if c[0] == "rebase_onto"])  # ff-only：零 rebase
        self.assertEqual([m for m in self.messages(WORKER)
                          if m.kind == "process"], [])

    def test_integration_red_is_quality_reopened(self):
        tid = self.seed("ag-e3", delivery="external")
        self.runner.script = [(1, ""), (0, ""), (1, "INT-FAIL")]
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "reopened")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("reopened", 1))

    def test_deliver_confirmation_full_chain_and_reclaim(self):
        tid = self.seed("ag-e4", delivery="external")
        self.put_credential(tid)
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        res = self.tasksvc.deliver(tid, DISPATCHER, "dispatcher",
                                   "delivered", C)
        self.assertEqual(res, {"ok": True, "outcome": "delivered"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status,
                          t.delivery.local_status, t.commit),
                         ("closed", "delivered", "not_required", C))
        self.assertIsNone(self.storage.get_credential(WORKER))  # 回收
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)

    def test_deliver_failed_and_non_ff_buckets(self):
        tid = self.seed("ag-e5", delivery="external",
                        delivery_extra=dict(commit=C))
        # 经真 handoff 建立 awaiting 态
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        self.assertEqual(
            self.tasksvc.deliver(tid, DISPATCHER, "dispatcher", "failed", C),
            {"ok": True, "outcome": "failed",
             "error": "external_delivery_failed"})
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status,
                          t.delivery.conflict_files),
                         ("blocked", "failed", None))  # 残值清
        tid = self.seed("ag-e6", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        res = self.tasksvc.deliver(tid, DISPATCHER, "dispatcher", "non_ff",
                                   C, base="d" * 40)
        self.assertEqual((res["outcome"], res["error"]),
                         ("failed", "diverged"))  # 非祖先＝结构性 diverged
        self.assertEqual(self.storage.get_task(tid).state, "blocked")

    def test_stale_commit_rejected_zero_rewrite(self):
        tid = self.seed("ag-e7", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        with self.assertRaises(ContractError) as cm:
            self.tasksvc.deliver(tid, DISPATCHER, "dispatcher",
                                 "delivered", "f" * 40)
        self.assertEqual(cm.exception.code, "conflict")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status), ("done",
                                                        "awaiting_external"))

    def test_awaiting_residue_run_skip_and_recover_untouched(self):
        tid = self.seed("ag-e8", delivery="external")
        self.green_script()
        self.set_anchor(tid)
        self.gate.run(tid)
        calls = len(self.runner.calls)
        ev = len(self.events(tid))
        self.assertEqual(self.gate.run(tid), "skip")  # 残留直通不重验
        self.assertEqual(len(self.runner.calls), calls)
        # Req: F144 skip 路径同占道＋门闩＋门入口通知审计（v2 run 同位）——
        # 判据＝零重验（runner 零新调用）＋零重复 handoff 落账
        self.assertGreater(len(self.events(tid)), ev)
        self.assertEqual(self.events(tid).count("awaiting_external"), 1)
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status),
                         ("done", "awaiting_external"))  # 恢复面零接触
        self.assertEqual(self.events(tid).count("awaiting_external"), 1)

    def test_missing_target_branch_terminal(self):
        tid = self.seed("ag-e9", delivery="external", target_branch=None,
                        delivery_extra=dict(target_branch=None))
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.delivery.error,), ("local_base",))
        self.assertTrue(any("不回退仓默认支" in self.text(m)
                            for m in self.messages(DISPATCHER)))


class TestOutOfBand(_Base):  # 断言7
    def seed_oob(self, tid, state="running", **over):
        return self.seed(tid, state=state, delivery="none",
                         deliverable="out_of_band", commit=None, **over)

    def test_accept_ref_body_then_close_without_git_delivery(self):
        tid = self.seed_oob("ag-o1")
        self.set_anchor(tid, head=None)  # 无锚要求
        self.assertEqual(self.gate.accept_done(tid, WORKER,
                                               "docs/report.md #结论"),
                         "accepted")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.commit), ("done", None))  # 零提交
        self.runner.script = [(1, "base"), (0, "env")]
        self.assertEqual(self.gate.run(tid), "merged")
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")
        ev = self.events(tid)
        self.assertIn("merged", ev)
        detail = [e.detail for e in self.storage.events_for_task(tid)
                  if e.event == "done"][0]
        self.assertIn("docs/report.md", detail["note"])  # 引用留痕
        ops = self.gitx.ops()
        self.assertNotIn("advance_local_target", ops)  # 零 Git 交付动作
        self.assertNotIn("merge_ff", ops)
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)  # 环境回收

    def test_verify_red_bounces(self):
        tid = self.seed_oob("ag-o2", state="done")
        self.runner.script = [(1, ""), (1, "FAIL")]
        self.assertEqual(self.gate.run(tid), "verify_failed")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("verify_failed", 1))

    def test_accept_reject_faces(self):
        tid = self.seed_oob("ag-o3")
        r = self.gate.accept_done(tid, WORKER, "   ")
        self.assertEqual(r.code, "body-format")
        tid = self.seed_oob("ag-o4",
                            verify=VerifySpec(state="pending"))
        self.assertEqual(self.gate.accept_done(tid, WORKER,
                                               "ref").code,
                         "verify-unregistered")
        tid = self.seed_oob("ag-o5")
        self.seed("ag-o5-kid", state="dispatched", dispatcher_sid=WORKER)
        self.assertEqual(self.gate.accept_done(tid, WORKER, "ref").code,
                         "children")


class TestQuickEnvelope(_Base):  # 断言8
    def test_over_threshold_bounces_like_verify_red(self):
        tid = self.seed("ag-q1", quick=True)
        self.runner.script = [(0, "env")]  # quick 探针豁免
        self.set_anchor(tid)
        self.gitx.diff = (9, 3, "9 files changed")
        self.assertEqual(self.gate.run(tid), "verify_failed")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.fail_streak), ("verify_failed", 1))
        self.assertEqual(self.advance_args(), [])  # 合并前退回
        self.assertTrue(any("quick envelope exceeded" in self.text(m)
                            for m in self.messages(WORKER)))

    def test_under_threshold_proceeds(self):
        tid = self.seed("ag-q2", quick=True)
        self.runner.script = [(0, "env"), (0, "int")]
        self.set_anchor(tid)
        self.gitx.diff = (1, 1, "")
        self.assertEqual(self.gate.run(tid), "merged")


class TestCleanCheck(_Base):  # 断言9
    def _roster(self):
        path = self.home.path / "ignore.roster"
        self.gate._roster_path = str(path)
        return path

    def test_all_hidden_untracked_dirs_autoenrolled_same_attempt(self):
        tid = self.seed("ag-s1")
        path = self._roster()
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty", "synced"]
        self.gitx.status_records_value = ["?? .cache/", "?? .omo/"]
        self.assertEqual(self.gate.run(tid), "merged")
        self.assertEqual(path.read_text(), ".cache/\n.omo/\n")  # 中央名册入册
        self.assertEqual(self.gitx.roster_applied, [".cache/", ".omo/"])
        ev = self.events(tid)
        self.assertIn("auto_ignore", ev)
        self.assertEqual(self.storage.get_task(tid).delivery.attempt, 1)
        self.assertEqual(self.advance_args(), [(C, TIP), (C, TIP)])  # 同轮重试

    def test_hidden_dir_face_colliding_not_exempted(self):
        # ag-9a680b 评审环 R2（A1/B1）：撞面禁 F97 豁免——exclude 后合并对
        # ignored 路径径直覆写＝静默丢字节；撞面走 terminal collision 明细
        tid = self.seed("ag-s1c")
        path = self._roster()
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.gitx.status_records_value = ["?? .cache/"]
        self.gitx.diff_paths_value = [".cache/model.bin", "src/a.py"]
        self.assertEqual(self.gate.run(tid), "blocked")  # 不入册不同轮重试
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error),
                         ("blocked", "local_target_dirty"))
        self.assertFalse(path.exists())  # 零豁免：名册未写
        self.assertEqual(self.gitx.roster_applied, [])
        self.assertEqual(len(self.advance_args()), 1)  # 无同轮重试
        self.assertIn("auto_ignore_skipped_collision", self.events(tid))
        body = "\n".join(self.text(m) for m in self.messages(DISPATCHER))
        self.assertIn("untracked_collision", body)  # 撞面明细指导 mv 出
        self.assertIn("never rm", body)

    def test_hidden_dir_face_collision_probe_sick_skips_exemption(self):
        # 碰撞取数病＝fail-closed：宁拦勿覆写（豁免跳过、留审计、terminal 收口）
        tid = self.seed("ag-s1d")
        self._roster()
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.gitx.status_records_value = ["?? .cache/"]
        orig = self.gitx.diff_paths

        def boom(repo_dir, a, b):
            raise RuntimeError("diff down")
        self.gitx.diff_paths = boom
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.gitx.roster_applied, [])  # 零豁免
        self.assertIn("auto_ignore_collision_probe_failed", self.events(tid))
        body = "\n".join(self.text(m) for m in self.messages(DISPATCHER))
        self.assertIn("碰撞面取数失败", body)  # 降级甄别提示在场
        self.assertIn("禁 exclude", body)
        self.gitx.diff_paths = orig  # 复原防泄漏后续用例

    def test_mixed_dirty_face_no_partial_exemption(self):
        tid = self.seed("ag-s2")
        path = self._roster()
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.gitx.status_records_value = ["?? .cache/", "M src/a.py"]
        self.assertEqual(self.gate.run(tid), "blocked")
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.error),
                         ("blocked", "local_target_dirty"))
        self.assertFalse(path.exists())  # 零部分豁免：不入册
        self.assertEqual(self.gitx.roster_applied, [])
        self.assertEqual(len(self.advance_args()), 1)

    def test_dirty_without_roster_face_terminal(self):
        tid = self.seed("ag-s3")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.advance_script = ["dirty"]
        self.gitx.status_records_value = ["?? .cache/"]
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "local_target_dirty")


class TestRoundResidue(_Base):  # 断言10
    def test_redispatch_resets_round_fields(self):
        tid = self.seed("ag-r1", state="blocked",
                        delivery_extra=dict(status="failed", attempt=2,
                                            base=TIP, error="rebase_conflict",
                                            commit=C,
                                            conflict_files=["a.py"]))
        self.tasksvc.dispatch(tid, DISPATCHER, "dispatcher")
        t = self.storage.get_task(tid)
        d = t.delivery
        self.assertEqual((t.state, d.status, d.attempt, d.commit, d.base,
                          d.error, d.conflict_files, d.local_status),
                         ("dispatched", "pending", 0, None, None, None, None,
                          "pending"))

    def test_handoff_clears_prior_round_residue(self):
        tid = self.seed("ag-r2", delivery="external",
                        delivery_extra=dict(base="x" * 40, error="prev",
                                            conflict_files=["old.py"]))
        self.green_script()
        self.set_anchor(tid)
        self.assertEqual(self.gate.run(tid), "awaiting_external")
        d = self.storage.get_task(tid).delivery
        self.assertEqual((d.attempt, d.base, d.error, d.conflict_files),
                         (1, TIP, None, None))  # 上轮终局不泄漏

    def test_delivery_fail_records_and_confirm_faces_clear(self):
        tid = self.seed("ag-r3", delivery="external",
                        delivery_extra=dict(status="awaiting_external",
                                            commit=C,
                                            conflict_files=["stale.py"]))
        res = self.tasksvc.deliver(tid, DISPATCHER, "dispatcher",
                                   "failed", C)
        self.assertEqual(res["outcome"], "failed")
        self.assertIsNone(self.storage.get_task(tid).delivery.conflict_files)


class TestCrashRecovery(_Base):  # 断言11
    def test_failed_done_patches_blocked_never_retries(self):
        tid = self.seed("ag-w1",
                        delivery_extra=dict(status="failed", attempt=2,
                                            error="rebase_conflict"))
        self.put_credential(tid)
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "blocked")  # 只补迁移
        self.assertEqual(t.delivery.status, "failed")  # 投影不动
        self.assertEqual(self.gitx.ops() & {"advance_local_target",
                                            "merge_ff"}, set())  # 绝不自动重试
        self.assertEqual(self.runner.calls, [])  # 不烧 verify
        self.assertIn("delivery_failed_recovery", self.events(tid))
        ev = len(self.events(tid))
        self.gate.recover()  # 幂等：终局行零接触
        self.assertEqual(len(self.events(tid)), ev)

    def test_run_entry_patches_managed_failed_done(self):
        tid = self.seed("ag-w2",
                        delivery_extra=dict(status="failed", error="diverged"))
        self.assertEqual(self.gate.run(tid), "blocked")
        self.assertEqual(self.runner.calls, [])
        self.assertIn("delivery_failed_recovery", self.events(tid))

    def test_pending_done_landed_finishes_with_local_probe_only(self):
        tid = self.seed("ag-w3",
                        delivery_extra=dict(status="pending", attempt=2,
                                            base=TIP))
        self.put_credential(tid)
        self.gitx.contained.add((C, BRANCH))
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual(t.state, "closed")
        d = t.delivery
        self.assertEqual((d.status, d.local_status, d.attempt, d.base),
                         ("delivered", "synced", 2, TIP))  # 在案基线保真
        self.assertIn("delivery_recovered", self.events(tid))
        # 恢复探针零网络：仅本地 contains/is_ancestor（无 fetch/push 面）
        probes = self.gitx.ops() & {"contains", "is_ancestor"}
        self.assertEqual(probes, {"contains"})
        self.assertNotIn("advance_local_target", self.gitx.ops())  # 零推进
        self.assertIsNone(self.storage.get_credential(WORKER))
        self.assertIn(f"/tmp/envs/{tid}", self.gitx.removed_envs)

    def test_pending_done_not_landed_left_for_gate_requeue(self):
        tid = self.seed("ag-w4", delivery_extra=dict(status="pending"))
        self.gate.recover()
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status), ("done", "pending"))
        self.assertNotIn("delivery_recovered", self.events(tid))

    def test_missing_commit_and_legacy_untouched(self):
        tid = self.seed("ag-w5", commit=None,
                        delivery_extra=dict(status="pending"))
        self.gate.recover()
        self.assertIn("delivery_recover_missing_commit", self.events(tid))
        legacy = self.seed("ag-w6", delivery="none")  # legacy 无投影行零接触
        n = len(self.events(legacy))
        self.gate.recover()
        self.assertEqual(len(self.events(legacy)), n)

    def test_probe_failure_audited_and_skipped(self):
        tid = self.seed("ag-w7", delivery_extra=dict(status="pending"))
        self.gitx.fail_local_sha = None
        orig = self.gitx.contains

        def boom(repo, commit, branch=None):
            raise RuntimeError("git sick")
        self.gitx.contains = boom
        self.gate.recover()
        self.assertIn("delivery_recover_probe_failed", self.events(tid))
        self.assertEqual(self.storage.get_task(tid).state, "done")
        self.gitx.contains = orig

    def test_uow_crash_delivery_fail_zero_half_state(self):
        tid = self.seed("ag-w8")
        self.green_script()
        self.set_anchor(tid)
        self.gitx.merge_ff_result = "non-ff"
        self.gitx.rebase_script = ["conflict"]
        # 武装点＝rebase 冲突返回处：终局序列（审计＋投影＋迁移）任一 UoW
        # 崩溃都须零半态——同点重放完整收敛
        self.gitx.on_rebase = lambda: self.injector.arm("uow", "after")
        # 双跳崩溃：终局 UoW 回滚后，except 面补 reopened 的 UoW 同点再崩
        # ——FaultInjected 逃出门体，交付投影零半态（pending 原样）
        with self.assertRaises(harness.FaultInjected):
            self.gate.run(tid)
        t = self.storage.get_task(tid)
        self.assertEqual((t.state, t.delivery.status), ("done", "pending"))
        self.assertNotIn("delivery_failed", self.events(tid))
        self.assertNotIn("rebase_conflict_same_base", self.events(tid))
        self.injector.disarm("uow")
        self.gitx.on_rebase = None
        self.runner.script = [(0, ""), (0, "int")]  # 探针缓存命中
        self.gitx.rebase_script = ["conflict"]  # 重演同冲突
        self.assertEqual(self.gate.run(tid), "blocked")  # 重放终局完整
        self.assertEqual(self.storage.get_task(tid).delivery.error,
                         "rebase_conflict")

    def test_publish_crash_before_advance_then_full_cycle(self):
        tid = self.seed("ag-w9")
        self.green_script()
        self.set_anchor(tid)
        self.injector.arm("publish", "before")
        self.assertEqual(self.gate.run(tid), "reopened")  # 门内崩弹回（捕获面）
        self.assertEqual(self.storage.get_task(tid).state, "reopened")
        self.assertEqual(self.advance_args(), [])  # 推进前崩：目标支未动
        self.injector.disarm("publish")
        self.assertEqual(self.requeue(tid), "accepted")
        self.runner.script = [(0, ""), (0, "int")]
        self.assertEqual(self.gate.run(tid), "merged")  # 完整续跑一轮交付
        self.assertEqual(len(self.advance_args()), 1)


class TestPureHelpers(unittest.TestCase):  # 断言9/3 纯函数单源
    def test_auto_ignore_patterns_faces(self):
        self.assertEqual(_auto_ignore_patterns(["?? .cache/", "?? .omo/"]),
                         [".cache/", ".omo/"])
        self.assertIsNone(_auto_ignore_patterns(["M a.py"]))  # 跟踪改动
        self.assertIsNone(_auto_ignore_patterns(["?? visible/"]))  # 非隐藏
        self.assertIsNone(_auto_ignore_patterns(["?? .root_file"]))  # 散文件
        self.assertIsNone(_auto_ignore_patterns(["?? .nested/dir/"]))  # 嵌套
        self.assertIsNone(_auto_ignore_patterns(["?? .tmp*/"]))  # glob 元字符
        self.assertIsNone(_auto_ignore_patterns([]))  # 空脏面不豁免

    def test_append_roster_idempotent(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "roster"
            _append_roster(p, [".a/", ".b/"])
            _append_roster(p, [".b/", ".c/"])  # 幂等＋防粘连
            self.assertEqual(p.read_text(), ".a/\n.b/\n.c/\n")
            _append_roster(p, [".c/"])
            self.assertEqual(p.read_text(), ".a/\n.b/\n.c/\n")

    # ---- ag-9a680b：三形细分纯函数（断言4/9 扩面） ----

    def test_dirty_face_detail_untracked_only(self):
        note = dirty_face_detail(["?? out/", "?? .req-gate/x.md"],
                                 ["src/a.py"])
        self.assertIn("untracked_only", note)
        self.assertIn("out/", note)
        self.assertIn(".req-gate/x.md", note)
        self.assertIn("safe disposition: append paths to .git/info/exclude "
                      "(local-only ignore) or mv them out (byte-preserving "
                      "quarantine) - never rm; tracked changes always require "
                      "human adjudication", note)

    def test_dirty_face_detail_tracked(self):
        # 记录用真 porcelain 形状（XY<space><path>：' M'=unstaged、'M '=staged）
        note = dirty_face_detail([" M a.py", "M  b.py", "?? out/"], [])
        self.assertIn("tracked_dirty", note)
        self.assertIn("requires human adjudication", note)
        self.assertIn("a.py", note)
        self.assertIn("b.py", note)
        self.assertNotIn("safe disposition", note)  # 处置句专属 untracked_only 形

    def test_dirty_face_detail_collision(self):
        # 目录折叠前缀判撞：?? dir/ 撞 incoming dir/x；零撞不误报
        note = dirty_face_detail(["?? dir/", "?? loose.bin"],
                                 ["dir/x.py", "dir/sub/y.py"])
        self.assertIn("untracked_collision", note)
        self.assertIn("dir/", note)
        self.assertIn("would be overwritten by merge", note)
        self.assertIn("never rm", note)
        self.assertEqual(dirty_face_detail(["?? a"], ["b"]),  # 零撞＝纯未跟踪
                         dirty_face_detail(["?? a"], []))

    def test_dirty_face_detail_cap_20_lines(self):
        note = dirty_face_detail([f"?? u{i}" for i in range(30)], [])
        self.assertIn("u19\n", note)   # 前 20 行在
        self.assertNotIn("u29", note)  # 超帽截断
        self.assertIn("共 30 条，仅列前 20 行", note)

    def test_dirty_face_detail_disposition_precedes_paths(self):
        # 评审环 R2（OCR#1-3/#5）：安全指引先行——20 条长路径不把处置句/
        # 「禁 exclude」挤出 note 帽口（截断面恒保全关键指引）
        longs = [f"very/deep/agent/build/tree/p{i}/linux-arm64-out.bin"
                 for i in range(20)]
        note = dirty_face_detail([f"?? {p}" for p in longs], [])
        self.assertLess(note.index("safe disposition"),
                        note.index(longs[0]))  # 处置句先于路径清单
        note = dirty_face_detail([f"?? {p}" for p in longs], [longs[0]])
        self.assertIn("untracked_collision", note)
        self.assertLess(note.index("disposition: mv them out"),
                        note.index(longs[0]))  # 撞面处置句先于撞面清单

    def test_dirty_face_detail_empty_records_unknown(self):
        self.assertIn("unknown", dirty_face_detail([], []))

    def test_dirty_face_warning_line(self):
        w = dirty_face_warning(["?? out/", " M a.py"])
        self.assertIn("target repo is dirty", w)
        self.assertIn("tracked_dirty", w)
        self.assertIn("1 tracked + 1 untracked", w)
        self.assertIn("a.py", w)
        self.assertEqual(dirty_face_warning([]), "")  # 全净零警行


class TestConflictFilesRoundtrip(_Base):  # 断言10：投影残值持久化
    def test_conflict_files_persist_through_storage(self):
        tid = self.seed("ag-f1", state="blocked",
                        delivery_extra=dict(
                            status="failed", error="rebase_conflict",
                            attempt=1, conflict_files=["a.py", "b.py"]))
        d = self.storage.get_task(tid).delivery
        self.assertEqual(d.conflict_files, ["a.py", "b.py"])


class TestSchemaUpgrade(unittest.TestCase):  # 评审环 R1 B1：v3→v4 补列迁移
    def test_old_v3_db_upgrades_in_place(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "agora.db"
            raw = sqlite3.connect(db)  # 旧 v3 库形状：task_delivery 无新列
            raw.execute(
                "CREATE TABLE task_delivery(task_id TEXT PRIMARY KEY,"
                " mode TEXT, status TEXT, commit_sha TEXT, base TEXT,"
                " target_branch TEXT, attempt INTEGER, local_status TEXT,"
                " error TEXT)")
            raw.execute("PRAGMA user_version=3")
            raw.commit()
            raw.close()
            from agorad.storage import Storage
            st = Storage(db)  # 迁移：ALTER 补列＋版本推进
            self.addCleanup(st.close)
            cols = {r[1] for r in st.open_reader().execute(
                "PRAGMA table_info(task_delivery)")}
            self.assertIn("conflict_files_json", cols)
            self.assertIn("delivered_at_ms", cols)  # Req: F144 v5 迁移
            self.assertEqual(st.open_reader().execute(
                "PRAGMA user_version").fetchone()[0], 7)  # Req: F145 v6/F184 v7
            with st.uow():
                st.upsert_task(domain.Task(
                    id="ag-u1", state="done",
                    delivery=DeliveryState(mode="managed",
                                           target_branch="b",
                                           conflict_files=["x.py"])))
            self.assertEqual(
                st.get_task("ag-u1").delivery.conflict_files, ["x.py"])


class TestAccessStatusRecords(unittest.TestCase):  # 评审环 R1 C1：取数面真件
    def test_records_shape_on_real_repo(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from agorad.access.gitx import Gitx
        with tempfile.TemporaryDirectory() as d:
            def g(*a):
                subprocess.run(["git", *a], cwd=d, check=True,
                               capture_output=True)
            g("init", "-q")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            (Path(d) / "f.txt").write_text("x", encoding="utf-8")
            g("add", "-A")
            g("commit", "-qm", "init")
            (Path(d) / ".cache").mkdir()
            (Path(d) / ".cache" / "x.bin").write_bytes(b"1")
            (Path(d) / "a.py").write_text("y", encoding="utf-8")
            self.assertEqual(sorted(Gitx().status_records(d)),
                             ["?? .cache/", "?? a.py"])


class TestAdvanceCollisionRouting(unittest.TestCase):
    """ag-9a680b（gt-fc6fb2 缺陷）：真 git 件验证「untracked working tree
    files would be overwritten by merge」输出路由 dirty 家族——此前该输出落
    advance 终端 failed（busy 家族），v2 前时代更被混报 non-ff 整链盲试。"""

    def _repo(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)

        def g(*a):
            subprocess.run(["git", *a], cwd=d, check=True,
                           capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (Path(d) / "f.txt").write_text("x", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "base")
        g("checkout", "-qb", "side")
        (Path(d) / "colliding.txt").write_text("new", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "side")
        g("checkout", "-q", "main")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        tip = subprocess.run(["git", "rev-parse", "side"], cwd=d,
                             capture_output=True, text=True).stdout.strip()
        return d, base, tip

    def test_merge_output_collision_routes_dirty(self):
        # 竞窗模拟：porcelain 前置检查通过后、merge 前未跟踪产物落盘（构建
        # 进程并发写输出——gt-fc6fb2/ag-9a680b 事故形态）；merge 拒绝文案
        # 「untracked working tree files would be overwritten by merge」
        # 须归 dirty（交付门三形细分），绝不 failed/busy、绝不 non-ff
        from pathlib import Path
        from agorad.access.gitx import Gitx
        d, base, tip = self._repo()

        class _RaceGitx(Gitx):
            def _git(self, args, cwd=None, check=True, timeout=None,
                     input_data=None):
                if args[:2] == ["merge", "--ff-only"]:
                    (Path(cwd) / "colliding.txt").write_text(
                        "junk", encoding="utf-8")  # 竞窗注入
                return super()._git(args, cwd, check=check, timeout=timeout,
                                    input_data=input_data)

        self.assertEqual(
            _RaceGitx().advance_local_target(d, "main", tip, base), "dirty")

    def test_porcelain_precheck_routes_dirty(self):
        # 非竞窗常规面：未跟踪产物在前置 porcelain 即检出（dirty 家族不变量）
        from pathlib import Path
        from agorad.access.gitx import Gitx
        d, base, tip = self._repo()
        (Path(d) / "colliding.txt").write_text("junk", encoding="utf-8")
        self.assertEqual(
            Gitx().advance_local_target(d, "main", tip, base), "dirty")

    def test_clean_repo_advances_synced(self):
        # 不变量：干净仓照常 ff 推进（本改动零接触成功路径）
        from agorad.access.gitx import Gitx
        d, base, tip = self._repo()
        self.assertEqual(
            Gitx().advance_local_target(d, "main", tip, base), "synced")

    def test_diff_paths_incoming_face(self):
        # 碰撞面取数：base..deliver diff 路径＝合并将写入的内容面
        from agorad.access.gitx import Gitx
        d, base, tip = self._repo()
        self.assertEqual(Gitx().diff_paths(d, base, tip), ["colliding.txt"])

    def test_diff_paths_raw_unquoted_non_ascii(self):
        # 评审环 R2（OCR）：-z raw 路径与 porcelain -z 同口径——无 -z 时
        # core.quotePath 会 C 引号化非 ASCII 路径（"\346\225\260..."），与
        # raw porcelain 路径永不相等——撞面被静默降级 zero-collision
        import subprocess as sp
        import tempfile
        from pathlib import Path
        from agorad.access.gitx import Gitx
        name = "数据构建产物.bin"
        with tempfile.TemporaryDirectory() as d:
            def g(*a):
                sp.run(["git", *a], cwd=d, check=True,
                       capture_output=True)
            g("init", "-q", "-b", "main")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            (Path(d) / "f.txt").write_text("x", encoding="utf-8")
            g("add", "-A")
            g("commit", "-qm", "base")
            g("checkout", "-qb", "side")
            (Path(d) / name).write_text("new", encoding="utf-8")
            g("add", "-A")
            g("commit", "-qm", "side")
            g("checkout", "-q", "main")
            base = sp.run(["git", "rev-parse", "HEAD"], cwd=d,
                          capture_output=True, text=True).stdout.strip()
            tip = sp.run(["git", "rev-parse", "side"], cwd=d,
                         capture_output=True, text=True).stdout.strip()
            gitx = Gitx()
            self.assertEqual(gitx.diff_paths(d, base, tip), [name])
            (Path(d) / name).write_text("junk", encoding="utf-8")
            records = gitx.status_records(d)  # porcelain -z raw 面
            self.assertEqual([r[3:] for r in records], [name])
            self.assertIn(name, gitx.diff_paths(d, base, tip))  # 撞面可判


if __name__ == "__main__":
    unittest.main()
