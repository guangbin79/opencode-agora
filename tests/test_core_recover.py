"""T9 core/recover 契约：启动复查重注入/终态不注入/凭据不动/失败不半写（R1）。

真 Store/State/Auth＋假 oc/gitx 全注入；恢复只管死（dispatched/running），
终态/blocked/verify_failed(等续修) 复查后一律不重注入。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.oc import OcUnavailable
from agorad.access.store import Store
from agorad.core.auth import Auth
from agorad.core.msg import Router
from agorad.core.recover import Recover
from agorad.core.state import State


class FakeOc:
    def __init__(self):
        self.liveness = {}  # sid->bool；缺席＝死
        self.sessions, self.injected = [], []
        self.down, self.fail_create = False, False

    def alive(self, sid):
        if self.down:
            raise OcUnavailable("stub down")
        return bool(self.liveness.get(sid))

    def create(self, cwd, boot, title=None):
        if self.fail_create:
            raise OcUnavailable("stub down")
        sid = f"r{len(self.sessions) + 1}"
        self.sessions.append((sid, cwd, boot))
        return sid

    def inject(self, sid, text):
        self.injected.append((sid, text))
        return True


class FakeGitx:
    def __init__(self, env_gone=False):
        self.env_gone, self.heads = env_gone, []

    def head(self, repo_dir):
        if self.env_gone:
            raise RuntimeError("git rev-parse: 环境已失")
        self.heads.append(repo_dir)
        return "aaa111"


class TestCoreRecover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp)
        self.state = State(self.store)
        self.auth = Auth(self.store)
        self.oc = FakeOc()
        self.gitx = FakeGitx()
        self.rec = Recover(self.state, self.oc, self.gitx, self.store, self.auth)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 夹具 ----

    def seed(self, tid, state="running", worker="w1"):
        self.state.add(tid, {"title": tid, "goal": "g", "project_dir": "/repo",
                             "env_dir": f"/envs/{tid}",
                             "verify": {"cmd": "pytest -q", "state": "registered",
                                        "registered_by": "main", "registered_at": 1.0},
                             "state": state, "dispatcher_sid": "main",
                             "worker_sid": worker, "chain_depth": 1,
                             "fail_streak": 0, "created": 0.0, "updated": 0.0})
        return tid

    def events(self):
        path = self.tmp.joinpath("events.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line]

    # ---- startup：in-flight 复查重注入 ----

    def test_startup_reinjects_running_dead_worker(self):  # Req: F6
        self.seed("t1")
        tok = self.auth.issue("w1", "worker")  # 牌持久化；恢复不碰凭据
        self.rec.startup()
        self.assertEqual(len(self.oc.sessions), 1)
        sid, env, boot = self.oc.sessions[0]
        self.assertEqual(env, "/envs/t1")  # 同环境现场
        self.assertIn(f"{self.tmp}/wiki/tasks/t1.md", boot)  # 任务页
        self.assertIn("check_mail", boot)  # 断点补拉指引
        self.assertEqual(self.state.get("t1")["worker_sid"], sid)  # 新 sid 覆写
        self.assertEqual(self.state.get("t1")["state"], "running")  # 非迁移
        ev = [e for e in self.events() if e["event"] == "reinjected"]
        self.assertEqual(ev, [{"ts": ev[0]["ts"], "actor": "agorad",
                               "event": "reinjected", "task": "t1",
                               "worker": sid}])
        self.assertEqual(self.auth.identify(tok), (sid, "worker"))  # 牌重绑新 sid
        cred = [i for i in self.oc.injected if "credential" in i[1]]
        self.assertEqual(cred,  # 凭据随 dispatch 同形送达新会话
                         [(sid, f"[agora] credential t1\ntoken: {tok}")])

    def test_startup_spares_dead_worker_of_non_inflight(self):  # Req: F6
        for i, st in enumerate(("cancelled", "blocked", "verify_failed",
                                "done", "merged", "closed")):
            self.seed(f"t{i}", state=st)
        self.rec.startup()
        self.assertEqual(self.oc.sessions, [])  # 终态/blocked/等续修不注入
        self.assertEqual([e for e in self.events()
                          if e["event"] == "reinjected"], [])

    def test_startup_spares_alive_worker(self):  # Req: F6
        self.seed("t1")
        self.oc.liveness["w1"] = True
        self.rec.startup()
        self.assertEqual(self.oc.sessions, [])

    def test_startup_clears_stale_gate_latch(self):  # Req: F5
        self.seed("t1", state="done")
        self.state.set_fields("t1", in_gate=True)  # kill -9 残闩
        self.rec.startup()
        self.assertFalse(self.state.get("t1")["in_gate"])
        ev = [e for e in self.events() if e["event"] == "gate_latch_reset"]
        self.assertEqual([(e["task"], e["actor"]) for e in ev],
                         [("t1", "agorad")])  # 恰一条审计行

    def test_startup_oc_down_neutral(self):  # Req: F6
        self.oc.down = True
        self.seed("t1")
        self.rec.startup()  # oc 不可达：不判死不误注入
        self.assertEqual(self.oc.sessions, [])

    # ---- reinject：复查/失败不半写 ----

    def test_reinject_refuses_non_inflight(self):  # Req: F6
        self.seed("t1", state="cancelled")
        self.seed("t2", state="todo", worker=None)
        self.assertFalse(self.rec.reinject("t1"))
        self.assertFalse(self.rec.reinject("t2"))
        self.assertEqual(self.oc.sessions, [])

    def test_reinject_failure_returns_false_no_overwrite(self):  # Req: F6
        self.oc.fail_create = True
        self.seed("t1")
        self.assertFalse(self.rec.reinject("t1"))
        self.assertEqual(self.state.get("t1")["worker_sid"], "w1")  # 不半写
        self.assertEqual([e for e in self.events()
                          if e["event"] == "reinjected"], [])

    def test_reinject_env_gone_fails(self):  # Req: F6
        self.gitx.env_gone = True
        self.seed("t1")
        self.assertFalse(self.rec.reinject("t1"))  # 环境已失不盲注
        self.assertEqual(self.oc.sessions, [])
        self.assertEqual(self.state.get("t1")["worker_sid"], "w1")

    def test_reinjected_worker_posts_over_chain_edge(self):  # Req: F6
        self.seed("t1")
        self.auth.issue("w1", "worker")
        self.rec.reinject("t1")
        new_sid = self.state.get("t1")["worker_sid"]
        router = Router(self.store, self.state, self.oc)
        mid = router.post(new_sid, ("up", "t1"), "process", "续跑中")  # 链边通
        msgs = [m for m in self.store.read_messages() if m["to"] == "main"]
        self.assertEqual([m["id"] for m in msgs], [mid])
        self.assertIn("续跑中", msgs[0]["body"])

    def test_reinject_unknown_task_false(self):  # Req: F6
        self.assertFalse(self.rec.reinject("nope"))


if __name__ == "__main__":
    unittest.main()
