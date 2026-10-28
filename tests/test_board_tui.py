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

from board_tui import (Action, Row, _row_text, build_rows, clamp_view,
                       detail_page, doctor_page, handle_key, rel_time,
                       try_cancel)

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
         "input": None, "buf": "", "filter": "", "bucket": None,
         "proj": False, "gexpanded": set()}
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
                         ["Merged 2", "In-flight 5", "Failed 0", "Blocked 1",
                          "Cancelled 1"])

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

    def test_task_line_fixed_columns(self):  # Req: F10
        s = snap(t1=trow(last_seen=NOW - 5), t2=trow(last_seen=None))
        s["tasks"]["t1"]["project_dir"] = "/home/u/r1"
        s["tasks"]["t2"]["project_dir"] = "/home/u/r2"
        lines = [_row_text(r) for r in task_rows(build_rows(s, [], now=NOW))]
        self.assertEqual(lines[0].index("r1"), lines[1].index("r2"))  # repo 列起点与 last_seen 宽度无关
        c1 = lines[1].index("r2")
        self.assertEqual(lines[1][c1 - 7:c1 - 2], "-    ")  # 缺失 last_seen 定宽 5

    def test_buckets_server_global_and_local_fallback(self):  # Req: gt-6def73
        s = {"tasks": {"t1": trow("merged")}, "chain": ["t1"],
             "buckets": {"已合并": ["t1", "t9"], "在飞": ["t2"], "失败": [],
                         "阻塞": [], "已取消": []}}
        buckets = [r for r in build_rows(s, [], now=NOW) if r.kind == "bucket"]
        self.assertEqual([r.title for r in buckets],
                         ["Merged 2", "In-flight 1", "Failed 0", "Blocked 0",
                          "Cancelled 0"])  # 服务端全局口径（含本地 tasks 外的 t9）

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

    def test_bucket_view_filters_by_shared_mapping(self):  # Req: gt-6a26e5
        s = snap(t1=trow("merged"), t2=trow("closed"), t3=trow("verify_failed"),
                 t4=trow("blocked"), t5=trow("cancelled"))
        rows = build_rows(s, [], now=NOW, bucket="已合并")
        self.assertEqual([r.tid for r in task_rows(rows)],
                         ["t1", "t2"])  # merged+closed 都归"已合并"视图
        self.assertEqual([r.tid for r in task_rows(
            build_rows(s, [], now=NOW, bucket="在飞"))], ["t3"])
        self.assertEqual([r.tid for r in task_rows(
            build_rows(s, [], now=NOW, bucket="阻塞"))], ["t4"])
        self.assertEqual([r.tid for r in task_rows(
            build_rows(s, [], now=NOW, bucket="已取消"))], ["t5"])

    def test_bucket_view_and_filter_overlap(self):  # Req: gt-6a26e5
        s = snap(t1=trow(title="登录页"), t2=trow(title="登录重试", state="blocked"),
                 t3=trow(title="账务", state="blocked"))
        rows = build_rows(s, [], filt="登录", now=NOW, bucket="阻塞")  # AND：桶∩文本
        self.assertEqual([r.tid for r in task_rows(rows)], ["t2"])

    def test_bucket_view_keeps_counts_and_marks_active(self):  # Req: gt-6a26e5
        s = snap(t1=trow("merged"), t2=trow("running"), t3=trow("blocked"))
        rows = build_rows(s, [], now=NOW, bucket="在飞")   # 计数行不受视图切换影响
        self.assertEqual([r.title for r in rows if r.kind == "bucket"],
                         ["Merged 1", "*In-flight 1", "Failed 0", "Blocked 1",
                          "Cancelled 0"])
        rows = build_rows(s, [], now=NOW)
        self.assertEqual([r.title for r in rows if r.kind == "bucket"],
                         ["Merged 1", "In-flight 1", "Failed 0", "Blocked 1",
                          "Cancelled 0"])

    def test_bucket_view_penetrates_fold(self):  # Req: gt-6a26e5
        s = snap(A=trow(disp="main", worker="wa"),
                 A1=trow(disp="wa", worker="wa1", state="merged"))
        rows = build_rows(s, [], now=NOW, bucket="已合并")  # 折叠下命中深层同行
        self.assertEqual([r.tid for r in task_rows(rows)], ["A1"])


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
        self.assertEqual(m["sel"], 1)  # 首任务行上方全桶头：原地不动不停桶头

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

    def test_k_at_first_task_stays_on_bucket_void(self):  # Req: F10
        rows = [Row("bucket", title=f"桶 {i} 0", domain="bucket")
                for i in range(5)] + [
                Row("task", "t1", 0, "running", "目标", "ses-9", "1m", "", "fly"),
                Row("task", "t2", 0, "done", "二", "ses-8", "2m", "", "done")]
        m = model(rows, sel=5)             # 首个任务行
        handle_key(m, "k")                 # 上方全为桶头：原地不动，不停桶头
        self.assertEqual(m["sel"], 5)
        handle_key(m, "k")
        handle_key(m, "k")
        self.assertEqual(m["sel"], 5)      # 连按多次仍不变（光标不隐身）
        handle_key(m, "j")
        self.assertEqual(m["sel"], 6)      # 常规移动不回归
        handle_key(m, "k")
        self.assertEqual(m["sel"], 5)

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

    def test_digit_keys_toggle_bucket_view(self):  # Req: gt-6a26e5
        m = model(self.ROWS, sel=1)
        a = handle_key(m, "2")
        self.assertEqual((a.op, m["bucket"]), ("apply", "在飞"))
        handle_key(m, "2")                 # 同键再按恢复全量
        self.assertIsNone(m["bucket"])
        handle_key(m, "5")
        self.assertEqual(m["bucket"], "已取消")
        handle_key(m, "0")                 # 0＝项目分组模式开关（不清桶，可叠加）
        self.assertIs(m["proj"], True)
        self.assertEqual(m["bucket"], "已取消")
        handle_key(m, "0")
        self.assertIs(m["proj"], False)    # 再按 0 退出分组模式
        self.assertEqual(handle_key(model(self.ROWS, sel=1, view="detail"), "2").op,
                         "noop")           # 全屏页不切换
        m = model(self.ROWS, sel=1, input="")
        handle_key(m, "2")                 # 输入态下数字是过滤字符
        self.assertEqual(m["buf"], "2")
        self.assertIsNone(m["bucket"])
        self.assertIn("1-5 bucket | 0 proj",
                      (ROOT / "board_tui.py").read_text(encoding="utf-8"))  # hint 0 键


class TestClampView(unittest.TestCase):
    """视口滚动：clamp_view 就地改 top，四情形（draw_board 画前调用）。"""

    ROWS = [Row("bucket", title="在飞 9", domain="bucket")] + [
        Row("task", f"t{i}", 0, "running", "目标", "ses-9", "1m", "", "fly")
        for i in range(9)]

    def test_sel_below_window_scrolls_down(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=7, top=0)
        clamp_view(m, 5)
        self.assertEqual(m["top"], 3)          # sel>=top+visible → top=sel-visible+1

    def test_sel_above_window_scrolls_up(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=2, top=4)
        clamp_view(m, 5)
        self.assertEqual(m["top"], 2)          # sel<top → top=sel
        m = model(self.ROWS, sel=0, top=0)
        clamp_view(m, 5)
        self.assertEqual(m["top"], 0)          # top 不小于 0

    def test_sel_inside_window_no_move(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=4, top=2)
        clamp_view(m, 5)
        self.assertEqual(m["top"], 2)          # 屏内不动（含底边界 sel=top+visible-1）

    def test_gG_extremes_land_window(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=0, top=3)
        handle_key(m, "G")                     # 极值跳底：sel=9 → 视口贴底
        clamp_view(m, 5)
        self.assertEqual((m["sel"], m["top"]), (9, 5))
        handle_key(m, "g")                     # 极值跳顶落首任务行：sel=1 → top=sel
        clamp_view(m, 5)
        self.assertEqual((m["sel"], m["top"]), (1, 1))

    def test_j_at_last_row_stays(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=9, top=5)
        self.assertEqual(handle_key(m, "j").op, "move")
        self.assertEqual(m["sel"], 9)          # 末行 j 不再下移
        clamp_view(m, 5)
        self.assertEqual((m["sel"], m["top"]), (9, 5))  # top 不越 len-visible 界

    def test_k_at_first_row_stays(self):  # Req: gt-4cb57c
        m = model(self.ROWS, sel=0, top=0)
        self.assertEqual(handle_key(m, "k").op, "move")
        self.assertEqual(m["sel"], 0)          # 首行 k 不再上移
        clamp_view(m, 5)
        self.assertEqual(m["top"], 0)          # top 不越 0 界

    def test_rows_shrink_reclaims_top(self):  # Req: gt-4cb57c
        m = model(self.ROWS[:6], sel=5, top=5)  # 滚底后过滤/轮询致行流收缩
        clamp_view(m, 5)
        self.assertEqual((m["sel"], m["top"]), (5, 1))  # top 回收至 len-visible

    def test_bucket_visible_after_g(self):  # Req: gt-cb1075
        """桶行固顶：G 跳底再 g 回顶，桶行恒在可见窗（top 不低于桶行数）。"""
        nb = 5
        rows = [Row("bucket", title=f"桶 {i} 0", domain="bucket")
                for i in range(nb)] + [
                Row("task", f"t{i}", 0, "running", "目标", "ses-9", "1m", "", "fly")
                for i in range(20)]
        m = model(rows, sel=nb, top=0)
        clamp_view(m, 8, nb)
        self.assertEqual(m["top"], nb)         # 启动即见桶行＋首个任务行
        handle_key(m, "G")                     # 跳底：sel=24
        clamp_view(m, 8, nb)
        self.assertEqual(m["top"], 17)         # 贴底但 >= 桶行数
        self.assertGreaterEqual(m["top"], nb)  # 桶行在滚动窗外＝固顶恒画恒可见
        handle_key(m, "g")                     # 回顶落首任务行：sel=5
        clamp_view(m, 8, nb)
        self.assertEqual((m["sel"], m["top"]), (nb, nb))  # 桶行仍在窗内


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


class TestGroupMode(unittest.TestCase):
    """0 键项目分组视图＋z 组展开＋Z 全折全展（Req: gt-46ceb0）。"""

    def _snap(self):
        return {"tasks": {
            "a1": dict(trow(disp="main", worker="wa"), project_dir="/u/alpha",
                       created=1.0),
            "a2": dict(trow(disp="wa", worker="wa2"), project_dir="/u/alpha",
                       created=3.0),          # a2 子 a1：组内保持 forest 近序
            "b1": dict(trow(disp="main", worker="wb"), project_dir="/u/beta",
                       created=2.0),
            "n1": dict(trow(), created=9.0),  # 缺 project_dir 归 "?" 组
        }, "chain": ["a1", "a2", "b1", "n1"]}

    def test_group_headers_only_sorted_by_latest_created(self):  # Req: gt-46ceb0
        rows = build_rows(self._snap(), [], now=NOW, group=True)  # 缺省全折叠
        heads = [r for r in rows if r.kind == "group"]
        self.assertEqual([r.tid for r in heads], ["?", "alpha", "beta"])
        self.assertEqual([r.title for r in heads], ["? 1", "alpha 2", "beta 1"])
        self.assertEqual([r.domain for r in heads], ["bucket"] * 3)  # 样式同桶行
        self.assertEqual(task_rows(rows), [])            # 组员行隐藏
        self.assertEqual(len([r for r in rows if r.kind == "bucket"]), 5)  # 桶行恒在

    def test_group_header_z_expands_members(self):  # Req: gt-46ceb0
        rows = build_rows(self._snap(), [], now=NOW, group=True,
                          gexpanded={"alpha"})
        self.assertEqual([r.tid for r in task_rows(rows)],
                         ["a1", "a2"])                     # 展开显全组员（DFS 序）
        rows = build_rows(self._snap(), [], now=NOW, group=True)
        self.assertEqual(task_rows(rows), [])              # 缺省再折叠
        heads = [Row("bucket", title="在飞 4", domain="bucket"),
                 Row("group", "alpha", title="alpha 2", domain="bucket")]
        m = model(heads, sel=1, gexpanded=set())
        a = handle_key(m, "z")
        self.assertEqual((a.op, a.task), ("fold", "alpha"))
        self.assertEqual(m["gexpanded"], {"alpha"})
        handle_key(m, "z")
        self.assertEqual(m["gexpanded"], set())            # 再按折叠

    def test_Z_folds_or_expands_all_levels(self):  # Req: gt-46ceb0
        rows = [Row("group", "alpha", title="alpha 1", domain="bucket"),
                Row("task", "t1", 0, "running", "目标", "ses-9", "1m", "", "fly")]
        m = model(rows, sel=1, expanded=set(), gexpanded=set(),
                  tids={"t1", "t2"}, groups={"alpha", "beta"})
        self.assertEqual(handle_key(m, "Z").op, "fold")    # 全折→全展（两层面）
        self.assertEqual(m["gexpanded"], {"alpha", "beta"})
        self.assertEqual(m["expanded"], {"t1", "t2"})
        self.assertEqual(handle_key(m, "Z").op, "fold")    # 有展开→全折
        self.assertEqual((m["gexpanded"], m["expanded"]), (set(), set()))
        m = model(rows, sel=1, expanded={"t1"})            # 仅子树展开也算展开态
        handle_key(m, "Z")
        self.assertEqual((m["gexpanded"], m["expanded"]), (set(), set()))
        self.assertEqual(handle_key(model(rows, sel=1, view="detail"), "Z").op,
                         "noop")                           # 全屏页不折叠

    def test_zero_exit_and_group_bucket_filter_overlap(self):  # Req: gt-46ceb0
        m = model([], sel=0)
        self.assertEqual(handle_key(m, "0").op, "apply")
        self.assertIs(m["proj"], True)
        self.assertEqual(handle_key(m, "0").op, "apply")   # 再按 0 退出分组模式
        self.assertIs(m["proj"], False)
        s = {"tasks": {
            "m1": dict(trow("merged"), project_dir="/u/alpha", created=1.0),
            "r1": dict(trow("running"), project_dir="/u/alpha", created=2.0),
            "m2": dict(trow("merged"), project_dir="/u/beta", created=3.0),
        }, "chain": ["m1", "r1", "m2"]}
        rows = build_rows(s, [], now=NOW, group=True, bucket="已合并")
        self.assertEqual([r.tid for r in task_rows(rows)],
                         ["m2", "m1"])                     # AND 叠加穿透组折叠
        self.assertEqual([r.tid for r in rows if r.kind == "group"],
                         ["beta", "alpha"])                # 组头恒在（最新 created 降序）
        rows = build_rows(s, [], filt="r1", now=NOW, group=True)
        self.assertEqual([r.tid for r in task_rows(rows)], ["r1"])  # 文本穿透组折叠
        self.assertEqual(len([r for r in rows if r.kind == "group"]), 2)


class TestDoctorPage(unittest.TestCase):
    def test_stale_candidates_section(self):  # Req: F5
        v = {"age_rank": [["t1", 900.0]], "last_seen_rank": [["t1", 940.0]],
             "stale_candidates": [["t1", 100.0], ["t2", 300.0]], "env": []}
        lines = doctor_page(v, now=NOW)
        head = "---- stale candidates (in-flight >20min idle, up to owner) ----"
        self.assertIn(head, lines)
        i = lines.index(head)
        self.assertEqual(lines[i + 1:i + 3], ["t1  15m", "t2  11m"])  # 最僵在前
        self.assertLess(lines.index("---- last activity (stalest first) ----"), i)
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
                             "cancel t1: cancelled=True")
            _CancelStub.fail = True
            bad = try_cancel(base, "tok", "t1")
            self.assertIn("cancel failed", bad)     # 取消失败≠离线
            self.assertNotIn("offline", bad)
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
