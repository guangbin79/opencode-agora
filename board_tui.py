#!/usr/bin/env python3
"""board_tui——Agent View（F10/F5）：curses 链树仪表盘，vi motion（less 形态）。

模型层纯函数与 curses 壳分离：build_rows（五桶计数行＋链树任务行＋升级事件
行）与 handle_key（键位表→Action）可测；壳只拉数据、画行、执行 Action。
数据面＝agorad HTTP /board（五桶＋链＋逐任务明细）；root 牌自动读
$AGORA_HOME/.agora/root.token，端口读 .agora/service.json；会话 tail 只读
$XDG_DATA_HOME/opencode/opencode.db（sqlite ro，失败降级为提示行）。
边界：无模式切换（less 同款）；x 取消须 y 确认行；:命令行不做。
"""
import curses
import json
import locale
import os
import sqlite3
import sys
import time
import urllib.request
from collections import namedtuple
from pathlib import Path

# 升级事件五类（与 core/health 字面量对齐——独立命令不 import agorad）
_KINDS = frozenset(("quality", "gatefiles", "reinject-fail", "dispatcher-dead",
                    "stalled"))

# 五桶固序（计数行与数字键 1-5 同序）；桶归属映射 _bucket 单点，禁两套口径
_BUCKETS = ("已合并", "在飞", "失败", "阻塞", "已取消")

Action = namedtuple("Action", "op task arg")
Action.__new__.__defaults__ = (None, None)
Row = namedtuple("Row", "kind tid depth state title worker last_seen note domain repo")
Row.__new__.__defaults__ = (None, 0, "", "", None, "", "", "", "")


# ---------------- 渲染模型（纯函数） ----------------


import unicodedata

def _dw(s):
    """Display width: CJK=2, others=1."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def _pad(s, width):
    """Truncate to display width then pad to width."""
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > width:
            break
        out.append(c)
        w += cw
    return "".join(out) + " " * (width - w)

def _bucket(state):
    """桶归属唯一口径：merged+closed→已合并；blocked→阻塞；cancelled→已取消；
    其余（dispatched/running/done/verify_failed/reopened…）→在飞。"""
    return ("已合并" if state in ("merged", "closed")
            else "阻塞" if state == "blocked"
            else "已取消" if state == "cancelled" else "在飞")


def _domain(state):
    """状态着色域：verify_failed/reopened 是续修态归在飞（同五桶口径）。"""
    return {"已合并": "merged", "在飞": "fly",
            "阻塞": "blocked", "已取消": "gone"}[_bucket(state)]


def rel_time(ts, now=None):
    """last_seen 相对时间：- / 5s / 1m / 2h / 2d。"""
    if ts is None:
        return "-"
    d = max(0.0, (now if now is not None else time.time()) - ts)
    if d < 60:
        return f"{int(d)}s"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    return f"{int(d // 86400)}d"


def build_rows(snap, events, filt="", now=None, expanded=None, bucket=None):
    """渲染模型：五桶计数行＋链树任务行（缩进/着色域/相对时间）＋升级事件行。

    snap＝HTTP /board 汇合（tasks 全集＋chain 根链 BFS 序）；events 全量审计
    行，只取五类 escalate 紧随其任务行高亮；filt 子串滤（tid/title/state，
    桶行恒在）；bucket＝激活桶视图（数字键 1-5）：只显该桶任务行，与文本
    过滤 AND 叠加、同样穿透折叠，计数行恒全局口径，激活桶头加 "*"。
    链深按 worker→dispatcher 边上溯，环有界。行序 DFS 子树聚簇
    （子代紧随父代，同层保持 chain 原序）；expanded＝展开集，任务行仅当全部
    严格祖先都在其中才显示（空集＝默认折叠只显根层），escalate 事件行随任务
    行同显隐；过滤非空时穿透折叠，命中行照常显示。
    """
    now = time.time() if now is None else now
    expanded = expanded if expanded is not None else set()
    tasks = snap.get("tasks", {})
    chain = [tid for tid in snap.get("chain", []) if tid in tasks]
    esc = {}
    for e in events:
        if (e.get("event") == "escalate" and e.get("kind") in _KINDS
                and e.get("task") in chain):
            esc.setdefault(e["task"], []).append(e["kind"])
    parent = {tid: next((p for p in chain if tasks[p].get("worker_sid")
                         == tasks[tid].get("dispatcher_sid")), None)
              for tid in chain}
    depth = {}
    for tid in chain:
        seen, d, cur = set(), 0, tid
        while cur not in seen:
            seen.add(cur)
            up = parent.get(cur)
            if up is None:
                break
            d, cur = d + 1, up
        depth[tid] = d
    kids = {tid: [] for tid in chain}
    roots = []
    for tid in chain:
        (roots if parent[tid] is None else kids[parent[tid]]).append(tid)
    order = []
    stack = roots[::-1]
    while stack:                       # 显式栈 DFS：链深由远端 JSON 定，不触递归上限
        tid = stack.pop()
        order.append(tid)
        stack.extend(reversed(kids[tid]))
    order += [tid for tid in chain if tid not in set(order)]  # 环上孤岛 DFS 不可达，保链序兜底

    def _shown(tid):
        cur, seen = parent.get(tid), set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur not in expanded:
                return False
            cur = parent.get(cur)
        return True

    sb = snap.get("buckets")
    if sb is not None:      # 服务端全局五桶口径（gt-6def73）
        buckets = {name: len(sb.get(name, [])) for name in _BUCKETS}
    else:                   # 旧响应无 buckets：本地计数兜底（同 _bucket 口径）
        buckets = dict.fromkeys(_BUCKETS, 0)
        for t in tasks.values():
            buckets[_bucket(t["state"])] += 1
    rows = [Row("bucket", title=f"{'*' if name == bucket else ''}{name} {n}",
                domain="bucket") for name, n in buckets.items()]
    f = filt.lower()
    for tid in order:
        t = tasks[tid]
        if f and f not in f"{tid} {t.get('title', '')} {t['state']}".lower():
            continue        # 过滤穿透折叠：命中行全部照常显示，不看 expanded
        if bucket is not None and _bucket(t["state"]) != bucket:
            continue        # 桶视图同语义穿透折叠（口径与计数行同源）
        if not f and bucket is None and not _shown(tid):
            continue
        d = depth[tid]
        rows.append(Row("task", tid, d, t["state"], t.get("title", ""),
                        t.get("worker_sid"), rel_time(t.get("last_seen"), now),
                        " ".join(esc.get(tid, [])), _domain(t["state"]),
                        Path(t["project_dir"]).name if t.get("project_dir")
                        else ""))
        for kind in esc.get(tid, []):
            rows.append(Row("event", tid, d + 1, "", f"↑ escalate[{kind}]",
                            note=kind, domain="warn"))
    return rows


def _sel_row(m):
    rows = m.get("rows") or []
    return rows[m["sel"]] if 0 <= m.get("sel", 0) < len(rows) else None


def _skip_bucket(m, down=True, origin=None):
    """桶头只作分隔：越过后滑到最近任务/事件行；该方向全程无行动行则退回 origin
    （按键前行，防 k 在首个任务行空滑后光标停桶头隐身），缺省＝进入时行。"""
    rows = m.get("rows") or []
    d = 1 if down else -1
    origin = m["sel"] if origin is None else origin
    while 0 <= m["sel"] < len(rows) and rows[m["sel"]].kind == "bucket":
        if not 0 <= m["sel"] + d < len(rows):
            break
        m["sel"] += d
    if 0 <= m["sel"] < len(rows) and rows[m["sel"]].kind == "bucket":
        m["sel"] = origin


def clamp_view(m, visible, nb=0):
    """视口滚动：保证 sel 落在 [top, top+visible) 窗口内（就地改，无返回值语义）。

    nb＝固顶桶行数：滚动窗只盖 body 行，top 钳在 [nb, len-visible]，桶行恒可见。
    """
    if m["sel"] < m["top"]:
        m["top"] = m["sel"]
    elif m["sel"] >= m["top"] + visible:
        m["top"] = m["sel"] - visible + 1
    m["top"] = min(max(nb, m["top"]),
                   max(nb, len(m.get("rows") or []) - visible))  # 行流收缩后回收 top


def handle_key(m, key):
    """vi 键位→Action，model 就地演进（less 形态，无模式切换）。

    键位：j/k 移动（桶头只作分隔不停驻） g/G 跳顶底 Enter/l 明细 h 返回 L 会话 tail d doctor
    x cancel（y/n 确认行） z 折叠/展开当前任务子树 / 过滤（单行输入态） 1-5 桶视图（同键/0
    恢复全量） r 即刷 q 退出。
    """
    if m.get("input") is not None:            # 过滤单行输入态
        if key in ("\n", "\r", "KEY_ENTER"):
            m["filter"], m["input"], m["buf"] = m["buf"], None, ""
            return Action("apply")
        if key == "\x1b":
            m["input"] = m["buf"] = None
            return Action("noop")
        if key in ("\x7f", "\b", "KEY_BACKSPACE"):
            m["buf"] = m["buf"][:-1]
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            m["buf"] += key                    # 其余功能键忽略
        return Action("noop")
    if m.get("confirm"):                       # x 破坏性确认行：y 才放行
        tid = m["confirm"]
        m["confirm"] = None
        return Action("cancel", task=tid) if key == "y" else Action("noop")
    r = _sel_row(m)
    if key in ("j", "KEY_DOWN"):
        origin = m["sel"]
        m["sel"] = min(m["sel"] + 1, max(0, len(m.get("rows") or []) - 1))
        _skip_bucket(m, down=True, origin=origin)
        return Action("move")
    if key in ("k", "KEY_UP"):
        origin = m["sel"]
        m["sel"] = max(m["sel"] - 1, 0)
        _skip_bucket(m, down=False, origin=origin)
        return Action("move")
    if key == "g":
        m["sel"] = 0
        _skip_bucket(m, down=True)
        return Action("move")
    if key in ("G", "KEY_END"):
        m["sel"] = max(0, len(m.get("rows") or []) - 1)
        return Action("move")
    if key in ("\n", "\r", "KEY_ENTER", "l"):
        if r and r.kind == "task":
            m["view"] = "detail"
            return Action("detail", task=r.tid)
        return Action("noop")
    if key == "h":
        if m.get("view") != "board":
            m["view"] = "board"
            return Action("back")
        return Action("noop")
    if key == "L":
        if r and r.kind == "task" and r.worker:
            m["view"] = "tail"
            return Action("tail", arg=r.worker)
        return Action("noop")
    if key == "d":
        m["view"] = "doctor"
        return Action("doctor")
    if key == "x":
        if m.get("view") == "board" and r and r.kind == "task":
            m["confirm"] = r.tid
            return Action("confirm", task=r.tid)
        return Action("noop")                  # 全屏页不武装确认（旁路门）
    if key == "z":
        if m.get("view") == "board" and r and r.kind == "task":
            m["expanded"].symmetric_difference_update({r.tid})  # set 就地 toggle
            return Action("fold", task=r.tid)
        return Action("noop")
    if key == "r":
        return Action("refresh")
    if key == "q":
        return Action("quit")
    if key == "/":
        if m.get("view") == "board":
            m["input"], m["buf"] = "", ""
        return Action("noop")
    if key in ("0", "1", "2", "3", "4", "5"):
        if m.get("view") == "board":
            cur = None if key == "0" else _BUCKETS[int(key) - 1]
            m["bucket"] = None if m.get("bucket") == cur else cur  # 同键再按复原
            return Action("apply")             # 与过滤 apply 同路：缓存帧即时重建
        return Action("noop")
    return Action("noop")                      # 非引导键忽略


# ---------------- 数据面（agorad HTTP＋会话 tail 只读） ----------------

def _pointer_home():
    """~/.agora-home 指针回退（gt-32c051：MCP 壳/install 写入的统一发现桥）；缺失/空白＝无。"""
    try:
        return ((Path.home() / ".agora-home").read_text(encoding="utf-8")
                .split("\n", 1)[0].strip() or None)  # 首行：多行指针不内嵌换行进 home
    except (OSError, ValueError):  # ValueError＝坏字节（UnicodeDecodeError 非 OSError 族）
        return None


def discover(home=None):
    """root 牌＋端口：$AGORA_HOME/.agora/{root.token,service.json}。"""
    home = Path(home or os.environ.get("AGORA_HOME") or _pointer_home()
                or Path.home() / "plaza")
    sj = json.loads((home / ".agora" / "service.json").read_text(encoding="utf-8"))
    token = (home / ".agora" / "root.token").read_text(encoding="utf-8").strip()
    return f"http://127.0.0.1:{sj['port']}", token


def _call(base, token, path, body, timeout=5):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Agora-Token": token},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_board(base, token):
    """board 全量：全局五桶＋森林序（旧 agorad 无 forest 回退 chain）＋逐任务明细。

    snap["chain"] 实为渲染序（forest 优先）；snap["buckets"]＝服务端全局五桶
    （旧响应缺失时 build_rows 本地计数兜底）。离线由壳兜住。
    """
    top = _call(base, token, "/board", {})
    order = top.get("forest", top.get("chain", []))
    tasks, events, details = {}, [], {}
    for tid in order:
        d = _call(base, token, "/board", {"task": tid})
        tasks[tid] = d["task"]
        events.extend(d.get("events", []))
        details[tid] = d
    return {"tasks": tasks, "chain": order,
            "buckets": top.get("buckets")}, events, details


def try_cancel(base, token, task):
    """task_cancel 一次并回状态文本：取消失败≠离线（离线只由轮询上报）。"""
    try:
        r = _call(base, token, "/task_cancel", {"task": task})
        return f"取消 {task}: cancelled={r.get('cancelled')}"
    except Exception as e:
        return f"取消失败: {e}"


def tail_session(sid, limit=40):
    """opencode 会话只读 tail：sqlite ro 读 session_message 尾段文本。"""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(data_home) / "opencode" / "opencode.db"
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT type, data FROM session_message WHERE session_id=?"
            " ORDER BY seq DESC LIMIT ?", (sid, limit)).fetchall()
    finally:
        con.close()
    out = []
    for typ, data in reversed(rows):
        try:
            d = json.loads(data)
        except ValueError:
            continue
        c = d.get("content")
        txt = (" ".join(p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text").strip()
               if isinstance(c, list) else str(c or d.get("text") or "").strip())
        if txt:
            out.append(f"[{typ}] {txt}")
    return out or ["（会话无文本消息）"]


def detail_page(d):
    """明细页文本：全字段＋事件尾＋done 结论。"""
    t = d.get("task", {})
    lines = [f"{k}: {t.get(k)}" for k in
             ("title", "state", "dispatcher_sid", "worker_sid", "created",
              "last_seen", "env_dir", "project_dir", "fail_streak",
              "chain_depth")]
    lines.append("---- 事件尾 ----")
    lines += [f"{e.get('ts', 0):.0f} {e.get('actor')} {e.get('event')}"
              f" {e.get('kind', '')}".rstrip() for e in d.get("events", [])[-10:]]
    if d.get("done_body"):
        lines += ["---- done 结论 ----", str(d["done_body"])[:2000]]
    return lines


def doctor_page(v, now=None):
    """doctor 页文本：年龄排行/最后活动排行/僵态候选/环境在盘性。"""
    now = time.time() if now is None else now
    lines = ["---- 任务年龄 ----"]
    lines += [f"{tid}  {rel_time(now - age, now)}" for tid, age in v.get("age_rank", [])]
    lines.append("---- 最后活动（最僵在前） ----")
    lines += [f"{tid}  {rel_time(ts, now)}" for tid, ts in v.get("last_seen_rank", [])]
    lines.append("---- 僵态候选（在飞>20min 无活动，判断归上级） ----")
    lines += [f"{tid}  {rel_time(ts, now)}" for tid, ts in v.get("stale_candidates", [])]
    lines.append("---- 环境 ----")
    lines += [f"{e['tid']} {'盘上' if e['on_disk'] else '缺失'} {e['env_dir']}"
              for e in v.get("env", [])]
    return lines


# ---------------- curses 壳 ----------------

_PAIR = {"fly": 1, "merged": 2, "blocked": 3, "gone": 4, "warn": 5, "bucket": 6}


def _init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    for i, fg in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN,
                            curses.COLOR_YELLOW, curses.COLOR_MAGENTA,
                            curses.COLOR_RED, curses.COLOR_BLUE), 1):
        curses.init_pair(i, fg, -1)


def _color(domain):
    return curses.color_pair(_PAIR[domain]) if domain in _PAIR and curses.has_colors() else 0


def _row_text(r):
    """任务/事件行文本：列全部定宽，last_seen 宽度浮动不漂移 repo 列。"""
    return (f"{'  ' * r.depth}{r.tid}: {_pad(r.title or '', 32)}"
            f"     {_pad(r.state, 13)} {_pad(r.last_seen or '-', 5)}"
            + (f"  {r.repo}" if r.repo else ""))


def draw_board(stdscr, m, status="", err=None):
    """一帧板面：头行＋固顶桶行＋滚动行流（选中反白/着色域）＋底行提示＋输入/确认行。"""
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    head = f" agora board  {status}" + (f"  离线: {err}" if err else "")
    stdscr.addnstr(0, 0, head, w - 1, curses.A_REVERSE)
    rows = m["rows"]
    nb = sum(1 for r in rows if r.kind == "bucket")
    visible = max(0, h - 4 - nb)           # 矮屏任务区可为 0：宁缺毋挤，不越提示行
    clamp_view(m, visible or 1, nb)        # 0 仍钳 top，空切片不画任务行
    for i in range(min(nb, max(0, h - 4))):  # 桶行也止于提示行上方
        stdscr.addnstr(i + 1, 0, f"─ {rows[i].title}", w - 1,
                       _color(rows[i].domain) | curses.A_BOLD)
    for i, r in enumerate(rows[m["top"]:m["top"] + visible]):
        attr = _color(r.domain)
        if r.kind == "event":
            attr |= curses.A_BOLD
        if m["top"] + i == m["sel"]:
            attr |= curses.A_REVERSE
        stdscr.addnstr(i + nb + 1, 0, _row_text(r), w - 1, attr)
    stdscr.addnstr(h - 3, 0,
                   " j/k move | g/G jump | Enter/l detail | L tail | d doctor"
                   " | x cancel | 1-5 bucket",
                   w - 1, curses.A_DIM)
    stdscr.addnstr(h - 2, 0,
                   f" r refresh | z fold | q quit | / filter[{m['filter']}]",
                   w - 1, curses.A_DIM)
    line = f"/{m['buf']}▌" if m.get("input") is not None else \
           (f"cancel {m['confirm']} ? y/n" if m.get("confirm") else None)
    if line:
        stdscr.addnstr(h - 1, 0, line, w - 1, curses.A_REVERSE)
    stdscr.refresh()


def draw_page(stdscr, title, lines):
    """全屏页（明细/doctor/会话 tail）：文本流＋底行 h 返回。"""
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addnstr(0, 0, f" {title}", w - 1, curses.A_REVERSE)
    for i, ln in enumerate(lines[:h - 2]):
        stdscr.addnstr(i + 1, 0, str(ln), w - 1, 0)
    stdscr.addnstr(h - 1, 0, " h back | q quit", w - 1, curses.A_DIM)
    stdscr.refresh()


def run(stdscr, base, token):
    """主循环：画→getch 阻塞 interval（AGORA_BOARD_INTERVAL 可配）；按键即时用
    内存模型重画，仅超时 -1 或 r 才 poll 拉 HTTP（避免每键 N+1 卡顿）。"""
    interval = max(0.2, float(os.environ.get("AGORA_BOARD_INTERVAL", "5")))
    curses.curs_set(0)
    _init_colors()
    stdscr.timeout(int(interval * 1000))
    m = {"rows": [], "sel": 0, "top": 0, "view": "board", "confirm": None,
         "input": None, "buf": "", "filter": "", "expanded": set(),
         "bucket": None}
    details, snap, events = {}, None, None
    status, err, page = "载入…", None, []

    def poll():
        """拉一帧刷新内存模型；失败保旧行置 err（err 语义＝轮询离线）。"""
        nonlocal details, status, err, snap, events
        try:
            snap, events, details = load_board(base, token)
            _rebuild()
        except Exception as e:                     # 离线/坏帧：保旧行＋报因
            err = str(e)

    def _rebuild():
        """用当前（缓存）帧按 m['filter'] 重建行＋刷新状态头。"""
        nonlocal status, err
        m["rows"] = build_rows(snap, events, m["filter"], expanded=m["expanded"],
                               bucket=m["bucket"])
        m["sel"] = min(m["sel"], max(0, len(m["rows"]) - 1))
        _skip_bucket(m, down=True)
        err = None
        status = f"{len(m['rows'])} 行  轮询 {interval:g}s"

    poll()                                         # 首帧先拉，免空板白等一个周期
    while True:
        if m["view"] == "board":
            draw_board(stdscr, m, status, err)
        else:
            draw_page(stdscr, m["view"], page)
        key = stdscr.getch()
        if key == -1:
            poll()                                 # 到点轮询刷一帧
            continue
        k = curses.keyname(key).decode() if key >= 256 else chr(key)
        a = handle_key(m, k)
        if a.op == "quit":
            return
        if a.op == "cancel":
            msg = try_cancel(base, token, a.task)
            poll()                                 # cancel 本就发 HTTP，顺势刷一帧
            status = msg                           # 保住取消回执不被轮询状态覆盖
        elif a.op == "refresh":
            poll()                                 # r 手动刷：立即拉一帧
        elif a.op in ("apply", "fold"):        # 过滤回车/z 折叠：用缓存帧即时重建，零 HTTP
            if snap is not None:
                try:
                    _rebuild()
                except Exception as e:             # 与 poll 同口径：坏帧保旧行＋报因
                    err = str(e)
        elif a.op == "detail":
            page = detail_page(details.get(a.task, {"task": {}, "events": []}))
        elif a.op == "doctor":
            try:
                page = doctor_page(_call(base, token, "/doctor", {}))
            except Exception as e:
                page = [f"doctor 不可用: {e}"]
        elif a.op == "tail":
            try:
                page = tail_session(a.arg)
            except Exception as e:                 # 只读降级为提示行
                page = [f"会话 tail 不可用: {e}"]
        elif a.op == "back":
            page = []


def selftest():
    """smoke：真连一次＋curses 画一帧即退（0＝帧成）。"""
    base, token = discover()
    err = None
    try:
        snap, events, _ = load_board(base, token)
        rows = build_rows(snap, events)
    except Exception as e:
        rows, err = [], str(e)
    m = {"rows": rows, "sel": 0, "top": 0, "view": "board", "confirm": None,
         "input": None, "buf": "", "filter": "", "expanded": set()}

    def once(stdscr):
        curses.curs_set(0)
        _init_colors()
        draw_board(stdscr, m, "selftest", err)

    curses.wrapper(once)
    print(f"selftest ok rows={len(m['rows'])}" + (f" err={err}" if err else ""))
    return 0


def _tty_setup():
    """ curses 前置：locale（CJK 宽字符/中文过滤输入）＋Esc 快速应答。"""
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        _tty_setup()
        if "--selftest" in argv:
            return selftest()
        base, token = discover()
        curses.wrapper(run, base, token)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"board_tui: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
