"""T8 board_tui 模型层契约：build_rows 渲染模型＋handle_key 键位（纯函数，F10/F5）。

curses 壳薄不可测，smoke 走 `--selftest` 一帧渲染（stub agorad 真进程）。
"""
import http.server
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from board_tui import (Action, Row, build_rows, detail_page, doctor_page,
                       handle_key, rel_time, try_cancel)

ROOT = Path(__file__).resolve().parent.parent
NOW = 1000.0


def trow(state="running", disp="main", worker="w1", title="目标", last_seen=940.0):
    return {"title": title, "state": state, "dispatcher_sid": disp,
            "worker_sid": worker, "created": 0.0, "last_seen": last_seen,
            "chain_depth": 1, "fail_streak": 0}


def snap(**tasks):
    return {"tasks": tasks, "chain": list(tasks)}


def model(rows=(), sel=0, **kw):
    m = {"rows": list(rows), "sel": sel, "view": "board", "confirm": None,
         "input": None, "buf": "", "filter": ""}
    m.update(kw)
    return m


def task_rows(rows):
    return [r for r in rows if r.kind == "task"]


class TestBuildRows(unittest.TestCase):
    def test_chain_tree_indent(self):  # Req: F10
        s = snap(t1=trow(disp="main", worker="w1"),
                 t2=trow(disp="w1", worker="w2"),
                 t3=trow(disp="w2", worker="w3"))
        rows = build_rows(s, [], now=NOW, expanded={"t1", "t2"})  # 全展开看缩进
        self.assertEqual([(r.tid, r.depth) for r in task_rows(rows)],
                         [("t1", 0), ("t2", 1), ("t3", 2)])

    def test_five_bucket_rows(self):  # Req: F10
        s = snap(t1=trow("merged"), t2=trow("closed"), t3=trow("verify_failed"),
                 t4=trow("reopened"), t5=trow("dispatched"), t6=trow("done"),
                 t7=trow("blocked"), t8=trow("cancelled"), t9=trow("todo"))
        buckets = [r for r in build_rows(s, [], now=NOW) if r.kind == "bucket"]
        self.assertEqual([r.title for r in buckets],
                         ["已合并 2", "在飞 5", "失败 0", "阻塞 1", "已取消 1"])

    def test_color_domain_verify_failed_reopened_in_flight(self):  # Req: F10
        s = snap(t1=trow("verify_failed"), t2=trow("reopened"), t3=trow("running"),
                 t4=trow("merged"), t5=trow("blocked"), t6=trow("cancelled"))
        dom = {r.tid: r.domain for r in task_rows(build_rows(s, [], now=NOW))}
        self.assertEqual(dom, {"t1": "fly", "t2": "fly", "t3": "fly",
                               "t4": "merged", "t5": "blocked", "t6": "gone"})

    def test_escalate_events_four_kinds_highlight(self):  # Req: F5
        ev = [{"event": "escalate", "task": "t1", "kind": "quality"},
              {"event": "escalate", "task": "t1", "kind": "dispatcher-dead"},
              {"event": "escalate", "task": "t2", "kind": "stalled"},  # F5 僵态提醒
              {"event": "dispatched", "task": "t1"},          # 非升级不亮
              {"event": "escalate", "task": "t2", "kind": "no-such-kind"}]
        rows = build_rows(snap(t1=trow(), t2=trow()), ev, now=NOW)
        events = [r for r in rows if r.kind == "event"]
        self.assertEqual([r.note for r in events],
                         ["quality", "dispatcher-dead", "stalled"])
        self.assertTrue(all(r.domain == "warn" for r in events))
        self.assertEqual(events[0].tid, "t1")  # 紧随其任务行

    def test_last_seen_relative(self):  # Req: F5
        self.assertEqual(rel_time(None, NOW), "-")
        self.assertEqual(rel_time(NOW - 5, NOW), "5s")
        self.assertEqual(rel_time(NOW - 90, NOW), "1m")
        self.assertEqual(rel_time(NOW - 7200, NOW), "2h")
        self.assertEqual(rel_time(NOW - 172800, NOW), "2d")
        s = snap(t1=trow(last_seen=940.0))
        self.assertEqual(task_rows(build_rows(s, [], now=NOW))[0].last_seen, "1m")

    def test_buckets_server_global_and_local_fallback(self):  # Req: gt-6def73
        s = {"tasks": {"t1": trow("merged")}, "chain": ["t1"],
             "buckets": {"已合并": ["t1", "t9"], "在飞": ["t2"], "失败": [],
                         "阻塞": [], "已取消": []}}
        buckets = [r for r in build_rows(s, [], now=NOW) if r.kind == "bucket"]
        self.assertEqual([r.title for r in buckets],
                         ["已合并 2", "在飞 1", "失败 0", "阻塞 0",
                          "已取消 0"])  # 服务端全局口径（含本地 tasks 外的 t9）

    def test_forest_multi_root_default_folded(self):  # Req: gt-6def73
        s = {"tasks": {"A": trow(disp="main", worker="wa"),
                       "B": trow(disp="other", worker="wb"),
                       "A1": trow(disp="wa", worker="wa1")},
             "chain": ["A", "B", "A1"]}  # forest 序：根层 A、B 两棵树
        rows = build_rows(s, [], now=NOW)  # 默认折叠只显所有根行
        self.assertEqual([r.tid for r in task_rows(rows)], ["A", "B"])
        rows = build_rows(s, [], now=NOW, expanded={"A"})
        self.assertEqual([r.tid for r in task_rows(rows)], ["A", "A1", "B"])

    def test_repo_column_from_project_dir(self):  # Req: gt-6def73
        s = snap(t1=trow(), t2=trow())
        s["tasks"]["t1"]["project_dir"] = "/home/u/myrepo"
        s["tasks"]["t2"]["project_dir"] = "/x/opencode-agora__wt-1"
        repo = {r.tid: r.repo for r in task_rows(build_rows(s, [], now=NOW))}
        self.assertEqual(repo, {"t1": "myrepo",
                                "t2": "opencode-agora__wt-1"})  # Path 尾段
        s["tasks"]["t2"].pop("project_dir")
        repo = {r.tid: r.repo for r in task_rows(build_rows(s, [], now=NOW))}
        self.assertEqual(repo["t2"], "")  # 缺失空串

    def test_detail_page_shows_project_dir(self):  # Req: gt-6def73
        d = {"task": dict(trow(), project_dir="/home/u/myrepo"), "events": []}
        self.assertIn("project_dir: /home/u/myrepo", detail_page(d))

    def test_filter_substring(self):  # Req: F10
        s = snap(t1=trow(title="登录页"), t2=trow(title="支付网关", state="blocked"),
                 t3=trow(title="账务"))
        rows = build_rows(s, [], filt="支付", now=NOW)
        self.assertEqual([r.tid for r in task_rows(rows)], ["t2"])
        self.assertTrue(any(r.kind == "bucket" for r in rows))  # 桶行恒在
        rows = build_rows(s, [], filt="t3", now=NOW)
        self.assertEqual([r.tid for r in task_rows(rows)], ["t3"])


class TestHandleKey(unittest.TestCase):
    ROWS = [Row("bucket", title="在飞 1", domain="bucket"),
            Row("task", "t1", 0, "running", "目标", "ses-9", "1m", "", "fly")]

    def test_motion_jk_gG_clamped(self):  # Req: F10
        m = model(self.ROWS, sel=0)
        self.assertEqual(handle_key(m, "j").op, "move")
        self.assertEqual(m["sel"], 1)
        handle_key(m, "j")
        self.assertEqual(m["sel"], 1)  # 底钳制
        handle_key(m, "g")
        self.assertEqual(m["sel"], 1)  # g 跳顶落首任务行，不停桶头
        self.assertEqual(handle_key(m, "G").op, "move")
        self.assertEqual(m["sel"], 1)
        handle_key(m, "k")
        handle_key(m, "k")
        self.assertEqual(m["sel"], 0)  # 顶钳制

    def test_jk_skip_bucket_headers(self):  # Req: F10
        rows = self.ROWS + [Row("bucket", title="失败 0", domain="bucket"),
                            Row("task", "t2", 0, "failed", "二", "ses-8", "2m", "", "fail")]
        m = model(rows, sel=1)
        handle_key(m, "j")                 # 越过"失败"桶头直达 t2
        self.assertEqual(m["sel"], 3)
        self.assertEqual(rows[m["sel"]].kind, "task")
        handle_key(m, "k")                 # 回程同样越过桶头
        self.assertEqual(m["sel"], 1)
        m = model([self.ROWS[0]])          # 纯桶列表：无行动行，钳端点不越界
        handle_key(m, "j")
        self.assertEqual(m["sel"], 0)

    def test_enter_l_detail_h_back(self):  # Req: F10
        m = model(self.ROWS, sel=1)
        a = handle_key(m, "\n")
        self.assertEqual((a.op, a.task), ("detail", "t1"))
        self.assertEqual(m["view"], "detail")
        a = handle_key(m, "h")
        self.assertEqual((a.op, m["view"]), ("back", "board"))
        m = model(self.ROWS, sel=1)
        self.assertEqual(handle_key(m, "l").op, "detail")
        self.assertEqual(handle_key(model(self.ROWS, sel=0), "\n").op, "noop")

    def test_L_tail_by_worker_sid(self):  # Req: F10
        m = model(self.ROWS, sel=1)
        a = handle_key(m, "L")
        self.assertEqual((a.op, a.arg), ("tail", "ses-9"))
        self.assertEqual(handle_key(model(self.ROWS, sel=0), "L").op, "noop")

    def test_d_doctor_r_refresh_q_quit(self):  # Req: F10
        m = model(self.ROWS)
        self.assertEqual((handle_key(m, "d").op, m["view"]), ("doctor", "doctor"))
        self.assertEqual(handle_key(m, "r").op, "refresh")
        self.assertEqual(handle_key(m, "q").op, "quit")

    def test_x_needs_confirm_then_y_cancels(self):  # Req: F11/F10
        m = model(self.ROWS, sel=1)
        a = handle_key(m, "x")
        self.assertEqual(a.op, "confirm")          # 第一击不动手，只出确认行
        self.assertEqual(m["confirm"], "t1")
        a = handle_key(m, "y")
        self.assertEqual((a.op, a.task), ("cancel", "t1"))
        self.assertIsNone(m["confirm"])
        self.assertEqual(handle_key(model(self.ROWS, sel=0), "x").op, "noop")

    def test_confirm_n_aborts_and_stray_y_ignored(self):  # Req: F11/F10
        m = model(self.ROWS, sel=1)
        handle_key(m, "x")
        self.assertEqual(handle_key(m, "n").op, "noop")
        self.assertIsNone(m["confirm"])
        self.assertEqual(handle_key(model(self.ROWS), "y").op, "noop")  # 无确认不动作

    def test_slash_input_mode_enter_applies_esc_aborts(self):  # Req: F10
        m = model(self.ROWS)
        handle_key(m, "/")
        self.assertIsNotNone(m["input"])
        handle_key(m, "支")
        self.assertEqual(handle_key(m, "j").op, "noop")  # 输入态下 j 是字符不是移动
        self.assertEqual(m["buf"], "支j")
        a = handle_key(m, "\r")
        self.assertEqual(a.op, "apply")
        self.assertEqual((m["filter"], m["input"]), ("支j", None))
        m = model(self.ROWS)
        handle_key(m, "/")
        handle_key(m, "x")
        self.assertEqual(handle_key(m, "\x1b").op, "noop")
        self.assertEqual((m["filter"], m["input"]), ("", None))
        m = model(self.ROWS)
        handle_key(m, "/")
        handle_key(m, "a")
        handle_key(m, "b")
        handle_key(m, "KEY_BACKSPACE")
        self.assertEqual(m["buf"], "a")

    def test_pages_ignore_x_and_slash_guard(self):  # Req: F11/F10
        m = model(self.ROWS, sel=1, view="detail")
        self.assertEqual(handle_key(m, "x").op, "noop")  # 全屏页不武装确认
        self.assertIsNone(m["confirm"])
        self.assertEqual(handle_key(m, "y").op, "noop")  # y 盲发被拒
        self.assertEqual(handle_key(model(self.ROWS, sel=1, view="doctor"), "x").op, "noop")
        m = model(self.ROWS, sel=1, view="tail")
        handle_key(m, "/")
        self.assertIsNone(m["input"])

    def test_unknown_key_ignored(self):  # Req: F10
        m = model(self.ROWS, sel=1)
        self.assertEqual(handle_key(m, "v").op, "noop")  # z 已成折叠键，另取未知键
        self.assertEqual(m["sel"], 1)


class TestFoldTree(unittest.TestCase):
    """链树折叠＋DFS 聚簇：链 [A,B,A1,B1,A2]（A1 子 A、A2 子 A1、B1 子 B）。"""

    def _chain(self):  # Req: F10
        return snap(A=trow(disp="main", worker="wa"),
                    B=trow(disp="main", worker="wb"),
                    A1=trow(disp="wa", worker="wa1"),
                    B1=trow(disp="wb", worker="wb1"),
                    A2=trow(disp="wa1", worker="wa2"))

    def test_dfs_cluster_order(self):  # Req: F10
        rows = build_rows(self._chain(), [], now=NOW, expanded={"A", "A1", "B"})
        self.assertEqual([r.tid for r in task_rows(rows)],
                         ["A", "A1", "A2", "B", "B1"])  # 子代紧随父代，同层保链序

    def test_default_folded_roots_only(self):  # Req: F10
        rows = build_rows(self._chain(), [], now=NOW)  # expanded 缺省＝空集
        self.assertEqual([r.tid for r in task_rows(rows)], ["A", "B"])

    def test_expand_stepwise(self):  # Req: F10
        s = self._chain()
        rows = build_rows(s, [], now=NOW, expanded={"A"})
        self.assertEqual([r.tid for r in task_rows(rows)], ["A", "A1", "B"])
        rows = build_rows(s, [], now=NOW, expanded={"A", "A1"})
        self.assertEqual([r.tid for r in task_rows(rows)],
                         ["A", "A1", "A2", "B"])  # B 未展开，B1 仍隐

    def test_z_toggles_expanded_in_place(self):  # Req: F10
        rows = [Row("bucket", title="在飞 1", domain="bucket"),
                Row("task", "t1", 0, "running", "目标", "ses-9", "1m", "", "fly")]
        m = model(rows, sel=1, expanded=set())
        exp = m["expanded"]
        a = handle_key(m, "z")
        self.assertEqual((a.op, a.task), ("fold", "t1"))
        self.assertEqual(m["expanded"], {"t1"})
        self.assertIs(m["expanded"], exp)          # 就地 toggle，不换集
        a = handle_key(m, "z")
        self.assertEqual((a.op, a.task), ("fold", "t1"))
        self.assertEqual(m["expanded"], set())
        self.assertEqual(handle_key(model(rows, sel=0, expanded=set()), "z").op,
                         "noop")                   # 桶头行不折叠
        self.assertEqual(
            handle_key(model(rows, sel=1, view="detail", expanded=set()),
                       "z").op, "noop")            # 全屏页不折叠

    def test_filter_penetrates_fold(self):  # Req: F10
        rows = build_rows(self._chain(), [], filt="A2", now=NOW)  # 折叠＋命中深层
        self.assertEqual([r.tid for r in task_rows(rows)], ["A2"])

    def test_deep_chain_no_recursion_limit(self):  # Req: F10
        n = 1500
        s = {"tasks": {f"t{i}": trow(disp="main" if i == 0 else f"w{i - 1}",
                                     worker=f"w{i}") for i in range(n)},
             "chain": [f"t{i}" for i in range(n)]}
        rows = build_rows(s, [], now=NOW, expanded=set())
        self.assertEqual([r.tid for r in task_rows(rows)], ["t0"])  # 折叠只显根
        rows = build_rows(s, [], now=NOW, expanded=set(s["tasks"]))
        self.assertEqual(len(task_rows(rows)), n)   # 全展开走完 DFS 不触递归上限


class TestDoctorPage(unittest.TestCase):
    def test_stale_candidates_section(self):  # Req: F5
        v = {"age_rank": [["t1", 900.0]], "last_seen_rank": [["t1", 940.0]],
             "stale_candidates": [["t1", 100.0], ["t2", 300.0]], "env": []}
        lines = doctor_page(v, now=NOW)
        head = "---- 僵态候选（在飞>20min 无活动，判断归上级） ----"
        self.assertIn(head, lines)
        i = lines.index(head)
        self.assertEqual(lines[i + 1:i + 3], ["t1  15m", "t2  11m"])  # 最僵在前
        self.assertLess(lines.index("---- 最后活动（最僵在前） ----"), i)  # 位于最后活动段后
        self.assertEqual(doctor_page({}, now=NOW).count(head), 1)  # 无键也照常渲染空段


class _StubBoard(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if "task" in body:
            out = {"task": trow(), "events": [{"ts": NOW, "actor": "health",
                                               "event": "escalate",
                                               "task": body["task"],
                                               "kind": "quality"}],
                   "done_body": None}
        else:
            out = {"buckets": {}, "chain": ["t1"]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _CancelStub(http.server.BaseHTTPRequestHandler):
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/task_cancel" and self.fail:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            data = json.dumps({"cancelled": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


class TestTryCancel(unittest.TestCase):
    def test_cancel_failure_is_not_offline(self):  # Req: F10/F11
        srv = http.server.HTTPServer(("127.0.0.1", 0), _CancelStub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            self.assertEqual(try_cancel(base, "tok", "t1"),
                             "取消 t1: cancelled=True")
            _CancelStub.fail = True
            bad = try_cancel(base, "tok", "t1")
            self.assertIn("取消失败", bad)          # 取消失败≠离线
            self.assertNotIn("离线", bad)
        finally:
            srv.shutdown()
            srv.server_close()


class TestSmoke(unittest.TestCase):
    def test_selftest_one_frame_against_stub_agorad(self):  # Req: F10/F5
        srv = http.server.HTTPServer(("127.0.0.1", 0), _StubBoard)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        home = Path(tempfile.mkdtemp())
        mfd, sfd = pty.openpty()  # curses 需真 tty：pty 哑端
        try:
            (home / ".agora").mkdir(parents=True)
            (home / ".agora" / "root.token").write_text("tok-smoke\n")
            (home / ".agora" / "service.json").write_text(
                json.dumps({"port": srv.server_address[1]}))
            env = dict(os.environ, AGORA_HOME=str(home), TERM="xterm",
                       AGORA_BOARD_INTERVAL="1")
            p = subprocess.Popen(
                [sys.executable, str(ROOT / "board_tui.py"), "--selftest"],
                stdin=sfd, stdout=sfd, stderr=subprocess.PIPE, env=env,
                cwd=str(ROOT))
            _, errs = p.communicate(timeout=30)
            out = b""
            while select.select([mfd], [], [], 1.0)[0]:
                try:
                    chunk = os.read(mfd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            self.assertEqual(p.returncode, 0, errs.decode())
            self.assertIn(b"selftest ok", out)
            self.assertIn(b"rows=7", out)  # 5 桶行＋任务行＋升级事件行
        finally:
            srv.shutdown()
            srv.server_close()
            os.close(mfd)
            os.close(sfd)
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
