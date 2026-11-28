"""Req: F162, F165 — v3 故障注入与差分测试设施（opencode-agora v3 P0-T2＋T2b 修复）。

测试支撑库（零生产码）：v3 迁移/差分/浸泡共用的四件设施——
① FaultInjector：命名 failpoint，before/after 两相位，触发抛可控异常并携上下文；
② TempAgoraHome：tmp AGORA_HOME 骨架（state/wiki/.agora/credentials），权限可
   断言，用例退出自动清理；
③ legacy fixture 五件：v2 磁盘形状权威＝agorad/access/store.py 布局＋
   agorad/core/task.py create 行构造＋agorad/core/state.py _normalize_snapshot
   ＋gate/health/recover 写点——供 v3 迁移器（P0-T11）与差分测试当输入语料；
④ 差分骨架 DiffSide/run_diff＋崩溃模拟 guarded_home_copy；
⑤ F149 双后端工厂 make_store＋Git journal 只读观察器 GitJournalObserver
（SQLite-v3 ↔ GitStore 差分基底）。
OC 面复用 tests/stub_opencode.py，本库不新建第二套 OC HTTP 桩。
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from agorad.access.store import Store
from agorad.core.msg import Router
from agorad.core.state import State

PHASES = ("before", "after")
SKELETON_DIRS = ("state", "state/messages", "wiki", ".agora", "credentials")
HOME_COMPLETE = ".home-complete"  # 完成标记：阶段化复制最后落盘（崩溃模拟判据）
DIR_MODE = 0o700  # 骨架目录权限面（harness 自定口径：可断言即可）
TOKEN_MODE = 0o600  # 同 store.write_ephemeral 凭据文件口径

# task.create 行构造全集（agorad/core/task.py create 的 add() 载荷——v3 迁移
# 字段映射的 v2 侧权威清单；测试另以真 TaskService.create 交叉锚定防漂移）。
CREATE_KEYS = (
    "title", "goal", "project_dir", "target_branch", "req_id", "model",
    "harness",  # Req: F184
    "deliverable", "client_key", "delivery", "env_dir", "after", "verify",
    "verify_pending", "waiting_on", "quick", "verify_base_exempt",
    "review_cmd", "state", "dispatcher_sid", "worker_sid", "root_sid",
    "chain_depth", "fail_streak", "last_seen", "created", "updated")
# gate/health/recover 写点字段（set_fields/transition/gate_tail/queue_cancel；
# env_base＝F111 评审锚消费（task.py dispatch 写入）、probe＝F107 base 探针缓存）。
GATE_HEALTH_KEYS = (
    "commit", "rebased_from", "env_base", "usage", "in_gate", "gate_env",
    "gate_origin_branch", "gate_artifact", "review_cache", "probe",
    "review_fail_streak", "pending_cancel", "pending_cancel_reason",
    # main F130 verify_infra 宽限兜底写键（ag-60f870 并 main 集成补齐）
    "verify_infra_grace", "verify_infra_backstop", "blocked_reason",
    "cancel_reason")


# ---- ① 命名 failpoint ----

class FaultInjected(Exception):
    """failpoint 缺省异常：failpoint/phase/context 供断言。"""

    def __init__(self, failpoint, phase, context):
        self.failpoint, self.phase, self.context = failpoint, phase, dict(context)
        super().__init__(f"failpoint={failpoint!r} phase={phase} context={context!r}")


class FaultInjector:
    """register 声明（含相位）→ arm 装填（可控异常）→ fire 触发。
    未装填 fire＝no-op（hits 只记武装触发）；未注册名/相位＝ValueError
    （拼写错防哑火——failpoint 名打错必须炸出来，不许静默不触发）。"""

    def __init__(self):
        self._failpoints = {}
        self._armed = {}
        self.hits = []  # [(name, phase, context)] 已触发记录

    def register(self, name, phases=PHASES):
        bad = set(phases) - set(PHASES)
        if bad:
            raise ValueError(f"failpoint {name!r} 相位须为 {PHASES}（收到 {sorted(bad)}）")
        self._failpoints[name] = set(phases)
        return self

    def arm(self, name, phase, exc=None):
        """exc：BaseException 实例＝原样抛；类/可调用＝无参调用构造；
        None＝FaultInjected(name, phase, context)。"""
        self._require(name, phase)
        self._armed[(name, phase)] = exc
        return self

    def disarm(self, name=None):
        if name is None:
            self._armed.clear()
        else:
            self._armed = {k: v for k, v in self._armed.items() if k[0] != name}
        return self

    def fire(self, name, phase, **context):
        self._require(name, phase)
        if (name, phase) not in self._armed:
            return
        self.hits.append((name, phase, dict(context)))
        exc = self._armed[(name, phase)]
        if isinstance(exc, BaseException):
            raise exc
        raise exc() if exc is not None else FaultInjected(name, phase, context)

    def _require(self, name, phase):
        if name not in self._failpoints:
            raise ValueError(f"未注册 failpoint: {name!r}（先 register 防拼写哑火）")
        if phase not in self._failpoints[name]:
            raise ValueError(f"failpoint {name!r} 未注册相位: {phase!r}")


# ---- ② 临时 AGORA_HOME ----

class TempAgoraHome:
    """tmp AGORA_HOME 骨架；ctx 管理器或 addCleanup(cleanup) 用毕即焚。
    权限断言面＝permissions_ok（目录 0700、token 文件 0600）。"""

    def __init__(self, root=None):
        self.path = Path(tempfile.mkdtemp(prefix="agora-v3-", dir=root))
        try:
            for d in SKELETON_DIRS:
                (self.path / d).mkdir(parents=True, exist_ok=True)
                os.chmod(self.path / d, DIR_MODE)  # 显式收紧：mkdir mode 受 umask 影响
        except BaseException:  # 半建骨架即焚：实例未成无人能 cleanup，防 tmp 泄漏
            shutil.rmtree(self.path, ignore_errors=True)
            raise

    @property
    def state(self):
        return self.path / "state"

    @property
    def messages(self):
        return self.path / "state" / "messages"

    @property
    def wiki(self):
        return self.path / "wiki"

    @property
    def agora(self):
        return self.path / ".agora"

    @property
    def credentials(self):
        return self.path / "credentials"

    def write_credential(self, tid, token):
        p = self.credentials / f"{tid}.token"
        p.write_text(json.dumps({"token": token}) + "\n", encoding="utf-8")
        os.chmod(p, TOKEN_MODE)  # store.write_ephemeral 同口径 0600
        return p

    def permissions_ok(self):
        return (all(_mode(self.path / d) == DIR_MODE for d in SKELETON_DIRS)
                and all(_mode(p) == TOKEN_MODE
                        for p in self.credentials.glob("*.token")))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _append_snapshot(home, tasks, mail_read=None):
    """按 store.append_state 同款形状写一行全量快照（legacy 语料落盘）。"""
    line = json.dumps({"version": 1, "tasks": tasks,
                       "mail_read": mail_read or {}}, ensure_ascii=False)
    with (home.state / "tasks.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---- ③ legacy fixture 五件 ----

def _matrix_row(tid, state, **over):
    """最小可 normalize 行（State 加载不炸的下限形状）。"""
    row = {"title": tid, "goal": "g", "project_dir": "/repo",
           "target_branch": "main", "env_dir": f"/envs/{tid}", "after": None,
           "verify": {"cmd": "true", "state": "registered",
                      "registered_by": "disp", "registered_at": 0.0,
                      "timeout": None, "timeout_source": "task"},
           "verify_pending": False, "state": state,
           "dispatcher_sid": "disp", "worker_sid": "w" if state != "todo" else None,
           "root_sid": "root", "chain_depth": 1, "fail_streak": 0,
           "created": 0.0, "updated": 0.0}
    if state in ("done", "merged", "closed"):
        row["commit"] = "c" * 40
    if state == "cancelled":
        row["cancel_reason"] = "superseded"
    row.update(over)
    return row


def full_field_row(tid="gt-full", **over):
    """task.create＋normalize 补齐＋gate/health 写点＝v2 任务行全字段投影。"""
    row = {
        # -- task.create 行构造（CREATE_KEYS 权威清单的实例化）--
        "title": "全字段样例", "goal": "goal text", "project_dir": "/repo",
        "target_branch": "main", "req_id": "F1", "model": None,
        "deliverable": None, "client_key": None,
        "delivery": {"mode": "managed", "status": "pending", "commit": None,
                     "base": None, "target_branch": "main", "attempt": 0,
                     "local_status": "pending", "error": None},
        "env_dir": f"/envs/{tid}", "after": None,
        "verify": {"cmd": "python3 -m pytest -q", "state": "registered",
                   "registered_by": "disp", "registered_at": 1780000000.0,
                   "timeout": 300, "timeout_source": "task"},
        "verify_pending": False, "waiting_on": None, "quick": False,
        "verify_base_exempt": False, "review_cmd": None,
        "harness": "dsh",  # Req: F184
        "state": "running", "dispatcher_sid": "disp", "worker_sid": "w",
        "root_sid": "root", "chain_depth": 2, "fail_streak": 0,
        "last_seen": 1780000100.0, "created": 1780000000.0,
        "updated": 1780000200.0,
        # -- gate/health/recover 写点（GATE_HEALTH_KEYS 权威清单的实例化）--
        "commit": "a" * 40, "rebased_from": "b" * 40, "env_base": "e" * 40,
        "usage": {"input": 1, "output": 2, "reasoning": 3, "cache_read": 4,
                  "cache_write": 5, "cost": 0.5, "msgs": 9},
        "in_gate": False, "gate_env": None, "gate_origin_branch": None,
        "gate_artifact": {"path": f"/h/gate/{tid}/output.log",
                          "phase": "verify", "rc": 0, "failure_kind": None,
                          "duration_s": 1.0, "bytes": 10},
        "review_cache": {"cmd": "true", "commit": "a" * 40, "ok": True,
                         "ran_at": 1780000150.0},
        "probe": {"result": "green", "cmd": "true", "base": "e" * 40,
                  "ran_at": 1780000050.0},
        "review_fail_streak": 0, "pending_cancel": False,
        "pending_cancel_reason": None, "cancel_reason": None,
        # main F130 verify_infra 宽限兜底写键（ag-60f870 并 main 集成补齐）
        "verify_infra_grace": {"deadline": 1780000300.0, "since": 1780000250.0},
        "verify_infra_backstop": 1780000350.0,
        "blocked_reason": None,
    }
    row.update(over)
    return row


def task_full_fields(home):
    """task-full-fields：全字段单任务快照入账，返回行（供 v3 迁移当输入）。"""
    row = full_field_row()
    _append_snapshot(home, {"gt-full": row})
    return row


def task_state_matrix(home):
    """task-state-matrix：v2 迁移矩阵全 10 态各一行，返回 {tid: row}。"""
    tasks = {f"gt-{s}": _matrix_row(f"gt-{s}", s) for s in (
        "todo", "dispatched", "running", "done", "verify_failed", "reopened",
        "merged", "blocked", "cancelled", "closed")}
    _append_snapshot(home, tasks)
    return tasks


def legacy_torn(home):
    """legacy-torn：断尾快照行＋撕裂消息＋坏事件行——v2 容错口径
    （末完整行胜出/撕裂件跳过）的机械语料。"""
    good = {"version": 1,
            "tasks": {"gt-torn-pre": _matrix_row("gt-torn-pre", "running")},
            "mail_read": {}}
    torn = json.dumps({"version": 1,
                       "tasks": {"gt-torn-lost": _matrix_row("gt-torn-lost", "todo")},
                       "mail_read": {}}, ensure_ascii=False)[:40]  # 断在行中
    (home.state / "tasks.jsonl").write_text(
        json.dumps(good, ensure_ascii=False) + "\n" + torn, encoding="utf-8")
    # 撕裂消息：正文分隔符前断尾（frontmatter 无 ": " 行 → 解析 ValueError 跳过）
    (home.messages / "00000001.md").write_text(
        "id: 1\nfrom: w\ntask: gt-torn-pre\nts: 100.0\nto: d\ntas",
        encoding="utf-8")
    ev = json.dumps({"ts": 1.0, "actor": "a", "event": "created",
                     "task": "gt-torn-pre"})
    ev3 = json.dumps({"ts": 3.0, "actor": "a", "event": "running",
                      "task": "gt-torn-pre"})
    (home.path / "events.jsonl").write_text(
        ev + "\n" + '{"ts": 2.0, "actor": \n' + ev3 + "\n", encoding="utf-8")
    return {"survivor": "gt-torn-pre", "lost": "gt-torn-lost"}


def legacy_unknown(home):
    """legacy-unknown：未知合法字段（v2 原样保留）＋gate_resource 瞬态渗漏
    （normalize 只规范内存视图、账本原文不动）——v3 legacy_extra_json 面语料。"""
    row = _matrix_row("gt-unknown", "running")
    row["future_v3_field"] = {"nested": [1, 2]}
    row["legacy_note"] = "2026-11 历史字段"
    row["gate_resource"] = {"waiting": True, "slot": 0}  # F116 瞬态：只进内存
    _append_snapshot(home, {"gt-unknown": row})
    return row


class _OcNoop:  # Router 构造缝最小件（check_mail 不触注入面；非 OC HTTP 桩）
    def inject(self, sid, text, model=None):
        return False

    def inject_silent(self, sid, text):
        return False

    def inject_notify(self, sid, text):
        return False


def mail_lineage(home, old_sid="ses-old111", new_sid="ses-new111"):
    """mail-lineage：换号血统语料——旧/新 sid 收件＋mail_read 游标＋
    delivered/inflight/pending 混合（后两者为 Router 内存态，预置注入）。
    期望 check_mail(new_sid) 拉 [read_old, pending_lineage, pending_new]——
    新 sid 游标从 0 起，旧收件全量血统补拉（spec §5 承重墙，at-least-once）。"""
    store = Store(home.path)
    tid = "gt-mail"
    spec = [(old_sid, 50.0), (old_sid, 200.0), (new_sid, 300.0),
            (new_sid, 400.0), (new_sid, 500.0)]
    ids = [store.write_message({"ts": ts, "task": tid, "kind": "note",
                                "from": "w", "to": to}, f"body-{i}")
           for i, (to, ts) in enumerate(spec, 1)]
    store.append_state({"version": 1, "tasks": {tid: _matrix_row(tid, "running")},
                        "mail_read": {old_sid: 100.0}})  # 旧 sid 游标居中
    state = State(store)
    router = Router(store, state, _OcNoop(),
                    same_lineage=lambda cur, old: (cur, old) == (new_sid, old_sid))
    router._delivered.update({ids[3]})  # 已在线送达：不重交
    router._inflight.update({ids[2]})   # 在线注入中：让位不抢交（F87）
    return {"store": store, "state": state, "router": router,
            "sids": (old_sid, new_sid),
            "ids": dict(zip(("read_old", "pending_lineage", "inflight",
                             "delivered", "pending_new"), ids))}


# ---- ④ 差分骨架＋崩溃模拟 ----

class DiffSide:
    """差分一侧后端协议：call＝一次工具调用的外部可见结果（HTTP 状态＋
    响应 JSON）；visible_state＝消息文件内容等外部可见面快照。
    v2 今日实现/v3 未来实现同接口。"""

    def call(self, tool, args):
        raise NotImplementedError

    def visible_state(self):
        raise NotImplementedError


class StubSide(DiffSide):
    """脚本化桩侧：script[tool]＝(status, json) 或 callable(args)→二者；
    emit 模拟消息落库驱动 visible_state。今日 v2/v3 两侧均以本桩跑通差分
    形状；真 v2 侧后续以 make_server 适配替换（不新建第二套 OC 桩）。"""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls = []
        self.messages = []

    def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        behavior = self.script.get(tool, (200, {"ok": True}))
        return behavior(dict(args)) if callable(behavior) else behavior

    def emit(self, fm, body):
        self.messages.append({**fm, "body": body})

    def visible_state(self):
        return {"messages": [dict(m) for m in self.messages]}


def run_diff(sides, ops):
    """同操作序列逐侧逐步执行，记录外部可见结果。
    ops＝[{"tool": 名, "args": {...}}, ...]；返回 {侧名: [记录]}，
    记录＝{"tool","status","json","visible"}（每步后快照 visible）。"""
    records = {name: [] for name in sides}
    for name, side in sides.items():
        for op in ops:
            status, body = side.call(op["tool"], op.get("args", {}))
            records[name].append({"tool": op["tool"], "status": status,
                                  "json": body,
                                  "visible": side.visible_state()})
    return records


def diff_mismatches(records):
    """逐侧逐 op 对齐比较：返回 [(op_index, 侧a, 侧b, key)] 失配清单；
    侧数 <2 即 ValueError——空/单侧无从对齐，禁冒充零失配。"""
    names = sorted(records)
    if len(names) < 2:
        raise ValueError(f"diff_mismatches 须至少两侧（收到 {len(names)}）")
    if len({len(records[n]) for n in names}) != 1:
        return [(-1, names[0], names[1], "length")]
    out = []
    for i in range(len(records[names[0]])):
        for other in names[1:]:
            for key in ("tool", "status", "json", "visible"):
                if records[other][i].get(key) != records[names[0]][i].get(key):
                    out.append((i, names[0], other, key))
    return out


def digest_tree(path):
    """目录指纹：相对路径＋文件字节 sha256（排序稳定）——「源目录不变」断言用。"""
    h = hashlib.sha256()
    root = Path(path)
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode() + b"\0")
        h.update(p.read_bytes() if p.is_file() else b"<dir>")
    return h.hexdigest()


def guarded_home_copy(inj, failpoint, src, dst):
    """阶段化 home 复制＋failpoint（迁移/切换的崩溃窗语料）：
    before＝动土前（dst 未生）；after＝复制完成、完成标记落盘前（dst 在盘
    但不完整）。两相位炸开均不损 src——回滚安全＝目录互换的前提。"""
    inj.fire(failpoint, "before", src=str(src), dst=str(dst))
    shutil.copytree(src, dst)
    inj.fire(failpoint, "after", src=str(src), dst=str(dst))
    (Path(dst) / HOME_COMPLETE).write_text("complete\n", encoding="utf-8")
    return Path(dst)


def home_is_complete(path):
    """完整 home 判据：完成标记＋骨架目录齐备（「目标不可用」断言面）。"""
    p = Path(path)
    return ((p / HOME_COMPLETE).is_file()
            and all((p / d).is_dir() for d in SKELETON_DIRS))


# ---- ⑤ 双后端工厂＋Git journal 只读观察器（Req: F149）----

BACKENDS = ("sqlite", "git")


def make_store(home, backend="sqlite", injector=None):
    """双后端仓库工厂：sqlite→Storage(state/agora.db)、git→GitStore(state)。
    Storage/GitStore import 惰性入体——git 路径零 SQL 模块触达自生产侧
    （gitstore 自身 import storage 取错误基类不在本面）；未知 backend 拒
    （拼写错防哑跑单侧冒充差分）。"""
    if backend == "sqlite":
        from agorad.storage import Storage
        return Storage(home.state / "agora.db", injector=injector)
    if backend == "git":
        from agorad.gitstore import GitStore
        return GitStore(home.state, injector=injector)
    raise ValueError(f"未知 backend {backend!r}（可用: {BACKENDS}）")


class GitJournalObserver:
    """state/journal.git 只读观察器（差分断言面）：resolve refs/agora/journal
    ＋首父 commit 链＋HEAD 对象路径＋tx JSON 解析＋相邻提交只增判定。
    零 SQL/零 sqlite 触达——只起只读 plumbing（rev-parse/ls-tree/cat-file/
    diff-tree）；写入/损毁语料归 test_v3_gitstore 侧 helpers。"""

    def __init__(self, state_path):
        self._git_dir = os.path.join(os.fspath(state_path), "journal.git")

    def _git(self, *args, check=True):
        env = {"PATH": os.environ.get("PATH", ""), "GIT_DIR": self._git_dir,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "LC_ALL": "C"}
        # timeout：挂死 git 有界炸出（评审环 B/C 册——测试面不输生产面）
        r = subprocess.run(["git", *args], env=env, capture_output=True,
                           timeout=60)
        if check and r.returncode != 0:
            raise RuntimeError(
                f"observer git {args[0]} rc={r.returncode}: "
                f"{r.stderr.decode('utf-8', 'replace').strip()}")
        return r.returncode, r.stdout

    def head(self):
        """refs/agora/journal → sha（空历史 None）。"""
        rc, out = self._git("rev-parse", "--verify", "refs/agora/journal",
                            check=False)
        return out.decode().strip() if rc == 0 else None

    def commits(self):
        """首父链 [head..root]（cat-file 逐级解 parent）。"""
        head = self.head()
        chain = []
        while head is not None:
            chain.append(head)
            _, out = self._git("cat-file", "commit", head)
            head = next(
                (l.split()[1].decode() for l in out.splitlines()
                 if l.startswith(b"parent ")), None)
        return chain

    def paths(self):
        """HEAD 树全路径→blob sha（-z：路径不转义）。"""
        head = self.head()
        if head is None:
            return {}
        _, out = self._git("ls-tree", "-r", "-z", head)
        entries = {}
        for chunk in out.split(b"\x00"):
            if not chunk:
                continue
            meta, _, path = chunk.partition(b"\t")
            entries[path.decode("utf-8", "surrogateescape")] = (
                meta.split()[2].decode())
        return entries

    def blob(self, sha) -> bytes:
        return self._git("cat-file", "blob", sha)[1]

    def txs(self):
        """{seq: tx dict}——tx/*.json 解析面。"""
        out = {}
        for path, sha in self.paths().items():
            if path.startswith("tx/"):
                tx = json.loads(self.blob(sha).decode("utf-8"))
                out[tx["seq"]] = tx
        return out

    def additions_only(self):
        """相邻提交 diff-tree --name-status 全 A（canonical 只增不改不删）。
        空链（ref 未解析/空历史）fail-closed——观察面失明≠不变量成立
        （评审环 OCR R1：防恒 True 空转断言）。"""
        chain = self.commits()
        if not chain:
            raise RuntimeError(
                f"{self._git_dir}: refs/agora/journal 未解析——"
                "观察面失明≠只增成立（fail-closed）")
        for old, new in zip(chain[1:], chain[:-1]):  # (旧, 新)
            _, out = self._git("diff-tree", "-r", "--name-status", old, new)
            lines = [l for l in out.decode().splitlines() if l.strip()]
            if not lines or any(not l.startswith("A\t") for l in lines):
                return False
        return True
