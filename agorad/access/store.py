"""账本唯一磁盘接触点：append-only 账本四原语＋flock 单实例。

布局 $AGORA_HOME：state/tasks.jsonl（全量快照行）、state/messages/<id>.md
（不可变，头部 key: value 行＋空行＋正文）、events.jsonl（审计）、
易失件（service.json 等，可覆写非账本）。
"""
import copy
import fcntl
import json
import os
import threading
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
        # 内存计数器；重启扫目录文件名 max+1 恢复
        # id 配给锁（Req: F75 评审残留 D3）：httpd/gate/health 三线程写者实证，
        # 无锁丢更新撞号即 open("x") 抛——配对（读＋自增）须原子，写盘在锁外
        self._next_mid = self.scan_max_msg_id() + 1
        self._mid_lock = threading.Lock()
        self._task_lines = None  # 懒播种的账本行数（flock 单写者安全）：免每次追加全文件重扫；崩溃漂移只挪瘦身时机
        self._ephemeral_lock = threading.Lock()  # 同名 tmp/replace 全区间串行（F86）
        self._messages_cache = None  # F92 惰性加载；None＝未载，[] 也合法
        self._messages_lock = threading.Lock()
        self._events_cache = None
        self._events_lock = threading.Lock()

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
        line = json.dumps(ev, ensure_ascii=False)
        with self._events_lock:  # F92：写盘与缓存发布同临界区，读者不见半态
            self._append_line(self._events, line)
            if self._events_cache is not None:
                self._events_cache.append(copy.deepcopy(ev))

    def write_message(self, fm: dict, body: str) -> int:
        with self._mid_lock:  # 配对原子（D3）：锁内读＋自增；文件写在锁外并发
            mid = self._next_mid
            self._next_mid += 1
        header = f"id: {mid}\n" + "".join(
            f"{k}: {fm[k]}\n" for k in sorted(fm)
        )
        path = self._messages / f"{mid:08d}.md"  # 补零保证文件名字典序＝msg-id 数值序
        # 缓存形状对齐磁盘解析面：frontmatter 值一律 str，仅 id 转 int（F92）
        record = {k: str(v) for k, v in fm.items()}
        record.update(id=mid, body=body)
        with self._messages_lock:  # F92：写盘与缓存发布同临界区
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as f:  # "x"＝已存在即拒，id 不可变
                f.write(f"{header}\n{body}")
                f.flush()
                os.fsync(f.fileno())
            if self._messages_cache is not None:
                self._messages_cache.append(copy.deepcopy(record))
                # mid 配给与发布分属两锁：晚号先发布时保 id 升序（F92 评审 Important）
                self._messages_cache.sort(key=lambda m: m["id"])
        return mid

    def scan_max_msg_id(self) -> int:
        if not self._messages.exists():
            return 0
        return max((int(p.stem) for p in self._messages.glob("*.md")), default=0)

    def _load_messages_from_disk(self) -> list[dict]:
        """全库消息按 msg-id 升序（补零文件名序＝id 序）；撕裂件跳过。"""
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

    def read_messages(self) -> list[dict]:
        """全库消息（frontmatter 键＋id(int)＋body）；热读走内存一层拷贝（F92）。"""
        with self._messages_lock:
            if self._messages_cache is None:
                self._messages_cache = self._load_messages_from_disk()
            # 一层 dict 拷贝：消息字段全是标量，足以保持“每次新对象”且免 deepcopy 热读税
            return [dict(m) for m in self._messages_cache]

    def _load_events_from_disk(self) -> list[dict]:
        """events.jsonl 全量按行序；断尾行丢弃同 load_state。"""
        out = []
        if not self._events.exists():
            return out
        with self._events.open(encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                out.append(ev)
        return out

    def read_events(self, tid=None) -> list[dict]:
        """events.jsonl 全量（tid 给定则按 task 过滤）；热读走内存一层拷贝（F92）。"""
        with self._events_lock:
            if self._events_cache is None:
                self._events_cache = self._load_events_from_disk()
            got = self._events_cache
            return [dict(e) for e in got
                    if tid is None or e.get("task") == tid]

    def write_ephemeral(self, name: str, data: dict) -> None:
        with self._ephemeral_lock:  # tmp 名按目标派生：并发同名写共享 tmp 必撕裂（F86）
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

    # ---- F43 worker 牌落盘（$AGORA_HOME/credentials/<tid>.token，0600） ----

    def write_credential(self, tid: str, token: str) -> str:
        """worker 牌经 write_ephemeral 口径落盘（0600）；返回绝对路径供指针文本。"""
        name = f"credentials/{tid}.token"
        self.write_ephemeral(name, {"token": token})
        return str(self.home / name)

    def remove_credential(self, tid: str) -> None:
        try:
            (self.home / f"credentials/{tid}.token").unlink(missing_ok=True)
        except OSError:
            pass  # 尽力而为 hygiene：credentials 杂散文件/权限坏等环境成因不拦断调用方（牌作废归 auth.revoke）

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
