"""T10 core/report 视图契约：五桶/链视图/明细/doctor/HTML（纯函数，真文件无 mock）。"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.report import (_env_activity, buckets, chain_view, detail,
                                doctor_view, forest_view, stale_candidates,
                                view_html)


def row(state="running", disp="d1", worker="w1", created=0.0, last_seen=None,
        env=None, title="目标"):
    return {"title": title, "state": state, "dispatcher_sid": disp,
            "worker_sid": worker, "created": created, "last_seen": last_seen,
            "env_dir": env, "fail_streak": 0, "chain_depth": 1}


def snap(**tasks):
    return {"version": 1, "tasks": tasks, "mail_read": {}}


class TestReport(unittest.TestCase):
    def test_buckets_five(self):  # Req: F10
        s = snap(t1=row("merged"), t2=row("closed"), t3=row("verify_failed"),
                 t4=row("reopened"), t5=row("dispatched"), t6=row("done"),
                 t7=row("blocked"), t8=row("cancelled"), t9=row("todo"))
        b = buckets(s)
        self.assertEqual(b["已合并"], ["t1", "t2"])
        self.assertEqual(b["在飞"], ["t3", "t4", "t5", "t6", "t9"])
        self.assertEqual(b["阻塞"], ["t7"])
        self.assertEqual(b["已取消"], ["t8"])
        self.assertEqual(b["失败"], [])  # v2 无独立失败终态：verify_failed 归在飞

    def test_chain_view_two_levels(self):  # Req: F10
        s = snap(t1=row(disp="d1", worker="w1"), t2=row(disp="w1", worker="w2"),
                 t3=row(disp="w2", worker="w3"), tx=row(disp="other"))
        self.assertEqual(chain_view(s, "d1"), ["t1", "t2", "t3"])
        self.assertEqual(chain_view(s, "w2"), ["t3"])
        self.assertEqual(chain_view(s, "nobody"), [])

    def test_chain_view_cycle_terminates(self):  # Req: F10
        s = snap(t1=row(disp="d1", worker="w1"), t2=row(disp="w1", worker="d1"))
        self.assertEqual(chain_view(s, "d1"), ["t1", "t2"])  # 环有界不挂死

    def test_forest_view_multi_root_cluster(self):  # Req: gt-6def73
        s = snap(t1=row(disp="d1", worker="w1"), t2=row(disp="w1", worker="w2"),
                 t3=row(disp="w2", worker="w3"), tx=row(disp="d9", worker="w9"),
                 ta=row(disp="d8", worker="w8"))
        self.assertEqual(forest_view(s),
                         ["t1", "t2", "t3", "ta", "tx"])  # 根层 tid 序（t1<ta<tx），子代紧随父代

    def test_forest_view_cycle_bounded(self):  # Req: gt-6def73
        s = snap(t1=row(disp="d1", worker="w1"), t2=row(disp="w1", worker="d1"),
                 t3=row(disp="d2", worker="w3"))
        out = forest_view(s)  # 环有界不挂死，环岛尾附，全员在列
        self.assertEqual(sorted(out), ["t1", "t2", "t3"])
        self.assertEqual(out[:1], ["t3"])  # 唯一根在前

    def test_forest_view_self_parent_bounded(self):  # Req: gt-6def73
        s = snap(t1=row(disp="d1", worker="d1"))  # 自环：dispatcher=worker
        self.assertEqual(forest_view(s), ["t1"])

    def test_detail_row_events_done_body(self):  # Req: F10
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            st.append_event({"ts": 1.0, "actor": "d1", "event": "dispatched",
                             "task": "t1"})
            st.append_event({"ts": 2.0, "actor": "w1", "event": "done",
                             "task": "t1"})
            st.append_event({"ts": 3.0, "actor": "d1", "event": "dispatched",
                             "task": "t2"})
            st.write_message({"task": "t1", "kind": "done"}, "结论 v1")
            st.write_message({"task": "t2", "kind": "done"}, "别家结论")
            st.write_message({"task": "t1", "kind": "done"}, "结论 v2")
            d = detail(snap(t1=row(), t2=row()), st, "t1")
            self.assertEqual(d["task"]["state"], "running")
            self.assertEqual([e["event"] for e in d["events"]],
                             ["dispatched", "done"])
            self.assertEqual(d["done_body"], "结论 v2")  # 最新 done 正文＝当前结论
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_doctor_view_ranks_and_env(self):  # Req: F10
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "envs" / "e1").mkdir(parents=True)
            s = snap(t1=row(created=100.0, last_seen=None,
                            env=str(tmp / "envs" / "e1")),
                     t2=row(created=50.0, last_seen=999.0,
                            env=str(tmp / "gone")))
            v = doctor_view(s, tmp)
            self.assertEqual([tid for tid, _ in v["age_rank"]], ["t2", "t1"])
            self.assertEqual({e["tid"]: e["on_disk"] for e in v["env"]},
                             {"t1": True, "t2": False})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_doctor_view_stale_candidates(self):  # Req: F5
        s = snap(t1=row("running", created=100.0, last_seen=300.0),   # >20min 无活动→标
                 t2=row("running", created=100.0, last_seen=1599.0),  # 刚动过→不标
                 t3=row("dispatched", created=100.0, last_seen=None),  # 从未上报→created 基线
                 t4=row("merged", created=0.0, last_seen=0.0),         # 终态→不标
                 t5=row("blocked", created=0.0, last_seen=0.0),        # blocked 已拍人→不标
                 t6=row("todo", created=100.0, last_seen=None))        # todo 无活动面→不标
        v = doctor_view(s, Path("."), now=1600.0)
        self.assertEqual(v["stale_candidates"],
                         [["t3", 100.0], ["t1", 300.0]])  # 基线升序（最僵在前）
        self.assertEqual([age for _, age in v["age_rank"]],
                         [1600.0, 1600.0, 1500.0, 1500.0, 1300.0, 1.0])  # F15 融合基线：t1(300)→1300、t2(1599)→1，复用同一 now

    def test_stale_candidates_skip_in_gate(self):  # Req: F5
        s = snap(tg=dict(row("done"), in_gate=True), tl=row("done"))
        self.assertEqual(stale_candidates(s, 3000.0), [["tl", 0.0]])  # 门内静默属正常：免判；同态无闩照拍

    def test_env_activity_missing_or_empty_zero(self):  # Req: F15
        self.assertEqual(_env_activity(None), 0.0)
        self.assertEqual(_env_activity("/nonexistent-env-dir"), 0.0)

    def test_stale_candidates_env_activity(self):  # Req: F15
        tmp = Path(tempfile.mkdtemp())
        try:
            live, dead = tmp / "live", tmp / "dead"
            live.mkdir()
            dead.mkdir()
            (live / "f.txt").write_text("x")
            (dead / "f.txt").write_text("x")
            os.utime(live / "f.txt", (2900.0, 2900.0))  # env 在写
            os.utime(dead / "f.txt", (500.0, 500.0))  # env 静默
            os.utime(live, (2900.0, 2900.0))
            os.utime(dead, (500.0, 500.0))
            s = snap(t1=row(created=100.0, last_seen=300.0, env=str(live)),
                     t2=row(created=100.0, last_seen=300.0, env=str(dead)),
                     t3=row(created=100.0, last_seen=300.0, env=str(tmp / "gone")))
            self.assertEqual(stale_candidates(s, 3000.0),
                             [["t3", 300.0], ["t2", 500.0]])  # env 在写＝活着不进候选；缺失退 last_seen 基线
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detail_env_active_seconds_ago(self):  # Req: F15
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            env = tmp / "e1"
            env.mkdir()
            (env / "f.txt").write_text("x")
            past = time.time() - 120.0
            os.utime(env / "f.txt", (past, past))
            os.utime(env, (past, past))
            d = detail(snap(t1=row(env=str(env))), st, "t1")
            self.assertGreaterEqual(d["env_active_seconds_ago"], 115)  # now-最新 mtime（int 秒）
            self.assertLessEqual(d["env_active_seconds_ago"], 130)
            self.assertNotIn("env_active_seconds_ago",  # env 回收→省略
                             detail(snap(t2=row(env=str(tmp / "gone"))), st, "t2"))
            self.assertNotIn("env_active_seconds_ago",  # 未设 env→省略
                             detail(snap(t3=row()), st, "t3"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_doctor_view_env_fused_ranks(self):  # Req: F15
        tmp = Path(tempfile.mkdtemp())
        try:
            live, dead = tmp / "live", tmp / "dead"
            live.mkdir()
            dead.mkdir()
            (live / "f.txt").write_text("x")
            (dead / "f.txt").write_text("x")
            os.utime(live / "f.txt", (2900.0, 2900.0))
            os.utime(dead / "f.txt", (500.0, 500.0))
            os.utime(live, (2900.0, 2900.0))
            os.utime(dead, (500.0, 500.0))
            s = snap(t1=row(created=100.0, last_seen=300.0, env=str(live)),
                     t2=row(created=100.0, last_seen=None, env=str(dead)),
                     t3=row(created=50.0, last_seen=999.0))
            v = doctor_view(s, tmp, now=3000.0)
            self.assertEqual(v["age_rank"],  # 融合基线升序（最僵在前）
                             [["t2", 2500.0], ["t3", 2001.0], ["t1", 100.0]])
            self.assertEqual(v["last_seen_rank"],
                             [["t2", 500.0], ["t3", 999.0], ["t1", 2900.0]])
            self.assertEqual(v["stale_candidates"],
                             [["t2", 500.0], ["t3", 999.0]])  # env 在写的 t1 不进候选
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_view_html_five_buckets(self):  # Req: F10
        s = snap(t1=row("merged", disp="d1"), t2=row("running"))
        html = view_html(s)
        for w in ("已合并", "在飞", "失败", "阻塞", "已取消", "t1", "d1"):
            self.assertIn(w, html)


if __name__ == "__main__":
    unittest.main()
