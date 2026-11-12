"""T10 core/report 视图契约：五桶/链视图/明细/doctor/HTML（纯函数，真文件无 mock）。"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agorad.access.store import Store
from agorad.core.report import (_env_activity, _model_stats, buckets,
                                chain_view, classify, detail, doctor_view,
                                forest_view, full_view, stale_candidates,
                                view_html)
from agorad.core.verify_hygiene import find_verify_hygiene_issues


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

    def test_buckets_delivery_failed_blocked_split(self):  # Req: F98
        """blocked 分桶：交付败（delivery.status=failed）归失败桶，其余 blocked
        （质量类）留阻塞桶；在飞行不受 delivery 投影影响。"""
        s = snap(
            t1=row("blocked"),
            t2=dict(row("blocked"), delivery={
                "mode": "managed", "status": "failed",
                "error": "rebase_conflict"}),
            t3=dict(row("running"), delivery={
                "mode": "managed", "status": "failed"}),
            t4=dict(row("blocked"), delivery={
                "mode": "managed", "status": "pending"}))
        b = buckets(s)
        self.assertEqual(b["失败"], ["t2"])
        self.assertEqual(b["阻塞"], ["t1", "t4"])
        self.assertEqual(b["在飞"], ["t3"])

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

    def test_forest_view_same_layer_recent_first(self):  # Req: gt-c05d6e
        s = snap(ta=row(disp="d1", worker="wa", created=100.0),
                 tb=row(disp="d2", worker="wb", created=300.0),
                 tc=row(disp="d3", worker="wc", created=200.0),
                 t1=row(disp="wa", worker="w1", created=50.0),
                 t2=row(disp="wa", worker="w2", created=400.0))
        self.assertEqual(forest_view(s),
                         ["tb", "tc", "ta", "t2", "t1"])  # 根层与子层同规：created 降序近者在前
        s["tasks"]["tx"] = row(disp="d9", worker="w9")  # created 缺失→兜底 tid 序垫底
        self.assertEqual(forest_view(s)[-1], "tx")

    def test_forest_view_cycle_bounded(self):  # Req: gt-6def73
        s = snap(t1=row(disp="d1", worker="w1"), t2=row(disp="w1", worker="d1"),
                 t3=row(disp="d2", worker="w3"))
        out = forest_view(s)  # 环有界不挂死，环岛尾附，全员在列
        self.assertEqual(sorted(out), ["t1", "t2", "t3"])
        self.assertEqual(out[:1], ["t3"])  # 唯一根在前

    def test_forest_view_self_parent_bounded(self):  # Req: gt-6def73
        s = snap(t1=row(disp="d1", worker="d1"))  # 自环：dispatcher=worker
        self.assertEqual(forest_view(s), ["t1"])

    def test_full_view_projection_and_events(self):  # Req: F48
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            st.append_event({"ts": 1.0, "actor": "h", "event": "escalate",
                             "task": "t1", "kind": "quality"})
            st.append_event({"ts": 2.0, "actor": "d", "event": "dispatched",
                             "task": "t1"})
            t = row(disp="d1", worker="w1", last_seen=900.0)
            t["project_dir"] = "/repo"        # 白名单内
            t["env_dir"] = "/repo/envs/t1"    # 白名单外：不得入投影
            t["fail_streak"] = 2
            t["commit"] = "abc"
            t["in_gate"] = True              # F54: 门内字段入投影
            t["usage"] = {"input": 5}        # F57: usage 经济性投影（dict 原样带）
            t3 = row(state="closed", disp="d2", worker="w3")
            t3["model"] = "glm-5.3"          # F57: closed 无失败事件 → merged
            t4 = row(state="closed", disp="d2", worker="w4")
            t4["model"] = "flash"
            st.append_event({"ts": 3.0, "actor": "d", "event": "reopened",
                             "task": "t4"})  # F57: rework 事件按 tid 归桶
            v = full_view(snap(t1=t, t2=row(state="todo"), t3=t3, t4=t4), st)
            self.assertEqual(sorted(v), ["buckets", "events", "forest", "stats",
                                         "tasks"])
            self.assertEqual(sorted(v["tasks"]["t1"]),
                             ["created", "dispatcher_sid", "in_gate", "last_seen",
                              "project_dir", "state", "title", "usage",
                              "worker_sid"])
            self.assertEqual(v["forest"], ["t1", "t2", "t3", "t4"])
            self.assertEqual(v["buckets"]["在飞"], ["t1", "t2"])
            self.assertEqual([e["event"] for e in v["events"]],
                             ["escalate"])    # 只含 escalate 事件
            self.assertEqual(v["stats"], {   # F57 分桶：merged＝终态且 rework==0 一次过
                "glm-5.3": {"tasks": 1, "merged": 1, "rework": 0, "win_tasks": 1,
                            "win_merged": 1, "win_rework": 0, "first_created": 0.0,
                            "last_created": 0.0, "drift": None},
                "flash": {"tasks": 1, "merged": 0, "rework": 1, "win_tasks": 1,
                          "win_merged": 0, "win_rework": 1, "first_created": 0.0,
                          "last_created": 0.0, "drift": None},
                "default": {"tasks": 2, "merged": 0, "rework": 0, "win_tasks": 2,
                            "win_merged": 0, "win_rework": 0, "first_created": 0.0,
                            "last_created": 0.0, "drift": None}})
            # ↑ F58：小桶 win_tasks<10 → drift null 不下判；win_* 口径与全期同
            sparse = full_view(snap(tx={"state": "todo"}), st)  # 缺字段行容错
            self.assertEqual(sparse["tasks"]["tx"], {"state": "todo"})  # 无 usage 不投影
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stats_drift_flag_divergence(self):  # Req: F58
        # 失败集中最新 30 单：全期率 0.3 vs 窗口率 1.0，差 >0.15 → true
        tasks = {f"t{i:03d}": dict(row(state="merged", created=1000.0 + i),
                                   model="m") for i in range(100)}
        events = [{"ts": 1.0, "actor": "x", "event": "verify_failed",
                   "task": f"t{i:03d}"} for i in range(70, 100)]
        b = _model_stats(tasks, events)["m"]
        self.assertIs(b["drift"], True)
        self.assertEqual(b["win_tasks"], 30)
        self.assertEqual(b["win_rework"], 30)
        self.assertEqual(b["rework"], 30)

    def test_stats_drift_thin_window(self):  # Req: F58
        # 桶 5 单 <10：win_tasks 不足 → null 不下判；缺 created 按 0.0 兜底
        tasks = {f"t{i}": row(state="merged") for i in range(5)}
        events = [{"ts": 1.0, "actor": "x", "event": "verify_failed", "task": "t0"}]
        b = _model_stats(tasks, events)["default"]
        self.assertIsNone(b["drift"])
        self.assertEqual(b["win_tasks"], 5)
        self.assertEqual(b["win_rework"], 1)
        self.assertEqual(b["first_created"], 0.0)
        self.assertEqual(b["last_created"], 0.0)

    def test_stats_drift_flat(self):  # Req: F58
        # 失败均匀铺开、窗口＝全桶 → 两率相等 → false
        tasks = {f"t{i:02d}": dict(row(state="merged", created=100.0 + i),
                                   model="m") for i in range(12)}
        events = [{"ts": 1.0, "actor": "x", "event": "reopened",
                   "task": f"t{i:02d}"} for i in range(3)]
        self.assertIs(_model_stats(tasks, events)["m"]["drift"], False)

    def test_stats_window_is_most_recent_by_created(self):  # Req: F58
        # 35 单取最近 30：窗口按任务入选切——旧 5 单的失败不进 win_*，跨度仍覆盖全桶
        tasks = {f"t{i:02d}": dict(row(state="merged", created=500.0 + i),
                                   model="m") for i in range(35)}
        events = ([{"ts": 1.0, "actor": "x", "event": "verify_failed",
                    "task": f"t{i:02d}"} for i in range(3)]        # 旧 3 单窗口外
                  + [{"ts": 1.0, "actor": "x", "event": "verify_failed",
                      "task": f"t{i:02d}"} for i in range(5, 15)])  # 新 10 单窗口内
        b = _model_stats(tasks, events)["m"]
        self.assertEqual((b["tasks"], b["rework"]), (35, 13))
        self.assertEqual(b["win_tasks"], 30)
        self.assertEqual(b["win_rework"], 10)   # 旧 5 单的 3 失败不入窗
        self.assertEqual(b["first_created"], 500.0)
        self.assertEqual(b["last_created"], 534.0)

    def test_stats_empty_bucket_drift_null(self):  # Req: F58
        # 空任务集不产出桶（不伪造 win_*/drift）；孤儿事件不许凭空造桶
        self.assertEqual(_model_stats({}, [{"ts": 1.0, "actor": "x",
                                            "event": "verify_failed",
                                            "task": "tx"}]), {})

    def test_detail_carries_delivery_error(self):  # Req: F98
        """交付失败机器可读 error 经任务明细可见（全字段行自然携带投影）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            s = snap(t1=dict(row("blocked"), delivery={
                "mode": "managed", "status": "failed",
                "error": "rebase_conflict", "attempt": 1}))
            d = detail(s, st, "t1")
            self.assertEqual(d["task"]["delivery"]["error"], "rebase_conflict")
            self.assertEqual(d["task"]["delivery"]["status"], "failed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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

    def test_classify_three_states_and_edges(self):  # Req: F64
        now = 1_000_000.0
        cases = [
            (row("running", last_seen=now - 60), set(), "fresh"),
            (row("running", last_seen=now - 3600), set(), "busy_round"),
            (row("running", last_seen=now - 3600), {"t1"}, "stale_candidate"),
            (row("running", last_seen=None), set(), "busy_round"),  # 无心跳＝冷
            (row("cancelled", last_seen=now - 60), set(), None),  # 终态无判读
            (row("todo", worker=None), set(), None),  # 无 worker 无判读
        ]
        for t, stale, want in cases:
            self.assertEqual(classify("t1", t, stale, now), want)

    def test_detail_carries_classification(self):  # Req: F64 单任务响应即带判读
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            s = snap(t1=row("running", created=1000.0, last_seen=None),
                     t2=row("running", last_seen=time.time() - 60),
                     t3=row("merged"))
            self.assertEqual(detail(s, st, "t1")["classification"],
                             "stale_candidate")  # 基线远古→候选
            self.assertEqual(detail(s, st, "t2")["classification"], "fresh")
            self.assertIsNone(detail(s, st, "t3")["classification"])
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

    def test_doctor_view_verify_hygiene_findings(self):  # Req: F99
        tmp = Path(tempfile.mkdtemp())
        try:
            proj = tmp / "repo"
            proj.mkdir()
            bad = "cd /tmp && pytest -q"
            rows = {
                "t1": dict(row("running"), project_dir=str(proj),
                           verify={"cmd": bad, "state": "registered"}),
                "t2": dict(row("closed"), project_dir=str(proj),
                           verify={"cmd": bad, "state": "registered"}),
                "t3": dict(row("dispatched"), project_dir=str(proj),
                           verify={"cmd": "python3 -m pytest tests/ -q",
                                   "state": "registered"}),
                "t4": dict(row("done"), project_dir=str(proj),
                           verify={"cmd": "cp x ~/.config/agora.conf",
                                   "state": "registered"}),
                "t5": dict(row("blocked"), project_dir=str(proj),
                           verify={"cmd": bad, "state": "registered"}),
                "t6": dict(row("merged"), project_dir=str(proj),
                           verify={"cmd": bad, "state": "registered"}),
            }
            v = doctor_view(snap(**rows), tmp, now=1600.0, models=["m"],
                            verify_hygiene=find_verify_hygiene_issues)
            f = v["verify_hygiene_findings"]
            # 非终态坏命令在列（含 blocked）；closed/merged 不扫；合法命令零 finding
            self.assertEqual(
                [(x["tid"], x["state"], x["code"], x["fragment"], x["blocking"])
                 for x in f],
                [("t1", "running", "ABSOLUTE_CD", "/tmp", True),
                 ("t4", "done", "GLOBAL_STATE", "~/.config/agora.conf", False),
                 ("t5", "blocked", "ABSOLUTE_CD", "/tmp", True)])
            self.assertEqual(sorted(f[0]),
                             ["blocking", "code", "fragment", "hint",
                              "state", "tid"])  # 固定六键
            self.assertTrue(all(x["hint"] for x in f))
            # 只读审计：既有字段与无 verify 行的基线完全一致
            base = doctor_view(snap(**{k: {kk: vv for kk, vv in r.items()
                                           if kk != "verify"}
                                      for k, r in rows.items()}),
                               tmp, now=1600.0, models=["m"])
            for k in ("age_rank", "last_seen_rank", "stale_candidates",
                      "env", "models", "models_note"):
                self.assertEqual(v[k], base[k])
            self.assertEqual(base["verify_hygiene_findings"],
                             [])  # 未注入检查器：空发现不误报（core 禁兄弟 import）
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_view_html_five_buckets(self):  # Req: F10
        s = snap(t1=row("merged", disp="d1"), t2=row("running"))
        html = view_html(s)
        for w in ("已合并", "在飞", "失败", "阻塞", "已取消", "t1", "d1"):
            self.assertIn(w, html)


    def test_full_view_exposes_external_delivery_projection(self):  # Req: F101
        """board full 视图投影 delivery 面（mode/status/commit/base/attempt/
        local_status/error）——外部任务观测不逐任务 N+1。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            ext = row("done", disp="d1")
            ext["delivery"] = {"mode": "external_branch",
                               "status": "awaiting_external",
                               "commit": "c" * 40, "base": "b" * 40,
                               "target_branch": "issue/7-fix", "attempt": 2,
                               "local_status": "not_required", "error": None}
            v = full_view(snap(t1=ext, t2=row("running")), st)
            self.assertEqual(v["tasks"]["t1"]["delivery"], ext["delivery"])
            self.assertNotIn("delivery", v["tasks"]["t2"])  # 缺键行不投影
            self.assertEqual(v["buckets"]["在飞"], ["t1", "t2"])  # awaiting 仍在飞
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detail_exposes_awaiting_external_fields(self):  # Req: F101
        """任务明细可见外部 handoff 全量投影（awaiting_external 不误判 merged）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            st = Store(tmp)
            s = snap(t1=dict(row("done"), delivery={
                "mode": "external_branch", "status": "awaiting_external",
                "commit": "c" * 40, "base": "b" * 40, "attempt": 1,
                "local_status": "not_required", "error": None}))
            d = detail(s, st, "t1")
            self.assertEqual(d["task"]["state"], "done")  # 非 merged
            self.assertEqual(d["task"]["delivery"]["status"],
                             "awaiting_external")
            self.assertEqual(d["task"]["delivery"]["attempt"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_buckets_external_failed_stays_failure_bucket(self):  # Req: F101
        """external 交付终局败（blocked＋delivery.status=failed）沿用 F98 失败桶。"""
        s = snap(t1=dict(row("blocked"), delivery={
            "mode": "external_branch", "status": "failed",
            "error": "retry_exhausted"}))
        self.assertEqual(buckets(s)["失败"], ["t1"])


if __name__ == "__main__":
    unittest.main()
