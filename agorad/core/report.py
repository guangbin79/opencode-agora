"""状态视图（F10，设计 §4.1）：五桶/链视图/明细/doctor/HTML——纯函数集。

snap/store 进来、dict/list/str 出去，零状态零副作用（store 仅读原语）。
五桶机械口径：已合并=merged+closed（closed 是合并后唯一后继）；在飞=其余
非终态全部（含 todo 未派、done 待并；verify_failed/reopened 是续修态归在飞）；
失败=空集保留位——v2 状态集无独立"失败"终态（质量败走 verify_failed 续修，
3 连败自动 blocked 归阻塞桶）；阻塞=blocked；已取消=cancelled。
"""
import html
import time
from pathlib import Path

_MERGED = {"merged", "closed"}


def buckets(snap):
    """五桶汇总：桶名→任务 id 列表（失败桶恒空，口径见模块 docstring）。"""
    out = {"已合并": [], "在飞": [], "失败": [], "阻塞": [], "已取消": []}
    for tid, t in sorted(snap["tasks"].items()):
        s = t["state"]
        if s in _MERGED:
            out["已合并"].append(tid)
        elif s == "blocked":
            out["阻塞"].append(tid)
        elif s == "cancelled":
            out["已取消"].append(tid)
        else:
            out["在飞"].append(tid)
    return out


def chain_view(snap, dispatcher_sid):
    """链级视图：该会话直接派的任务＋其 worker 再派的下级（逐跳传递展开）。

    seen 集合防环（账本行语义上无环，但环行入账时 /view 不得挂死）兼去重。
    """
    out, frontier, seen = [], [dispatcher_sid], {dispatcher_sid}
    while frontier:
        sid = frontier.pop(0)
        for tid, t in sorted(snap["tasks"].items()):
            if t.get("dispatcher_sid") == sid:
                out.append(tid)
                w = t.get("worker_sid")
                if w and w not in seen:
                    seen.add(w)
                    frontier.append(w)
    return out


def detail(snap, store, tid):
    """单任务明细三件套：全字段行＋事件链＋done 正文（最新一条＝当前结论）。"""
    done = [m for m in store.read_messages()
            if m.get("task") == tid and m.get("kind") == "done"]
    return {"task": dict(snap["tasks"][tid]), "events": store.read_events(tid),
            "done_body": done[-1]["body"] if done else None}


def doctor_view(snap, store_home):
    """账本侧事实：任务年龄/最后活动排行＋env_dir 在盘性（盘上孤儿扫描属②期）。"""
    home, tasks = Path(store_home), snap["tasks"]
    age = sorted(tasks.items(), key=lambda kv: kv[1].get("created", 0.0))
    stale = sorted(tasks.items(),
                   key=lambda kv: (kv[1].get("last_seen") is not None,
                                   kv[1].get("last_seen") or 0.0))
    env = []
    for tid, t in sorted(tasks.items()):
        e = t.get("env_dir")
        if not e:
            continue
        p = Path(e)
        env.append({"tid": tid, "env_dir": e,
                    "on_disk": (p if p.is_absolute() else home / p).exists()})
    return {"age_rank": [[tid, time.time() - t.get("created", 0.0)]
                         for tid, t in age],
            "last_seen_rank": [[tid, t.get("last_seen")] for tid, t in stale],
            "env": env}


def view_html(snap):
    """/view 最小页：五桶计数明细＋链表（dispatcher→worker 链边）。"""
    esc = lambda v: html.escape(str(v))
    parts = ["<html><body><h1>agora board</h1>"]
    for name, tids in buckets(snap).items():
        parts.append(f"<h2>{name} {len(tids)}</h2><ul>")
        parts.extend(f"<li>{esc(i)}</li>" for i in tids)
        parts.append("</ul>")
    parts.append("<h2>链</h2><ul>")
    for tid, t in sorted(snap["tasks"].items()):
        parts.append(f"<li>{esc(tid)}: {esc(t['state'])} "
                     f"{esc(t.get('dispatcher_sid'))}→{esc(t.get('worker_sid'))} "
                     f"{esc(t.get('title', ''))}</li>")
    parts.append("</ul></body></html>")
    return "".join(parts)
