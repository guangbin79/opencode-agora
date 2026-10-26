"""账本唯一磁盘接触点：append-only 账本四原语＋flock 单实例。

布局 $AGORA_HOME：state/tasks.jsonl（全量快照行）、state/messages/<id>.md
（不可变，头部 key: value 行＋空行＋正文）、events.jsonl（审计）、
易失件（service.json 等，可覆写非账本）。
"""
import fcntl
import json
import os
from pathlib import Path


class Store:
    def __init__(self, home: Path):
        self.home = Path(home)
        self._tasks = self.home / "state" / "tasks.jsonl"
        self._events = self.home / "events.jsonl"
        self._messages = self.home / "state" / "messages"
        self._lock_path = self.home / "agorad.lock"
        self._lock_fd = None
        # 单写者内存计数器；重启扫目录文件名 max+1 恢复
        self._next_mid = self.scan_max_msg_id() + 1

    def append_state(self, snap: dict) -> None:
        self._append_line(self._tasks, json.dumps(snap, ensure_ascii=False))

    def load_state(self) -> dict | None:
        if not self._tasks.exists():
            return None
        snap = None
        with self._tasks.open(encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)  # 断尾行丢弃，取最后完整行
                except ValueError:
                    pass
        return snap

    def append_event(self, ev: dict) -> None:
        self._append_line(self._events, json.dumps(ev, ensure_ascii=False))

    def write_message(self, fm: dict, body: str) -> int:
        mid = self._next_mid
        self._next_mid += 1
        header = f"id: {mid}\n" + "".join(
            f"{k}: {fm[k]}\n" for k in sorted(fm)
        )
        path = self._messages / f"{mid:08d}.md"  # 补零保证文件名字典序＝msg-id 数值序
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as f:  # "x"＝已存在即拒，id 不可变
            f.write(f"{header}\n{body}")
            f.flush()
            os.fsync(f.fileno())
        return mid

    def scan_max_msg_id(self) -> int:
        if not self._messages.exists():
            return 0
        return max((int(p.stem) for p in self._messages.glob("*.md")), default=0)

    def read_messages(self) -> list[dict]:
        """全库消息按 msg-id 升序（补零文件名序＝id 序）；frontmatter 键＋id(int)＋body。"""
        out = []
        for p in sorted(self._messages.glob("*.md")):
            head, _, body = p.read_text(encoding="utf-8").partition("\n\n")
            fm = dict(line.split(": ", 1) for line in head.splitlines())
            fm["id"] = int(fm["id"])
            fm["body"] = body
            out.append(fm)
        return out

    def read_events(self, tid=None) -> list[dict]:
        """events.jsonl 全量（tid 给定则按 task 过滤）按行序；断尾行丢弃同 load_state。"""
        out = []
        if not self._events.exists():
            return out
        with self._events.open(encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if tid is None or ev.get("task") == tid:
                    out.append(ev)
        return out

    def write_ephemeral(self, name: str, data: dict) -> None:
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_ephemeral(self, name: str) -> dict | None:
        path = self.home / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None  # 断尾/损坏件视为缺席，同 load_state 容错口径

    def flock_singleton(self) -> bool:
        if self._lock_fd is not None:
            return True
        self.home.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd  # 持有至进程退出，关闭即释放
        return True

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
