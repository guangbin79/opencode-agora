"""T11 transport/httpd＋main 装配契约：真 HTTP 全链/鉴权/后台门/健康定时/携带补齐。

真 agorad HTTPServer（urllib 客户端打 127.0.0.1）；StubOpencode 走真 HTTP；
gitx/read_anchor/runner 假注入（main.assemble 单一装配路径，R2）。
携带项（装配期补齐）：gate 尾 cancel 凭据作废＋终止通知、dispatch 凭据
直投失败落库兜底、enforce 端点、anchors 默认实现。
"""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agorad.access import anchors
from agorad.main import assemble, start_health_thread
from agorad.transport.httpd import make_server
from tests.stub_opencode import VERSION, StubOpencode


SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


RUNNER_CALLS = []  # (cwd, cmd) 门体验证实调记录（验收④取证用）


def ok_runner(cwd, cmd, timeout=None, ceiling=None):
    RUNNER_CALLS.append((cwd, cmd))
    return 0, "pass"


def fail_runner(cwd, cmd, timeout=None, ceiling=None):
    return 1, "boom"


class FakeGitx:
    """build_env 造真环境目录＋.review-loop（镜像生产：建环境含评审钩装配）。"""

    def __init__(self, base):
        self.base = Path(base)
        self.removed, self.branch = [], None

    def build_env(self, project_dir, tid, branch=None):
        self.branch = branch
        env = self.base / "envs" / tid
        (env / ".review-loop").mkdir(parents=True)
        return str(env)

    def materialize_checkout(self, tree_dir, source_dir):  # Req: ag-0d2daa
        self.materials = getattr(self, "materials", [])
        self.materials.append((tree_dir, source_dir))
        return ""

    def symref(self, repo_dir):
        return "main"

    def push(self, repo_dir, branch):
        self.pushes = getattr(self, "pushes", [])
        self.pushes.append((repo_dir, branch))
        return "none", ""

    def remove_env(self, env_dir):
        self.removed.append(env_dir)
        shutil.rmtree(env_dir, ignore_errors=True)

    def check_branch(self, repo_dir, branch):
        pass

    def contains(self, repo_dir, commit, branch=None, fetch_timeout=None):
        return False

    def ensure_clean(self, repo_dir, exempt=()):
        pass

    def clean_ignore(self, repo_dir, env_value=""):
        return []

    def build_integration(self, target_dir, integration_dir, branch=None):
        Path(integration_dir).mkdir(parents=True, exist_ok=True)
        return integration_dir

    def remove_integration(self, integration_dir, target_dir):
        shutil.rmtree(integration_dir, ignore_errors=True)

    def merge_ff(self, env_dir, target, branch=None):
        return "ok"

    def head(self, repo_dir, branch=None):
        return "aaa111"

    def touches_gate(self, env_dir, patterns, branch=None):
        return False

    def diff_check(self, env_dir, branch=None):
        return ""


class DeadInjectOc:
    """create 成功但 inject 一律拒——凭据直投失败路径。"""

    def create(self, cwd, boot, title=None, model=None):  # F55：签名随 Oc 加性扩展
        return "sw1"

    def inject(self, sid, text, model=None):
        return False

    def alive(self, sid):
        return False

    def session(self, sid):
        return None

    def probe(self):
        return {"ok": True, "oc_version": "fake"}


def call(port, method, path, body=None, token=None, sid=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agora-Token"] = token
    if sid:
        headers["X-Agora-Sid"] = sid  # R25-A：MCP 壳透传调用者真实会话 sid
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            if r.headers.get("Content-Type", "").startswith("application/json"):
                return r.status, json.loads(raw or b"{}")
            return r.status, raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


class HttpdTest(unittest.TestCase):
    """真服务夹具：StubOpencode＋假 gitx/锚/runner，root 牌 sid=main。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = StubOpencode()
        self.stub.start()
        self.gitx = FakeGitx(self.tmp)
        self.app = assemble(self.tmp, oc_url=self.stub.base_url,
                            gitx=self.gitx,
                            read_anchor=lambda env: {"head": SHA,
                                                     "clean": True},
                            runner=getattr(self, "gate_runner", ok_runner))
        self.root = self.app.auth.issue("main", "root")
        self.httpd = make_server(self.app)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def create_task(self, body=None):
        st, r = call(self.port, "POST", "/task_create",
                     body or {"goal": "g", "project_dir": "/repo"}, self.root)
        self.assertEqual(st, 200)
        return r["task"]

    def test_task_create_missing_project_dir_rejected_for_root(self):  # Req: F37
        st, r = call(self.port, "POST", "/task_create", {"goal": "g"}, self.root)
        self.assertEqual(st, 400)
        self.assertIn("project_dir", r["error"])  # 受信角色仍必填；worker 走继承

    def test_target_branch_flows_to_dispatch(self):  # Req: gt-132747
        tid = self.create_task({"goal": "g", "project_dir": "/repo",
                                "target_branch": "issue/7"})
        self.dispatch_task(tid)
        self.assertEqual(self.gitx.branch, "issue/7")  # httpd→task→gitx 全链透传
        tid2 = self.create_task()
        self.dispatch_task(tid2)
        self.assertIsNone(self.gitx.branch)  # 缺省＝None（向后兼容）

    def dispatch_task(self, tid):
        st, r = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": "true"}, self.root)
        self.assertEqual(st, 200)
        st, r = call(self.port, "POST", "/task_dispatch", {"task": tid},
                     self.root)
        self.assertEqual(st, 200)
        return r["token"]


class TestHttpSmoke(HttpdTest):
    def test_full_chain_create_dispatch_done_gate_close(self):  # Req: F2/F4/F7
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        # T5 真形状：boot 随 create 走 prompt 通道，先于 dispatch 的 credential 注入（F43 指针形态）
        self.assertEqual(self.stub.sessions[wsid]["injects"][-1],
                         f"[agora] credential {tid}"
                         f"\nfile: {self.app.store.home}/credentials/{tid}.token")
        boot_text = self.stub.sessions[wsid]["injects"][0]
        self.assertTrue(boot_text.startswith("[agora] ") and tid in boot_text)
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, wtok)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "closed"))
        t = self.app.state.get(tid)
        self.assertEqual(t["commit"], SHA)  # 证据指针落行
        self.assertEqual(self.app.gitx.removed,
                         [self.app.state.get(tid)["env_dir"]])  # 环境回收
        self.assertIn("merged", self.stub.sessions[wsid]["injects"][-1])
        st, r = call(self.port, "POST", "/board", {}, self.root)
        self.assertIn(tid, r["buckets"]["已合并"])
        self.assertEqual(r["chain"], [tid])
        st, r = call(self.port, "POST", "/board", {"task": tid}, self.root)
        self.assertEqual(r["done_body"], SHA)  # 结论取自消息库 done 正文
        st, html_text = call(self.port, "GET", "/view", token=self.root)
        self.assertEqual(st, 200)
        self.assertIn("<html>", html_text)
        st, r = call(self.port, "GET", "/health", token=self.root)
        self.assertTrue(r["ok"])
        self.assertTrue(r["enforce_on"])

    def test_missing_or_bad_token_401(self):  # Req: F1
        st, _ = call(self.port, "POST", "/board", {})
        self.assertEqual(st, 401)
        st, _ = call(self.port, "POST", "/board", {}, token="junk")
        self.assertEqual(st, 401)

    def test_board_top_level_includes_forest(self):  # Req: gt-6def73
        tid = self.create_task()
        st, r = call(self.port, "POST", "/board", {}, self.root)
        self.assertEqual(st, 200)
        self.assertIn("forest", r)  # 顶层新字段（chain/buckets 语义不动）
        self.assertEqual(r["forest"], [tid])  # 全任务集口径：根＝无父

    def test_board_full_view_projection_single_payload(self):  # Req: F48
        tid = self.create_task()
        st, r = call(self.port, "POST", "/board", {"view": "full"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r), ["buckets", "events", "forest", "stats",
                                     "tasks"])  # F57：stats 分桶随返回体
        self.assertEqual(r["forest"], [tid])
        self.assertEqual(sorted(r["tasks"][tid]),
                         ["created", "dispatcher_sid", "last_seen",
                          "model", "project_dir", "state", "title",
                          "worker_sid"])  # F55：model 恒在键，随白名单投影
        self.assertTrue(all(e.get("event") == "escalate" for e in r["events"]))
        st, top = call(self.port, "POST", "/board", {}, self.root)
        self.assertEqual(st, 200)
        self.assertNotIn("tasks", top)         # 紧凑 {} 响应形状不变
        self.assertNotIn("events", top)
        st, d = call(self.port, "POST", "/board", {"task": tid}, self.root)
        self.assertEqual(st, 200)              # {"task":…} 既有语义回归
        self.assertEqual(d["task"]["state"], "todo")
        self.assertIsNone(d["done_body"])

    def test_worker_enforce_denied_403_and_audited(self):  # Req: F1
        wtok = self.app.auth.issue("w9", "worker")
        st, r = call(self.port, "POST", "/enforce", {"on": False}, wtok)
        self.assertEqual(st, 403)
        self.assertTrue(self.app.auth.enforce_on())  # 旗标未被拨动
        ev = self.app.store.read_events()
        self.assertTrue(any(e["event"] == "auth_denied"
                            and e.get("tool") == "enforce" and e["actor"] == "w9"
                            for e in ev))

    def test_get_view_health_authed(self):  # Req: F1
        st, _ = call(self.port, "GET", "/view")
        self.assertEqual(st, 401)
        st, _ = call(self.port, "GET", "/health")
        self.assertEqual(st, 401)
        st, _ = call(self.port, "GET", "/view", token="junk")
        self.assertEqual(st, 401)
        wtok = self.app.auth.issue("w8", "worker")
        st, _ = call(self.port, "GET", "/view", token=wtok)
        self.assertEqual(st, 403)  # may 矩阵：worker 白名单无查询面
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "auth_denied" and e.get("tool") == "board"
              and e["actor"] == "w8"]
        self.assertEqual(len(ev), 1)  # 越权审计行
        st, _ = call(self.port, "GET", "/health", token=wtok)
        self.assertEqual(st, 403)
        dtok = self.app.auth.issue("d1", "dispatcher")
        st, html_text = call(self.port, "GET", "/view", token=dtok)
        self.assertEqual(st, 200)
        self.assertIn("<html>", html_text)
        st, r = call(self.port, "GET", "/health", token=dtok)
        self.assertEqual(st, 200)
        self.assertIn("enforce_on", r)

    def test_enforce_non_bool_on_rejected(self):  # Req: F1
        st, _ = call(self.port, "POST", "/enforce", {"on": "false"}, self.root)
        self.assertEqual(st, 400)  # 字符串 "false" 不得反转成 True
        st, _ = call(self.port, "POST", "/enforce", {"on": 1}, self.root)
        self.assertEqual(st, 400)
        self.assertTrue(self.app.auth.enforce_on())  # 旗标未被拨动
        self.assertEqual([e for e in self.app.store.read_events()
                          if e["event"].startswith("enforce")], [])  # 无审计行

    def test_task_create_verify_params_type_checked(self):  # Req: F23
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo",
                      "verify_pending": "false"}, self.root)
        self.assertEqual(st, 400)  # 豁免旗语义反转拒收（enforce 同口径）
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "verify_cmd": 123},
                     self.root)
        self.assertEqual(st, 400)  # 非串 verify_cmd 400 报因，不落 500
        tid = self.create_task()
        st, r = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": 123}, self.root)
        self.assertEqual(st, 400)  # task_update 同字段同守卫（create/update 不对称即缝隙）
        self.assertEqual(self.app.state.get(tid)["verify"]["state"], "pending")  # 未被污染
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g2", "project_dir": "/repo",
                      "verify_cmd": "true", "verify_pending": True}, self.root)
        self.assertEqual(st, 403)  # 互斥组合 core 层拒（同 after/target_branch 先例）
        self.assertEqual([t for t in self.app.state.snapshot()["tasks"].values()
                          if t["goal"] == "g2"], [])  # 拒绝零建行
        # 快速路正向锚：httpd 透传 verify_cmd，建单登记后 dispatch 免 update 过闸
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "fast", "project_dir": "/repo",
                      "verify_cmd": "true"}, self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        st, r = call(self.port, "POST", "/task_dispatch", {"task": tid}, self.root)
        self.assertEqual(st, 200)  # 无 update 直派放行
        self.assertEqual(self.app.state.get(tid)["verify"]["state"], "registered")

    def test_root_enforce_toggles_and_audits(self):  # Req: F1
        st, r = call(self.port, "POST", "/enforce", {"on": False}, self.root)
        self.assertEqual(st, 200)
        self.assertFalse(r["enforce_on"])
        st, r = call(self.port, "GET", "/health", token=self.root)
        self.assertFalse(r["enforce_on"])
        st, r = call(self.port, "POST", "/enforce", {"on": True}, self.root)
        self.assertTrue(r["enforce_on"])
        evs = [e["event"] for e in self.app.store.read_events()]
        self.assertIn("enforce_off", evs)
        self.assertIn("enforce_on", evs)

    def test_ten_posts_reach_live_worker_in_order(self):  # Req: F4
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        for i in range(10):
            st, r = call(self.port, "POST", "/post",
                         {"task": tid, "kind": "note", "body": f"m{i}"},
                         self.root)
            self.assertEqual(st, 200)
        expect = [f"[agora] task {tid} note from main\nm{i}" for i in range(10)]
        self.assertEqual(self.stub.sessions[wsid]["injects"][-10:], expect)

    def test_root_call_with_real_sid_binds_main(self):  # Req: F4（R25-A 拍人真链）
        st, _ = call(self.port, "POST", "/board", {}, self.root, sid="ses-m1")
        self.assertEqual(st, 200)
        self.assertEqual(self.app.main_sid, "ses-m1")
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "main_sid_bound"]
        self.assertEqual([e["actor"] for e in ev], ["ses-m1"])  # 换绑审计
        tid = self.create_task()  # root 牌同步改绑：task 行 dispatcher 落真实 sid
        self.assertEqual(self.app.state.get(tid)["dispatcher_sid"], "ses-m1")
        st, _ = call(self.port, "POST", "/board", {}, self.root, sid="ses-m2")
        self.assertEqual(self.app.main_sid, "ses-m2")  # 多主会话最近活跃覆盖
        call(self.port, "POST", "/board", {}, self.root)  # 无头直连不动绑定
        self.assertEqual(self.app.main_sid, "ses-m2")
        wtok = self.app.auth.issue("w6", "worker")
        st, _ = call(self.port, "POST", "/board", {}, wtok, sid="ses-w6")
        self.assertEqual(st, 403)  # 非 root 携头：矩阵拒，绑定不动
        self.assertEqual(self.app.main_sid, "ses-m2")

    def test_dispatcher_renumber_chain_edge_lineage(self):  # Req: F16
        """sid 血统：dispatcher 换号（bind_main rebind）后 post 旧任务仍达 worker，
        旧行 dispatcher_sid 不改写、归因记真实新 sid；无血缘 root 仍拒。"""
        tid = self.create_task()  # 无头建任务：dispatcher_sid=main
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, _ = call(self.port, "POST", "/board", {}, self.root, sid="ses2")  # 换号 rebind
        self.assertEqual(st, 200)
        self.assertEqual(self.app.state.get(tid)["dispatcher_sid"], "main")  # 旧行不改写
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "note", "body": "blood-note-16"},
                     self.root, sid="ses2")
        self.assertEqual(st, 200)  # 血统放行：down 边直达 worker
        self.assertIn(f"[agora] task {tid} note from ses2",
                      self.stub.sessions[wsid]["injects"][-1])
        (m,) = [x for x in self.app.store.read_messages()
                if x["body"] == "blood-note-16"]
        self.assertEqual((m["from"], m["to"]), ("ses2", wsid))  # 归因真实新 sid
        outsider = self.app.auth.issue("ses9", "root")
        self.stub.sessions["main"] = {"injects": [], "directory": "/"}  # F21：登记号仍活
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "note", "body": "x"},
                     outsider, sid="ses9")
        self.assertEqual(st, 400)  # 无血缘且登记号活：非链边（死号则走 F21 改绑）

    def test_wiki_write_read_and_guards(self):  # Req: F1
        st, r = call(self.port, "POST", "/wiki_write",
                     {"path": "notes/a.md", "text": "hello"}, self.root)
        self.assertEqual(st, 200)
        st, r = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/a.md"}, self.root)
        self.assertEqual(r["text"], "hello")
        st, _ = call(self.port, "POST", "/wiki_write",
                     {"path": "tasks/x.md", "text": "no"}, self.root)
        self.assertEqual(st, 403)  # 任务页前缀服务独占
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "../secret"}, self.root)
        self.assertEqual(st, 400)  # 越出 wiki 根
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/none.md"}, self.root)
        self.assertEqual(st, 404)

    def test_doctor_endpoint_view(self):  # Req: F10
        self.create_task()
        st, r = call(self.port, "POST", "/doctor", {}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(len(r["age_rank"]), 1)
        self.assertEqual(r["env"], [])

    def test_doctor_carries_models_and_degrades(self):  # Req: F56
        st, r = call(self.port, "POST", "/doctor", {}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r["models"],
                         ["zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"])
        self.assertIn("绑定面", r["models_note"])
        self.stub.stop()
        st, r = call(self.port, "POST", "/doctor", {}, self.root)
        self.assertEqual(st, 200)
        self.assertNotIn("models", r)  # oc 不可达降级：键缺席不 500（F10 doctor 面照旧）
        self.stub.start()  # 复原监听面：tearDown.stop() 需活 httpd（oc 已换面失效不受影响）


class TestNestedDispatchLoop(HttpdTest):
    def test_worker_nested_child_result_unblocks_parent_done(self):
        RUNNER_CALLS.clear()
        parent = self.create_task({"goal": "parent", "project_dir": "/repo"})
        parent_token = self.dispatch_task(parent)
        parent_sid = self.app.state.get(parent)["worker_sid"]
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "child"}, parent_token)  # F37：缺省继承父仓与验证配置
        self.assertEqual(st, 200)
        child = r["task"]
        self.assertEqual(self.app.state.get(child)["dispatcher_sid"], parent_sid)
        self.assertEqual(self.app.state.get(child)["chain_depth"], 2)
        self.assertEqual(self.app.state.get(child)["project_dir"], "/repo")
        self.assertEqual(self.app.state.get(child)["verify"],
                         self.app.state.get(parent)["verify"])
        st, r = call(self.port, "POST", "/task_dispatch",
                     {"task": child}, parent_token)
        self.assertEqual(st, 200)
        child_token = r["token"]

        st, r = call(self.port, "POST", "/post",
                     {"task": parent, "kind": "done", "body": SHA}, parent_token)
        self.assertEqual(st, 200)
        self.assertIn("rejected:children", r["result"])
        self.assertEqual(self.app.state.get(parent)["state"], "running")

        # 父会话死亡期间子任务结案：result 先留库，恢复后的同牌会话可补拉。
        self.stub.kill(parent_sid)
        st, r = call(self.port, "POST", "/post",
                     {"task": child, "kind": "done", "body": SHA}, child_token)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})
        # 等 result 消息而非状态（评审环 A 册：gate_tail 态翻先于 closed 落库，
        # 负载下有窗）；check_mail 带游标副作用——跨轮聚合断言。两拍齐＝门毕，
        # 其后 runner/终态断言才有时序保证。
        seen = []

        def pull_results():
            st, r = call(self.port, "POST", "/check_mail", {}, parent_token)
            self.assertEqual(st, 200)
            seen.extend(m for m in r["messages"] if m["kind"] == "result")
            return len(seen)

        self.assertTrue(wait_until(lambda: pull_results() >= 3))  # F54 启动＋merged＋closed
        self.assertTrue(any(cwd.endswith(f"/envs/{child}") and cmd == "true"
                            for cwd, cmd in RUNNER_CALLS))  # 门跑继承的 verify_cmd
        self.assertEqual(self.app.state.get(child)["state"], "closed")
        results = seen  # ag-acae3b：merged＋closed 两拍均实时（F54 起前置门启动拍）
        self.assertEqual([m["task"] for m in results], [child, child, child])
        self.assertIn("已入验证门", results[0]["body"])
        self.assertIn("merged @", results[1]["body"])
        self.assertIn("已 closed", results[2]["body"])

        st, r = call(self.port, "POST", "/post",
                     {"task": parent, "kind": "done", "body": SHA}, parent_token)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})
        self.assertTrue(wait_until(
            lambda: self.app.state.get(parent)["state"] == "closed"))

    def test_worker_rebind_keeps_todo_child_ownership(self):
        parent = self.create_task({"goal": "parent", "project_dir": "/repo"})
        parent_token = self.dispatch_task(parent)
        old_sid = self.app.state.get(parent)["worker_sid"]
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "child"}, parent_token)  # F37：缺省继承
        self.assertEqual(st, 200)
        child = r["task"]
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/none.md"}, parent_token, sid="parent-new")
        self.assertEqual(st, 404)
        self.assertEqual(self.app.state.get(parent)["worker_sid"], "parent-new")
        self.assertEqual(self.app.state.get(child)["dispatcher_sid"], "parent-new")
        self.assertNotEqual(old_sid, "parent-new")
        st, r = call(self.port, "POST", "/task_dispatch",
                     {"task": child}, parent_token, sid="parent-new")
        self.assertEqual(st, 200)  # 改绑后仍可首次派发已建未派子任务


class TestWorkerRebind(HttpdTest):
    """gt-d55b9e：worker 牌换号自愈——持牌即身份，漂移即改绑（镜像 R25-A root 面）。"""

    def test_worker_continued_session_rebinds_and_done_accepted(self):  # Req: F19
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, wtok,
                     sid="ses-new")  # 续接换号：牌面 sid≠真实会话
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})  # 六拒一克不减：受理绿
        self.assertEqual(self.app.auth.identify(wtok), ("ses-new", "worker"))
        self.assertTrue(self.app.auth.same_lineage("ses-new", wsid))  # 旧 sid 入血统
        self.assertEqual(self.app.state.get(tid)["worker_sid"], "ses-new")  # 行覆写
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "worker_rebound"]
        self.assertEqual([(e["task"], e["from"], e["to"]) for e in ev],
                         [(tid, wsid, "ses-new")])  # 改绑审计
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "closed"))  # 全链照常走完

    def test_foreign_or_missing_token_rejected_without_rebind(self):  # Req: F19
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        other = self.app.auth.issue("ses-x", "worker")  # 他牌：注册 sid 无主
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, other,
                     sid="ses-y")
        self.assertEqual(st, 400)
        self.assertIn("done 仅任务 worker", r["error"])
        self.assertEqual(self.app.auth.identify(other), ("ses-x", "worker"))  # 未改绑
        self.assertEqual(self.app.state.get(tid)["worker_sid"], wsid)
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, None, sid="ses-y")
        self.assertEqual(st, 401)  # 无牌：既有拒绝路径，零改绑
        self.assertEqual(self.app.state.get(tid)["worker_sid"], wsid)

    def test_terminal_task_no_rebind(self):  # Req: F19
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, _ = call(self.port, "POST", "/task_cancel", {"task": tid}, self.root)
        self.assertEqual(st, 200)  # cancel 已废旧凭据
        dead = self.app.auth.issue(wsid, "worker")  # 终态后持同 sid 牌
        st, _ = call(self.port, "POST", "/wiki_read",
                     {"path": "notes/none.md"}, dead, sid="ses-new")
        self.assertEqual(st, 404)  # 工具面照常应答
        self.assertEqual(self.app.auth.identify(dead), (wsid, "worker"))  # 不改绑
        self.assertEqual(self.app.state.get(tid)["worker_sid"], wsid)  # 行不动

    def test_forged_victim_sid_header_cannot_hijack(self):  # Req: F18（终闸 C 册冒充面）
        tid = self.create_task()
        atok = self.dispatch_task(tid)
        avid = self.create_task()
        self.dispatch_task(avid)
        vsid = self.app.state.get(avid)["worker_sid"]
        st, r = call(self.port, "POST", "/post",
                     {"task": avid, "kind": "done", "body": SHA}, atok, sid=vsid)
        self.assertEqual(st, 400)  # 冒充受害 worker：done 照拒
        self.assertIn("done 仅任务 worker", r["error"])
        self.assertEqual(self.app.auth.identify(atok)[0],
                         self.app.state.get(tid)["worker_sid"])  # 牌与己行未动
        self.assertEqual(self.app.state.get(avid)["worker_sid"], vsid)  # 受害行未动


class SwitchRunner:
    """首跑败后续绿：驱动 done→verify_failed→running→done→closed 续修循环。"""

    def __init__(self):
        self.n = 0

    def __call__(self, cwd, cmd, timeout=None, ceiling=None):
        self.n += 1
        return (1, "boom") if self.n == 1 else (0, "pass")


class TestReworkLoop(HttpdTest):
    """F-1：verify_failed/reopened→running 续修边（worker 任意工具调用即开工）。"""

    def setUp(self):
        self.gate_runner = SwitchRunner()
        super().setUp()

    def test_verify_failed_then_rework_reaches_closed(self):  # Req: F7
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, wtok)
        self.assertEqual(r, {"result": "accepted"})
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "verify_failed"))
        st, _ = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "note", "body": "fixed"}, wtok)
        self.assertEqual(st, 200)
        self.assertEqual(self.app.state.get(tid)["state"], "running")  # 续修开工
        self.gate_runner.n = 1  # 尾部修完：verify 转绿
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": SHA}, wtok)
        self.assertEqual(r, {"result": "accepted"})
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "closed"))
        running = [e for e in self.app.store.read_events(tid)
                   if e["event"] == "running"]
        self.assertEqual(len(running), 2)  # 首跑＋续修两段开工均审计


class TestDispatcherRebind(HttpdTest):
    """F21：post 非链边死号改绑放行——壳重启 dispatcher 换号后旧任务可达。"""

    def test_post_dead_dispatcher_number_rebinds_and_delivers(self):  # Req: F21
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        # 壳重启模拟：任务登记 dispatcher_sid 是旧号（stub 无此会话＝死）
        self.app.state.set_fields(tid, dispatcher_sid="ses-old-dead")
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "note", "body": "nudge"},
                     self.root, sid="ses-new-main")
        self.assertEqual(st, 200)
        self.assertEqual(self.app.state.get(tid)["dispatcher_sid"], "ses-new-main")
        self.assertIn(f"[agora] task {tid} note from ses-new-main",
                      self.stub.sessions[wsid]["injects"][-1])  # down 边直达 worker
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "dispatcher_rebind"]
        self.assertEqual([(e["task"], e["from"], e["to"]) for e in ev],
                         [(tid, "ses-old-dead", "ses-new-main")])

    def test_post_live_dispatcher_number_keeps_rejecting(self):  # Req: F21
        tid = self.create_task()
        self.dispatch_task(tid)
        self.app.state.set_fields(tid, dispatcher_sid="ses-other-live")
        self.stub.sessions["ses-other-live"] = {
            "injects": [], "directory": "/"}  # 登记号仍活
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "note", "body": "x"},
                     self.root, sid="ses-new-main")
        self.assertEqual(st, 400)  # 非链边保持既有拒绝
        self.assertIn("非链边", r["error"])
        self.assertEqual(self.app.state.get(tid)["dispatcher_sid"], "ses-other-live")
        self.assertEqual([e for e in self.app.store.read_events()
                          if e["event"] == "dispatcher_rebind"], [])


class TestParamContract(HttpdTest):
    """F13 参数契约：未知 kind 白名单 400＋bad_request 留痕、403 文案附可用面。"""

    def test_unknown_kind_400_lists_vocab(self):  # Req: F13
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "progress", "body": "x"}, wtok)
        self.assertEqual(st, 400)
        self.assertIn("verify_reject", r["error"])  # 列举合法词汇
        self.assertIn("progress", r["error"])  # 附收到值

    def test_bad_kind_post_audits_bad_request_with_worker_actor(self):  # Req: F13
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, _ = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "progress", "body": "x"}, wtok)
        self.assertEqual(st, 400)
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "bad_request" and e["tool"] == "post"]
        self.assertEqual([e["actor"] for e in ev], [wsid])

    def test_worker_board_403_hints_available_surface(self):  # Req: F13
        wtok = self.app.auth.issue("w9", "worker")
        st, r = call(self.port, "POST", "/board", {}, wtok)
        self.assertEqual(st, 403)
        self.assertIn("check_mail", r["error"])  # 可用面指引

    def test_worker_403_hint_points_to_verify_proposal_channel(self):  # Req: F13/F37
        wtok = self.app.auth.issue("w9", "worker")
        st, r = call(self.port, "POST", "/board", {}, wtok)
        self.assertIn("post(kind=verify)", r["error"])  # verify 提案专用通道
        self.assertIn("全局 worker 角色永拒", r["error"])  # F37 验收录回文案
        self.assertNotIn("question", r["error"])  # 不再指问询通道

    def test_unauthenticated_bad_body_400_without_audit(self):  # Req: F13
        st, _ = call(self.port, "POST", "/post", [1], "no-such-token")
        self.assertEqual(st, 400)
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "bad_request"]
        self.assertEqual(ev, [])  # 鉴权前协议噪声不入计量

    def test_bad_request_audit_failure_still_400(self):  # Req: F13
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        orig = self.app.store.append_event

        def boom(*a, **k):
            raise OSError("disk full")

        self.app.store.append_event = boom
        try:
            st, _ = call(self.port, "POST", "/post",
                         {"task": tid, "kind": "progress", "body": "x"}, wtok)
        finally:
            self.app.store.append_event = orig
        self.assertEqual(st, 400)  # 审计写失败不影响应答（best-effort）

    def test_task_timeout_flows_and_bad_values_rejected(self):  # Req: F51
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo",
                      "verify_cmd": "true", "timeout": 600}, self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        self.assertEqual(self.app.state.get(tid)["verify"]["timeout"], 600)
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g2", "project_dir": "/repo",
                      "verify_cmd": "true", "timeout": "600"}, self.root)
        self.assertEqual(st, 400)  # 非整数 400 报因（同 verify_cmd 口径）
        st, r = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": "true", "timeout": 0}, self.root)
        self.assertEqual(st, 403)  # 坏值 core 层 fail-fast（正整数秒文案）
        self.assertEqual(self.app.state.get(tid)["verify"]["timeout"], 600)  # 未被污染
        st, r = call(self.port, "POST", "/task_update",
                     {"task": tid, "verify_cmd": "pytest -q", "timeout": 1200},
                     self.root)
        self.assertEqual(st, 200)
        self.assertEqual(self.app.state.get(tid)["verify"]["timeout"], 1200)

    def test_worker_task_create_with_timeout_403(self):  # Req: F51
        tid = self.create_task({"goal": "g", "project_dir": "/repo",
                                "verify_cmd": "true", "timeout": 600})
        wtok = self.dispatch_task(tid)
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "child", "timeout": 300}, wtok)
        self.assertEqual(st, 403)
        self.assertIn("timeout", r["error"])
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "auth_denied" and "timeout" in str(e.get("note"))]
        self.assertTrue(ev)  # F37 同款 auth_denied 审计

    def test_task_create_model_type_checked_and_projected(self):  # Req: F55
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "model": 123}, self.root)
        self.assertEqual(st, 400)  # 非串 400 报因（同 verify_cmd 口径）
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "verify_cmd": "true",
                      "model": "zai-coding-plan/glm-5.3"}, self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        st, r = call(self.port, "POST", "/board", {"view": "full"}, self.root)
        self.assertEqual(r["tasks"][tid]["model"],
                         "zai-coding-plan/glm-5.3")  # 派发决策投影可审计


class TestStartupRequeue(unittest.TestCase):
    """F-4：kill -9 门中残留（done＋in_gate）→ make_server 重排门队列，verify 幂等重跑。"""

    def test_done_residue_requeued_and_gate_completes(self):  # Req: F6
        tmp = Path(tempfile.mkdtemp())
        try:
            app = assemble(tmp, oc=DeadInjectOc(), gitx=FakeGitx(tmp),
                           read_anchor=lambda env: {"head": SHA,
                                                    "clean": True},
                           runner=ok_runner)
            app.state.add("t1", {
                "title": "t1", "goal": "g", "project_dir": "/repo",
                "env_dir": app.gitx.build_env("/repo", "t1"), "after": None,
                "verify": {"cmd": "pytest -q", "state": "registered",
                           "registered_by": "main", "registered_at": 1.0},
                "state": "done", "commit": SHA, "in_gate": True,
                "dispatcher_sid": "main", "worker_sid": "w1",
                "chain_depth": 1, "fail_streak": 0, "created": 0.0,
                "updated": 0.0})
            httpd = make_server(app)  # 装配即重排 done 残留
            try:
                self.assertTrue(wait_until(
                    lambda: app.state.get("t1")["state"] == "closed"))
                self.assertEqual(app.state.get("t1")["commit"], SHA)
                names = [e["event"] for e in app.store.read_events("t1")]
                self.assertIn("merged", names)
                self.assertIn("closed", names)
            finally:
                httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGateTailCancel(unittest.TestCase):
    """携带项 a：gate 尾 cancelled 分支＝T7 直路径同形（凭据作废＋终止通知）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=DeadInjectOc(), gitx=FakeGitx(self.tmp),
                            read_anchor=lambda env: {"head": SHA,
                                                     "clean": True},
                            runner=fail_runner)
        self.root = self.app.auth.issue("main", "root")
        self.app.state.add("t1", {
            "title": "t1", "goal": "g", "project_dir": "/repo",
            "env_dir": "/envs/t1", "after": None,
            "verify": {"cmd": "pytest -q", "state": "registered",
                       "registered_by": "main", "registered_at": 1.0},
            "state": "running", "dispatcher_sid": "main", "worker_sid": "w1",
            "chain_depth": 1, "fail_streak": 0, "created": 0.0, "updated": 0.0})
        self.wtok = self.app.auth.issue("w1", "worker")
        self.assertEqual(self.app.gate.accept_done("t1", "w1", SHA),
                         "accepted")
        self.app.task.cancel("t1", self.root)  # done 态→pending_cancel 排队

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gate_tail_cancel_revokes_worker_token(self):  # Req: F11
        self.app.gate.run("t1")
        self.assertEqual(self.app.state.get("t1")["state"], "cancelled")
        self.assertIsNone(self.app.auth.identify(self.wtok))  # 门尾凭据作废

    def test_gate_tail_cancel_notifies_worker(self):  # Req: F11
        self.app.gate.run("t1")
        msgs = [m for m in self.app.store.read_messages()
                if m["to"] == "w1" and m["kind"] == "cancel"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("t1", msgs[0]["body"])


class TestCancelBusyGate(HttpdTest):
    """F28 防误杀忙碌闸全链：httpd force 校验＋透传，busy 应答 {cancelled, reason, hint}。"""

    def test_busy_reject_then_force_kills(self):  # Req: F28
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        self.stub.sessions[wsid]["busy"] = True  # stub time.updated>idle＝回合进行中
        st, r = call(self.port, "POST", "/task_cancel", {"task": tid}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r["cancelled"], 0)
        self.assertEqual(r["reason"], "worker_busy")
        self.assertIn("force", r["hint"])
        self.assertEqual(self.app.state.get(tid)["state"], "dispatched")
        ev = [e for e in self.app.store.read_events(tid)
              if e["event"] == "cancel_rejected_busy"]
        self.assertEqual(len(ev), 1)  # 专属审计行
        st, r = call(self.port, "POST", "/task_cancel",
                     {"task": tid, "force": True}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r["cancelled"], 1)  # 强杀照常取消
        self.assertEqual(self.app.state.get(tid)["state"], "cancelled")
        ev = [e for e in self.app.store.read_events(tid)
              if e["event"] == "cancelled"]
        self.assertIn("forced", ev[-1]["note"])  # 审计注明 forced

    def test_force_must_be_bool(self):  # Req: F28（enforce/verify_pending 同口径）
        tid = self.create_task()
        st, r = call(self.port, "POST", "/task_cancel",
                     {"task": tid, "force": "true"}, self.root)
        self.assertEqual(st, 400)  # 字符串 "true" 不得反转成强杀
        self.assertEqual(self.app.state.get(tid)["state"], "todo")

    def test_stub_idle_session_cancels_unchanged(self):  # Req: F28
        tid = self.create_task()
        self.dispatch_task(tid)  # stub 缺省 time 0/0＝不 busy
        st, r = call(self.port, "POST", "/task_cancel", {"task": tid}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"cancelled": 1})  # 应答形状与既有路径一致
        self.assertEqual(self.app.state.get(tid)["state"], "cancelled")


class TestCredentialFallback(unittest.TestCase):
    """携带项 b：dispatch 凭据直投失败→落消息库（check_mail 可取回）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = assemble(self.tmp, oc=DeadInjectOc(), gitx=FakeGitx(self.tmp),
                            read_anchor=lambda env: None, runner=ok_runner)
        self.root = self.app.auth.issue("main", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dispatch_credential_inject_fail_lands_in_library(self):  # Req: F3
        tid = self.app.task.create("g", "/repo", None, self.root, verify_cmd="true")
        tok = self.app.task.dispatch(tid, self.root)
        self.assertEqual(self.app.state.get(tid)["state"], "dispatched")
        msgs = [m for m in self.app.store.read_messages()
                if m["kind"] == "credential"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["to"], "sw1")
        self.assertNotIn(tok, msgs[0]["body"])  # F43：落库消息也不携 token 本体
        self.assertIn(f"file: {self.app.store.home}/credentials/{tid}.token",
                      msgs[0]["body"])  # 指针形态落库，check_mail 补拉可取回
        got = self.app.router.check_mail("sw1")  # worker 补拉可取回
        self.assertEqual([m["body"] for m in got], [msgs[0]["body"]])


class TestHealthThread(unittest.TestCase):
    """R10：health 定时线程按间隔 tick（死 worker 自愈）＋probe 记 oc_version。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = StubOpencode()
        self.stub.start()
        self.app = assemble(self.tmp, oc_url=self.stub.base_url,
                            gitx=FakeGitx(self.tmp),
                            read_anchor=lambda env: None, runner=ok_runner)
        self.app.state.add("t9", {
            "title": "t9", "goal": "g", "project_dir": "/repo",
            "env_dir": "/envs/t9", "after": None,
            "verify": {"cmd": "pytest -q", "state": "registered",
                       "registered_by": "main", "registered_at": 1.0},
            "state": "dispatched", "dispatcher_sid": "main",
            "worker_sid": None, "chain_depth": 1, "fail_streak": 0,
            "created": 0.0, "updated": 0.0})

    def tearDown(self):
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_short_interval_ticks_reinject_and_probe(self):  # Req: F5
        dead = self.app.oc.create("/x", "boot")
        self.stub.kill(dead)
        self.app.state.set_fields("t9", worker_sid=dead)
        start_health_thread(self.app, 0.05)
        self.assertTrue(wait_until(
            lambda: self.app.state.get("t9")["worker_sid"] != dead))
        new = self.app.state.get("t9")["worker_sid"]
        self.assertIn(new, self.stub.sessions)  # 新会话在册＝活（T5：死＝消失）
        self.assertTrue(wait_until(          # 审计行落在 worker 覆写后：等终态非即读
            lambda: any(e["event"] == "reinjected"
                        for e in self.app.store.read_events("t9"))))
        self.assertTrue(wait_until(lambda: (
            self.app.store.read_ephemeral(".agora/service.json") or {})
            .get("oc_version") == VERSION))


class TestAnchors(unittest.TestCase):
    """携带项 d：read_anchor access 默认实现——锚＝env 分支 HEAD 即绿。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_anchor_matches_env_head(self):  # Req: F7
        repo = self.tmp / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(
            ["git", *a], cwd=repo, check=True, capture_output=True, text=True)
        git("init", "-q")
        git("-c", "user.email=a@b", "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "x")
        sha = git("rev-parse", "HEAD").stdout.strip()
        self.assertIsNone(anchors.read_anchor(str(repo)))  # 缺锚文件
        marker = repo / ".review-loop" / ".last-reviewed"
        marker.parent.mkdir()
        marker.write_text(sha, encoding="utf-8")
        self.assertEqual(anchors.read_anchor(str(repo)),
                         {"head": sha, "clean": True})
        marker.write_text("old", encoding="utf-8")  # 锚落后
        self.assertIsNone(anchors.read_anchor(str(repo)))
        self.assertIsNone(anchors.read_anchor(None))  # 未派发无环境


class TestPeerSurface(HttpdTest):
    """F22 同仓协作面全链：peer 写读/分区/权限/BOOT 契约行。"""

    def dispatch_two(self, repo_a="/repoA", repo_b="/repoB"):
        ta = self.create_task({"goal": "g", "project_dir": repo_a})
        wa = self.dispatch_task(ta)
        tb = self.create_task({"goal": "g", "project_dir": repo_b})
        wb = self.dispatch_task(tb)
        return ta, wa, tb, wb

    def test_worker_peer_write_read_loop(self):  # Req: F22
        tid = self.create_task({"goal": "g", "project_dir": "/repoX"})
        wtok = self.dispatch_task(tid)
        st, r = call(self.port, "POST", "/post",
                     {"kind": "peer", "body": "hi from worker"}, wtok)
        self.assertEqual(st, 200)
        self.assertIn("message", r)
        st, r = call(self.port, "POST", "/repo_board", {}, wtok)
        self.assertEqual(st, 200)
        self.assertEqual(r["repo"], "/repoX")  # worker 分区＝己任务仓
        self.assertEqual([t["tid"] for t in r["tasks"]], [tid])
        (m,) = r["messages"]
        self.assertEqual((m["author"], m["body"], m["untrusted"]),
                         (tid, "hi from worker", True))
        st, r = call(self.port, "POST", "/repo_board", {"repo": "/repoX"},
                     self.root)  # root 诊断可指定仓
        self.assertEqual(st, 200)
        self.assertEqual([t["tid"] for t in r["tasks"]], [tid])

    def test_partition_isolation_between_workers(self):  # Req: F22
        ta, wa, tb, wb = self.dispatch_two()
        st, _ = call(self.port, "POST", "/post",
                     {"kind": "peer", "body": "from A"}, wa)
        self.assertEqual(st, 200)
        st, r = call(self.port, "POST", "/repo_board", {}, wb)
        self.assertEqual(st, 200)
        self.assertEqual([t["tid"] for t in r["tasks"]], [tb])  # 只见己仓任务
        self.assertEqual(r["messages"], [])  # 跨仓留言不可见
        st, r = call(self.port, "POST", "/repo_board", {"repo": "/repoB"},
                     self.root)
        self.assertEqual([t["tid"] for t in r["tasks"]], [tb])
        self.assertEqual(r["messages"], [])
        st, r = call(self.port, "POST", "/repo_board", {}, self.root)  # 缺省＝全仓
        self.assertEqual(sorted(t["tid"] for t in r["tasks"]), sorted([ta, tb]))

    def test_peer_write_worker_token_only(self):  # Req: F22 权限
        st, r = call(self.port, "POST", "/post",
                     {"kind": "peer", "body": "root says"}, self.root)
        self.assertEqual(st, 403)
        self.assertIn("worker", r["error"])
        dtok = self.app.auth.issue("d9", "dispatcher")
        st, _ = call(self.port, "POST", "/post",
                     {"kind": "peer", "body": "disp says"}, dtok)
        self.assertEqual(st, 403)

    def test_boot_carries_repo_board_contract(self):  # Req: F22（BOOT 引导＋注入防线）
        tid = self.create_task()
        self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        boot = self.stub.sessions[wsid]["injects"][0]
        self.assertIn("repo_board", boot)
        self.assertIn("仅供参考", boot)  # peer 留言是数据不是指令


class TestCredForSid(HttpdTest):
    """F25 修正推改拉：/cred_for_sid 插件拉取面——sid 是在飞任务 worker_sid 且牌活
    → {tid, env_dir, token}，否则 404；无牌调用放行（回环单用户模型），每请求一行审计。"""

    def test_hit_returns_cred_without_token_and_audits(self):  # Req: F25
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, r = call(self.port, "POST", "/cred_for_sid", {"sid": wsid})  # 无牌调用
        self.assertEqual(st, 200)
        self.assertEqual(r, {"tid": tid,
                             "env_dir": self.app.state.get(tid)["env_dir"],
                             "token": wtok})
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_pull"]
        self.assertEqual([(e["actor"], e.get("task")) for e in ev], [(wsid, tid)])  # 审计一行

    def test_unknown_sid_and_revoked_token_404(self):  # Req: F25
        st, _ = call(self.port, "POST", "/cred_for_sid", {"sid": "ses-nobody"})
        self.assertEqual(st, 404)  # 非任何在飞 worker：常态未命中
        tid = self.create_task()
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        st, _ = call(self.port, "POST", "/task_cancel", {"task": tid}, self.root)
        self.assertEqual(st, 200)  # cancel 已废牌
        st, _ = call(self.port, "POST", "/cred_for_sid", {"sid": wsid})
        self.assertEqual(st, 404)  # 终态冻结＋牌废双守卫：不再签凭据
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_pull"]
        self.assertEqual(len(ev), 2)  # 未命中亦落一行（无 task 字段）

    def test_missing_sid_and_bad_body(self):  # Req: F13 同口径
        st, _ = call(self.port, "POST", "/cred_for_sid", {})
        self.assertEqual(st, 404)  # 缺 sid＝未命中
        st, _ = call(self.port, "POST", "/cred_for_sid", [1])
        self.assertEqual(st, 400)  # body 非 JSON 对象


class TestPerWindowDispatchCred(HttpdTest):
    """ag-af96bb① 每窗口独立派发牌：root 牌持有者经 /cred_for_sid 拉取面为
    新 sid（无牌）签发 dispatcher 牌（服务端单侧，插件 lift 适配属后续任务）——
    多窗口操作审计/邮件路由归各自 sid（混 sid 修复）。"""

    def test_root_issues_dispatcher_token_for_new_sid(self):  # ag-af96bb①
        st, r = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-b"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r["role"], "dispatcher")
        self.assertEqual(self.app.auth.identify(r["token"]),
                         ("ses-win-b", "dispatcher"))  # issue(sid, role) 已有机制
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_issue"]
        self.assertEqual([(e["actor"], e["note"]) for e in ev],
                         [("main", "dispatcher:ses-win-b")])  # 签发审计一行

    def test_dispatched_task_attributes_to_own_sid(self):  # ag-af96bb① 混 sid 归位
        _, r = call(self.port, "POST", "/cred_for_sid",
                    {"sid": "ses-win-b"}, self.root)
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo"},
                     r["token"], sid="ses-win-b")
        self.assertEqual(st, 200)
        self.assertEqual(self.app.state.get(r["task"])["dispatcher_sid"],
                         "ses-win-b")  # 任务归本窗口 sid（非 root 绑定 sid）
        ev = [e for e in self.app.store.read_events()
              if e["event"] == "task_created"]
        self.assertEqual([e["actor"] for e in ev], ["ses-win-b"])  # 审计同归位

    def test_issue_idempotent_pull_returns_same_token(self):  # ag-af96bb①
        _, r1 = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-b"}, self.root)
        _, r2 = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-b"}, self.root)
        self.assertEqual(r1["token"], r2["token"])  # 已持牌＝拉取不重签
        self.assertEqual(r2["role"], "dispatcher")
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_issue"]
        self.assertEqual(len(ev), 1)

    def test_bad_root_header_keeps_worker_pull_semantics(self):  # ag-af96bb①
        st, _ = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-b"}, "garbage")
        self.assertEqual(st, 404)  # 非 root 牌：回落原 worker 拉取面（未命中 404）
        st, _ = call(self.port, "POST", "/cred_for_sid", {"sid": ""}, self.root)
        self.assertEqual(st, 404)  # 空 sid 不签发


class TestRootPoisonReissue(HttpdTest):
    """ag-588eb8 裁决 A：会话 sid 持 root 牌（R25-A 活绑定产物）＝污染——
    cred_issue 换牌面幂等回 dispatcher 新牌；root 条目保留不撤（root 常为唯一
    治理牌，撤销即断 CLI/enforce/doctor）。换牌防膨胀：先扫该 sid 名下非 root
    活牌，有则幂等回它，无才新签（插件侧有牌缓存，换牌是低频事件）。"""

    def test_poisoned_sid_reissues_dispatcher_and_keeps_root_entry(self):
        tok_r = self.app.auth.issue("ses-win-p", "root")  # 预置污染条目
        st, r = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-p"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(r["role"], "dispatcher")  # 换牌回 dispatcher（非 root）
        self.assertNotEqual(r["token"], tok_r)
        self.assertEqual(self.app.auth.identify(r["token"]),
                         ("ses-win-p", "dispatcher"))
        self.assertEqual(self.app.auth.identify(tok_r),
                         ("ses-win-p", "root"))  # 污染条目保留不撤（裁决授权偏离）
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_issue"]
        self.assertEqual(ev[-1]["note"],
                         "dispatcher:ses-win-p root-poison reissue")  # 审计一行
        st, r2 = call(self.port, "POST", "/cred_for_sid",
                      {"sid": "ses-win-p"}, self.root)
        self.assertEqual(r2["token"], r["token"])  # 再问幂等回同牌不重铸
        self.assertEqual(r2["role"], "dispatcher")
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_issue"]
        self.assertEqual(len(ev), 1)  # 签发仅一次

    def test_reissue_prefers_existing_non_root_token(self):
        _, d1 = call(self.port, "POST", "/cred_for_sid",
                     {"sid": "ses-win-b"}, self.root)
        self.app.auth.issue("ses-win-b", "root")  # 事后污染叠加（双角色并存）
        st, d2 = call(self.port, "POST", "/cred_for_sid",
                      {"sid": "ses-win-b"}, self.root)
        self.assertEqual(st, 200)
        self.assertEqual(d2["role"], "dispatcher")
        self.assertEqual(d2["token"], d1["token"])  # 幂等回既有非 root 活牌
        ev = [e for e in self.app.store.read_events() if e["event"] == "cred_issue"]
        self.assertEqual(len(ev), 1)  # 不因污染叠加重签


class TestOutOfBandChain(HttpdTest):
    """ag-af96bb② 全链：deliverable 建单→派发→零提交 done→verify 绿→closed（零合入）。"""

    def test_full_chain_zero_commit_done_closes(self):  # ag-af96bb②
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "评审-only", "project_dir": "/repo",
                      "deliverable": "out_of_band"}, self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        self.assertEqual(self.app.state.get(tid)["deliverable"], "out_of_band")
        wtok = self.dispatch_task(tid)
        wsid = self.app.state.get(tid)["worker_sid"]
        boot = self.stub.sessions[wsid]["injects"][0]
        self.assertIn("零提交豁免", boot)  # BOOT done 行换装
        st, r = call(self.port, "POST", "/post",
                     {"task": tid, "kind": "done", "body": "docs/review.md 在案"},
                     wtok)
        self.assertEqual(st, 200)
        self.assertEqual(r, {"result": "accepted"})  # 零提交 done 受理
        self.assertTrue(wait_until(
            lambda: self.app.state.get(tid)["state"] == "closed"))
        t = self.app.state.get(tid)
        self.assertNotIn("commit", t)  # 无 sha 落账
        self.assertEqual(getattr(self.gitx, "pushes", []), [])  # 零合入零 push
        self.assertEqual(self.gitx.removed, [t["env_dir"]])  # 环境照常回收


class TestReqIdPassthrough(HttpdTest):
    """F34：req_id httpd→task 全链透传＋重复发放非 200 拒。"""

    def req_repo(self):
        repo = self.tmp / "repo"
        (repo / "docs" / "reqs").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "reqs" / "opencode-agora.md").write_text("### F33 x\n")
        return str(repo)

    def test_create_and_duplicate_403_with_pointer(self):  # Req: F34
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": self.req_repo(),
                      "req_id": "F34"}, self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        self.assertEqual(self.app.state.get(tid)["req_id"], "F34")
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": self.req_repo(),
                      "req_id": "F34"}, self.root)
        self.assertEqual(st, 403)  # 重复发放非 200
        self.assertIn(tid, r["error"])  # 报因含已发任务指针

    def test_non_string_req_id_400(self):  # Req: F34
        st, _ = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "req_id": 34},
                     self.root)
        self.assertEqual(st, 400)  # 非串 400 报因（同 verify_cmd 口径）


class TestQuickMicro(HttpdTest):
    """F67 quick 微任务全链：端点布尔校验/透传＋账本历史判定原子登记＋BOOT 减配。"""

    def merged(self, cmd="true"):
        """播种判定证据：非 quick 任务建单带 cmd，直迁 merged（账本历史）。"""
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "verify_cmd": cmd},
                     self.root)
        assert st == 200, r
        tid = r["task"]
        self.app.state.transition(tid, "dispatched")
        self.app.state.transition(tid, "running")
        self.app.state.transition(tid, "done", commit="c" * 40)
        self.app.state.transition(tid, "merged")

    def test_quick_history_registers_and_boot_loses_three_lines(self):  # Req: F67
        self.merged()
        self.merged()
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "quick": True},
                     self.root)
        self.assertEqual(st, 200)
        tid = r["task"]
        t = self.app.state.get(tid)
        self.assertTrue(t["quick"])  # 行内落旗（快照随行）
        self.assertEqual(t["verify"]["state"], "registered")  # 建单原子登记
        self.assertEqual(t["verify"]["cmd"], "true")
        st, r = call(self.port, "POST", "/task_dispatch", {"task": tid}, self.root)
        self.assertEqual(st, 200)  # 登记即过派发硬闸（免两步舞）
        boot = self.stub.sessions[self.app.state.get(tid)["worker_sid"]]["injects"][0]
        for line in ("嵌套:", "协作:", "沉淀:"):
            self.assertNotIn(line, boot)  # F65 减配三行
        self.assertIn("开工:", boot)  # 其余行逐字不动
        self.assertIn("done:", boot)

    def test_normal_boot_keeps_three_lines(self):  # Req: F65
        tid = self.create_task()
        self.dispatch_task(tid)
        boot = self.stub.sessions[self.app.state.get(tid)["worker_sid"]]["injects"][0]
        for line in ("嵌套:", "协作:", "沉淀:"):
            self.assertIn(line, boot)  # 非 quick BOOT 不变

    def test_quick_without_history_403(self):  # Req: F67
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "quick": True},
                     self.root)
        self.assertEqual(st, 403)
        self.assertIn("证据不足", r["error"])
        self.assertNotIn("无该仓条目", r["error"])

    def test_quick_with_verify_cmd_403(self):  # Req: F65
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "quick": True,
                      "verify_cmd": "pytest -q"}, self.root)
        self.assertEqual(st, 403)
        self.assertIn("勿自带命令", r["error"])

    def test_quick_must_be_bool(self):  # Req: F65（verify_pending/enforce 同口径）
        st, r = call(self.port, "POST", "/task_create",
                     {"goal": "g", "project_dir": "/repo", "quick": "yes"},
                     self.root)
        self.assertEqual(st, 400)
        self.assertIn("quick", r["error"])


if __name__ == "__main__":
    unittest.main()
