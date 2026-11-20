"""Req: F142 — v3 栈装配（opencode-agora v3 P4-T13a，设计 §1/§4/§8）。

compose＝v3 装配层（§1「main 唯一装配」的 v3 对应件，v2 生产 main/httpd/
agora_mcp 运行路径零改动——本模块新增并行 v3 面）：从 AGORA_HOME 建
Storage（schema 初始化）→§4 单一全局写锁（RLock 包 uow——多线程
transport/gate/health 并发下写事务互斥、无 interleaving 半态；读走独立
读者连接不被阻塞；同线程嵌套先拿锁再由 storage 拒「不可嵌套」，原语义
不变不化死锁）→messaging/auth/tasksvc/gatedelivery/healthsvc/peer 全接线。

oc/gitx/runner/anchors/exporter 一律构造注入（测试注入 stub——「oc/gitx
Protocol 注入」架构守卫锚）：缺省 anchors＝access.read_anchor、runner＝
本模块压缩版子进程 runner（F36 软硬双顶口径）、exporter＝cutover.
MessageFileExporter（§8 派生导出＋「引用行不悬空」规则复用）。

V3Stack 生命周期：start() 幂等（auth 清扫＋outbox 恢复＋managed 崩窗
收敛＋done 残留重排恰一次＋单 gate 后台线程——v2 make_server 同款重排）；
close() 收口（队列哨兵→join→持写锁 wal_checkpoint(TRUNCATE)＋关连接）。
enforce 旗＝内存态（v2 同款：重启默认 on、开/关写审计）。

边界：timer/approval 服务本体归 T14/T16（传输层占位）；真实 OpenCode
接线归 T17；wiki 任务页常驻渲染（v2 page_writer 面）不在本任务断言面
（§8 任务页可从库重建，T13b/T17 再议）；verify 卫生闸（F99）与 usage
经济投影（F57）v3 服务面未铺，doctor.findings 恒空/行键缺席（F141 已注）。
"""
import json
import os
import pathlib
import queue
import signal
import subprocess
import sys
import threading
import time

from agorad.access import anchors
from agorad.authsvc import ROOT_SID, AuthService
from agorad.contracts import ContractError
from agorad.cutover import MessageFileExporter
from agorad.domain import Event, epoch_ms, epoch_seconds
from agorad.gatedelivery import GateDeliveryService
from agorad.gateverify import DiskArtifactSink
from agorad.healthsvc import HealthService
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService

# F32 门环境消毒（压缩版）：剥服务自身态变量后显式副本传子进程
_GATE_ENV_STRIP = frozenset({"AGORA_HOME", "AGORAD_URL", "AGORA_OC_URL",
                             "AGORA_OC_PASSWORD", "AGORA_API_VERSION"})
_VERIFY_POLL = 0.2  # 逃逸后代弃读宽限窗（v2 core.gate 同款）
_PEER_SOFT_LIMIT = 10     # 同作者任务留言软限（v2 peer.SOFT_LIMIT 同款）
_PEER_READ_N = 20         # 读面条数上限（最新在前）
_PEER_MAX_LEN = 500
_PEER_LIMIT_MSG = f"留言软限 {_PEER_SOFT_LIMIT} 条/任务已满——沉淀请走 knowledge index"


def subprocess_runner(env_dir, cmd, *, timeout=None, ceiling=None,
                      on_soft=None):
    """缺省 gate runner（v2 core.gate._subprocess_runner 压缩版——装配面
    默认，测试注入桩）：软顶到点提醒恰一次不杀（等待窗取 min(软顶, 硬顶)
    ——软顶点必醒）；硬顶（缺省 4×软顶）SIGKILL 全组 rc=124；杀后限宽
    communicate＋超时弃读（逃逸后代持管道时防门线程挂死，v2 同款收尾）；
    输出显式 UTF-8 宽容解码。"""
    timeout = 300 if timeout is None else timeout
    ceiling = 4 * timeout if ceiling is None else ceiling
    child_env = {k: v for k, v in os.environ.items()
                 if k not in _GATE_ENV_STRIP}
    proc = subprocess.Popen(cmd, shell=True, cwd=env_dir, env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    soft, start = False, time.monotonic()
    while True:
        now = time.monotonic()
        waits = [_VERIFY_POLL, ceiling - (now - start)]
        if not soft:  # 软顶未拍：软顶点也在等待窗内（到点必醒）
            waits.append(timeout - (now - start))
        try:
            out, err = proc.communicate(timeout=max(min(waits), 0.001))
            return proc.returncode, (out or "") + (err or "")
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            if not soft and elapsed >= timeout:
                soft = True
                if proc.poll() is None and on_soft is not None:
                    try:
                        on_soft()
                    except Exception:
                        pass  # 提醒链故障不废硬顶杀保证
            if elapsed >= ceiling:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:  # 逃逸后代持管道时弃读，防挂死（v2 core.gate 收尾）
                    out, err = proc.communicate(timeout=2 * _VERIFY_POLL)
                except subprocess.TimeoutExpired:
                    out = err = ""
                return 124, ((out or "") + (err or "")
                             + "\nverify 进程组被硬顶终止（gate ceiling）")


class _LockedUow:
    """uow 上下文：RLock 包 BEGIN..COMMIT（§4 单一全局写锁归装配）。"""

    def __init__(self, store, lock):
        self._store, self._lock, self._cm = store, lock, None

    def __enter__(self):
        self._lock.acquire()
        try:
            self._cm = self._store.uow()
            return self._cm.__enter__()
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, *exc):
        try:
            return self._cm.__exit__(*exc)
        finally:
            self._lock.release()


class SerialRepo:
    """Storage 写锁代理：uow 经全局 RLock 串行；其余方法/属性直通。"""

    def __init__(self, store, lock):
        self._store, self._lock = store, lock

    def uow(self):
        return _LockedUow(self._store, self._lock)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _repo_key(path):
    """F90 仓根归一（v2 peer._repo_key 同口径；坏路径回退原串）。"""
    try:
        return str(pathlib.Path(path or "").resolve())
    except (OSError, ValueError):
        return str(path or "")


def _env_activity(env_dir):
    """env 工作区最新 mtime（F59 活性口径压缩版；缺/空＝0.0）。"""
    latest = 0.0
    for root, _dirs, files in os.walk(env_dir):
        for name in files:
            try:
                latest = max(latest,
                             os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
    return latest


def load_verify_profiles(home):
    """F126 画像装配面（v2 main.load_verify_profiles 压缩版）：缺文件/
    坏字节＝空画像；其余读失败（EACCES/EISDIR 等）raise ValueError
    fail-fast——勿静默空画像，否则预算无声回落 global（v2 评审环 R1 裁定）。"""
    path = pathlib.Path(home) / ".agora" / "verify-profiles.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return {}
    except OSError as e:
        raise ValueError(f"{path} 读取失败：{e}") from None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} 顶层须为对象（收到 {type(data).__name__}）")
    out = {}
    for key, val in data.items():
        if (not isinstance(key, str) or not os.path.isabs(key)
                or not isinstance(val, dict)):
            raise ValueError(f"{path} key {key!r} 须绝对路径→对象")
        timeout = val.get("timeout")
        if (isinstance(timeout, bool) or not isinstance(timeout, int)
                or timeout <= 0):
            raise ValueError(f"{path} key {key!r} 字段 timeout 须为正整数秒"
                             f"（收到 {timeout!r}）")
        prof = {"timeout": timeout}
        if val.get("note") is not None:
            prof["note"] = val["note"]
        canon = _repo_key(key)
        if canon in out:
            raise ValueError(f"{path} 键归一后重复：{key!r} 同指 {canon!r}")
        out[canon] = prof
    return out


class PeerBoard:
    """F22 同仓留言板（v2 core.peer 平移；协作面非 §4 记账权威→文件
    state/peers.jsonl）。写面：作者任务锁（worker 现役行）＋软限/任务＋
    500 截断；读面：同仓在飞五字段＋最近 20 条（最新在前、untrusted 标记）。"""

    _TERMINAL = frozenset({"merged", "closed", "cancelled"})
    _EXCL = _TERMINAL | {"blocked"}

    def __init__(self, repo, home, clock=None, lock=None):
        self._repo = repo
        self._path = pathlib.Path(home) / "state" / "peers.jsonl"
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._lock = lock or threading.RLock()  # 读-算-写临界区（并发留言串行）

    def _rows(self):
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # 撕裂行跳过（v2 消息面同口径）
            if isinstance(row, dict):
                out.append(row)
        return out

    def _own_task(self, sid):
        active = [t for t in self._repo.tasks_all()
                  if t.worker_sid == sid and t.state not in self._TERMINAL]
        if not active:
            raise ContractError(
                "forbidden", f"peer 留言: {sid} 无 worker 任务行（作者不可定）")
        return max(active, key=lambda t: (t.created_ms or 0, t.id))

    def post(self, sid, body):
        t = self._own_task(sid)
        repo = _repo_key(t.project_dir)
        with self._lock:  # 软限判定与 mid 编号同临界区（并发留言不撞号不失守）
            rows = self._rows()
            if sum(1 for r in rows if r.get("kind") == "peer"
                   and r.get("task") == t.id) >= _PEER_SOFT_LIMIT:
                raise ContractError("conflict", _PEER_LIMIT_MSG)
            mid = max((r["id"] for r in rows
                       if isinstance(r.get("id"), int) and r["id"] > 0),
                      default=0) + 1
            text = str(body)[:_PEER_MAX_LEN]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"id": mid, "ts": self._clock() / 1000.0, "task": t.id,
                     "kind": "peer", "repo": repo, "from": sid, "body": text},
                    ensure_ascii=False) + "\n")
        with self._repo.uow():
            self._repo.append_event(Event(
                seq=0, ts_ms=self._clock(), actor=sid, event="peer_post",
                task=t.id, detail={"repo": repo, "mid": mid}))
        return mid

    def partition(self, sid):
        return _repo_key(self._own_task(sid).project_dir)

    def board(self, tasks, repo=None):
        """v2 peer.board 形状：{repo, tasks[五字段], messages[最新在前]}。"""
        repo = _repo_key(repo) if repo else None
        tasks_map = {t.id: t for t in tasks}
        now = self._clock() / 1000.0
        rows = [{"tid": t.id, "title": t.title, "state": t.state,
                 "last_seen": (None if t.last_seen_ms is None
                               else epoch_seconds(t.last_seen_ms)),
                 "req_id": t.req_id}
                for t in sorted(tasks, key=lambda t: t.id)  # Task 无序——显式键
                if t.state not in self._EXCL
                and (repo is None or _repo_key(t.project_dir or "") == repo)]
        peers = [r for r in self._rows() if r.get("kind") == "peer"
                 and (repo is None
                      or _repo_key(r.get("repo") or "") == repo)]
        msgs = []
        for m in reversed(peers[-_PEER_READ_N:]):
            author = tasks_map.get(m.get("task"))
            state = author.state if author else None
            try:
                age = int(now - float(m.get("ts", 0)))
            except (TypeError, ValueError):
                age = 0
            msgs.append({"id": m.get("id"), "author": m.get("task"),
                         "author_state": state,
                         "age_seconds": age,
                         "is_stale": (state in self._TERMINAL
                                      if author else True),
                         "body": m.get("body", ""), "untrusted": True})
        return {"repo": repo, "tasks": rows, "messages": msgs}


class V3Stack:
    """v3 全栈（storage schema→services 全接线）；单实例＝单 home。"""

    def __init__(self, home, *, oc, gitx, runner=None, anchor_reader=None,
                 clock=None, injector=None, exporter=None, emailer=None,
                 verify_timeout=300, verify_ceiling=None, quick_max_files=3,
                 quick_max_lines=30, lock_wait_s=300.0, enforce=True):
        self.home = str(home)
        self.oc, self.gitx, self.runner = oc, gitx, runner
        self.clock = clock or (lambda: epoch_ms(time.time()))
        self.write_lock = threading.RLock()
        self.storage = Storage(pathlib.Path(home) / "state" / "agora.db",
                               injector=injector)
        repo = SerialRepo(self.storage, self.write_lock)
        self.repo = repo
        exporter = (exporter if exporter is not None
                    else MessageFileExporter(self.home))
        self.messaging = MessagingService(
            repo, oc, home=self.home, clock=self.clock, injector=injector,
            exporter=exporter)
        self.auth = AuthService(repo, home=self.home, clock=self.clock, oc=oc)
        self.tasks = TaskService(repo, gitx, oc, messaging=self.messaging,
                                 home=self.home, clock=self.clock)
        self.verify_profiles = load_verify_profiles(self.home)
        self.gate = GateDeliveryService(
            repo, gitx, runner or subprocess_runner, messaging=self.messaging,
            tasksvc=self.tasks,
            anchors=anchor_reader or anchors.read_anchor,
            artifacts=DiskArtifactSink(
                pathlib.Path(home) / "state" / "gate-artifacts"),
            home=self.home, clock=self.clock, injector=injector,
            verify_timeout=verify_timeout, verify_ceiling=verify_ceiling,
            verify_profiles=self.verify_profiles,
            quick_max_files=quick_max_files, quick_max_lines=quick_max_lines,
            lock_wait_s=lock_wait_s,
            roster_path=str(pathlib.Path(home) / ".agora" / "ignore.roster"))
        self.health = HealthService(repo, oc, self.messaging, clock=self.clock,
                                    tasks=self.tasks, emailer=emailer,
                                    gitx=gitx)
        self.peer = PeerBoard(repo, self.home, clock=self.clock,
                              lock=self.write_lock)
        self._enforce = bool(enforce)
        self._gate_q = queue.Queue()
        self._started, self._closed = False, False
        self._worker = self._health_thread = None
        self._stop = threading.Event()
        self.root_token = None

    # ---- 生命周期 ----

    def start(self):
        """幂等启动：清扫/恢复＋done 残留重排恰一次＋gate 后台线程＋root
        牌 bootstrap（无 root 牌即签，daemon 身份 sid="root"）。"""
        if self._started:
            return self
        self._started = True
        if self.storage.get_credential(ROOT_SID) is None:
            self.root_token, _ = self.auth.issue("root", "root")
        self.auth.sweep()
        self.messaging.recover()
        try:
            self.gate.recover()  # managed 交付崩窗收敛（恢复面尽力而为）
        except Exception as e:
            print(f"agorad-v3 gate recover failed: {e}", file=sys.stderr,
                  flush=True)  # 无痕即失察（B 册 R1：留 stderr 兜底）
        for t in self.storage.tasks_all():
            if t.state == "done":
                self.enqueue(t.id)  # 重启重排（v2 make_server 同款）
        self._worker = threading.Thread(target=self._gate_loop, daemon=True,
                                        name="v3-gate")
        self._worker.start()
        return self

    def enqueue(self, tid):
        if not self._closed:
            self._gate_q.put(tid)

    def _gate_loop(self):
        while True:
            tid = self._gate_q.get()
            try:
                if tid is not None and not self._closed:
                    self.gate.run(tid)
            except Exception as e:  # 门体崩溃留痕（P0-1：无痕 except pass 已废）
                try:
                    with self.repo.uow():
                        self.repo.append_event(Event(
                            seq=0, ts_ms=self.clock(), actor="gate",
                            event="gate_crashed", task=tid,
                            detail={"note": str(e)[:200]}))
                except Exception:
                    # 审计面也病（关停竞态/库已关）：stderr 兜底留痕不静默
                    print(f"agorad-v3 gate crashed on {tid}: {e}",
                          file=sys.stderr, flush=True)
            finally:
                self._gate_q.task_done()
                if tid is None:
                    break

    def start_health_thread(self, interval=300):
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.health.tick()
                except Exception as e:
                    # 无痕即失察（与 gate 线程同纪律：stderr 兜底留痕）
                    print(f"agorad-v3 health tick failed: {e}",
                          file=sys.stderr, flush=True)
        self._health_thread = threading.Thread(
            target=loop, daemon=True, name="v3-health")
        self._health_thread.start()
        return self._health_thread

    def close(self):
        """收口：队列哨兵→join→持写锁 checkpoint(TRUNCATE)＋关连接
        （断言「关闭时 DB 连接收敛＋WAL checkpoint」）。幂等。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._gate_q.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)
        with self.write_lock:
            try:
                self.storage.checkpoint_truncate()
            except Exception:
                pass  # 连接已被外部蒸发（崩溃模拟/迟到写者）：收口尽力而为
            finally:
                try:
                    self.storage.close()
                except Exception:
                    pass

    # ---- 装配面查询/旗标 ----

    def enforce_on(self):
        return self._enforce

    def set_enforce(self, on):
        """仅 root 生效（may 矩阵前置）；开/关审计（v2 Auth 同款）。"""
        self._enforce = bool(on)
        with self.repo.uow():
            self.repo.append_event(Event(
                seq=0, ts_ms=self.clock(), actor="root",
                event="enforce_on" if self._enforce else "enforce_off"))
        return self._enforce

    def env_activity(self, tid):
        t = self.storage.get_task(tid)
        return _env_activity(t.env_dir) if t is not None and t.env_dir else 0.0

    def models(self):
        """doctor 注入面：oc.models()（不可得＝None→条件键缺席）。"""
        try:
            return self.oc.models()
        except Exception:
            return None

    def oc_version(self):
        try:
            return (self.oc.probe() or {}).get("oc_version")
        except Exception:
            return None
