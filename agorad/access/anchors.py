"""评审环锚默认实现（R3/F111，access 薄函数）：锚＝HEAD＋评审链根 base 双件。

`.review-loop/.last-reviewed`＝分支 HEAD（单 sha，与既有消费者同构）；
`.review-loop/.review-base`＝评审链根（首轮＝评审起点，续轮沿旧值不动——
增量评审链的覆盖起点）。两件齐全且 HEAD 一致才视为绿（blocker/high 扫描
归评审环自身，此处只认锚）；缺任一件/落后/格式非法/非 git 环境一律
None（done 拒收路径：旧单件锚的链根不可证，重建锚＝再跑一次评审环）。
仅 stdlib。
"""
import re
import subprocess
from pathlib import Path

_SHA_RE = re.compile(r"[0-9a-fA-F]{4,40}")


def _resolve_sha(env_dir, value):
    """短/全 sha 一律经 rev-parse 归一为全 sha（ag-4d17ea：评审环工具写出的
    形态不一，字符串全等恒不等）；非 sha 文本（HEAD/分支名/选项样垃圾）与
    rev-parse 失败（歧义/未知）一律 None——符号引用会解析到漂移后的 tip，
    令"锚落后"检测变恒真，故必须拒于门外。"""
    if not _SHA_RE.fullmatch(value):
        return None
    r = subprocess.run(["git", "rev-parse", "--verify", value],
                       cwd=str(env_dir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else None


def read_anchor(env_dir):
    """返回 {"head": sha, "base": base, "clean": True} 或 None。"""
    if not env_dir:
        return None
    marker = Path(env_dir) / ".review-loop" / ".last-reviewed"
    base_file = Path(env_dir) / ".review-loop" / ".review-base"
    if not marker.is_file() or not base_file.is_file():
        return None
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(env_dir),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sha = r.stdout.strip() if r.returncode == 0 else ""
    if not sha:
        return None
    if _resolve_sha(env_dir,
                    marker.read_text(encoding="utf-8",
                                     errors="replace").strip()) != sha:
        return None  # 锚落后＝评审后又有提交（标记同型归一后比较）
    base = _resolve_sha(env_dir,
                        base_file.read_text(encoding="utf-8",
                                            errors="replace").strip())
    if not base:
        return None  # 链根空/不可解析＝不可证（格式非法同拒）
    return {"head": sha, "base": base, "clean": True}
