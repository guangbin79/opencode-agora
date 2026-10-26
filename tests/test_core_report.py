"""T10 core/report 视图契约：五桶/链视图/明细/doctor/HTML（纯函数，真文件无 mock）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.report import buckets, chain_view, detail, doctor_view, view_html


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
            self.assertEqual([tid for tid, _ in v["last_seen_rank"]],
                             ["t1", "t2"])  # 从未上报列最僵
            self.assertEqual({e["tid"]: e["on_disk"] for e in v["env"]},
                             {"t1": True, "t2": False})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_view_html_five_buckets(self):  # Req: F10
        s = snap(t1=row("merged", disp="d1"), t2=row("running"))
        html = view_html(s)
        for w in ("已合并", "在飞", "失败", "阻塞", "已取消", "t1", "d1"):
            self.assertIn(w, html)


if __name__ == "__main__":
    unittest.main()
