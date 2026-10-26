"""任务页渲染视图（R6，access 层薄函数）：wiki/tasks/<id>.md＝当前态＋锚。

服务生成 frontmatter；任务页只存当前态（交互原文归消息库、历史归审计），
重渲染整文件覆写即同步。仅 stdlib，无包内 import。
"""
from pathlib import Path

_FM_KEYS = ("id", "state", "chain_depth", "project_dir", "env_dir",
            "dispatcher_sid", "worker_sid")


def write_task_page(home, tid, snap) -> Path:
    home = Path(home)
    page = home / "wiki" / "tasks" / f"{tid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(f"{k}: {snap.get(k, '')}" for k in _FM_KEYS)
    v = snap.get("verify") or {}
    vline = "verify: " + v.get("state", "pending")
    if v.get("cmd"):
        vline += " - " + v["cmd"]
    page.write_text(
        "---\n" + fm + "\n---\n\n# " + tid + "\n\ngoal: "
        + str(snap.get("goal", "")) + "\n\n" + vline + "\n",
        encoding="utf-8")
    return page
