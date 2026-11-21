"""Req: F131 — v3 domain 层：实体、状态机与 DeliveryPolicy（设计 §3/§5-§7）。

纯 stdlib 值对象层：不 import 兄弟包、不触存储/transport——持久化归 storage
（T6）、服务归 application（T8+）、装配归 main。任务状态矩阵自 v2
core.state.LEGAL 全量平移（tests/test_v3_domain.py 与 v2 活码对照锁等）；时间
统一 epoch 毫秒 INTEGER，v2 浮点秒经 epoch_ms/epoch_seconds 适配；未知旧字段
入 legacy_extra_json 只读审计位（迁移器 T11 写入）。
"""
import dataclasses
import enum
import hashlib
import time

# ---- 时间规范（设计 §3：epoch 毫秒 INTEGER；v2 适配输出浮点秒）----


def epoch_ms(seconds: float) -> int:
    """浮点 epoch 秒→epoch 毫秒整数（毫秒粒度内与源值往返无损）。

    用 round 不用 int：二进制表示可能落在整数下方（2.046*1000＝
    2045.999…8），截断会偏 1ms 破坏往返契约（评审环 round1）。
    """
    return round(seconds * 1000)


def epoch_seconds(ms: int) -> float:
    """epoch 毫秒整数→浮点 epoch 秒（v2 适配层的输出形状，设计 §3）。"""
    return ms / 1000


def _now(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else epoch_ms(time.time())


# ---- 任务状态机：v2 core.state LEGAL 全量平移（对照锚见 tests/test_v3_domain.py）----
# 主链 todo→dispatched→running→done→merged→closed；blocked 双出口（cancel/再
# dispatch）；running→dispatched 仅 stalled 僵态重派（门侧先判僵再迁）；
# done→verify_failed/reopened→running 续修再 done；done→blocked 仅限交付失败
# （Req: F98，须显式 delivery_failure=True）；verify_failed→blocked 非手工边
# ——只由 fail_streak≥3 自动触发。
TASK_LEGAL = {
    "todo": frozenset({"dispatched", "cancelled"}),
    "dispatched": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "cancelled", "dispatched"}),
    "done": frozenset({"merged", "verify_failed", "reopened", "blocked"}),
    "verify_failed": frozenset({"running", "cancelled"}),
    "reopened": frozenset({"running", "cancelled"}),
    "merged": frozenset({"closed"}),
    "blocked": frozenset({"cancelled", "dispatched"}),
    "cancelled": frozenset(),
    "closed": frozenset(),
}
TERMINAL_TASK_STATES = frozenset({"cancelled", "closed"})
FAIL_STREAK_BLOCK = 3  # 连败≥3 自动 blocked（自动边，非手工迁移）


class InvalidTransition(Exception):
    """与 v2 core.state.InvalidTransition 同形（tid/frm/to/legal 供审计）。"""

    def __init__(self, tid, frm, to, legal):
        self.tid, self.frm, self.to, self.legal = tid, frm, to, legal
        super().__init__(f"{tid}: {frm}->{to} 非法（合法后继：{sorted(legal)}）")


# ---- 实体（设计 §3）----


@dataclasses.dataclass(frozen=True)
class VerifySpec:
    cmd: str = ""
    state: str = "pending"  # pending|registered（registered 才过闸）
    registered_by: str | None = None
    registered_at_ms: int | None = None
    timeout_s: int | None = None  # None＝继承全局预算
    timeout_source: str = "default"  # task|profile|default（F126）


@dataclasses.dataclass(frozen=True)
class DeliveryState:
    """Req: F94 交付投影（out_of_band/旧行＝Task.delivery None 不建投影）。"""

    mode: str = "managed"  # managed|external_branch
    status: str = "pending"
    commit: str | None = None
    base: str | None = None
    target_branch: str | None = None
    attempt: int = 0
    local_status: str = "pending"
    error: str | None = None
    conflict_files: list[str] | None = None  # F105 终局冲突清单（交付轮残值面）
    # Req: F144 delivered 时点（v2 行投影键 delivered_at——managed 交付落账；
    # None＝未交付不入键）＋external handoff 时点（v2 handoff_at——awaiting
    # 驻留龄基线）
    delivered_at_ms: int | None = None
    handoff_at_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class Artifact:
    """门执行产物（F122 概念平移；task 行只投影最新一条）。"""

    task_id: str
    phase: str
    path: str = ""
    cmd: str = ""
    rc: int | None = None
    failure_kind: str | None = None
    duration_s: float | None = None
    bytes: int = 0
    error: str | None = None
    started_at_ms: int | None = None
    ended_at_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class GateState:
    in_gate: bool = False
    gate_env: str | None = None
    pending_cancel: bool = False
    pending_cancel_reason: str | None = None
    gate_artifact: Artifact | None = None
    review_cache: dict | None = None  # {cmd, commit, ok, ran_at_ms, ...}（F114）
    review_fail_streak: int = 0
    gate_resource: dict | None = None  # 瞬态投影（F116）：不落库，重启重排重建


@dataclasses.dataclass(frozen=True)
class BaseProbe:  # base-red 探针结果缓存（F107/F111：判别力探针，缓存不重跑）
    result: str = ""  # red|green|skip
    cmd: str = ""
    base: str = ""
    ran_at_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class Task:
    """设计 §3：身份/归属/生命周期/依赖/幂等/活动＋持久硬字段全集。"""

    id: str
    state: str = "todo"
    title: str = ""
    goal: str = ""
    req_id: str | None = None
    model: str | None = None
    quick: bool = False
    deliverable: str | None = None
    chain_depth: int = 1
    root_sid: str | None = None
    dispatcher_sid: str | None = None
    worker_sid: str | None = None
    created_ms: int | None = None
    updated_ms: int | None = None
    fail_streak: int = 0
    cancel_reason: str | None = None
    after: tuple[str, ...] = ()
    waiting_on: str | None = None  # 受控等待（F124）
    client_key: str | None = None  # (root_sid,key) 唯一（F95）
    last_seen_ms: int | None = None  # 活动心跳（节流持久：批量窗 ≤5s，§3）
    project_dir: str | None = None
    target_branch: str | None = None
    env_dir: str | None = None
    env_base: str | None = None  # 评审链根锚（F111）
    commit: str | None = None
    rebased_from: str | None = None  # 崩溃重排后原 sha 留痕（F32）
    verify: VerifySpec = dataclasses.field(default_factory=VerifySpec)
    verify_pending: bool = False
    verify_base_exempt: bool = False
    review_cmd: str | None = None
    delivery: DeliveryState | None = None
    gate: GateState = dataclasses.field(default_factory=GateState)
    probe: BaseProbe | None = None
    # 未知旧字段只读审计位：域逻辑不读（§3；迁移器写入）
    legacy_extra_json: str = "{}"


class TimerState(str, enum.Enum):
    PENDING = "pending"
    FIRING = "firing"  # 消息未提交的进行态：崩溃窗内可回 pending（设计 §6）
    FIRED = "fired"
    CANCELLED = "cancelled"  # 显式 cancel／任务终态自动取消（设计 §6）


# pending→firing→fired 主链；firing→pending＝消息提交前崩溃回退（提交后按
# timer_id 对账补 fired）；fired 终态无出边；cancel 仅受理 pending（firing 属
# 事务内态，先结算再取消，不与 fired 竞态）。
TIMER_LEGAL = {
    TimerState.PENDING: frozenset({TimerState.FIRING, TimerState.CANCELLED}),
    TimerState.FIRING: frozenset({TimerState.PENDING, TimerState.FIRED}),
    TimerState.FIRED: frozenset(),
    TimerState.CANCELLED: frozenset(),
}


@dataclasses.dataclass(frozen=True)
class Timer:
    id: str
    task_id: str = ""
    target_role: str = "worker"  # worker|dispatcher——触发时解析当前 sid（设计 §6）
    due_ms: int = 0
    state: TimerState = TimerState.PENDING
    created_ms: int | None = None
    fired_ms: int | None = None
    body: str = ""  # Req: F145 触发消息正文（1-2000 可打印，信封带 timer id）
    created_by: str | None = None  # 创建者 sid（worker 仅取消自建判据）
    client_key: str | None = None  # 幂等键（同键同载荷返原 timer；异载荷 409）


class ApprovalState(str, enum.Enum):
    PENDING = "pending"
    RESOLVING = "resolving"  # 已记 attempted_decision、原生调用在途（设计 §7）
    APPROVED = "approved"
    REJECTED = "rejected"
    REJECTED_SIBLING = "rejected_sibling"  # 同 session 连带显式记档（设计 §7）
    EXPIRED = "expired"
    EXPIRED_SIBLING = "expired_sibling"
    RESOLVED_EXTERNAL = "resolved_external"  # 原生记录消失且无证据——不猜（设计 §7）
    ORPHANED = "orphaned"  # worker_rebind/oc_restart/task_cancel/session_deleted/backend_unavailable
    CANCELLED = "cancelled"


TERMINAL_APPROVAL_STATES = frozenset({
    ApprovalState.APPROVED, ApprovalState.REJECTED,
    ApprovalState.REJECTED_SIBLING, ApprovalState.EXPIRED,
    ApprovalState.EXPIRED_SIBLING, ApprovalState.RESOLVED_EXTERNAL,
    ApprovalState.ORPHANED, ApprovalState.CANCELLED})

# pending→resolving＝reply 事务入口（记 attempted_decision）；resolving→pending
# ＝原生仍 pending 可重试；deadline 自 pending/resolving 过期；orphaned/
# resolved_external＝恢复面终局（消失且无证据不猜）；sibling 终态自 pending
# 直接记档；全部终态无出边。
APPROVAL_LEGAL = {
    ApprovalState.PENDING: frozenset({
        ApprovalState.RESOLVING, ApprovalState.CANCELLED,
        ApprovalState.EXPIRED, ApprovalState.ORPHANED,
        ApprovalState.RESOLVED_EXTERNAL, ApprovalState.REJECTED_SIBLING,
        ApprovalState.EXPIRED_SIBLING}),
    ApprovalState.RESOLVING: frozenset({
        ApprovalState.PENDING, ApprovalState.APPROVED,
        ApprovalState.REJECTED, ApprovalState.EXPIRED,
        ApprovalState.ORPHANED, ApprovalState.RESOLVED_EXTERNAL}),
}


@dataclasses.dataclass(frozen=True)
class Approval:
    id: str
    task_id: str = ""
    requester_sid: str | None = None
    backend: str = "protocol"  # native|protocol（设计 §7）
    state: ApprovalState = ApprovalState.PENDING
    question: str = ""
    metadata: dict | None = None  # 白名单 task/business_action/reason/timeout_seconds/approval_key
    native_request_id: str | None = None  # 唯一（设计 §4）
    attempted_decision: str | None = None  # resolving 先记欲写终态（崩溃对账）
    deadline_ms: int | None = None  # 缺省 30min 上限 24h（缺省/上限归 contracts）
    orphan_reason: str | None = None
    created_ms: int | None = None
    resolved_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class Message:
    id: int
    ts_ms: int
    task_id: str
    kind: str
    from_sid: str
    to_sid: str
    body: str = ""
    dedup_key: str | None = None  # transition/timer/replay 幂等锚（设计 §5）
    replay_of: int | None = None  # 积压补交副本锚（F75）


@dataclasses.dataclass(frozen=True)
class DeliveryReceipt:
    """外发投递状态（设计 §5：与 mail_cursors 分离）。"""

    message_id: int
    status: str = "pending"  # pending|inflight|delivered
    attempts: int = 0
    last_error: str | None = None
    email_status: str | None = None
    lease_until_ms: int | None = None  # inflight 30s lease 超时回 pending（设计 §5）
    updated_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class MailCursor:
    """会话消费进度（设计 §5：新 sid＝0 全量血统补拉——换号恢复承重墙）。"""

    sid: str
    last_read_ms: int = 0


@dataclasses.dataclass(frozen=True)
class Event:
    seq: int  # 按 seq append＋fsync（设计 §8）
    ts_ms: int
    actor: str
    event: str
    task: str | None = None
    detail: dict | None = None  # 其余键值（kind/phase/rc/…）


@dataclasses.dataclass(frozen=True)
class Credential:
    sid: str
    token: str
    task_id: str | None = None  # 签发任务
    lineage: tuple[str, ...] = ()  # 血统先代 sid 链（F16 换号补拉）
    created_ms: int | None = None


def dispatch_secret_key(task_id: str, token: str) -> str:
    """Req: F152 — 派发签牌外部 secret 键位（home 相对文件名）：
    ``<task_id>.<sha256(token) 前 16 位>.token``。per-dispatch 唯一——
    token 变即键变，重派永不覆写旧活牌文件（F151 redispatch 风险封堵）；
    同 token 幂等同键重写。tasksvc 写盘/清理与 gitstore put 验证/记账共一
    派生面（两侧皆可达 domain，stdlib 层铁则不破）。本尊键位
    ``<task_id>.token`` 保留给 BOOT 指针回填（插件 pin 面），不入 canonical。"""
    return (f"{task_id}."
            f"{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}.token")


# ---- 状态机（纯函数：非法抛 InvalidTransition，frozen 实体天然零变更）----


def task_transition(task: Task, to: str, *, delivery_failure: bool = False,
                    now_ms: int | None = None) -> Task:
    """矩阵内迁移→新 Task（updated_ms 推进；dispatched 清 fail_streak）。

    done→blocked 是交付失败专用边（Req: F98，delivery 终局非质量败）——必须
    显式 delivery_failure=True，普通调用与矩阵外迁移同罪拒绝。
    """
    legal = TASK_LEGAL.get(task.state, frozenset())
    if (to not in legal
            or (task.state == "done" and to == "blocked" and not delivery_failure)
            or (delivery_failure and (task.state, to) != ("done", "blocked"))):
        raise InvalidTransition(task.id, task.state, to, legal)
    fields: dict = {"state": to, "updated_ms": _now(now_ms)}
    if to == "dispatched":
        fields["fail_streak"] = 0  # 对同任务再 dispatch 清零连败（v2 语义）
    return dataclasses.replace(task, **fields)


def bump_fail_streak(task: Task, *, now_ms: int | None = None) -> Task:
    """verify 连败＋1；≥3 且非终态/非 blocked→自动 blocked。矩阵不含
    verify_failed→blocked 手工边——自动触发是唯一入口（v2 口径平移）。"""
    t = dataclasses.replace(task, fail_streak=task.fail_streak + 1,
                            updated_ms=_now(now_ms))
    if (t.fail_streak >= FAIL_STREAK_BLOCK
            and t.state not in TERMINAL_TASK_STATES | {"blocked"}):
        return dataclasses.replace(t, state="blocked")
    return t


def reset_fail_streak(task: Task, *, now_ms: int | None = None) -> Task:
    return dataclasses.replace(task, fail_streak=0, updated_ms=_now(now_ms))


def timer_transition(timer: Timer, to: TimerState, *,
                     now_ms: int | None = None) -> Timer:
    legal = TIMER_LEGAL.get(timer.state, frozenset())
    if to not in legal:
        raise InvalidTransition(timer.id, timer.state.value, to.value,
                                {s.value for s in legal})
    return dataclasses.replace(
        timer, state=to,
        fired_ms=_now(now_ms) if to is TimerState.FIRED else timer.fired_ms)


def approval_transition(approval: Approval, to: ApprovalState, *,
                        now_ms: int | None = None,
                        orphan_reason: str | None = None) -> Approval:
    legal = APPROVAL_LEGAL.get(approval.state, frozenset())
    if to not in legal:
        raise InvalidTransition(approval.id, approval.state.value, to.value,
                                {s.value for s in legal})
    if to is ApprovalState.ORPHANED and not orphan_reason:
        # 终态无出边——reason 缺失即永久悬空审计（R4；五类见 ApprovalState.ORPHANED）
        raise ValueError(
            f"{approval.id}: ORPHANED 须携带 orphan_reason"
            "（worker_rebind/oc_restart/task_cancel/session_deleted/backend_unavailable）")
    return dataclasses.replace(
        approval, state=to,
        resolved_ms=(_now(now_ms) if to in TERMINAL_APPROVAL_STATES
                     else approval.resolved_ms),
        orphan_reason=(orphan_reason if to is ApprovalState.ORPHANED
                       else approval.orphan_reason))


# ---- DeliveryPolicy（设计 §5：digest/wake/notify+email 封闭映射）----


class Delivery(enum.Enum):
    DIGEST = "digest"  # F42 聚合缓冲：满 5 条/120s 免通知冲刷（note 专用）
    WAKE = "wake"  # 真实回合注入（缺省——question/result/timer/…）
    NOTIFY_EMAIL = "notify+email"  # synthetic 通知通道＋邮件例外（quiet escalate）


@dataclasses.dataclass(frozen=True)
class DeliveryDecision:
    mode: Delivery
    email: bool = False
    email_window: bool = False  # 终态邮件 60s 窗标记：只吞 merged→closed 邻对（F63）


class DeliveryPolicy:
    """kind/上下文→投递模式唯一仲裁（设计 §5）。调用方不可传任意 severity：
    无 severity 形参，决策由封闭映射推导——kind 与 escalate 类别取自固定
    集合，集合外值拒绝。"""

    NOTE = "note"
    ESCALATE = "escalate"
    TIMER = "timer"
    APPROVAL = "approval"
    RESULT = "result"

    # v2 现行 kind 全集＋v3 域内 kind（timer/approval，设计 §2/§5）。
    # Req: F144 补 v2 通知类 milestone/cancel（v2 router milestone/cancel 面
    # 平移——门入口/merged/取消通知通道；决策缺省 WAKE 同 result 族）
    KINDS = frozenset({"note", "question", "answer", "done", "verify",
                       "verify_reject", "peer", "process", "result",
                       "escalate", "timer", "approval", "milestone",
                       "cancel", "credential"})  # credential=reinject 兜底
    # （v2 recover 同 kind；决策缺省 WAKE——dispatch 面不入 ValueError）
    # 六类 quiet escalate：通知通道＋邮件例外（health.escalate 口径平移）
    QUIET_ESCALATES = frozenset({"stalled", "env-gone", "verify-dead",
                                 "verify-infra", "external-stale", "unpublished"})
    ESCALATES = QUIET_ESCALATES | frozenset(
        {"quality", "gatefiles", "reinject-fail", "dispatcher-dead"})
    # 终态邮件集（F63）；60s 窗只吞 merged→closed 门尾相邻对
    TERMINAL_EMAIL_STATES = frozenset({"merged", "closed", "blocked", "cancelled"})
    EMAIL_WINDOW_S = 60

    def decision(self, kind: str, *, escalate_kind: str | None = None,
                 task_state: str | None = None) -> DeliveryDecision:
        if kind not in self.KINDS:
            raise ValueError(
                f"未知消息 kind：{kind!r}（合法：{sorted(self.KINDS)}）")
        if kind == self.NOTE:
            return DeliveryDecision(Delivery.DIGEST)
        if kind == self.ESCALATE:
            if escalate_kind not in self.ESCALATES:
                raise ValueError(f"未知 escalate 类别：{escalate_kind!r}"
                                 f"（合法：{sorted(self.ESCALATES)}）")
            if escalate_kind in self.QUIET_ESCALATES:
                return DeliveryDecision(Delivery.NOTIFY_EMAIL, email=True)
            return DeliveryDecision(Delivery.WAKE)
        if kind in (self.TIMER, self.APPROVAL):
            return DeliveryDecision(Delivery.WAKE)  # timer/approval→唤醒
        if kind == self.RESULT and task_state in self.TERMINAL_EMAIL_STATES:
            return DeliveryDecision(Delivery.WAKE, email=True,
                                    email_window=True)
        return DeliveryDecision(Delivery.WAKE)
