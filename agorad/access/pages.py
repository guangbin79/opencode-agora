"""任务页渲染视图（R6，access 层薄函数）：wiki/tasks/<id>.md＝当前态＋锚。

服务生成 frontmatter；任务页只存当前态（交互原文归消息库、历史归审计），
重渲染整文件覆写即同步。仅 stdlib，无包内 import。
F44 链式召回：全表快照与事件由装配处 callable 注入（access 不触 core/store），
页尾内联父/子/after 链 id＋近 8 事件摘要；无任何关联整段省略。
"""
from pathlib import Path
import re
import time

_FM_KEYS = ("id", "state", "chain_depth", "project_dir", "env_dir",
            "dispatcher_sid", "worker_sid", "req_id")

# F59 草稿页尾段（授权边界逐字，规格 spec/f59-stall-evidence.md）
_STALLED_TAIL = ("——草稿未经用户裁定；据此派发前须 dispatcher 报人确认"
                 "（自动修本体不在授权内）")
_DRAFT_N = re.compile(r"第 (\d+) 次升级")


def write_task_page(home, tid, snap, tasks=None, events=None) -> Path:
    home = Path(home)
    page = home / "wiki" / "tasks" / f"{tid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(f"{k}: {snap.get(k) or ''}" for k in _FM_KEYS)  # None＝空（round-1）
    v = snap.get("verify") or {}
    vline = "verify: " + v.get("state", "pending")
    if v.get("cmd"):
        vline += " - " + v["cmd"]
    body = ("---\n" + fm + "\n---\n\n# " + tid + "\n\ngoal: "
            + str(snap.get("goal", "")) + "\n\n" + vline + "\n")
    rel = _relations(tid, snap, tasks or {}, events or [])
    if rel:
        body += "\n## 关联\n\n" + rel
    page.write_text(body, encoding="utf-8")
    return page


def write_stalled_draft(home, tid, dispatcher_sid, evidence_lines) -> Path:
    """卡住证据包草稿页（F59）：wiki/drafts/stalled-<tid>.md，任务页同款写入通道。

    同任务重复升级覆写，首行锚记「第 n 次升级」；草稿止步——据此派发前须
    dispatcher 报人确认（尾段授权边界逐字）。"""
    home = Path(home)
    page = home / "wiki" / "drafts" / f"stalled-{tid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    try:
        m = _DRAFT_N.search(page.read_text(encoding="utf-8"))
        if m:
            n = int(m.group(1)) + 1
    except (OSError, UnicodeDecodeError):
        pass  # 首写无旧页/旧页残断（坏 UTF-8）＝第 1 次（覆写续写不堵死）
    anchor = (f"created: {time.strftime('%Y-%m-%d')} tid: {tid} "
              f"dispatcher: {dispatcher_sid or '-'} 第 {n} 次升级")
    page.write_text("\n".join([anchor, "", *evidence_lines, _STALLED_TAIL]) + "\n",
                    encoding="utf-8")
    return page


def _relations(tid, snap, tasks, events) -> str:
    """F44 关联段行集：父/子/after 链＋近 8 事件；返回空串＝整段省略。"""
    dsp = snap.get("dispatcher_sid")
    lines = [f"- 父: {pid} {t.get('state', '')} {t.get('title', '')}".rstrip()
             for pid, t in tasks.items()
             if dsp and t.get("worker_sid") == dsp]
    if snap.get("worker_sid"):  # 无 worker 无人可为其子（dispatcher_sid 恒非空）
        lines += [f"- 子: {cid} {t.get('state', '')}".rstrip()
                  for cid, t in tasks.items()
                  if t.get("dispatcher_sid") == snap["worker_sid"]]
    if snap.get("after"):
        lines.append(f"- after: {snap['after']}")
    succ = sorted(k for k, t in tasks.items() if t.get("after") == tid)
    if succ:
        lines.append("- 后继: " + " ".join(succ))
    created = snap.get("created") or 0
    for ev in events[-8:]:  # 最近 8 条（events 已按 task 过滤、行序即时序）
        row = f"- 事件: {ev.get('event', '')} +{int((ev.get('ts') or 0) - created)}s"
        note = str(ev.get("note") or "")[:60]
        if note:
            row += " " + note
        lines.append(row)
    return "\n".join(lines)
