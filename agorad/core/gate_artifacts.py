"""F122 gate 执行产物存储：verify/review/base_probe/integration 全量输出落盘。

ag-50ef74 实证：门通知只带输出尾（_TAIL），并发 oomd/clang-tidy 被杀时
事后无法定位具体阶段/ TU——本模块把每次执行的完整 combined output（不
分离 stdout/stderr）与 meta 原子落盘到 $AGORA_HOME/state/gate-artifacts/
<tid>/，供任务行 gate_artifact 投影、gate_artifact_written 事件与通知
artifact= 路径消费。failure taxonomy（classify_failure）归 gate.py 与
_cmd_dead 同源单点（core 禁兄弟 import），由 Gate 调用方算好 kind 传入。

目录 <ts>-<phase>-<seq>/ 含 output.log＋meta.json；keep 按 tid 内目录数
清最旧（缺省 50，AGORA_GATE_ARTIFACT_KEEP）；目录 0700/文件 0600；
temp＋os.replace 原子写；内置锁线程安全（F112 车道并行）；tid 只允许
[A-Za-z0-9_-]+（防路径逃逸）。纯 stdlib。
"""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

_TID = re.compile(r"^[A-Za-z0-9_-]+$")


def _write_atomic(path, data, mode=0o600):
    """temp＋os.replace 原子落盘（崩溃不留半写文件；权限显式设不受 umask 漂移）。"""
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fchmod(f.fileno(), mode)
    os.replace(tmp, path)


class ArtifactStore:
    """每次 gate 执行一个 artifact 目录；record 线程安全、keep 清最旧。"""

    def __init__(self, root, keep=50):
        if isinstance(keep, bool) or not isinstance(keep, int) or keep <= 0:
            raise ValueError(f"keep 须为正整数（收到 {keep!r}）")
        self._root = Path(root)
        self._keep = keep
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, tid, phase, env_dir, cmd, rc, output, started_at,
               ended_at, error=None, extra=None, failure_kind=None):
        """落一个 artifact 目录，返回 meta dict（含 path/log）；非法 tid 拒收。"""
        if not isinstance(tid, str) or not _TID.match(tid):
            raise ValueError(f"tid 只允许 [A-Za-z0-9_-]+（收到 {tid!r}）")
        text = output if isinstance(output, str) else ""
        data = text.encode("utf-8", errors="replace")
        try:
            duration = round(float(ended_at) - float(started_at), 3)
        except (TypeError, ValueError):
            duration = None
        with self._lock:
            self._seq += 1
            ts_base = ended_at if isinstance(ended_at, (int, float)) \
                else time.time()
            ts = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts_base))
            self._root.mkdir(parents=True, exist_ok=True)
            os.chmod(self._root, 0o700)
            tdir = self._root / tid
            tdir.mkdir(exist_ok=True)
            os.chmod(tdir, 0o700)
            d = tdir / f"{ts}-{phase}-{self._seq:04d}"
            d.mkdir(exist_ok=False)
            os.chmod(d, 0o700)
            _write_atomic(d / "output.log", data)
            meta = {"tid": tid, "phase": phase, "cmd": cmd, "cwd": env_dir,
                    "rc": rc, "started_at": started_at, "ended_at": ended_at,
                    "duration_s": duration, "bytes": len(data),
                    "failure_kind": failure_kind, "error": error,
                    "extra": extra, "path": str(d),
                    "log": str(d / "output.log")}
            _write_atomic(d / "meta.json",
                          json.dumps(meta, ensure_ascii=False,
                                     indent=1).encode("utf-8"))
            self._prune(tdir)
        return meta

    def _prune(self, tdir):
        """keep 清理：tid 目录内 artifact 目录按尾缀 seq（全局单调，权威创建序
        ——同秒跨 phase 时目录名字典序会乱序）留最新 N 个。"""
        try:
            dirs = [d for d in tdir.iterdir() if d.is_dir()]
        except OSError:
            return
        dirs.sort(key=lambda d: d.name.rsplit("-", 1)[-1])
        for old in dirs[:max(0, len(dirs) - self._keep)]:
            try:
                shutil.rmtree(old)
            except OSError:
                pass  # 清理败：artifact 只多不少，下轮 record 重试
