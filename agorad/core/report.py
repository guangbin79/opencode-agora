"""状态视图（F10，设计 §4.1）：五桶/链视图/明细/doctor/HTML——纯函数集。

snap/store 进来、dict/list/str 出去，零状态零副作用（store 仅读原语）。
五桶机械口径：已合并=merged+closed（closed 是合并后唯一后继）；在飞=其余
非终态全部（含 todo 未派、done 待并；verify_failed/reopened 是续修态归在飞）；
失败=空集保留位——v2 状态集无独立"失败"终态（质量败走 verify_failed 续修，
3 连败自动 blocked 归阻塞桶）；阻塞=blocked；已取消=cancelled。
"""
import html
import os
import time
from pathlib import Path

_MERGED = {"merged", "closed"}
_STALE_AFTER = 1200
# 僵态候选口径（F5 决策记录 2026-10-28）：blocked 已属失控拍过人、终态与 todo
# 无 worker 活动面——均排除。
_STALE_STATES = {"dispatched", "running", "done", "verify_failed", "reopened"}


def _env_activity(env_dir):
    """env 工作区文件活性（F15）：目录树最新 mtime（含目录）——worker 原生干活期
    不碰工具面，文件在写即活着；缺失/不可读返 0.0。纯 stdlib（core 勿 import access）。
    """
    if not env_dir:
        return 0.0
    latest = 0.0
    for root, _dirs, files in os.walk(env_dir):
        for p in [root, *(os.path.join(root, f) for f in files)]:
            try:
                latest = max(latest, os.stat(p).st_mtime)
            except OSError:
                pass
    return latest


def _stale_baseline(t):
    """僵态活性基线（F15）：max(last_seen or created, env 活性）——文件持续在写＝活着。"""
    return max(t.get("last_seen") or t.get("created", 0.0),
               _env_activity(t.get("env_dir")))


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


def forest_view(snap):
    """全局链森林（gt-6def73）：父子边仍按 worker_sid↔dispatcher_sid 上溯，
    但在全任务集找父；根＝全任务集内无父的任务。输出 DFS 子树聚簇序 tid 列表
    （同层按 tid 字典序，稳定可测）；显式栈防递归，seen 集环防护有界
    （环岛 DFS 不可达，按 tid 序尾附兜底，全员在列）。
    """
    tasks = snap["tasks"]
    parent = {tid: next((p for p in sorted(tasks)
                         if tasks[p].get("worker_sid")
                         == t.get("dispatcher_sid")), None)
              for tid, t in tasks.items()}
    kids, roots = {tid: [] for tid in tasks}, []
    for tid in sorted(tasks):
        p = parent[tid]
        (roots if p is None else kids[p]).append(tid)
    out, seen, stack = [], set(), roots[::-1]
    while stack:
        tid = stack.pop()
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
        stack.extend(reversed(kids[tid]))
    return out + [tid for tid in sorted(tasks) if tid not in seen]


def detail(snap, store, tid):
    """单任务明细三件套：全字段行＋事件链＋done 正文（最新一条＝当前结论）。

    F15 增 env_active_seconds_ago：env 在盘时 now-最新 mtime（int 秒）；回收/缺失省略。
    """
    done = [m for m in store.read_messages()
            if m.get("task") == tid and m.get("kind") == "done"]
    out = {"task": dict(snap["tasks"][tid]), "events": store.read_events(tid),
           "done_body": done[-1]["body"] if done else None}
    env = out["task"].get("env_dir")
    if env and Path(env).is_dir():
        act = _env_activity(env)
        if act > 0.0:  # is_dir 后被并发回收→walk 空，省略而非输出 1.7e9 假值
            out["env_active_seconds_ago"] = int(time.time() - act)
    return out


def stale_candidates(snap, now, base=None):
    """僵态候选公开口径（F5；F15 融 env 活性）：[[tid, 基线ts],...] 基线升序（最僵在前）。

    在飞口径状态（_STALE_STATES）且 now-_stale_baseline(t)>_STALE_AFTER，
    基线＝max(last_seen or created, env 文件活性)；health tick 构造注入复用同一口径。
    base＝doctor_view 预计算基线 map（评审环 round1：免同请求二遍全树 walk），缺省现算。
    """
    cand = []
    for tid, t in snap["tasks"].items():
        # shortcut: 门线程挂死（verify 永不返回）随 in_gate 永久免检，门故障类报警另立，when: 出现首例门挂死
        if t["state"] in _STALE_STATES and not t.get("in_gate"):
            b = base.get(tid) if base is not None else None
            if b is None:
                b = _stale_baseline(t)
            if now - b > _STALE_AFTER:
                cand.append([tid, b])
    cand.sort(key=lambda kv: kv[1])
    return cand


def doctor_view(snap, store_home, now=None):
    """账本侧事实：任务年龄/最后活动排行＋僵态候选＋env_dir 在盘性（盘上孤儿扫描属②期）。

    排行活性基线（F15）：age_rank/last_seen_rank 同取 _stale_baseline（与
    stale_candidates 同口径），字段名不变、值改为融合基线的 now- 差与原值。
    僵态候选（F5）：同 stale_candidates 口径，纯显示，处置归上级判断。
    """
    now = time.time() if now is None else now
    home, tasks = Path(store_home), snap["tasks"]
    base = {tid: _stale_baseline(t) for tid, t in tasks.items()}
    ranked = sorted(tasks, key=lambda tid: base[tid])
    cand = stale_candidates(snap, now, base=base)
    env = []
    for tid, t in sorted(tasks.items()):
        e = t.get("env_dir")
        if not e:
            continue
        p = Path(e)
        env.append({"tid": tid, "env_dir": e,
                    "on_disk": (p if p.is_absolute() else home / p).exists()})
    return {"age_rank": [[tid, now - base[tid]] for tid in ranked],
            "last_seen_rank": [[tid, base[tid]] for tid in ranked],
            "stale_candidates": cand,
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
