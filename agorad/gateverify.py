"""Req: F136 — v3 verify/review 门执行体（opencode-agora v3 P3-T9a，设计 §3/§4/§5）。

accept_done 前置（锚三检＋F111 链根＋vacuous/子任务闸＋六类结构化拒因）与
门体上篇：F107 base-red 探针（cmd+base 缓存、豁免、分类路由）、env verify
分类（质量红弹 worker 计连败／126·127 命令死与 signal 类机械败驻留 done
路由派发方）、F36/F51 软硬双顶（注入 runner 经 timeout/ceiling kwargs 观察
生效预算；task→profile→global 三级解析；等待不折预算——预算恒按任务行
现值解析，与入门前耗时无关）、F114 门审（review_cache cmd+HEAD 双键缓存、
独立 review_fail_streak、三轮分歧 blocked）、F123 集成复跑（detached
integration 树上 export 前缀注入 AGORA_INTEGRATION_BASE，集成红＝质量
口径 reopened）、F122 执行产物（每轮 output.log＋meta.json 落盘＋DB 行＋
任务行投影）与崩溃窗（门闩/排队取消原子结算、done 残留重排恰一次、
FaultInjector verify/结算前后）。

语义自 v2 agorad/core/gate.py 平移（成熟算法不重写）；分层（§1）：仓库/
gitx/runner/锚读取/产物存储/Notifier/streak 全 Protocol 构造注入——不
import access/storage，零 SQL；runner（子进程）与产物落盘恒在 UoW 外
（§4 禁子进程），迁移＋审计＋消息同一 UoW 复合提交（§5）。

T9a 边界：不实现交付推进（T9b：目标支 ff/CAS/external/out_of_band/幻影
守卫）；不接 HTTP/MCP；不改 v2 gate.py 生产路径。集成复跑在 detached
worktree 内组树（build_integration＋merge_ff/rebase_onto 冲突弹回），不
推进真实目标支；verify 全绿后任务驻留 done 待 T9b 装配收尾。
"""
import dataclasses
import json
import os
import pathlib
import re
import shlex
import sys
import time
import typing

from agorad.contracts import ContractError
from agorad.domain import (Artifact, BaseProbe, Event, Task, epoch_ms,
                           reset_fail_streak, task_transition,
                           dispatch_secret_key)

_TAIL = 2000            # 通知面输出尾截断（v2 同口径）
_RAW_TAIL = 400         # escalate raw 头截 500 预留量（v2 同口径）
_CEILING_FACTOR = 4     # F36 硬顶＝4×生效软顶（任务/画像级恒倍率）
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # F123 export 键
_TID_RE = re.compile(r"^[A-Za-z0-9_-]+$")               # 产物路径逃逸守卫
FAILPOINTS = ("probe", "verify", "review", "integration", "settle")


def cmd_dead(rc, out=""):
    """F81/F82 命令死判定单点：rc∈{126,127} 或 rc==2 且输出含 ENOENT 文案。"""
    return rc in (126, 127) or (rc == 2 and "No such file or directory" in out)


def classify_failure(rc, out="", error=None):  # Req: F122
    """机械失败分型纯单源：runner 异常→runner_exception；0→green；124→
    overrun；命令死→command_dead；rc<0 或 129..165→signal；其余→content_failed。"""
    if error is not None:
        return "runner_exception"
    if rc == 0:
        return "green"
    if rc == 124:
        return "overrun"
    if cmd_dead(rc, out):
        return "command_dead"
    if rc < 0 or 129 <= rc <= 165:
        return "signal"
    return "content_failed"


def with_shell_exports(cmd, env):  # Req: F123
    """export 前缀注入（`export K=V ...; <cmd>`——对整条 shell 链生效）；
    禁 `VAR=x cmd` 前缀赋值（只作用首条命令）。键值安全校验＋shlex.quote。"""
    if not env:
        return cmd
    parts = []
    for key, value in env.items():
        value = str(value)
        if not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"export 注入变量名非法: {key!r}")
        if not value or re.search(r"[\s\n\r;`$\"'\\]", value):
            raise ValueError(f"export 注入值非法: {key}")
        parts.append(f"{key}={shlex.quote(value)}")
    return f"export {' '.join(parts)}; {cmd}"


@dataclasses.dataclass(frozen=True)
class DoneReject:
    """F13 结构化拒因：code＝六类（state/verify-unregistered/verify-empty/
    body-format/children/anchor）＋vacuous-done（锚族计连败口径）。"""

    code: str
    message: str

    def __str__(self):
        return f"rejected:{self.code}: {self.message}"


class TaskRepo(typing.Protocol):
    """storage 公开仓库接口（结构化满足；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def get_task(self, tid): ...
    def tasks_all(self): ...
    def upsert_task(self, t: Task): ...
    def append_event(self, ev: Event): ...
    def insert_artifact(self, a: Artifact): ...
    def delete_credential(self, sid) -> bool: ...


class Gitx(typing.Protocol):
    """gitx 注入面（access.gitx 同形子集；调用必在 UoW 外）。"""

    def contains(self, repo_dir, commit, branch=None) -> bool: ...
    def is_ancestor(self, repo_dir, base, commit) -> bool: ...
    def head(self, repo_dir, branch=None): ...
    def symref(self, repo_dir): ...
    def build_integration(self, target_dir, integration_dir, branch=None): ...
    def build_integration_at(self, target_dir, integration_dir, base_sha=None): ...
    def merge_ff(self, env_dir, target, branch=None) -> str: ...
    def rebase_onto(self, env_dir, integration, branch=None) -> str: ...
    def materialize_checkout(self, tree_dir, source_dir) -> str: ...
    def remove_integration(self, integration_dir, target_dir): ...
    def remove_env(self, env_dir): ...


class Runner(typing.Protocol):
    """命令执行注入面：返回 (rc, combined_output)；软硬顶经 kwargs 观察。
    v2 core.gate._subprocess_runner 同形（timeout/ceiling/on_soft）。"""

    def __call__(self, env_dir, cmd, *, timeout=None, ceiling=None,
                 on_soft=None): ...


class AnchorReader(typing.Protocol):
    """评审锚读取（access.anchors.read_anchor 同形）：双件齐全且 HEAD 一致
    返回 {"head","base","clean"}，否则 None。"""

    def __call__(self, env_dir): ...


class ArtifactSink(typing.Protocol):
    """F122 执行产物落盘面（DiskArtifactSink 结构化满足）。"""

    def record(self, tid, phase, env_dir, cmd, rc, output, started_ms,
               ended_ms, *, error=None, extra=None,
               failure_kind=None) -> dict: ...


class Notifier(typing.Protocol):
    """messaging.MessagingService 结构化满足（record＋dispatch＋post）。"""

    def record(self, *, ts_ms, from_sid, to_sid, task_id, kind, body,
               dedup_key=None): ...
    def dispatch(self, mid, *, escalate_kind=None) -> bool: ...
    def post(self, from_sid, to_sid, task_id, kind, body, *,
             dedup_key=None, escalate_kind=None): ...


class StreakKeeper(typing.Protocol):
    """tasksvc.TaskService 结构化满足（连败自动 blocked 语义单源复用）。"""

    def bump_fail_streak(self, tid, *, actor="gate"): ...


class DiskArtifactSink:
    """F122 默认产物存储：$root/<tid>/<ts>-<phase>-<seq>/ 内 output.log＋
    meta.json；目录 0700/文件 0600 原子写；追加式保留（只增不剪——审计产物
    面；seq 单调防同秒同 phase 撞名覆写，重启扫描续序）。"""

    def __init__(self, root):
        self._root = pathlib.Path(root)
        self._seq = self._max_seq()

    def _max_seq(self):
        mx = 0
        try:
            for tdir in self._root.iterdir():
                if tdir.is_dir():
                    for d in tdir.iterdir():
                        s = d.name.rsplit("-", 1)[-1]
                        if d.is_dir() and s.isdigit():
                            mx = max(mx, int(s))
        except OSError:
            pass
        return mx

    def record(self, tid, phase, env_dir, cmd, rc, output, started_ms,
               ended_ms, *, error=None, extra=None, failure_kind=None):
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            raise ValueError(f"tid 只允许 [A-Za-z0-9_-]+（收到 {tid!r}）")
        text = output if isinstance(output, str) else ""
        data = text.encode("utf-8", errors="replace")
        try:
            duration = round((ended_ms - started_ms) / 1000, 3)
        except TypeError:
            duration = None
        self._seq += 1
        d = self._root / tid / (
            time.strftime("%Y%m%dT%H%M%S",
                          time.localtime(ended_ms / 1000))
            + f"-{phase}-{self._seq:04d}")
        d.mkdir(parents=True, exist_ok=True)
        for p in (self._root, self._root / tid, d):
            os.chmod(p, 0o700)
        self._atomic(d / "output.log", data)
        meta = {"tid": tid, "phase": phase, "cmd": cmd, "cwd": env_dir,
                "rc": rc, "started_at_ms": started_ms,
                "ended_at_ms": ended_ms, "duration_s": duration,
                "bytes": len(data), "failure_kind": failure_kind,
                "error": str(error) if error is not None else None,
                "extra": extra, "path": str(d),
                "log": str(d / "output.log")}
        self._atomic(d / "meta.json",
                     json.dumps(meta, ensure_ascii=False, indent=1).encode())
        return meta

    @staticmethod
    def _atomic(path, data):
        tmp = path.parent / (path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), 0o600)
        os.replace(tmp, path)


def _hex40(v):
    c = str(v or "").lower()
    return c if len(c) == 40 and all(ch in "0123456789abcdef" for ch in c) else None


class GateVerifyService:
    """verify/review 门执行体（T9a 上篇）；transport 单写者装配假设。"""

    def __init__(self, store: TaskRepo, gitx: Gitx, runner: Runner, *,
                 messaging: Notifier | None = None,
                 tasksvc: StreakKeeper | None = None,
                 anchors: AnchorReader | None = None,
                 artifacts: ArtifactSink | None = None,
                 home="", clock=None, injector=None,
                 verify_timeout=300, verify_ceiling=None,
                 verify_profiles=None, timers=None):
        self._store, self._gitx, self._runner = store, gitx, runner
        self._msg, self._tasksvc = messaging, tasksvc
        self._timers = timers  # Req: F145 门体终态路径自动取消 pending timer
        self._anchors = anchors or (lambda env_dir: None)
        self._artifacts = artifacts
        self._home = str(home)
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._injector = injector
        if injector is not None:
            for name in FAILPOINTS:
                injector.register(name)
        self._verify_timeout = verify_timeout
        self._verify_ceiling = verify_ceiling
        self._verify_profiles = verify_profiles or {}
        self._lane = {}  # Req: F144 F116 内存 lane 投影（tid→dict，v2 同款不落盘）

    def _autocancel_timers(self, tid, state, now):
        """Req: F145 任务终态自动取消 pending timer（身处调用方 UoW；
        timersvc.cancel_task_timers_uow 同款幂等——无 pending 零写入）。"""
        if (self._timers is not None
                and state in ("merged", "closed", "cancelled")):
            self._timers.cancel_task_timers_uow(tid, now, f"task_{state}")

    def _fire(self, name, phase, **context):
        if self._injector is not None:
            self._injector.fire(name, phase, **context)

    # ---- done 受理（前置六拒；锚族计连败） ----

    def accept_done(self, tid, worker_sid, commit):
        t = self._store.get_task(tid)
        if t is None:
            raise ContractError("not_found", f"{tid}: 任务不存在", {"task": tid})
        if t.state != "running":  # 含二次 done：防重入门
            return self._deny(tid, "state", f"任务 {tid} 当前 {t.state} 非 "
                            "running——board 查看状态，仅 running 受理 done")
        if t.verify.state != "registered":
            return self._deny(tid, "verify-unregistered",
                            f"任务 {tid} verify 未登记——post 通知派发方 "
                            "task_update 登记 verify 命令，登记后重新 "
                            "post(done, commit)")
        if not t.verify.cmd.strip():
            return self._deny(tid, "verify-empty", f"任务 {tid} verify 命令为"
                            " 空——task_update 登记非空命令后重新 post(done)")
        c = _hex40(commit)
        if c is None:
            return self._deny(tid, "body-format",
                            "done body 须为纯 40 位十六进制完整 commit sha"
                            f"（收到 {str(commit)[:20]!r}）——无前缀无附言；"
                            "结论文本另发 post(kind=note)")
        open_children = self._open_children(worker_sid)
        if open_children:
            return self._deny(tid, "children", "嵌套子任务未结案："
                            f"{', '.join(sorted(open_children))}——等待 result "
                            "回传，或由你取消子任务；成功/有意跳过后再 post(done)")
        # F114 门审任务跳过锚申报面（合并权威＝门亲自跑 review_cmd）
        a = None if (t.review_cmd or "").strip() else self._anchors(t.env_dir)
        if a is None and not (t.review_cmd or "").strip():
            return self._deny_anchor(tid, "anchor", f"任务 {tid} 合并前置失败："
                                     "锚缺失/落后（.review-loop/.last-reviewed "
                                     "缺失或不等于分支 HEAD，或 .review-loop/"
                                     ".review-base 链根"
                                     "缺失——F111 双件齐全才算在锚）——跑评审环"
                                     "重建锚（==HEAD 且评审环绿）后重新 "
                                     "post(done, commit)")
        if a is not None:
            if a.get("head") != c:
                return self._deny_anchor(tid, "anchor", f"任务 {tid} 合并前置"
                                         f"失败：{c} ≠ 分支 HEAD {a.get('head')}"
                                         "——以 env 内完整 HEAD sha 重新 "
                                         "post(done, commit)")
            if t.env_base and a.get("base") != t.env_base:  # Req: F111
                return self._deny_anchor(tid, "anchor", f"任务 {tid} 合并前置"
                                         f"失败：评审链根 {a.get('base')} ≠ 任务"
                                         f"起点 {t.env_base}——评审范围未覆盖"
                                         "任务全量交付，以任务起点为链根重跑"
                                         "评审环后重新 post(done, commit)")
        try:  # F69 vacuous 探针（谓词病带痕放行——拒收须正据）
            vacuous = self._gitx.contains(t.project_dir, c, t.target_branch)
        except RuntimeError:
            self._audit(tid, "zero_commit_probe_failed")
            vacuous = False
        if vacuous and not self._vacuous_delivered(t, c):
            return self._deny_anchor(tid, "vacuous-done", f"任务 {tid} 零提交"
                                     " done 拒收：本任务相对目标分支无新增提交"
                                     "——若无需改动或活已被上游完成，"
                                     "post(kind=question, body=no-op: 结论与证据) "
                                     "申报——需派发方处置；勿空回填 done")
        if a is not None and not a.get("clean"):  # F114 门审任务全免
            return self._deny_anchor(tid, "anchor", f"任务 {tid} 合并前置失败："
                                     "评审环未绿——修复 blocker/high 后重新 "
                                     "post(done, commit)")
        now, raced = self._now(), None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh.state != "running":  # 受理窗竞速（并发 cancel 等）：
                # 结构化 state 拒，不让裸 InvalidTransition 逃到 transport（B册 F-1）
                raced = f"任务 {tid} 当前 {fresh.state} 非 running——仅 running 受理 done"
            else:
                task = task_transition(fresh, "done", now_ms=now)
                task = dataclasses.replace(task, commit=c, rebased_from=None)
                self._store.upsert_task(task)
                self._store.append_event(self._ev(now, worker_sid, "done", tid,
                                                  {"commit": c}))
        if raced is not None:
            return self._deny(tid, "state", raced)
        return "accepted"

    def _open_children(self, worker_sid):
        return [x.id for x in self._store.tasks_all()
                if x.dispatcher_sid == worker_sid
                and (x.state not in ("merged", "closed", "cancelled")
                     or str(x.cancel_reason or "").startswith(
                         "dependency_failed"))]

    def _vacuous_delivered(self, t, commit):  # ag-1ae0f1 空集放行判据
        prev = t.commit
        if prev == commit:
            return True  # 主探针已证本 commit 目标支可达
        try:
            if (t.env_base and commit != t.env_base
                    and self._gitx.is_ancestor(t.project_dir, t.env_base,
                                               commit)):
                return True  # env HEAD 相对起点有前进（hotfix 形态）
            if prev and self._gitx.contains(t.project_dir, prev,
                                            t.target_branch):
                return True  # 前次受理留痕已在目标支（reopen 期间人工合入）
        except RuntimeError:
            self._audit(t.id, "zero_commit_probe_failed")
        return False

    def zero_commit(self, tid):  # Req: F174
        """no-op 申报零提交判定：done 零提交锚同口径（F69 contains 探针＋
        ag-1ae0f1 空集放行判据）以 env 当前 HEAD 为判据；谓词病＝False
        （判据不可确立→照常规投递人裁，带痕同 accept_done 口径）。v2
        core/gate.py 同构。"""
        t = self._store.get_task(tid)
        if not t.env_dir:
            return False
        try:
            head = self._gitx.head(t.env_dir)
            vacuous = self._gitx.contains(t.project_dir, head,
                                          t.target_branch)
        except RuntimeError:
            self._audit(tid, "zero_commit_probe_failed")
            return False
        return vacuous and not self._vacuous_delivered(t, head)

    def _deny(self, tid, code, message):
        """ag-d03e0e 拒因落消息面：受理段拒因原文持久化为任务消息（from=gate、
        kind=note→digest 缓冲免即时注入），check_mail/board 可查；留痕面尽力
        而为（F54）：get_task 并入 try——存储读败视同无收件人，不得把结构化
        拒回（400＋拒因）吹成 500。"""
        self._audit(tid, "done_rejected", note=code)
        try:
            t = self._store.get_task(tid)
            if self._msg is not None and t is not None and t.worker_sid:
                self._msg.post("gate", t.worker_sid, tid, "note",
                               f"rejected:{code}: {message}")
        except Exception:
            pass
        return DoneReject(code, message)

    def _deny_anchor(self, tid, code, message):
        """锚族拒收计连败（ag-084a22 A/C：受理锚拒与门体质量败同口径）。"""
        if self._tasksvc is not None:
            r = self._tasksvc.bump_fail_streak(tid)  # ≥3 自动 blocked＋消息
            if r.get("state") == "blocked":
                self._escalate(tid, "quality", f"任务 {tid} 受理连败 blocked")
        return self._deny(tid, code, message)

    # ---- 门体（上篇：探针→env verify→门审/锚复验→集成复跑） ----

    def run(self, tid) -> str:
        """门入口（Req: F144 起＝F112/F116 车道面包装——wait/acquire 事件＋
        内存 lane 投影，v2 run_guarded 同位：排队/获锁先于一切门体判定，
        非 done/外部驻留 skip 路径同占道释放）。"""
        t = self._store.get_task(tid)
        if t is None:
            return "skip"  # v2 run_guarded 于行缺失处先抛（无车道面可占）
        res = self._lane_acquire(tid, t)
        try:
            return self._run(tid, t)
        finally:
            self._lane_release(tid, res)

    def _run(self, tid, t) -> str:
        if t.state != "done":
            return "skip"  # F36 R2：非 done（驻留取消直结/重启残留）不跑门
        env, target = t.env_dir, t.project_dir
        integration = t.gate.gate_env or f"{env}__gate"
        try:
            now = self._now()
            with self._store.uow():  # 门闩＋gate_env＋事件同事务
                fresh = self._store.get_task(tid)
                gate = dataclasses.replace(fresh.gate, in_gate=True,
                                           gate_env=integration)
                self._store.upsert_task(dataclasses.replace(
                    fresh, gate=gate, updated_ms=now))
                self._store.append_event(self._ev(
                    now, "gate", "gate_enter", tid,
                    {"gate_env": integration}))
            # Req: F144 门启动拍双通道通知（v2 run() 同位同序）：milestone→
            # worker＋result→dispatcher——通知覆盖矩阵 (done, 双通道) 面
            self._gate_enter_notify(tid, t)
            d = t.delivery
            if (d is not None and d.mode == "external_branch"
                    and d.status in ("awaiting_external", "delivered",
                                     "failed")):
                return "skip"  # F101 外部交付驻留/终局＝确认面权威，不重验
            budget, ceiling = self._budget(t)
            soft = self._soft_notify(tid, budget)
            runner = lambda e, cmd: self._runner(  # noqa: E731
                e, cmd, timeout=budget, ceiling=ceiling, on_soft=soft)
            # T9b 交付执行体钩：门闩后、崩溃残留探针前（out_of_band 豁免面＋
            # 交付终局崩窗收敛）；基类无交付装配＝直通 None。
            outcome = self._pre_pipeline(tid, t, runner)
            if outcome is None:
                outcome = self._landed_probe(tid, t)
            if outcome is None:
                outcome = (self._base_probe(tid, t, target, integration, runner)
                           or self._env_verify(tid, env, t.verify.cmd, runner)
                           or self._review_or_anchor(tid, t, env, runner)
                           or self._integration(tid, t, env, target, integration,
                                                runner))
            if outcome is None:
                self._audit(tid, "verify_green")
                outcome = self._after_green(tid, t, runner)
            return outcome
        except Exception as e:  # 门内崩：reopened 弹回（不计连败——非质量败）
            fresh = self._store.get_task(tid)
            if fresh is not None and fresh.state == "done":
                now = self._now()
                with self._store.uow():
                    x = self._store.get_task(tid)
                    task = task_transition(x, "reopened", now_ms=now)
                    self._store.upsert_task(task)
                    self._store.append_event(self._ev(
                        now, "gate", "reopened", tid,
                        {"note": f"门内异常: {str(e)[:120]}"}))
                self._worker_note(tid, f"门内异常：{e}——处理目标分支后重新 "
                                      "post(done, commit)")
                return "reopened"
            self._audit(tid, "reclaim_failed", note=str(e)[:200])
            return "crashed"
        finally:
            try:
                self._gitx.remove_integration(integration, target)
            except Exception as e:
                self._audit(tid, "reclaim_failed",
                            note=f"integration: {str(e)[:150]}")
            self._tail(tid)

    def _pre_pipeline(self, tid, t, runner):  # T9b 交付执行体钩
        """门体扩展点（T9b GateDeliveryService 覆写）：门闩后、崩溃残留探针
        前——out_of_band 豁免面与交付终局崩窗收敛。基类＝直通 None。"""
        return None

    # ---- Req: F144 F112/F116 门车道面（wait/acquire/release 事件＋内存投影） ----

    _LANE_LIMIT = 1  # 单门线程＝同仓/跨仓全串行（F112 同仓串行的强化超集；
    # 车道事件与 v2 逐键一致：repo/limit[/slot/duration]）

    def _lane_acquire(self, tid, t):
        """v2 run_guarded._acquire_resources 镜像（单线程即取即得）：排队前
        落 waiting 投影＋wait 事件，获锁翻转＋acquire 事件；投影＝内存态
        （v2 set_gate_resource 同款不落盘，重启 done 残留重排重建）。"""
        repo = t.project_dir
        if not repo:
            return None
        started = time.monotonic()
        now = self._now()
        with self._store.uow():
            self._store.append_event(self._ev(
                now, "gate", "gate_resource_wait", tid,
                {"repo": repo, "limit": self._LANE_LIMIT}))
        self._lane[tid] = {"waiting": True, "resource": repo,
                           "limit": self._LANE_LIMIT}  # 事务成后置（C3 同款
        # 防悬挂：uow 提交败不留内存 waiting 残影）
        waited = time.monotonic() - started
        now = self._now()
        acquired_at = time.time()
        try:
            with self._store.uow():
                self._store.append_event(self._ev(
                    now, "gate", "gate_resource_acquire", tid,
                    {"repo": repo, "limit": self._LANE_LIMIT, "slot": 0,
                     "duration": round(waited, 3)}))
                self._lane[tid] = {"waiting": False, "resource": repo,
                                   "limit": self._LANE_LIMIT, "slot": 0,
                                   "acquired_at": acquired_at,
                                   "waited": round(waited, 3)}  # v2 同键
        except BaseException:
            self._lane.pop(tid, None)  # 评审环 C3：wait/acquire 间失败清投影
            raise                      # 防 waiting=True 悬挂至重启
        return {"repo": repo, "t0": time.monotonic(),
                "acquired_at": acquired_at}

    def _lane_release(self, tid, res):
        """v2 _release_resources 镜像：finally 面全释放＋release 事件＋投影
        released_at 翻转（进程死亡不释放——崩溃窗与 v2 flock 随进程消亡同口径，
        重启重排后重取重投影）。评审环 B-F2：尽力而为——release 面异常不
        置换 run() 原发结果（_gate_loop 崩溃归因不失真）。"""
        if res is None:
            return
        held = time.monotonic() - res["t0"]
        now = self._now()
        try:
            with self._store.uow():
                self._store.append_event(self._ev(
                    now, "gate", "gate_resource_release", tid,
                    {"repo": res["repo"], "limit": self._LANE_LIMIT,
                     "duration": round(held, 3)}))
                self._lane[tid] = {"waiting": False, "resource": res["repo"],
                                   "limit": self._LANE_LIMIT, "slot": 0,
                                   "acquired_at": res["acquired_at"],
                                   "released_at": time.time()}
        except Exception as e:
            self._lane.pop(tid, None)  # 投影不悬挂（C3 同款）
            print(f"agorad-v3 gate lane release failed on {tid}: {e}",
                  file=sys.stderr, flush=True)

    def lane_projection(self, tid):
        """内存 lane 投影只读面（board 明细/full_view 注入——v2 行内投影）。"""
        return self._lane.get(tid)

    def _gate_enter_notify(self, tid, t):
        """v2 门启动拍镜像：milestone→worker＋result→dispatcher。两 record
        同一 UoW＝同生共死（评审环 C4 注释修正：并非独立互压）；整体
        尽力而为（F54）——败不断门。"""
        if self._msg is None:
            return
        msgs = []
        try:
            with self._store.uow():
                if t.worker_sid:
                    m, _ = self._msg.record(
                        ts_ms=self._now(), from_sid=t.dispatcher_sid,
                        to_sid=t.worker_sid, task_id=tid, kind="milestone",
                        body="已入验证门：verify 运行中——绿/败另有消息")
                    msgs.append(m)
                if t.dispatcher_sid:
                    m, _ = self._msg.record(
                        ts_ms=self._now(), from_sid="agorad",
                        to_sid=t.dispatcher_sid, task_id=tid, kind="result",
                        body=f"任务 {tid} 已入验证门（verify 运行中）")
                    msgs.append(m)
        except Exception:
            pass
        for m in msgs:
            try:
                self._msg.dispatch(m.id)
            except Exception:
                pass

    def _landed_probe(self, tid, t):  # T9b 交付执行体钩
        """崩溃残留探针（基类口径＝legacy 无投影行）：done 落账 commit 已在
        目标支＝上轮推进已完成，_land 补终态不重验（恰一次）；探针病照走
        验证路径。T9b 覆写＝交付协议行收口拆分（managed 补投影／external 直通）。"""
        if not (t.commit and t.target_branch is not None):
            return None
        try:
            landed = self._gitx.contains(t.project_dir, t.commit,
                                         t.target_branch)
        except RuntimeError:
            return None
        return self._land(tid) if landed else None


    def _after_green(self, tid, t, runner):  # T9b 交付执行体钩
        """verify/review 全绿出口（T9b 覆写＝交付推进）。基类＝驻留 done
        报装配待续（无交付装配的 legacy/无投影行）。"""
        budget, _ = self._budget(t)
        self._result(tid, f"任务 {tid} verify/review 全绿（预算 "
                        f"{budget:g}s）——交付推进待 T9b 装配")
        return "green"

    def _budget(self, t):
        """F51/F126 生效预算：task > profile > global；任务/画像级硬顶恒
        4×，服务级旋钮仅作用于 default 源。预算按任务行现值解析——入门前
        排队等待不折预算（F116 口径）。"""
        if t.verify.timeout_s is not None:
            budget, source = t.verify.timeout_s, "task"
        else:
            prof = self._verify_profiles.get(t.project_dir)
            if prof is not None:
                budget, source = prof["timeout"], "profile"
            else:
                budget, source = self._verify_timeout, "default"
        ceiling = (self._verify_ceiling if source == "default"
                   and self._verify_ceiling is not None
                   else _CEILING_FACTOR * budget)
        return budget, ceiling

    def _soft_notify(self, tid, budget):
        """F36 软顶提醒：到点不杀、提醒恰一次（本门运行内 once-guard）。"""
        fired = []

        def _on():
            if fired:
                return
            fired.append(True)
            self._audit(tid, "verify_soft_timeout", timeout_s=budget)
            self._post_kind(tid, "note", f"任务 {tid} verify 超软顶"
                                         f"（>{budget:g}s）——仍在跑、无需操作、"
                                         "确认挂死才 cancel")
        return _on

    def _base_probe(self, tid, t, target, integration, runner):  # Req: F107
        base = t.env_base
        if t.verify_base_exempt or t.quick or not base:
            return None  # 豁免/quick/env_base 缺席（fail-open）＝放行
        probe = t.probe or BaseProbe()
        if probe.cmd == t.verify.cmd and probe.base == base:
            if probe.result == "red":
                return None  # 缓存命中：reopen/崩溃重排不重跑
            self._probe_green(tid)
            return "reopened"
        self._phase(tid, "base_probe")
        try:
            self._gitx.build_integration_at(target, integration, base_sha=base)
            self._gitx.materialize_checkout(integration, target)
        except RuntimeError as e:  # base 对象缺失＝机械面不可用：驻留路由
            self._audit(tid, "base_probe_failed", note=str(e)[:200])
            self._result(tid, f"任务 {tid} base-red 探针无法运行：{e}——核对 "
                          "env_base 对象在位后重排，或换验收命令／豁免"
                          "（非质量败，无需 worker 重试）")
            return "probe_failed"
        rc, out, art = self._exec(tid, "base_probe", "probe", integration,
                                  t.verify.cmd, runner)
        if rc == 124:
            self._audit(tid, "base_probe_overrun")
            self._result(tid, f"任务 {tid} base-red 探针超硬顶——进程组已终止"
                          "（非质量败，无需 worker 重试）"
                          f"{self._tail_block(out)}{self._art_line(art)}")
            return "overrun"
        red = rc != 0  # base 阶段命令死（126/127）＝判别红（ag-d68faf）
        now = self._now()
        with self._store.uow():
            fresh = self._store.get_task(tid)
            self._store.upsert_task(dataclasses.replace(
                fresh, probe=BaseProbe(result="red" if red else "green",
                                       cmd=t.verify.cmd, base=base,
                                       ran_at_ms=now), updated_ms=now))
        self._audit(tid, "base_probe_red" if red else "base_probe_green", rc=rc)
        if not red:
            self._probe_green(tid)
            return "reopened"
        return None

    def _probe_green(self, tid):  # 裁决②：无判别力硬拒弹回（worker 无豁免权）
        self._quality_fail(
            tid, "reopened",
            "base-red 探针拒收：verify 命令在任务起点 base 上即绿（无判别力，"
            "F107）——换能区分任务前后的验收命令，或由派发方豁免"
            "（task_create verify_base_exempt=true／quick）；worker 自己无豁免"
            "权——post(kind=question) 请派发方处置后重新 post(done, commit)",
            reason="base-red 探针：验收命令在 base 上绿")

    def _env_verify(self, tid, env, cmd, runner):
        self._phase(tid, "verify")
        rc, out, art = self._exec(tid, "verify", "verify", env, cmd, runner)
        return self._route_verify(tid, rc, out, art, "")

    def _route_verify(self, tid, rc, out, art, where):
        """verify 阶段分类路由（质量红/命令死/signal/超顶）；where＝文案前缀。"""
        if rc == 124:  # F36 超硬顶：驻留 done、不计连败、不弹 worker
            self._audit(tid, "verify_overrun")
            self._result(tid, f"{where}verify 超硬顶——进程组已终止（非质量败，"
                          "无需 worker 重试）；输出尾部:"
                          f"{self._tail_block(out)}{self._art_line(art)}")
            return "overrun"
        if cmd_dead(rc, out):  # F81/F82 命令自身死：驻留＋升级派发方
            self._audit(tid, "verify_dead", rc=rc)
            self._escalate(tid, "verify-dead",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._worker_note(tid, f"{where}verify 命令自身死（exit {rc}：命令"
                                "不存在/不可执行）——非活不达标，无需自愈重试；"
                                "已升级派发方处置，等待通知即可")
            return "dead"
        if classify_failure(rc, out) == "signal":  # Req: F122
            self._audit(tid, "verify_infra_failed", rc=rc)
            self._escalate(tid, "verify-infra",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._result(tid, f"任务 {tid} {where}verify 机械失败（exit {rc}："
                          "signal 类——SIGKILL/SIGSEGV/SIGABRT，疑似 OOM/"
                          f"基础设施）——非质量败：任务驻留 done 待处置"
                          f"{self._art_line(art)}")
            self._worker_note(tid, f"{where}verify 机械失败（exit {rc}：signal "
                                "类，疑似基础设施/OOM）——非质量败，无需自愈"
                                "重试；已升级派发方处置，等待通知即可")
            return "infra"
        if rc != 0:
            self._quality_fail(
                tid, "verify_failed" if not where else "reopened",
                f"{where}verify 失败——修复后重新 post(done, commit)"
                f"\n输出尾部:{self._tail_block(out)}{self._art_line(art)}",
                reason=f"{where}verify 失败" if where else "verify 失败")
            return "verify_failed" if not where else "reopened"
        return None

    def _review_or_anchor(self, tid, t, env, runner):
        if (t.review_cmd or "").strip():
            return self._gate_review(tid, t, env, runner)
        a = self._anchors(env)  # 合并前锚重验：堵 accept→run 窗口 TOCTOU
        if (not a or a.get("head") != (t.rebased_from or t.commit)
                or not a.get("clean")
                or (t.env_base and a.get("base") != t.env_base)):
            self._audit(tid, "review_absent_no_merge")
            self._quality_fail(
                tid, "reopened",
                "合并前置失败：verify 绿但合并时 .review-loop 锚缺失/落后/"
                "未绿/链根漂移（F111：.review-loop/.review-base ≠ 任务起点）"
                "——重建锚（.last-reviewed ==HEAD 且评审环绿且 "
                ".review-loop/.review-base == 任务"
                "起点）后重新 post(done, commit)",
                reason="锚缺失/落后/未绿/链根漂移")
            return "reopened"
        return None

    def _gate_review(self, tid, t, env, runner):  # Req: F114
        cmd, base = t.review_cmd, t.env_base
        if not base:  # 升级前在飞：评审范围不可划定，带痕放行
            self._audit(tid, "review_base_unavailable")
            return None
        head = self._gitx.head(env)
        cache = t.gate.review_cache or {}
        if cache.get("cmd") == cmd and cache.get("commit") == head \
                and cache.get("ok"):
            self._audit(tid, "gate_review_cached")
            return None
        self._phase(tid, "review")
        rc, out, art = self._exec(
            tid, "review", "review", env,
            with_shell_exports(cmd, {"AGORA_REVIEW_BASE": base}),
            runner, extra={"review_base": base})
        if rc == 124:
            self._audit(tid, "review_overrun")
            self._result(tid, f"任务 {tid} 门审超硬顶——进程组已终止（非质量"
                          f"败，无需 worker 重试）；输出尾部:"
                          f"{self._tail_block(out)}{self._art_line(art)}")
            return "overrun"
        if cmd_dead(rc, out):
            self._audit(tid, "review_dead", rc=rc)
            self._escalate(tid, "verify-dead",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._worker_note(tid, "门审命令自身死（exit "
                                f"{rc}：命令不存在/不可执行）——非活不达标，"
                                "无需自愈重试；已升级派发方处置，等待通知即可")
            return "dead"
        if classify_failure(rc, out) == "signal":
            self._audit(tid, "review_infra", rc=rc)
            self._escalate(tid, "verify-infra",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._worker_note(tid, "门审机械失败（exit "
                                f"{rc}：signal 类，疑似基础设施/OOM）——非质量"
                                "败，无需自愈重试；已升级派发方处置，"
                                "等待通知即可")
            return "infra"
        if rc != 0:
            return self._review_fail(
                tid, f"门审未通过——按评审发现修复后重新 post(done, commit)"
                     f"\n输出尾部:{self._tail_block(out)}{self._art_line(art)}")
        now = self._now()
        with self._store.uow():
            fresh = self._store.get_task(tid)
            gate = dataclasses.replace(
                fresh.gate, review_cache={"cmd": cmd, "commit": head,
                                          "ok": True, "ran_at_ms": now},
                review_fail_streak=0)  # 绿即复位（轮次态非任务态）
            self._store.upsert_task(dataclasses.replace(
                fresh, gate=gate, updated_ms=now))
        self._audit(tid, "gate_reviewed", commit=head)
        return None

    def _review_fail(self, tid, body):
        """评审红独立连败（与质量 fail_streak 解耦）：三轮分歧 blocked＋
        升级人工判读；未达阈值 reopened 弹回 worker。done→blocked 分歧
        终局＝自动边直写（domain 矩阵该边仅 F98 旗——bump_fail_streak 自动
        blocked 同款 bypass，事件带 review_disagreement 留痕）。"""
        now = self._now()
        msg = None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            n = fresh.gate.review_fail_streak + 1
            gate = dataclasses.replace(fresh.gate, review_fail_streak=n)
            if n >= 3:
                task = dataclasses.replace(
                    fresh, state="blocked", gate=gate, updated_ms=now)
                self._store.append_event(self._ev(
                    now, "gate", "blocked", tid,
                    {"note": "review_disagreement"}))
                if self._msg is not None and task.dispatcher_sid:
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=f"任务 {tid} 门审与 worker 三轮分歧"
                             "（review_disagreement）——已 blocked；人工判读："
                             "代码问题修代码，评审器误报调 review_cmd 或取消重派",
                        dedup_key=f"transition:{tid}:blocked:{now}")
            else:
                task = task_transition(fresh, "reopened", now_ms=now)
                task = dataclasses.replace(task, gate=gate)
                self._store.append_event(self._ev(
                    now, "gate", "review_failed", tid))
                if self._msg is not None and task.worker_sid:
                    msg, _ = self._msg.record(
                        ts_ms=now, from_sid="gate", to_sid=task.worker_sid,
                        task_id=tid, kind="process", body=body)
            self._store.upsert_task(task)
        if msg is not None:
            self._msg.dispatch(msg.id)
        if n >= 3:
            self._escalate(tid, "quality", f"任务 {tid} 门审三轮分歧 blocked")
            self._worker_note(tid, body)
            return "blocked"
        return "reopened"

    def _integration(self, tid, t, env, target, integration, runner):  # F123
        self._phase(tid, "integration")
        branch = t.target_branch
        if branch is None:  # 缺省支实名解析（ag-bf61e7）：分支 ref 为事实源
            branch = self._gitx.symref(target)
            if branch == "HEAD":
                raise RuntimeError("目标检出已脱离分支（detached HEAD），"
                                   "无法解析缺省目标支——恢复分支检出后重新 "
                                   "post(done, commit)")
        self._gitx.build_integration(target, integration, branch)
        before = self._gitx.head(integration)
        e = t.rebased_from or t.commit  # done sha＝交付权威
        r = None  # R＝重放后任务提交（None＝ff 直通）
        if self._gitx.merge_ff(env, integration) != "ok":
            if self._gitx.rebase_onto(env, integration) == "conflict":
                return self._conflict(tid, before)
            r = self._gitx.head(integration)
            if r != before:  # 先落库再验证（崩溃幂等）
                now = self._now()
                with self._store.uow():
                    fresh = self._store.get_task(tid)
                    self._store.upsert_task(dataclasses.replace(
                        fresh, commit=r, rebased_from=e, updated_ms=now))
                    self._store.append_event(self._ev(
                        now, "gate", "rebased", tid,
                        {"from": e, "to": r}))
        report = self._gitx.materialize_checkout(integration, target)
        if report:
            self._audit(tid, "checkout_materialize", note=report)
        rc, out, art = self._exec(
            tid, "integration_verify", "integration", integration,
            with_shell_exports(t.verify.cmd,
                               {"AGORA_INTEGRATION_BASE": before}),
            runner, extra={"integration_base": before})
        where = ("rebase 后集成 verify 失败" if r else "集成 verify 失败",
                 "rebase 后集成" if r else "集成")
        return self._route_int(tid, rc, out, art, where)

    def _route_int(self, tid, rc, out, art, where):
        """集成阶段 verify 分类路由（T9a 集成段与 T9b 交付循环共用单源）；
        where＝(质量文案, 驻留文案前缀)。目标支零改动口径由调用方组树面保证。"""
        if rc == 124:
            self._audit(tid, "verify_overrun")
            self._result(tid, f"{where[1]} verify 超硬顶，目标分支未改动——"
                          "进程组已终止（非质量败，无需 worker 重试）；"
                          f"输出尾部:{self._tail_block(out)}"
                          f"{self._art_line(art)}")
            return "overrun"
        if cmd_dead(rc, out):
            self._audit(tid, "verify_dead", rc=rc)
            self._escalate(tid, "verify-dead",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._worker_note(tid, f"{where[1]} verify 命令自身死（exit "
                                f"{rc}）——非活不达标，无需自愈重试；"
                                "已升级派发方处置，等待通知即可")
            return "dead"
        if classify_failure(rc, out) == "signal":
            self._audit(tid, "verify_infra_failed", rc=rc)
            self._escalate(tid, "verify-infra",
                           f"exit={rc} {out[-_RAW_TAIL:]}"
                           f"{self._art_line(art)}")
            self._worker_note(tid, f"{where[1]} verify 机械失败（exit "
                                f"{rc}：signal 类）——非质量败，无需自愈重试；"
                                "已升级派发方处置，等待通知即可")
            return "infra"
        if rc != 0:
            self._quality_fail(
                tid, "reopened",
                f"{where[0]}，目标分支未改动——修复后重新 post(done, commit)"
                f"\n输出尾部:{self._tail_block(out)}{self._art_line(art)}",
                reason=where[0])
            return "reopened"
        return None

    def _conflict(self, tid, before):
        """F32 同步冲突弹回：重开不计质量连败，目标主检出不动。"""
        now = self._now()
        msg = None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = task_transition(fresh, "reopened", now_ms=now)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(now, "gate", "reopened", tid))
            if self._msg is not None and task.worker_sid:
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid="gate", to_sid=task.worker_sid,
                    task_id=tid, kind="process",
                    body=f"同步冲突：{before}——解冲突后重新 post(done, commit)")
        if msg is not None:
            self._msg.dispatch(msg.id)
        return "reopened"

    def _land(self, tid):
        """崩溃残留收口（上篇唯一成功出口）：目标支已含交付即补 merged→
        closed（两跳同事务，tasksvc._deliver_ok 同款）＋撤牌回收；幻影守卫
        （落账 commit 不在目标支的假成功）归 T9b。Req: F144 closed 跳 ts=
        merged+1ms（同事务原子保持）——merged 窗通知可落 [merged, closed)。"""
        now, msgs = self._now(), []
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = task_transition(fresh, "merged", now_ms=now)
            task = reset_fail_streak(task, now_ms=now)
            task = task_transition(task, "closed", now_ms=now + 1)
            self._store.upsert_task(task)
            self._autocancel_timers(tid, "closed", now + 1)  # Req: F145
            self._store.append_event(self._ev(
                now, "gate", "merged", tid, {"commit": task.commit}))
            self._store.append_event(
                self._ev(now + 1, "gate", "closed", tid))
            if self._msg is not None and task.dispatcher_sid:
                for ts_ms, state, text in (
                        (now, "merged", f"@{(task.commit or '')[:7]} 崩溃残留收口"
                         "（权威已含交付）"),
                        (now + 1, "closed", f"任务 {tid} 已 closed；"
                         f"commit={task.commit or '-'}")):
                    msgs.append(self._msg.record(
                        ts_ms=ts_ms, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=text,
                        dedup_key=f"transition:{tid}:{state}:"
                                  f"{task.updated_ms}")[0])
        for m in msgs:
            self._msg.dispatch(m.id)
        self._revoke(tid, task)
        return "merged"

    def _tail(self, tid):
        """门尾原子结算（F-4/F36 R2）：排队取消经本点消费——pending 且非
        merged/closed/cancelled→cancelled（done 直结：矩阵无出边）、
        merged→closed（不逆转历史）、余者仅清门闩；迁移＋事件＋消息同 UoW。"""
        self._fire("settle", "before", tid=tid)
        now, msgs, task, cancelled = self._now(), [], None, False
        revoke = None  # Req: F152——撤牌前留牌文本（双键清理用）
        with self._store.uow():
            fresh = self._store.get_task(tid)
            if fresh is None:
                return
            pending = fresh.gate.pending_cancel
            reason = fresh.gate.pending_cancel_reason
            gate = dataclasses.replace(fresh.gate, pending_cancel=False,
                                       pending_cancel_reason=None,
                                       in_gate=False, gate_env=None)
            task = dataclasses.replace(fresh, gate=gate, updated_ms=now)
            if pending and task.state not in ("merged", "closed", "cancelled"):
                if task.state == "done":  # 驻留 done 直结（F36 R2 同款）
                    task = dataclasses.replace(task, state="cancelled")
                else:
                    task = task_transition(task, "cancelled", now_ms=now)
                if reason:
                    task = dataclasses.replace(task, cancel_reason=reason)
                cancelled = True
                self._autocancel_timers(tid, "cancelled", now)  # Req: F145
                self._store.append_event(self._ev(
                    now, "gate", "cancelled", tid,
                    {"reason": reason} if reason else None))
                if task.worker_sid:
                    revoke = self._store.get_credential(task.worker_sid)
                    self._store.delete_credential(task.worker_sid)
                if self._msg is not None and task.dispatcher_sid:
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=f"task {tid} cancelled at gate tail",
                        dedup_key=f"transition:{tid}:cancelled:{now}")[0])
            elif task.state == "merged":
                task = task_transition(task, "closed", now_ms=now)
                self._autocancel_timers(tid, "closed", now)  # Req: F145
                self._store.append_event(self._ev(now, "gate", "closed", tid))
                if self._msg is not None and task.dispatcher_sid:
                    msgs.append(self._msg.record(
                        ts_ms=now, from_sid="gate",
                        to_sid=task.dispatcher_sid, task_id=tid, kind="result",
                        body=f"任务 {tid} 已 closed；commit={task.commit or '-'}",
                        dedup_key=f"transition:{tid}:closed:{task.updated_ms}")[0])
            self._store.upsert_task(task)
        for m in msgs:
            self._msg.dispatch(m.id)
        if cancelled:  # 终止副作用与 tasksvc._settle_cancel 同款（断言9锚；
            # Req: F152 双键清理）
            self._remove_credential_file(
                tid, revoke.token if revoke is not None else None)
            self._reclaim_env(tid, task.env_dir, "gateverify:tail_cancel")  # Req: ag-0e6ca3
        self._fire("settle", "after", tid=tid)

    # ---- 执行面（runner＋artifact；恒在 UoW 外） ----

    def _exec(self, tid, phase, failpoint, env_dir, cmd, runner, extra=None):
        """runner 执行＋产物全量落盘（rc 无论多少；runner 异常也先落
        runner_exception 再原样上抛）；崩溃窗 failpoint 前后包执行。"""
        self._fire(failpoint, "before", tid=tid, phase_name=phase)
        started = self._now()
        try:
            rc, out = runner(env_dir, cmd)
            error = None
        except Exception as exc:
            rc, out, error = None, "", exc
        art = self._record_artifact(tid, phase, env_dir, cmd, rc, out,
                                    started, error=error, extra=extra)
        self._fire(failpoint, "after", tid=tid, phase_name=phase)
        if error is not None:
            raise error  # 不得伪装 content failed（run 门内异常面）
        return rc, out, art

    def _record_artifact(self, tid, phase, env_dir, cmd, rc, out, started_ms,
                         *, error=None, extra=None):
        """落盘（UoW 外）＋DB 行＋任务行 gate_artifact 投影＋事件（同 UoW）；
        best-effort 三段隔离：败不断门（投影缺席＝artifact unavailable 语义）。"""
        if self._artifacts is None:
            return None
        text = out if isinstance(out, str) else ""
        kind = classify_failure(rc, text, error)
        try:
            meta = self._artifacts.record(
                tid, phase, env_dir, cmd, rc, text, started_ms, self._now(),
                error=str(error) if error is not None else None, extra=extra,
                failure_kind=kind)
        except Exception:
            self._audit(tid, "gate_artifact_write_failed", phase=phase)
            return None
        try:
            now = self._now()
            with self._store.uow():
                fresh = self._store.get_task(tid)
                art = Artifact(task_id=tid, phase=meta["phase"],
                               path=meta["path"], cmd=cmd, rc=meta["rc"],
                               failure_kind=meta["failure_kind"],
                               duration_s=meta["duration_s"],
                               bytes=meta["bytes"], error=meta["error"],
                               started_at_ms=started_ms, ended_at_ms=now)
                self._store.insert_artifact(art)
                self._store.upsert_task(dataclasses.replace(
                    fresh, gate=dataclasses.replace(
                        fresh.gate, gate_artifact=art), updated_ms=now))
                self._store.append_event(self._ev(
                    now, "gate", "gate_artifact_written", tid,
                    {"phase": art.phase, "rc": art.rc,
                     "failure_kind": art.failure_kind, "bytes": art.bytes,
                     "path": art.path}))
        except Exception:
            pass
        return meta

    # ---- 质量败/通知/审计公共面 ----

    def _quality_fail(self, tid, to, body, reason=""):
        """质量败：迁移＋审计＋worker 弹回（同 UoW）→ 连败＋1（≥3 自动
        blocked）；通知面尽力而为（F54）——败不得断连败计数或改写门结果。"""
        now = self._now()
        msg = None
        with self._store.uow():
            fresh = self._store.get_task(tid)
            task = task_transition(fresh, to, now_ms=now)
            self._store.upsert_task(task)
            self._store.append_event(self._ev(
                now, "gate", to, tid, {"reason": reason} if reason else None))
            if self._msg is not None and task.worker_sid:
                msg, _ = self._msg.record(
                    ts_ms=now, from_sid="gate", to_sid=task.worker_sid,
                    task_id=tid, kind="process", body=body)
        if msg is not None:
            self._msg.dispatch(msg.id)
        if self._tasksvc is not None:
            r = self._tasksvc.bump_fail_streak(tid)
            if r.get("state") == "blocked":
                self._escalate(tid, "quality", f"任务 {tid} 质量三连败 blocked")

    def _result(self, tid, body):
        """dispatcher 结果单行（kind=result 唤醒通道）；尽力而为。"""
        self._post_kind(tid, "result", body)

    def _worker_note(self, tid, body):
        """worker 等待说明（kind=process 唤醒）；尽力而为。"""
        self._post_kind(tid, "process", body)

    def _post_kind(self, tid, kind, body):
        t = self._store.get_task(tid)
        if self._msg is None or t is None or not t.dispatcher_sid:
            return
        to = t.worker_sid if kind == "process" else t.dispatcher_sid
        if not to:
            return
        try:
            self._msg.post("gate", to, tid, kind, body)
        except Exception:
            pass

    def _escalate(self, tid, kind, raw=""):
        """quiet escalate（verify-dead/verify-infra）与 quality 升级：
        notify 通道唤醒派发方；尽力而为（F54 口径）。"""
        t = self._store.get_task(tid)
        if self._msg is None or t is None or not t.dispatcher_sid:
            return
        try:
            self._msg.post("gate", t.dispatcher_sid, tid, "escalate",
                           f"任务 {tid} escalate[{kind}] {raw}"[:500],
                           escalate_kind=kind)
        except Exception:
            pass

    def _revoke(self, tid, task):
        """终局撤牌（DB 行＋secret 文件）＋env 回收（败留痕不断终局）。
        Req: F152——双键清理（本尊＋per-dispatch 既录键位），与
        tasksvc._settle_cancel 同款。"""
        revoke = None
        if task.worker_sid:
            with self._store.uow():
                revoke = self._store.get_credential(task.worker_sid)
                self._store.delete_credential(task.worker_sid)
        self._remove_credential_file(
            tid, revoke.token if revoke is not None else None)
        self._reclaim_env(tid, task.env_dir, "gateverify:revoke")  # Req: ag-0e6ca3

    def _reclaim_env(self, tid, env_dir, site):  # Req: ag-0e6ca3
        """env 回收前置审计（task._reclaim_env 同款）：任务 id＋resolve 后路径
        ＋调用点标签先落事件流再 remove_env。审计尽力而为（B 册终闸：审计
        uow 竞争失败置于保护外＝回收未执行且无 reclaim_failed 痕的无归因
        孤儿 env）——败不断回收，顺序仍先审计后动手。"""
        if not env_dir:
            return
        try:
            self._audit(tid, "env_removed",
                        env=str(pathlib.Path(env_dir).resolve()), site=site)
        except Exception:
            pass  # F54 尽力而为口径：观测面病不断权威回收链
        try:
            self._gitx.remove_env(env_dir)
        except Exception as e:
            self._audit(tid, "reclaim_failed", note=str(e)[:200])

    def _remove_credential_file(self, tid, token=None):
        """终局撤牌文件清理（Req: F152＝本尊＋per-dispatch 既录键位；与
        tasksvc._remove_credential_file 同款）；幂等（缺文件静默）。"""
        if not isinstance(tid, str) or not tid or "/" in tid:
            return
        self._unlink_secret(f"{tid}.token")
        if token is not None:
            self._unlink_secret(dispatch_secret_key(tid, token))

    def _unlink_secret(self, key):
        try:
            (pathlib.Path(self._home) / "credentials" / key).unlink()
        except OSError:
            pass

    def _audit(self, tid, event, **extra):
        with self._store.uow():
            self._store.append_event(self._ev(self._now(), "gate", event, tid,
                                              extra or None))

    def _phase(self, tid, name, **extra):
        try:
            self._audit(tid, f"phase_{name}", **extra)
        except Exception:
            pass

    @staticmethod
    def _tail_block(out):
        return f"\n{out[-_TAIL:]}"

    @staticmethod
    def _art_line(art):
        return f"\nartifact={art['path']}" if art else ""

    def _now(self):
        return self._clock()

    def _ev(self, ts_ms, actor, event, task=None, detail=None):
        return Event(seq=0, ts_ms=ts_ms, actor=actor, event=event, task=task,
                     detail=detail)

    def _fire(self, name, phase, **context):
        if self._injector is not None:
            self._injector.fire(name, phase, **context)
