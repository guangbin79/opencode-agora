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

Req: F155 — 后端选择（spec gitstore-phase3-v3stack-backend）：V3Stack 增
显式 backend 参数（缺省 sqlite、未知 fail-closed、选择集中 _select_storage
唯一落点）；git 后端＝GitStore 自 <home>/state 构建＋同 SerialRepo/服务全接
线，close()/checkpoint_truncate() 兼容语义归 GitStore（幂等 no-op＋ref 失联
fail-closed）；本切片无 daemon/env/config 生产开关。

边界：timer/approval 服务本体归 T14/T16（传输层占位）；真实 OpenCode
接线归 T17；wiki 任务页常驻渲染（v2 page_writer 面）不在本任务断言面
（§8 任务页可从库重建，T13b/T17 再议）；verify 卫生闸（F99）与 usage
经济投影（F57）v3 服务面未铺，doctor.findings 恒空/行键缺席（F141 已注）。
"""
import dataclasses
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
from agorad.access.dsh import Dsh, HarnessRouter  # Req: F184
from agorad.authsvc import ROOT_SID, AuthService
from agorad.contracts import ContractError
from agorad.core.peer import TAGS as _PEER_TAGS, claim_path, parse_tag  # Req: F173
from agorad.core.verify_hygiene import find_verify_hygiene_issues  # Req: F175（core 纯函数复用勿复制；serverv3 传输层禁 core import，经 V3Stack 属性注入）
from agorad.cutover import MessageFileExporter
from agorad.domain import Event, epoch_ms, epoch_seconds
from agorad.gatedelivery import GateDeliveryService
from agorad.gateverify import DiskArtifactSink
from agorad.gitstore import BACKEND as GIT_BACKEND, GitStore
from agorad.healthsvc import HealthService
from agorad.messaging import MessagingService
from agorad.storage import Storage
from agorad.tasksvc import TaskService
from agorad.timersvc import TimerService

# F32 门环境消毒（压缩版）：剥服务自身态变量后显式副本传子进程
_GATE_ENV_STRIP = frozenset({"AGORA_HOME", "AGORAD_URL", "AGORA_OC_URL",
                             "AGORA_OC_PASSWORD", "AGORA_API_VERSION"})
_VERIFY_POLL = 0.2  # 逃逸后代弃读宽限窗（v2 core.gate 同款）
_PEER_SOFT_LIMIT = 10     # 同作者任务留言软限（v2 peer.SOFT_LIMIT 同款）
_PEER_READ_N = 20         # 读面条数上限（最新在前）
_PEER_FAIL_N = 50         # Req: F173 此路不通尾舱（v2 peer.FAIL_N 同值）
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


def _select_storage(home, backend, injector):
    """Req: F155 — 后端选择唯一落点（spec gitstore-phase3-v3stack-backend）：
    缺省 sqlite 不变、git 经 GitStore 自 <home>/state 构建、未知即拒
    （fail-closed 零动土）。选择集中装配层——服务/传输零复制（分层守卫
    禁其 import 存储后端）；生产启动面（daemon/env/config）本切片零改动。"""
    state = pathlib.Path(home) / "state"
    if backend != "sqlite" and backend != GIT_BACKEND:  # 先拒后动土（零残留空目录）
        raise ValueError(f"未知 backend {backend!r}（可用: sqlite、{GIT_BACKEND}）")
    state.mkdir(parents=True, exist_ok=True)  # Req: F158 部署面裸 home 自举（Storage 不建父目录）
    if backend == "sqlite":
        return Storage(state / "agora.db", injector=injector)
    return GitStore(state, injector=injector)


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
    500 截断；读面：同仓在飞五字段＋最近 20 条（最新在前、untrusted 标记）。
    Req: F173：留言类型化（tag 解析复用 v2 core.peer 纯函数）＋claim 撞车
    回执＋repo_board tag 过滤/fails 尾舱——响应形状与 v2 逐字段同构。"""

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
        """Req: F173 落 tag（行加 "tag" 键）＋claim 撞车回执；返回
        {"id", "collision"}（撞车仅提醒不拒写——写库永远先成功）。"""
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
            tag, rest = parse_tag(text)  # Req: F173（v2 core.peer 同款纯函数）
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"id": mid, "ts": self._clock() / 1000.0, "task": t.id,
                     "kind": "peer", "repo": repo, "from": sid, "body": text,
                     "tag": tag},
                    ensure_ascii=False) + "\n")
        with self._repo.uow():
            self._repo.append_event(Event(
                seq=0, ts_ms=self._clock(), actor=sid, event="peer_post",
                task=t.id, detail={"repo": repo, "mid": mid}))
        collision = None
        if tag == "claim":
            path = claim_path(rest)
            if path is not None:
                collision = self._collision(t.id, repo, path)
        return {"id": mid, "collision": collision}

    def _collision(self, tid, repo, path):
        """Req: F173 撞车回执（纯读不落状态）：落库后扫同仓 claim——四条件
        同仓/他人/作者非终态/path 逐字相等；同 path 多人取最新一条。"""
        tasks = {t.id: t for t in self._repo.tasks_all()}
        hit = None
        for m in self._rows():
            if (m.get("kind") != "peer" or m.get("task") == tid
                    or (m.get("tag") or "note") != "claim"):
                continue
            if _repo_key(m.get("repo") or "") != repo:
                continue
            author = tasks.get(m.get("task"))
            if author is None or author.state in self._TERMINAL:
                continue
            if claim_path(parse_tag(m.get("body", ""))[1]) == path:
                hit = {"with_task": m.get("task"), "path": path,
                       "author_env": author.env_dir}
        return hit

    def partition(self, sid):
        return _repo_key(self._own_task(sid).project_dir)

    def board(self, tasks, repo=None, tag=None):
        """v2 peer.board 形状：{repo, tasks[五字段], messages[最新在前]}。
        Req: F173：tag 过滤（值域 TAGS，非法值 ContractError）、messages
        元素带 tag、claim 带 author_env、fails 尾舱恒在（不受 tag 影响）。"""
        if tag is not None and tag not in _PEER_TAGS:
            raise ContractError(
                "invalid_argument",
                f"repo_board tag 须为 {'/'.join(_PEER_TAGS)} 之一"
                f"（收到 {tag!r}）",
                {"field": "tag", "enum": list(_PEER_TAGS)})
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
        pool = [r for r in self._rows() if r.get("kind") == "peer"
                and (repo is None
                     or _repo_key(r.get("repo") or "") == repo)]
        shown = (pool if tag is None
                 else [m for m in pool if (m.get("tag") or "note") == tag])
        msgs = [self._row(tasks_map, m, now)
                for m in reversed(shown[-_PEER_READ_N:])]
        fails = [self._row(tasks_map, m, now) for m in reversed(
            [m for m in pool if (m.get("tag") or "note") == "fail"]
            [-_PEER_FAIL_N:])]
        return {"repo": repo, "tasks": rows, "messages": msgs, "fails": fails}

    def _row(self, tasks_map, m, now):
        """留言投影（messages/fails 同构）：Req: F173 加 tag＋claim 的
        author_env（与 v2 core.peer.PeerBoard._row 同构）。"""
        author = tasks_map.get(m.get("task"))
        state = author.state if author else None
        try:
            age = int(now - float(m.get("ts", 0)))
        except (TypeError, ValueError):
            age = 0
        out = {"id": m.get("id"), "author": m.get("task"),
               "author_state": state,
               "age_seconds": age,
               "is_stale": (state in self._TERMINAL
                            if author else True),
               "body": m.get("body", ""),
               "tag": m.get("tag") or "note",  # 存量无 tag 键归一 note
               "untrusted": True}
        if out["tag"] == "claim" and author is not None and author.env_dir:
            out["author_env"] = author.env_dir
        return out


class Recover:
    """Req: F144 — 死 worker 会话重建（v2 core/recover.Recover 平移）。

    startup：启动复查——dispatched/running 且有 worker_sid 的任务逐个探活
    （oc 不可达≠死，本轮免判），死会话 reinject。reinject：oc.create 同
    BOOT 语义（(reinject) 标记＋任务页/env/合同——先 check_mail 补拉）→
    新 sid 覆写 worker_sid＋嵌套子任务 dispatcher_sid 同步（dispatcher_
    rebind 审计）→auth.rebind 旧牌改绑新会话（token 本体不变）→reinjected
    审计行→凭据文件重写＋直投（失败落消息兜底，kind=credential）。任何
    失败返回 False 不抛：先 create 成功才覆写行，不半写。"""

    _REINJECTABLE = frozenset({"dispatched", "running"})
    _NESTED_OPEN = frozenset({"todo", "dispatched", "running", "done",
                              "verify_failed", "reopened", "blocked"})

    def __init__(self, repo, oc, gitx, auth, messaging, *, home="",
                 clock=None):
        self._repo, self._oc, self._gitx = repo, oc, gitx
        self._auth, self._msg = auth, messaging
        self._home, self._clock = str(home), clock
        self._now = clock or (lambda: epoch_ms(time.time()))

    def startup(self):
        for t in self._repo.tasks_all():
            if t.gate.in_gate:  # 评审环 A-F1：kill -9 残闩清扫（v2
                # clear_gate_latches 同位——门线程不跨进程/栈存活，重启在
                # 闩＝残闩；done 行随后重排重闩，终态行免永久 gate_busy）
                now = self._now()
                with self._repo.uow():
                    fresh = self._repo.get_task(t.id)
                    if fresh is not None and fresh.gate.in_gate:
                        self._repo.upsert_task(dataclasses.replace(
                            fresh, updated_ms=now,
                            gate=dataclasses.replace(fresh.gate,
                                                     in_gate=False)))
                        self._repo.append_event(Event(
                            seq=0, ts_ms=now, actor="agorad",
                            event="gate_latch_reset", task=t.id))
        for t in self._repo.tasks_all():
            if t.state not in self._REINJECTABLE or not t.worker_sid:
                continue
            try:
                alive = self._oc.alive(t.worker_sid)
            except Exception:  # oc 不可达≠会话死：本轮免判，tick 再兜
                continue
            if not alive:
                try:
                    self.reinject(t.id)  # 评审环 B-F4：逐任务隔离，单任务
                except Exception:  # 败不拦余下死任务的启动复查
                    pass

    def reinject(self, tid, expect_wsid=None):
        t = self._repo.get_task(tid)
        if t is None or t.state not in self._REINJECTABLE or not t.env_dir:
            return False  # 复查：终态/续修态不重注入
        if expect_wsid is not None and t.worker_sid != expect_wsid:
            return None  # 身份已易主：让位他车道（CAS，与失败区分）
        try:
            self._gitx.head(t.env_dir)  # 环境在册探针：worktree 已失不盲注
            title = " ".join(str(t.title or tid).splitlines())
            boot = (f"[agora] {tid}: {title} (reinject)\n"
                    f"page: {self._home}/wiki/tasks/{tid}.md\n"
                    f"env: {t.env_dir}\n"
                    "contract: 会话曾死亡已重建——先 check_mail 补拉断点上下文，"
                    "继续任务页目标；结论经 post(done) 回填")
            # Req: F184 harness 分流（dsh 重建走 dsh 面；oc 形参零 churn）
            if (t.harness or "opencode") == "dsh":
                wsid = self._oc.create(t.env_dir, boot, model=t.model,
                                       harness="dsh")
            else:
                wsid = self._oc.create(t.env_dir, boot, model=t.model)
        except Exception:
            return False
        old = t.worker_sid
        now = self._now()
        with self._repo.uow():
            fresh = self._repo.get_task(tid)
            if (fresh is None or fresh.state not in self._REINJECTABLE
                    or fresh.worker_sid != old):
                return None  # 评审环 A-F4：create 窗内易主＝让位（CAS）非
                # 失败——False 会令 tick 对合法重派误升级 reinject-fail
                # （孤儿会话无 oc delete 面，交会话 GC——B-F5/C2 在案）
            self._repo.upsert_task(dataclasses.replace(
                fresh, worker_sid=wsid, updated_ms=now))
            for c in self._repo.tasks_all():
                if c.dispatcher_sid == old and c.state in self._NESTED_OPEN:
                    self._repo.upsert_task(dataclasses.replace(
                        c, dispatcher_sid=wsid, updated_ms=now))
                    self._repo.append_event(Event(
                        seq=0, ts_ms=now, actor="agorad",
                        event="dispatcher_rebind", task=c.id,
                        detail={"from": old, "to": wsid}))
            self._repo.append_event(Event(
                seq=0, ts_ms=now, actor="agorad", event="reinjected",
                task=tid, detail={"worker": wsid}))
        token = None
        try:
            token = self._auth.rebind(old, wsid)  # 旧牌改绑新会话
        except Exception as e:
            # 评审环 B-F4：行已易主而 rebind 失败＝身份卡死窗——留审计行，
            # rebind 幂等（按 old sid 查牌）可下轮 startup/tick 重入自愈
            with self._repo.uow():
                self._repo.append_event(Event(
                    seq=0, ts_ms=self._now(), actor="agorad",
                    event="rebind_failed", task=tid,
                    detail={"note": str(e)[:200], "worker": wsid}))
        if token:  # 凭据随 dispatch 同形送达（直投失败落库兜底）
            try:
                path = pathlib.Path(self._home) / "credentials" / f"{tid}.token"
                path.parent.mkdir(parents=True, exist_ok=True)
                # 建时 0600＋不跟符号链接（authsvc._write_secret 铁则同款，
                # 评审环 C1：write_text＋事后 chmod 有 0644 窗与预置链写面）
                data = (json.dumps({"token": token}) + "\n").encode("utf-8")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                text = f"[agora] credential {tid}\nfile: {path}"
                try:
                    ok = self._oc.inject(wsid, text, model=t.model)
                except Exception:
                    ok = False
                if not ok and self._msg is not None:
                    with self._repo.uow():
                        self._msg.record(
                            ts_ms=self._now(), from_sid="agorad",
                            to_sid=wsid, task_id=tid, kind="credential",
                            body=text,
                            dedup_key=f"credential:{tid}:{wsid}")
            except Exception:
                pass  # 落盘/直投败不废重注入：pull 自愈兜底
        return True


class V3Stack:
    """v3 全栈（storage schema→services 全接线）；单实例＝单 home。"""

    def __init__(self, home, *, oc, gitx, runner=None, anchor_reader=None,
                 clock=None, injector=None, exporter=None, emailer=None,
                 verify_timeout=300, verify_ceiling=None, quick_max_files=3,
                 quick_max_lines=30, lock_wait_s=300.0, enforce=True,
                 backend="sqlite", verify_hygiene=None,
                 dsh=None):  # Req: F184 dsh 适配器（缺省真装；测试可注桩）
        self.home = str(home)
        self.oc, self.gitx, self.runner = oc, gitx, runner
        self.clock = clock or (lambda: epoch_ms(time.time()))
        self.write_lock = threading.RLock()
        self.backend = backend
        # Req: F175 提案机械预检谓词（F99 单一规则，core 纯函数——compose 装配
        # 层可入 core；serverv3 传输层禁 core import，经本属性注入，测试可换桩）
        self.verify_hygiene = verify_hygiene or find_verify_hygiene_issues
        self.storage = _select_storage(self.home, backend, injector)
        repo = SerialRepo(self.storage, self.write_lock)
        self.repo = repo
        # Req: F184 harness 分流面：dispatch（tasksvc create）/health
        # （session/alive）/messaging（inject×3）经本面按 harness/sid 路由；
        # self.oc 保持原生 oc（doctor models/oc_version 面照旧）。
        self.dsh = dsh if dsh is not None else Dsh(home=self.home)
        face = HarnessRouter(oc, self.dsh)
        exporter = (exporter if exporter is not None
                    else MessageFileExporter(self.home))
        self.messaging = MessagingService(
            repo, face, home=self.home, clock=self.clock, injector=injector,
            exporter=exporter)
        self.auth = AuthService(repo, home=self.home, clock=self.clock,
                                oc=face)
        self.timers = TimerService(repo, self.messaging, clock=self.clock,
                                   injector=injector)
        self.tasks = TaskService(repo, gitx, face, messaging=self.messaging,
                                 home=self.home, clock=self.clock,
                                 timers=self.timers)
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
            lock_wait_s=lock_wait_s, timers=self.timers,  # Req: F145 终态钩
            roster_path=str(pathlib.Path(home) / ".agora" / "ignore.roster"))
        self.recover = Recover(repo, face, gitx, self.auth, self.messaging,
                               home=self.home, clock=self.clock)
        self.health = HealthService(repo, face, self.messaging, clock=self.clock,
                                    tasks=self.tasks, emailer=emailer,
                                    gitx=gitx, recover=self.recover)
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
        try:  # Req: F144 死 worker 启动复查重注入（v2 recover.startup 同位）
            self.recover.startup()
        except Exception as e:
            print(f"agorad-v3 recover startup failed: {e}", file=sys.stderr,
                  flush=True)
        try:
            self.gate.recover()  # managed 交付崩窗收敛（恢复面尽力而为）
        except Exception as e:
            print(f"agorad-v3 gate recover failed: {e}", file=sys.stderr,
                  flush=True)  # 无痕即失察（B 册 R1：留 stderr 兜底）
        try:  # Req: F145 overdue 启动即补发（分批；门控不过零写入）
            self.timers.catch_up()
        except Exception as e:
            print(f"agorad-v3 timer catch-up failed: {e}", file=sys.stderr,
                  flush=True)
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
        # 评审环 B-F2 注：单 gate 消费线程＝_LANE_LIMIT=1 车道互斥的唯一
        # 保证（gateverify 车道非真实锁）——加第二消费者前须先铺真锁
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

    def start_timer_thread(self, interval=30):
        """Req: F145 两级扫描的 timer 级（§6：30s 或 next due 提前醒）——
        首轮立即扫（启动补发兜底），其后按 next_wait_seconds 收敛醒点。"""
        def loop():
            while not self._stop.is_set():
                try:  # 评审环 B3：醒点计算与扫描同护——一次读错不杀线程
                    self.timers.catch_up()
                    wait = self.timers.next_wait_seconds(interval)
                except Exception as e:
                    print(f"agorad-v3 timer tick failed: {e}",
                          file=sys.stderr, flush=True)
                    wait = interval
                if self._stop.wait(max(wait, 0.05)):
                    break
        thread = threading.Thread(target=loop, daemon=True, name="v3-timer")
        thread.start()
        return thread

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
            except Exception as e:
                # 收口尽力而为（连接被外部蒸发/迟到写者），但无痕即失察
                # （评审环 B1：Git 面 ref 失联 fail-closed 信号在此被吞——
                # 至少 stderr 兜底，与 gate crashed 同纪律）
                print(f"agorad-v3 checkpoint on close failed: {e}",
                      file=sys.stderr, flush=True)
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
