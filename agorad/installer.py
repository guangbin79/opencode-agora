"""F31 PyPI 分发：agora-install 入口。

pipx install opencode-agora 后本命令定位安装记录内 wheel 数据面 share/opencode-agora，
注入 AGORA_PKG_ROOT（plugin/skills/install.sh/agora 拷贝源）与 AGORA_AGORAD_SRC
（site-packages 的 agorad 包＝lift 载荷拷贝源）后原样转跑 install.sh——
repo 模式（env 缺省）行为零变，pkg 分支仅由本命令的两个 env 触发。
"""

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath


def find_share_dir(entries):
    """RECORD 相对路径中命中 share/opencode-agora 段的 install.sh → 返回自 share 段起的尾段；落空 None。"""
    for e in entries:
        parts = PurePosixPath(str(e)).parts
        if parts and parts[-1] == "install.sh" and "share" in parts and "opencode-agora" in parts:
            return PurePosixPath(*parts[parts.index("share"):])
    return None


def main():
    dist = metadata.distribution("opencode-agora")
    rel = find_share_dir(dist.files or ())
    if rel is None:
        sys.exit("agora-install: 安装记录缺 share/opencode-agora 数据面——分发不完整，请重装 opencode-agora")
    script = dist.locate_file(rel)
    if not os.path.isfile(script):
        # locate_file 以 site-packages 为基；数据面 RECORD 形态不带 ../.. 前缀时落 sys.prefix/share
        script = Path(sys.prefix) / rel
    if not os.path.isfile(script):
        sys.exit(f"agora-install: install.sh 缺失（{script}）——分发不完整，请重装 opencode-agora")
    import agorad
    env = {**os.environ,
           "AGORA_PKG_ROOT": str(script.parent),
           "AGORA_AGORAD_SRC": os.path.dirname(agorad.__file__)}
    return subprocess.call(["bash", str(script)], env=env)
