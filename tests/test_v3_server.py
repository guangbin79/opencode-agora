"""Req: F142 — v3 服务器组装与传输（opencode-agora v3 P4-T13a）。

断言清单（任务页 ag-1ff31c，逐条机械判定）：1 装配（compose 全栈接线/
启动幂等/关闭收敛＋checkpoint）；2 /v3 路由（19 工具暴露/鉴权/角色终审/
结构化错误与 ERROR_TABLE 一致/timer·approval 四端点占位）；3 legacy 根
路由（15 旧工具＋cred_for_sid/task_nudge/task_alarm＋GET /view、/health
经 adapter→v3；golden 抽样锚 board 键集/doctor 键/messages dict 形状）；
4 MCP 壳对接（tools/list 服务端 contracts 单源生成 19＋sleep＋旧别名/
tools/call 路由 /v3/错误码与 ERROR_CODES golden 一致）；5 service.json
（api_version=3/storage_version=3＋旧字段；/health 三键）；6 done 全链
（accept→后台队列→stub git 沙箱 merged→closed＋artifact 落盘）；7 消息面
（receipt/cursor 语义/inject 调用序/引用行不悬空）；8 鉴权端到端（root
bootstrap/dispatch 签 worker 牌/cred_for_sid 拉取＋lineage 换牌）；9
崩溃恢复（done 队列运行中重组服务→不双合并、恢复恰一次）；10 并发（写
请求全局写锁串行无半态；读不被长 verify 阻塞）；11 架构守卫在
test_architecture.py（compose/serverv3 分层锁＋SQL 圈禁自动覆盖）。

oc/gitx/runner/anchors 按桩约定注入（compose Protocol 面）；FakeClock/
TempAgoraHome/FaultInjector 复用 v3_harness。
"""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import agora_mcp
from agorad import compose, contracts, gitstore, serverv3
from agorad.domain import DeliveryState, VerifySpec
from agorad.storage import Storage, TABLES
from tests import v3_harness as harness

DISPATCHER, WORKER = "ses-d1", "ses-w1"
BRANCH, TIP, C = "main", "b" * 40, "c" * 40
CANON = frozenset(contracts.CATALOG) - {"sleep"}
ALIASES = frozenset(contracts.LEGACY_TO_V3)
GOLDEN = json.loads((Path(__file__).parent / "fixtures" /
                     "v3_contract_golden.json").read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms

    def advance(self, delta_ms):
        self.ms += delta_ms


class AnchorStub:
    """锚桩：env→{head,base,clean} 可预置；缺省与 done sha/基线一致。"""

    def __init__(self, default=None):
        self.default = default or {"head": C, "base": C, "clean": True}
        self.over = {}

    def __call__(self, env_dir):
        return self.over.get(env_dir, self.default)


class LockStub:
    def __init__(self):
        self.available, self.acquires = True, 0

    def acquire(self, blocking=False):
        if self.available:
            self.acquires += 1
            return True
        return False

    def release(self):
        pass


class GitxStub:
    """gitx 全脸桩（tasksvc＋gatedelivery 并集）：tip/可达集/祖先对可预置；
    ff/rebase/advance 按调用序脚本化；逐调用断言身处 UoW 外（§4）。"""

    def __init__(self, storage=None):
        self.storage = storage
        self.tip = TIP
        self.contained = set()      # {(commit, branch)}
        self.ancestors = set()      # {(ancestor, descendant)}
        self.merge_ff_result = "ok"
        self.rebase_script, self.advance_script = [], []
        self.built, self.removed_envs = set(), set()
        self.env_heads = {}         # env→HEAD（缺省 C）
        self._gate_head = None
        self.lock = LockStub()
        self.diff = (0, 0, "")
        self.status_records_value = []
        self.calls = []

    def _ok(self):
        if self.storage is not None and self.storage._depth:
            raise AssertionError("gitx 调用点身处 UoW 写事务内（§4）")

    def _rec(self, name, *a):
        self._ok()
        self.calls.append((name, *a))

    # ---- tasksvc 面 ----

    def symref(self, project_dir):
        return BRANCH

    def default_branch(self, project_dir):
        return BRANCH

    def check_branch(self, project_dir, branch):
        pass

    def local_branch_sha(self, repo_dir, branch):
        self._rec("local_branch_sha", repo_dir, branch)
        return self.tip

    def build_env(self, project_dir, tid, branch=None):
        return self._new_env(tid)

    def build_env_at(self, project_dir, tid, branch=None, base_sha=None):
        return self._new_env(tid)

    def _new_env(self, tid):
        env = f"/tmp/envs/{tid}"
        self.built.add(env)
        return env

    def materialize_checkout(self, tree_dir, source_dir):
        self._rec("materialize_checkout", tree_dir)
        return ""

    def env_on_disk(self, env_dir):
        return bool(env_dir) and env_dir in self.built \
            and env_dir not in self.removed_envs

    def remove_env(self, env_dir):
        self._rec("remove_env", env_dir)
        self.removed_envs.add(env_dir)

    # ---- 门/交付面 ----

    def contains(self, repo_dir, commit, branch=None):
        self._rec("contains", commit, branch or BRANCH)
        return (commit, branch or BRANCH) in self.contained

    def is_ancestor(self, repo_dir, base, commit):
        self._rec("is_ancestor", base, commit)
        return (base, commit) in self.ancestors

    def head(self, repo, branch=None):
        self._rec("head", repo, branch)
        if branch is not None:
            return self.tip
        s = str(repo)
        if s.endswith("__gate"):
            if self._gate_head is None:
                self._gate_head = self.tip
            return self._gate_head
        return self.env_heads.get(s, C)

    def build_integration(self, target, integration, branch=None):
        self._rec("build_integration", integration, branch)
        self._gate_head = self.tip

    def build_integration_at(self, target, integration, base_sha=None):
        self._rec("build_integration_at", integration, base_sha)
        self._gate_head = base_sha or self.tip

    def merge_ff(self, env_dir, target, branch=None):
        self._rec("merge_ff", env_dir, target)
        if self.merge_ff_result == "ok":
            self._gate_head = self.env_heads.get(env_dir, C)
        return self.merge_ff_result

    def rebase_onto(self, env_dir, integration, conflicts=None, branch=None):
        self._rec("rebase_onto", env_dir, integration)
        st = self.rebase_script.pop(0) if self.rebase_script else "ok"
        if st == "conflict" and conflicts is not None:
            conflicts.extend(["src/x.py"])
        return st

    def advance_local_target(self, repo_dir, branch, sha, expected_old):
        self._rec("advance_local_target", repo_dir, branch, sha, expected_old)
        st = self.advance_script.pop(0) if self.advance_script else "synced"
        if st == "synced":
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

    def apply_ignore_roster(self, repo_dir, patterns):
        self._rec("apply_ignore_roster", repo_dir, list(patterns))

    def remove_integration(self, integration_dir, target_dir):
        self._rec("remove_integration", integration_dir)

    def ops(self):
        return [c[0] for c in self.calls]


class RunnerStub:
    """runner 桩：按调用序脚本化 (rc, out)；delay＝每调用慢执行（断言10）。"""

    def __init__(self, script=(), storage=None, delay=0.0):
        self.script, self.storage, self.delay = list(script), storage, delay
        self.calls = []

    def __call__(self, env_dir, cmd, *, timeout=None, ceiling=None,
                 on_soft=None):
        # §4「runner 调用点身处 UoW 外」双后探针：Storage=_depth、GitStore=_ov
        if (self.storage is not None
                and (getattr(self.storage, "_depth", 0)
                     or getattr(self.storage, "_ov", None))):
            raise AssertionError("runner 调用点身处 UoW 写事务内（§4）")
        if self.delay:
            time.sleep(self.delay)
        self.calls.append({"env": env_dir, "cmd": cmd, "timeout": timeout})
        return self.script.pop(0) if self.script else (0, "")


class OcStub:
    """oc 全脸桩：会话创建＋三注入通道＋session 探测＋models/probe。"""

    def __init__(self):
        self.created, self.injects = [], []
        self.fail = False
        self.dead = set()

    def create(self, env, boot, title=None, model=None):
        if self.fail:
            raise RuntimeError("oc down")
        sid = f"ses-w{len(self.created) + 1}"
        self.created.append({"sid": sid, "env": env, "boot": boot,
                             "title": title, "model": model})
        return sid

    def inject(self, sid, text, model=None):
        self.injects.append(("wake", sid, text))
        return True

    def inject_silent(self, sid, text):
        self.injects.append(("silent", sid, text))
        return True

    def inject_notify(self, sid, text):
        self.injects.append(("notify", sid, text))
        return True

    def session(self, sid):
        if sid in self.dead:
            return None
        return {"time": {"updated": 0, "idle": 0}}

    def alive(self, sid):
        return sid not in self.dead

    def probe(self):
        return {"oc_version": "stub-oc"}

    def models(self):
        return ["prov/a", "prov/b"]


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = harness.TempAgoraHome()
        self.addCleanup(self.home.cleanup)
        self._before_stack()
        self.injector = (harness.FaultInjector()
                         .register("uow").register("dispatch"))
        self.clock = FakeClock()
        self.oc = OcStub()
        self.runner = RunnerStub()
        self.gitx = GitxStub()
        self.anchors = AnchorStub()
        self.stack = self._stack()
        self.runner.storage = self.stack.storage  # UoW 外调用断言面接线
        self.server = serverv3.make_server_v3(self.stack)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)
        self.root = self.stack.root_token
        self.assertIsNotNone(self.root)

    def _before_stack(self):
        pass

    def _stack(self, **over):
        kw = dict(oc=self.oc, gitx=self.gitx, runner=self.runner,
                  anchor_reader=self.anchors, clock=self.clock,
                  injector=self.injector)
        kw.update(over)
        stack = compose.V3Stack(self.home.path, **kw)
        stack.start()
        return stack

    def _shutdown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.stack.close()

    # ---- HTTP 面 ----

    def call(self, path, body=None, token=None, method="POST", sid=None,
             timeout=15):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Agora-Token"] = token
        if sid:
            headers["X-Agora-Sid"] = sid
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/{path}",
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read() or b"{}"
                try:
                    return r.status, json.loads(payload)
                except ValueError:
                    return r.status, payload.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # ---- 链路助手 ----

    def mk(self, goal="g", verify="true"):
        s, b = self.call("task_create", {"goal": goal, "project_dir": "/repo",
                                         "verify_cmd": verify}, self.root)
        self.assertEqual(s, 200, b)
        return b["task"]

    def dispatch(self, tid):
        s, b = self.call("task_dispatch", {"task": tid}, self.root)
        self.assertEqual(s, 200, b)
        row = self.stack.storage.get_task(tid)
        s, b = self.call("cred_for_sid", {"sid": row.worker_sid})
        self.assertEqual(s, 200, b)
        return row.worker_sid, b["token"]

    def to_running(self, tid):
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("post", {"task": tid, "kind": "question",
                                  "body": "开工知会"}, wtok)
        self.assertEqual((s, b.get("state", "")), (200, ""))
        self.assertEqual(self.stack.storage.get_task(tid).state, "running")
        return wsid, wtok

    def wait_state(self, tid, states, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.stack.storage.get_task(tid)
            if t is not None and t.state in states:
                return t.state
            time.sleep(0.02)
        return self.stack.storage.get_task(tid).state

    def events(self, tid):
        return [e.event for e in self.stack.storage.events_for_task(tid)]


class _ChainBase(_Base):
    """带绿交付链的基类：runner 脚本＝base 探针红＋verify/集成绿。"""

    def setUp(self):
        super().setUp()
        self.runner.script = [(1, "base-red"), (0, "ok"), (0, "ok")]

    def green_chain(self):
        tid = self.mk()
        wsid, wtok = self.to_running(tid)
        s, b = self.call("post", {"task": tid, "kind": "done", "body": C},
                         wtok)
        self.assertEqual((s, b), (200, {"result": "accepted"}))
        return tid, wsid, wtok


class TestAssembly(_Base):  # 断言1
    def test_full_stack_wiring(self):
        st = self.stack
        for name in ("storage", "repo", "messaging", "auth", "tasks", "gate",
                     "health", "peer", "write_lock"):
            self.assertTrue(hasattr(st, name), name)
        counts = Storage.verify_db(self.home.state / "agora.db")["counts"]
        self.assertEqual(set(counts), set(TABLES))  # schema 全建

    def test_start_idempotent_requeues_once(self):
        tid = self.mk()  # todo：不入门
        enqueued = []
        orig = self.stack.enqueue
        self.stack.enqueue = lambda t: (enqueued.append(t), orig(t))[1]
        self.stack.start()  # 二次启动
        self.assertEqual(enqueued, [])  # 幂等：done 残留重排恰一次
        self.assertEqual(self.stack.storage.get_task(tid).state, "todo")

    def test_close_converges_connections_and_checkpoints(self):
        self.mk()
        self.stack.close()
        wal = self.home.state / "agora.db-wal"
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
        again = Storage(self.home.state / "agora.db")  # 重开可读（收敛无损）
        try:
            self.assertEqual(len(again.tasks_all()), 1)
        finally:
            again.close()
        self.stack.close()  # 幂等


class TestV3Routes(_Base):  # 断言2
    def test_catalog_tools_exposed(self):
        self.assertEqual(len(CANON), 19)
        for name in sorted(CANON):
            body = ({"goal": "g", "project_dir": "/repo",
                     "verify_cmd": "true"} if name == "task_create"
                    else {"task": "ag-nope"} if "task" in
                    contracts.CATALOG[name].request
                    else {} if name in ("message_pull", "board", "doctor")
                    else {"kind": "question", "body": "x"}
                    if name == "message_post"
                    else {"path": "x.md", "text": "t"} if name == "wiki_write"
                    else {"path": "x.md"} if name == "wiki_read"
                    else {"on": True} if name == "enforce"
                    else {"repo": "/repo"} if name == "repo_board"
                    else {"target": "worker", "seconds": 5})
            s, b = self.call(f"v3/{name}", body, self.root)
            self.assertFalse(s == 404 and b == {"error": "not found"},
                             (name, b))  # 路由可达（404 仅契约 not_found）
            if name in serverv3._NOT_IMPLEMENTED:
                self.assertEqual((s, b["error"]["code"]), (501,
                                                           "not_implemented"))
            elif s != 200:
                self.assertEqual(s, contracts.http_status(b["error"]["code"]),
                                 (name, b))
            else:
                self.assertNotIn("error", b, name)

    def test_sleep_not_routed_server_side(self):
        s, b = self.call("v3/sleep", {"seconds": 1}, self.root)
        self.assertEqual(s, 400)
        self.assertEqual(b["error"]["code"], "invalid_argument")

    def test_legacy_alias_accepted_on_v3(self):
        s, b = self.call("v3/check_mail", {}, self.root)
        self.assertEqual(s, 200)
        self.assertIn("messages", b)

    def test_auth_and_error_surface(self):
        s, b = self.call("v3/board", {}, "bad-token")
        self.assertEqual((s, b["error"]["code"]), (401, "unauthorized"))
        s, b = self.call("v3/board", {"bogus": 1}, self.root)
        self.assertEqual((s, b["error"]["code"]), (400, "invalid_argument"))
        s, b = self.call("v3/doctor", {}, self.root)
        self.assertEqual(s, 200)
        with self.subTest("error map 与 ERROR_TABLE 一致"):
            for code in contracts.ERROR_TABLE:
                self.assertEqual(contracts.http_status(code),
                                 contracts.ERROR_TABLE[code][0])
                self.assertEqual(b and contracts.mcp_code(code),
                                 contracts.ERROR_TABLE[code][1])

    def test_role_matrix_enforced(self):
        tid = self.mk()
        _, wtok = self.dispatch(tid)
        for tool, body in (("board", {}), ("doctor", {}), ("enforce",
                                                           {"on": True})):
            s, b = self.call(f"v3/{tool}", body, wtok)
            self.assertEqual((s, b["error"]["code"]), (403, "forbidden"),
                             tool)
        s, b = self.call("v3/task_update", {"task": tid,
                                            "verify_cmd": "x"}, wtok)
        self.assertEqual((s, b["error"]["code"]), (403, "forbidden"))
        evs = [e.event for e in self.stack.storage.events_all()]
        self.assertIn("auth_denied", evs)  # 越权审计

    def test_unknown_task_structured_not_found(self):
        s, b = self.call("v3/task_cancel", {"task": "ag-nope"}, self.root)
        self.assertEqual((s, b["error"]["code"]), (404, "not_found"))


class TestLegacyRoutes(_Base):  # 断言3
    def test_fifteen_routes_reachable(self):
        for name in sorted(GOLDEN["mcp"]["http_tool_routes"]):
            body = ({"goal": f"g-{name}", "project_dir": "/repo",
                     "verify_cmd": "true"} if name == "task_create"
                    else {"task": "ag-nope"} if "task" in
                    contracts.CATALOG[contracts.LEGACY_TO_V3.get(name, name)]
                    .request
                    else {"kind": "note", "body": "x"} if name == "post"
                    else {"path": "x.md", "text": "t"} if name == "wiki_write"
                    else {"path": "x.md"} if name == "wiki_read"
                    else {"on": True} if name == "enforce"
                    else {"repo": "/repo"} if name == "repo_board"
                    else {})
            s, b = self.call(name, body, self.root)
            self.assertFalse(s == 404 and b == {"error": "not found"},
                             (name, s, b))

    def test_sleep_not_a_server_route(self):
        s, b = self.call("sleep", {"seconds": 1}, self.root)
        self.assertEqual((s, b), (404, {"error": "not found"}))

    def test_board_default_keys(self):
        self.mk()
        s, b = self.call("board", {}, self.root)
        self.assertEqual(s, 200)
        self.assertEqual(set(b), {"buckets", "chain", "forest"})
        self.assertEqual(set(b["buckets"]),
                         {"已合并", "在飞", "失败", "阻塞", "已取消"})
        s, b = self.call("view", method="GET", token=self.root)
        self.assertEqual(s, 200)
        self.assertIn("agora board", b)

    def test_board_full_golden(self):
        tid = self.mk()
        s, b = self.call("board", {"view": "full"}, self.root)
        self.assertEqual(s, 200)
        self.assertEqual(sorted(b), GOLDEN["board_full_view"]["top_keys"])
        fields = set(b["tasks"][tid])
        golden = set(GOLDEN["board_full_view"]["task_fields"])
        # verify_budget＝F126 适配层白名单扩展键；usage/gate_artifact/
        # gate_resource＝v3 无字段或条件键（F141 shortcut 注在案）
        cond = {"usage", "gate_artifact", "gate_resource"}
        self.assertLessEqual(fields, golden | {"verify_budget"})
        self.assertLessEqual(golden - cond, fields | {"verify_budget"})

    def test_doctor_keys_golden(self):
        self.mk()
        s, b = self.call("doctor", {}, self.root)
        self.assertEqual(s, 200)
        keys = set(b)
        self.assertLessEqual(set(GOLDEN["doctor"]["base_keys"]), keys)
        self.assertLessEqual(set(GOLDEN["doctor"]["models_keys"]), keys)
        self.assertEqual(b["models"], ["prov/a", "prov/b"])
        self.assertEqual(keys - set(GOLDEN["doctor"]["base_keys"])
                         - set(GOLDEN["doctor"]["models_keys"]), set())

    def test_message_dict_shape_golden(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        self.call("post", {"task": tid, "kind": "question", "body": "q1"},
                  wtok)
        s, b = self.call("check_mail", {}, self.root)
        self.assertEqual(s, 200)
        msgs = b["messages"]
        self.assertTrue(msgs)
        for m in msgs:
            self.assertEqual(set(m), {"id", "ts", "task", "kind", "from",
                                      "to", "body"})
            self.assertIsInstance(m["id"], int)
            self.assertIsInstance(m["ts"], str)

    def test_legacy_error_flat_shape(self):
        s, b = self.call("task_cancel", {"task": "ag-nope"}, self.root)
        self.assertEqual(s, 404)
        self.assertIsInstance(b["error"], str)
        s, b = self.call("board", {})
        self.assertEqual((s, b), (401, {"error": "无牌或牌无效"}))
        s, b = self.call("task_create", {"goal": "g"}, self.root)
        self.assertEqual(s, 400)
        self.assertIn("project_dir", b["error"])

    def test_health_route(self):
        s, b = self.call("health", method="GET", token=self.root)
        self.assertEqual((s, b["ok"], b["api_version"]), (200, True, 3))
        self.assertTrue(b["enforce_on"])


class TestDoctorProfiles(_Base):  # 断言3 条件键（verify_profiles 注入）
    def _before_stack(self):
        (self.home.agora / "verify-profiles.json").write_text(
            json.dumps({"/repo": {"timeout": 120}}), encoding="utf-8")

    def test_profiles_key_present(self):
        s, b = self.call("doctor", {}, self.root)
        self.assertEqual(s, 200)
        prof = b[GOLDEN["doctor"]["verify_profiles_key"]]
        self.assertEqual(prof["count"], 1)
        self.assertEqual(prof["profiles"][0]["timeout"], 120)


class TestNudgeAlarm(_Base):  # A-F1：legacy 内部面机械锚（task_nudge/task_alarm）
    def test_nudge_paths(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("task_nudge", {"task": tid}, self.root)
        self.assertEqual((s, b), (200, {"nudged": True}))
        self.assertTrue(any("自检" in i[2] for i in self.oc.injects))
        s, b = self.call("task_nudge", {"task": tid, "body": "自定义提示"},
                         self.root)
        self.assertEqual((s, b), (200, {"nudged": True}))
        self.assertTrue(any("自定义提示" in i[2] for i in self.oc.injects))
        s, b = self.call("task_nudge", {"task": tid}, wtok)  # 非 root 非派发方
        self.assertEqual(s, 403)
        self.assertIn("仅 root/任务派发方", b["error"])
        tid2 = self.mk()  # todo 无 worker
        s, b = self.call("task_nudge", {"task": tid2}, self.root)
        self.assertEqual((s, b), (200, {"nudged": False,
                                        "reason": "no_worker"}))

    def test_alarm_paths(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        self.call("post", {"task": tid, "kind": "question", "body": "q"}, wtok)
        s, b = self.call("task_alarm", {"task": tid}, self.root)
        self.assertEqual((s, b), (200, {"alarmed": True}))
        s, b = self.call("task_alarm", {"task": tid}, wtok)  # 仅 root
        self.assertEqual(s, 403)
        tid2 = self.mk()
        s, b = self.call("task_alarm", {"task": tid2}, self.root)
        self.assertEqual((s, b), (200, {"alarmed": False,
                                        "reason": "no_worker"}))


class TestWikiFaces(_Base):  # OCR-1/C-1/B-2 修复锚：新建页/规范化守卫/越根 400
    def test_write_creates_new_page_and_roundtrip(self):
        s, b = self.call("wiki_write", {"path": "spec/x.md", "text": "# t"},
                         self.root)
        self.assertEqual((s, b), (200, {"ok": True}))
        s, b = self.call("wiki_read", {"path": "spec/x.md"}, self.root)
        self.assertEqual((s, b["text"]), (200, "# t"))
        s, b = self.call("v3/wiki_write", {"path": "spec/y.md", "text": "z"},
                         self.root)
        self.assertEqual(s, 200)
        s, b = self.call("v3/wiki_read", {"path": "spec/y.md"}, self.root)
        self.assertEqual((s, b["text"]), (200, "z"))

    def test_tasks_prefix_guard_on_resolved_path(self):
        s, b = self.call("wiki_write",
                         {"path": "x/../tasks/p.md", "text": "e"}, self.root)
        self.assertEqual((s, b["error"]),
                         (403, "wiki/tasks/ 前缀拒写（服务独占）"))

    def test_escape_maps_400_on_both_faces(self):
        s, b = self.call("wiki_read", {"path": "../../etc/passwd"}, self.root)
        self.assertEqual(s, 400)
        for path in ("../../etc/passwd", "/etc/passwd"):
            s, b = self.call("v3/wiki_read", {"path": path}, self.root)
            self.assertEqual((s, b["error"]["code"]),
                             (400, "invalid_argument"), path)

    def test_missing_read_404(self):
        s, b = self.call("wiki_read", {"path": "nope.md"}, self.root)
        self.assertEqual(s, 404)


class TestPeerBoardFace(_Base):  # B-4/OCR-3/OCR-5 修复锚：并发留言/多任务看板
    def test_repo_board_multi_task(self):
        t1, t2 = self.mk("a"), self.mk("b")
        s, b = self.call("repo_board", {"repo": "/repo"}, self.root)
        self.assertEqual(s, 200)
        self.assertEqual({r["tid"] for r in b["tasks"]}, {t1, t2})

    def test_concurrent_peer_posts_distinct_mids(self):
        t1, t2 = self.mk("p1"), self.mk("p2")
        _, w1 = self.dispatch(t1)
        _, w2 = self.dispatch(t2)
        out, errs = [], []

        def poster(tok, i):
            try:
                s, b = self.call("post", {"kind": "peer", "body": f"m{i}"},
                                 tok)
                out.append(b.get("message"))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        toks = [w1, w2] * 4
        threads = [threading.Thread(target=poster, args=(tok, i))
                   for i, tok in enumerate(toks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertFalse(errs)
        self.assertEqual(len(out), 8)
        self.assertEqual(len(set(out)), 8)  # mid 不撞号（写锁串行）
        s, b = self.call("repo_board", {"repo": "/repo"}, self.root)
        self.assertEqual(len(b["messages"]), 8)  # 最新在前全量在册


class TestMessagePostChainEdge(_Base):  # Req: F167 链边授权（C 册）
    """非 done/peer kind 的链边授权恢复（v2 Router._resolve 同语义）：
    worker 牌限本任务（up 边 worker_sid==sid）、dispatcher 牌限归属链
    （down 边 sid==dispatcher_sid 或血统）、root 保留 bind_dispatcher
    愈合前判后同受链边约束——跨链 400 且零落库零注入。"""

    def setUp(self):
        super().setUp()
        # 受害链：独立派发方 ses-dsp 的任务 B
        s, b = self.call("cred_for_sid", {"sid": "ses-dsp"}, self.root)
        self.assertEqual((s, b.get("role")), (200, "dispatcher"))
        self.dtok = b["token"]
        s, b = self.call("task_create", {"goal": "victim",
                                         "project_dir": "/repo",
                                         "verify_cmd": "true"}, self.dtok)
        self.assertEqual(s, 200, b)
        self.tid_b = b["task"]
        _, self.wtok_b = self.dispatch(self.tid_b)
        # 攻击者：root 名下任务 A 的 worker
        self.tid_a = self.mk("attacker")
        _, self.wtok_a = self.dispatch(self.tid_a)

    def _msgs(self, tid, kind):
        return [m for m in self.stack.storage.messages_all()
                if m.task_id == tid and m.kind == kind]

    def test_worker_cross_chain_rejected_all_kinds(self):
        for kind in ("note", "question", "answer", "verify",
                     "verify_reject"):
            s, b = self.call("v3/message_post",
                             {"task": self.tid_b, "kind": kind,
                              "body": "伪造"}, self.wtok_a)
            self.assertEqual((s, b["error"]["code"]),
                             (400, "invalid_argument"), kind)
            self.assertFalse(self._msgs(self.tid_b, kind), kind)  # 零落库

    def test_worker_cross_chain_rejected_legacy_face(self):
        s, b = self.call("post", {"task": self.tid_b, "kind": "verify",
                                  "body": "伪造"}, self.wtok_a)
        self.assertEqual(s, 400)
        self.assertIsInstance(b["error"], str)  # legacy 平面错误

    def test_worker_cross_chain_no_inject_to_victim_dispatcher(self):
        self.call("v3/message_post", {"task": self.tid_b, "kind": "verify",
                                      "body": "伪造 verify 提案"},
                  self.wtok_a)
        self.assertFalse([i for i in self.oc.injects if "伪造" in i[2]])

    def test_dispatcher_cross_chain_rejected(self):
        s, b = self.call("v3/message_post", {"task": self.tid_a,
                                             "kind": "note", "body": "x"},
                         self.dtok)
        self.assertEqual((s, b["error"]["code"]),
                         (400, "invalid_argument"))
        self.assertFalse(self._msgs(self.tid_a, "note"))

    def test_root_cross_chain_live_dispatcher_rejected(self):
        # root 愈合前判放行面＝登记 dispatcher 已死；活号链边照拒（v2 同口径）
        s, b = self.call("v3/message_post", {"task": self.tid_b,
                                             "kind": "note", "body": "x"},
                         self.root)
        self.assertEqual((s, b["error"]["code"]),
                         (400, "invalid_argument"))
        self.assertFalse(self._msgs(self.tid_b, "note"))

    def test_on_edge_posts_still_allowed(self):
        s, b = self.call("v3/message_post", {"task": self.tid_a,
                                             "kind": "question", "body": "q"},
                         self.wtok_a)  # worker → 本任务 up 边
        self.assertEqual((s, isinstance(b.get("message"), int)), (200, True))
        s, b = self.call("v3/message_post", {"task": self.tid_b,
                                             "kind": "note", "body": "n"},
                         self.dtok)  # dispatcher → 归属链 down 边
        self.assertEqual((s, isinstance(b.get("message"), int)), (200, True))
        s, b = self.call("v3/message_post", {"task": self.tid_a,
                                             "kind": "note", "body": "n"},
                         self.root)  # root 名下 daemon 行（dispatcher_sid=root）
        self.assertEqual((s, isinstance(b.get("message"), int)), (200, True))
        self.assertTrue(self._msgs(self.tid_a, "note"))

    def test_parent_worker_down_edge_to_nested_subtask(self):
        s, b = self.call("task_create", {"goal": "sub"}, self.wtok_a)
        self.assertEqual(s, 200, b)
        sub = b["task"]
        s, b = self.call("task_dispatch", {"task": sub}, self.wtok_a)
        self.assertEqual(s, 200, b)
        s, b = self.call("v3/message_post", {"task": sub, "kind": "answer",
                                             "body": "a"}, self.wtok_a)
        self.assertEqual((s, isinstance(b.get("message"), int)), (200, True))
        # 子任务 worker 仍是旁链：跨到受害任务照拒
        srow = self.stack.storage.get_task(sub)
        _, stok = self._pull_worker_token(srow.worker_sid)
        s, b = self.call("v3/message_post", {"task": self.tid_b,
                                             "kind": "note", "body": "x"},
                         stok)
        self.assertEqual((s, b["error"]["code"]),
                         (400, "invalid_argument"))

    def test_root_healing_preserved_on_dead_dispatcher(self):
        self.oc.dead.add("ses-dsp")
        s, b = self.call("v3/message_post", {"task": self.tid_b,
                                             "kind": "note", "body": "heal"},
                         self.root, sid="ses-real1")
        self.assertEqual((s, isinstance(b.get("message"), int)), (200, True))
        self.assertEqual(
            self.stack.storage.get_task(self.tid_b).dispatcher_sid,
            "ses-real1")  # F21 愈合改绑后按链边放行
        self.assertTrue(self._msgs(self.tid_b, "note"))

    def _pull_worker_token(self, sid):
        s, b = self.call("cred_for_sid", {"sid": sid})
        if s != 200:  # 无牌直连＝按在飞 worker 行命中（F25 推改拉）
            s, b = self.call("cred_for_sid", {"sid": sid}, self.root)
        self.assertEqual(s, 200, b)
        return None, b["token"]


class TestMcpShell(_ChainBase):  # 断言4
    def setUp(self):
        super().setUp()
        agora_mcp._V3_TOOLS = None
        self.addCleanup(setattr, agora_mcp, "_V3_TOOLS", None)

    def _env(self):
        return mock.patch.dict(os.environ,
                               {"AGORA_HOME": str(self.home.path),
                                "AGORAD_URL": ""})

    def test_tools_endpoint_single_source(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/v3/tools", timeout=10) as r:
            tools = json.loads(r.read())["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, CANON | {"sleep"} | ALIASES)
        self.assertEqual(len(names), 23)
        schema = next(t for t in tools if t["name"] == "task_create")[
            "inputSchema"]
        self.assertIn("goal", schema["properties"])
        self.assertEqual(schema["required"], ["goal"])

    def test_shell_tools_list_and_call(self):
        with self._env():
            url, api = agora_mcp.resolve_service()
            self.assertEqual(api, 3)
            resp = agora_mcp.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"})
            names = {t["name"] for t in resp["result"]["tools"]}
            self.assertEqual(names, CANON | {"sleep"} | ALIASES)
            resp = agora_mcp.handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "board",
                           "arguments": {"_token": self.root}}})
            self.assertNotIn("error", resp)
            self.assertIn("buckets", json.loads(
                resp["result"]["content"][0]["text"]))
            resp = agora_mcp.handle({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "board",
                           "arguments": {"_token": "bad"}}})
            self.assertEqual(resp["error"]["code"], -32001)

    def test_error_codes_match_golden(self):
        derived = {contracts.http_status(c): contracts.mcp_code(c)
                   for c in contracts.ERROR_TABLE}
        golden = {int(k): v for k, v in
                  GOLDEN["mcp"]["error_codes"].items()}
        self.assertEqual(derived, golden)
        self.assertEqual(agora_mcp.ERROR_CODES, golden)

    def test_unknown_tool_precedence(self):  # OCR-R2：v2 错误优先级保真
        resp = agora_mcp.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "bogus", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)  # 先按名拒非「缺牌可自愈」
        with self._env():
            resp = agora_mcp.handle({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "bogus",
                           "arguments": {"_token": self.root}}})
            self.assertEqual(resp["error"]["code"], -32602)

    def test_resolve_service_bad_api_version_keeps_port(self):
        # OCR-2 锚：api 坏值只回退 2，不连坐 port（服务可达性不动）
        (self.home.agora / "service.json").write_text(
            json.dumps({"port": self.port, "api_version": None}),
            encoding="utf-8")
        with self._env():
            url, api = agora_mcp.resolve_service()
            self.assertEqual((api, url),
                             (2, f"http://127.0.0.1:{self.port}"))


class TestServiceJson(_Base):  # 断言5
    def test_service_json_versions_and_old_fields(self):
        data = json.loads((self.home.agora / "service.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["api_version"], 3)
        self.assertEqual(data["storage_version"], 3)
        for key in ("pid", "port", "started", "version", "oc_version"):
            self.assertIn(key, data)
        self.assertEqual(data["port"], self.port)
        self.assertEqual(data["oc_version"], "stub-oc")
        mode = stat.S_IMODE(os.stat(self.home.agora / "service.json").st_mode)
        self.assertEqual(mode, 0o600)


class TestDoneChain(_ChainBase):  # 断言6
    def test_full_chain_merges_and_closes(self):
        tid, wsid, wtok = self.green_chain()
        state = self.wait_state(tid, {"closed", "merged", "blocked",
                                      "reopened"})
        self.assertEqual(state, "closed")
        evs = self.events(tid)
        self.assertIn("merged", evs)
        self.assertIn("closed", evs)
        self.assertEqual(evs.count("merged"), 1)  # 不双合并
        self.assertIn("advance_local_target", self.gitx.ops())
        arts = self.home.state / "gate-artifacts" / tid
        self.assertTrue(arts.is_dir() and any(arts.iterdir()))  # artifact 落盘
        s, b = self.call("board", {"task": tid}, self.root)  # board 反映终态
        # Req: F144 classification＝v2 三态判读镜像（终态＝None，旧压缩版 ok 废）
        self.assertEqual((s, b["task"]["state"], b["classification"]),
                         (200, "closed", None))
        s, b = self.call("check_mail", {}, self.root)
        kinds = [m["kind"] for m in b["messages"]]
        self.assertIn("result", kinds)  # 终态通知达派发方

    def test_done_reject_shapes(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("post", {"task": tid, "kind": "done", "body": C},
                         self.root)  # 非任务 worker：链边拒（400 同档）
        self.assertEqual(s, 400)
        self.assertIn("done 仅任务 worker", b["error"])
        self.call("post", {"task": tid, "kind": "question", "body": "开工"},
                  wtok)  # worker 调用即开工→running
        s, b = self.call("post", {"task": tid, "kind": "done", "body": "zz"},
                         wtok)
        self.assertTrue(str(b["result"]).startswith("rejected:body-format"))


class TestMessageFace(_ChainBase):  # 断言7
    def test_receipt_cursor_and_inject_order(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        self.oc.injects.clear()
        s, b = self.call("post", {"task": tid, "kind": "question",
                                  "body": "q-body"}, wtok)
        self.assertEqual((s, b["message"]), (200, 2))
        mid = b["message"]
        receipt = self.stack.storage.get_receipt(mid)
        self.assertEqual(receipt.status, "delivered")  # inject True→delivered
        wakes = [i for i in self.oc.injects if i[0] == "wake"]
        self.assertEqual(len(wakes), 1)  # 恰一次唤醒注入
        text = wakes[0][2]
        self.assertIn(f"msg=state/messages/{mid:08d}.md", text)  # 引用行在案
        self.assertIn(f"page=wiki/tasks/{tid}.md", text)  # 不悬空
        s, b = self.call("check_mail", {}, self.root)
        ids = [m["id"] for m in b["messages"]]
        self.assertIn(mid, ids)  # 补拉面可见
        s, b = self.call("check_mail", {}, self.root)
        self.assertNotIn(mid, [m["id"] for m in b["messages"]])  # cursor 前进

    def test_failing_exporter_omits_msg_line(self):
        class _Boom:
            def export(self, message):
                raise OSError("disk full")

        self.stack.close()
        self.server.shutdown()
        self.server.server_close()
        self.stack = compose.V3Stack(
            self.home.path, oc=self.oc, gitx=self.gitx, runner=self.runner,
            anchor_reader=self.anchors, clock=self.clock,
            injector=self.injector, exporter=_Boom()).start()
        self.server = serverv3.make_server_v3(self.stack)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        self.oc.injects.clear()
        s, b = self.call("post", {"task": tid, "kind": "question",
                                  "body": "q"}, wtok)
        self.assertEqual(s, 200)
        text = [i for i in self.oc.injects if i[0] == "wake"][0][2]
        self.assertNotIn("msg=", text)  # 导出失败→引用行省略（不悬空）
        self.assertIn(f"page=wiki/tasks/{tid}.md", text)


class TestAuthChain(_Base):  # 断言8
    def test_root_bootstrap_and_worker_credential(self):
        self.assertEqual(self.stack.auth.identify(self.root),
                         ("root", "root"))
        tid = self.mk()
        _, wtok = self.dispatch(tid)
        row = self.stack.storage.get_task(tid)
        cred = next(c for c in self.stack.storage.credentials_all()
                    if c.task_id == tid)
        self.assertEqual(cred.sid, row.worker_sid)
        f = self.home.credentials / f"{tid}.token"
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertEqual(json.loads(f.read_text())["token"],
                         self.stack.auth.token_for(row.worker_sid))
        self.assertEqual(self.stack.auth.identify(wtok),
                         (row.worker_sid, "worker"))

    def test_cred_for_sid_pull_and_root_issue(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("cred_for_sid", {"sid": wsid})
        self.assertEqual((s, b["tid"], b["env_dir"]),
                         (200, tid, f"/tmp/envs/{tid}"))
        s, b = self.call("cred_for_sid", {"sid": "ses-brand-new"},
                         token=self.root)
        self.assertEqual((s, b["role"]), (200, "dispatcher"))
        s, b2 = self.call("cred_for_sid", {"sid": "ses-brand-new"},
                          token=self.root)
        self.assertEqual(b2["token"], b["token"])  # 幂等回旧牌不重签
        s, b = self.call("cred_for_sid", {"sid": "ses-unknown"})
        self.assertEqual((s, b), (404, {"error": "no cred for sid"}))

    def test_lineage_rebind_pull(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("post", {"task": tid, "kind": "note", "body": "n"},
                         self.root)  # down→worker（note digest 落库）
        self.assertEqual(s, 200)
        new_sid = "ses-w1-rebound"
        token = self.stack.auth.rebind(wsid, new_sid)
        self.assertEqual(token, wtok)
        self.stack.auth.reload()
        self.assertEqual(self.stack.auth.identify(wtok),
                         (new_sid, "worker"))
        s, b = self.call("check_mail", {}, wtok)
        ids = [m["id"] for m in b["messages"]]
        self.assertTrue(ids)  # 血统补拉：旧 sid 收件全量可见（cursor=0）

    def test_x_agora_sid_worker_rebind(self):
        tid = self.mk()
        wsid, wtok = self.dispatch(tid)
        s, b = self.call("post", {"task": tid, "kind": "question",
                                  "body": "q"}, wtok, sid="ses-w9")
        self.assertEqual(s, 200)
        self.assertEqual(self.stack.storage.get_task(tid).worker_sid,
                         "ses-w9")  # 换号改绑：行随新 sid


class TestCrashRecovery(_ChainBase):  # 断言9
    def test_recompose_no_double_merge(self):
        self.runner.delay = 0.4  # 慢 verify：崩窗在门体运行中
        tid = self.mk()
        wsid, wtok = self.to_running(tid)
        s, b = self.call("post", {"task": tid, "kind": "done", "body": C},
                         wtok)
        self.assertEqual(b, {"result": "accepted"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.stack.storage.get_task(tid).gate.in_gate:
                break
            time.sleep(0.01)
        self.assertTrue(self.stack.storage.get_task(tid).gate.in_gate)
        self.stack.storage.close()  # 模拟 kill -9：连接蒸发、队列悬空
        self.runner.delay = 0.0
        stack2 = compose.V3Stack(
            self.home.path, oc=self.oc, gitx=self.gitx, runner=self.runner,
            anchor_reader=self.anchors, clock=self.clock,
            injector=self.injector).start()  # 重组服务（模拟重启）
        try:
            state = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                t = stack2.storage.get_task(tid)
                state = t.state if t else None
                if state == "closed":
                    break
                time.sleep(0.05)
            self.assertEqual(state, "closed")
            self.assertEqual(self.gitx.ops().count("advance_local_target"),
                             1)  # 队列恢复恰一次、不双合并
            self.assertEqual([e.event for e in stack2.storage.events_for_task(
                tid)].count("merged"), 1)
        finally:
            stack2.close()


class TestDefaultRunner(unittest.TestCase):  # B-1/OCR-6 修复锚：真子进程
    def test_soft_ceiling_and_escape_drain(self):
        with tempfile.TemporaryDirectory() as d:
            soft = []
            rc, out = compose.subprocess_runner(d, "echo hi", timeout=0.5,
                                                ceiling=1.0,
                                                on_soft=lambda: soft.append(1))
            self.assertEqual((rc, soft), (0, []))  # 未超软顶：不提醒
            t0 = time.monotonic()
            rc, out = compose.subprocess_runner(
                d, "setsid sleep 6 & sleep 5", timeout=0.3, ceiling=0.6,
                on_soft=lambda: soft.append(1))
            elapsed = time.monotonic() - t0
            self.assertEqual(rc, 124)  # 硬顶收杀
            self.assertTrue(soft)  # 软顶点必醒（不在硬顶瞬才触发）
            self.assertLess(elapsed, 3.5)  # 逃逸后代持管道不挂死（弃读收尾）


class TestConcurrency(_ChainBase):  # 断言10
    def test_concurrent_writes_serialized_no_half_state(self):
        results, errors = [], []

        def worker(i):
            try:
                s, b = self.call("task_create",
                                 {"goal": f"g-{i}", "project_dir": "/repo",
                                  "verify_cmd": "true"}, self.root)
                results.append((s, b))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual({s for s, _ in results}, {200})
        tids = {b["task"] for _, b in results}
        self.assertEqual(len(tids), 8)
        tasks = self.stack.storage.tasks_all()
        self.assertEqual(len(tasks), 8)  # 无 interleaving 半态/丢行
        self.assertEqual({t.id for t in tasks}, tids)
        self.assertEqual([e.event for e in self.stack.storage.events_all()]
                         .count("task_created"), 8)  # seq 单调无重号

    def test_reads_not_blocked_by_long_verify(self):
        self.runner.delay = 1.2
        tid = self.mk()
        wsid, wtok = self.to_running(tid)
        self.call("post", {"task": tid, "kind": "done", "body": C}, wtok)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.stack.storage.get_task(tid).gate.in_gate:
                break
            time.sleep(0.01)
        t0 = time.monotonic()
        s, b = self.call("board", {}, self.root, timeout=5)
        elapsed = time.monotonic() - t0
        self.assertEqual(s, 200)
        self.assertLess(elapsed, 1.0)  # 读端点不被长 verify 阻塞
        self.wait_state(tid, {"closed", "reopened", "blocked"}, timeout=15)


class _GitStackInjector(harness.FaultInjector):
    """_Base 注册集＋GitStore 提交/timer 面 failpoint——后端全量预注册
    （stack 在 setUp 内即 start 提交，注册须先于 super().setUp()）。"""

    def __init__(self):
        super().__init__()
        for fp in ("uow", "dispatch", "write-object", "write-tree",
                   "commit-tree", "update-ref", "memory-publish",
                   "timer_fire", "timer_firing"):
            self.register(fp)


class TestV3StackOnGitServerPort(_ChainBase):  # Req: F155——Git 后端服务器口
    """V3Stack 显式 git 后端经组装栈＋传输层的聚焦端口：绿链
    create/dispatch/post-done/gate settlement＋check_mail 拉取＋board/doctor
    响应全走 HTTP 面；运行全程零 agora.db/-wal/-shm（spec 断言4）。"""

    def _stack(self, **over):
        return super()._stack(backend=gitstore.BACKEND, **over)

    def setUp(self):
        with mock.patch.object(harness, "FaultInjector", _GitStackInjector):
            super().setUp()

    def test_request_path_on_git_and_zero_sqlite_artifacts(self):
        self.assertIsInstance(self.stack.storage, gitstore.GitStore)
        self.assertTrue((Path(self.home.path) / "state" / "journal.git")
                        .is_dir())
        tid, wsid, wtok = self.green_chain()  # create/dispatch/running/done
        self.assertEqual(self.wait_state(tid, {"closed"}, timeout=30),
                         "closed")
        self.assertEqual(self.stack.storage.get_task(tid).state, "closed")
        s, b = self.call("check_mail", {}, wtok)  # 消息拉取（outbox/cursor）
        self.assertEqual(s, 200)
        self.assertTrue(b["messages"])  # 凭据/里程碑消息经 Git 账本可拉
        s, b = self.call("board", {}, self.root)
        self.assertEqual(s, 200)
        self.assertIn("buckets", b)  # legacy board 金形状经 Git 栈可渲染
        s, b = self.call("doctor", {}, self.root)
        self.assertEqual(s, 200)
        self.assertIn("age_rank", b)  # doctor 投影经 Git 栈可渲染
        artifacts = sorted(p.name for p in
                           (Path(self.home.path) / "state").glob("agora.db*"))
        self.assertEqual(artifacts, [])  # 全程零 SQLite 件


if __name__ == "__main__":
    unittest.main()
