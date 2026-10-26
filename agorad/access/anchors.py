"""评审环锚默认实现（R3，access 薄函数）：.review-loop/.last-reviewed＝分支 HEAD。

锚文件存在且内容与环境分支 HEAD 一致即视为绿（blocker/high 扫描归评审环自身，
此处只认锚）；缺锚/落后/非 git 环境一律 None（done 拒收路径）。仅 stdlib。
"""
import subprocess
from pathlib import Path


def read_anchor(env_dir):
    """返回 {"head": sha, "clean": True} 或 None（缺锚/落后/无环境）。"""
    if not env_dir:
        return None
    marker = Path(env_dir) / ".review-loop" / ".last-reviewed"
    if not marker.is_file():
        return None
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(env_dir),
                       capture_output=True, text=True)
    sha = r.stdout.strip() if r.returncode == 0 else ""
    if not sha or marker.read_text(encoding="utf-8").strip() != sha:
        return None
    return {"head": sha, "clean": True}
