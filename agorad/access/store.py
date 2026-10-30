"""账本唯一磁盘接触点：append-only 账本四原语＋flock 单实例。

布局 $AGORA_HOME：state/tasks.jsonl（全量快照行）、state/messages/<id>.md
（不可变，头部 key: value 行＋空行＋正文）、events.jsonl（审计）、
易失件（service.json 等，可覆写非账本）。
"""
import fcntl
import json
import os
from pathlib import Path

TAIL_BYTES = 1 << 20  # 启动尾读窗口：load 耗时与账本大小解耦；窗内无完整行回退全扫
COMPACT_LINES = 500  # 瘦身阈值：行数超限即原子重写仅留末完整行（flock 单写者、迁移路径时机避开启动），账本有上界


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
        self._task_lines = None  # 懒播种的账本行数（flock 单写者安全）：免每次追加全文件重扫；崩溃漂移只挪瘦身时机

    def append_state(self, snap: dict) -> None:
        if self._task_lines is None:  # 播种在追加前：计磁盘实有行，此后内存自增不重扫
            self._task_lines = self._count_lines() if self._tasks.exists() else 0
        line = json.dumps(snap, ensure_ascii=False)
        self._append_line(self._tasks, line)  # 先追加后瘦身：崩溃至多留下超限账本，状态不丢
        self._task_lines += 1
        if self._task_lines > COMPACT_LINES:
            try:
                self._compact(line)
                self._task_lines = 1
            except OSError:
                pass  # 追加已 fsync 持久化，瘦身属维护动作：失败不回滚成功语义，计数器留超阈待下次追加重试

    def load_state(self) -> dict | None:
        if not self._tasks.exists():
            return None
        if self._tasks.stat().st_size > TAIL_BYTES:
            snap = self._load_tail()
            if snap is not None:
                return snap
            # 尾窗无完整行（巨行/断尾贯穿全窗）→ 回退全文件扫，末行胜出语义不变
        snap = None
        with self._tasks.open(encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)  # 断尾行丢弃，取最后完整行
                except ValueError:
                    pass
        return snap

    def _load_tail(self) -> dict | None:
        with self._tasks.open("rb") as f:
            f.seek(-TAIL_BYTES, os.SEEK_END)
            block = f.read()
        i = block.find(b"\n")  # 跨窗切割的首段残行丢弃，只解析完整行
        if i == -1:
            return None
        snap = None
        for raw in block[i + 1:].split(b"\n"):
            try:
                snap = json.loads(raw)  # 断尾行丢弃，取最后完整行（同全扫口径）
            except ValueError:
                pass
        return snap

    def _count_lines(self) -> int:
        n = 0
        with self._tasks.open("rb") as f:
            for chunk in iter(lambda: f.read(TAIL_BYTES), b""):
                n += chunk.count(b"\n")
        return n

    def _compact(self, line: str) -> None:
        # 刚追加行即末完整行（flock 单写者）；原子重写同 write_ephemeral 口径
        tmp = self._tasks.with_name(self._tasks.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._tasks)

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
        """全库消息按 msg-id 升序（补零文件名序＝id 序）；frontmatter 键＋id(int)＋body。

        门中崩溃撕裂件（坏头/断尾/空文件）跳过——同 load_state 断尾容错口径。
        """
        out = []
        for p in sorted(self._messages.glob("*.md")):
            try:
                head, _, body = p.read_text(encoding="utf-8").partition("\n\n")
                fm = dict(line.split(": ", 1) for line in head.splitlines())
                fm["id"] = int(fm["id"])
                fm["body"] = body
            except (ValueError, KeyError, OSError):
                continue
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
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 建时即 0600：tokens 含 root 牌，umask 只能收紧
        os.fchmod(fd, 0o600)  # O_CREAT mode 仅建时生效：复用旧版崩溃遗留 .tmp 时显式收紧，自愈才闭环
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # 原子覆写：读者（lift 解析 service.json）不见半写

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
