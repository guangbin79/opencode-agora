"""Req: F134 — v3 message/outbox 应用服务（opencode-agora v3 P2-T7，设计 §5/§8）。

应用服务层（§1 分层第 3 层）：OC 客户端、消息仓库、文件导出器全部经
Protocol 构造注入——本模块不 import agorad.access／agorad.storage
（Storage 结构化满足仓库 Protocol 即可；test_architecture.py 守卫），
零 SQL（SQL 圈禁仅 storage；durable inflight 扫描经仓库只读方法
receipts_by_status，随 F134 落在 storage.py）。

写路径铁则（§5）：message＋receipt＋event 同一 UoW 复合提交（中途故障
三表零残留），提交成功后才调 OC——UoW 禁网络（§4），注入必在事务外。
投递生命周期 pending→inflight→delivered；OC 异常/返回 False 回 pending
（attempts/last_error 记账）；inflight 30s lease 过期（时钟可注入）或
启动恢复（recover：全部 inflight→pending）归位。

message_pull（§5 承重语义）：候选＝id 大于本 sid cursor 且地址命中当前
sid 或 credential lineage；遇第一条 inflight 即停（其后不返回、cursor
不越过）；receipt=delivered 不阻断消费；返回后推进 cursor 并把 pending
receipt 标 delivered 抑制 Outbox 重复注入；同 sid 二次 pull 不重复返回。
cursor 值＝last-read message id（§5「id 大于 cursor」口径——mail_cursors
列名 last_read_ms 系 v2 时间戳语义残留，§9 迁移器同按 id 写入）。新 sid
cursor=0 对血统全量补拉（换号恢复承重墙）。

dedup_key 持久幂等（storage.insert_message 撞键回读原行）；新消息活性
判定＝receipt 缺席（本服务不变量：每条新消息同事务配 receipt，故撞键
回读行必有 receipt 在案）。note digest 满 5 条/最旧存龄 120s 聚合冲刷
（内存缓冲崩可丢——本体已落库，补拉兜底）；关键 kind 先冲同目标 digest
再发自家。引用行不悬空（§8/§10）：消息文件导出失败→注入文本省略 msg=
路径行（home/page 行保留）。线程模型：按 transport 单写者装配假设实现
（v2 _delivery_lock 同款加固归 T8+ 装配面）；写串行由 Storage 单一写锁保证。
"""
import dataclasses
import time
import typing

from agorad.domain import (Delivery, DeliveryPolicy, DeliveryReceipt, Event,
                           Message, epoch_ms)

LEASE_MS = 30_000       # §5 inflight lease（时钟可注入超时）
DIGEST_N = 5            # §5 digest 冲刷条数阈值
DIGEST_T_MS = 120_000   # §5 digest 最旧存龄阈值


class MessageStore(typing.Protocol):
    """storage 公开仓库接口（结构化；agorad.storage.Storage 天然满足）。"""

    def uow(self): ...
    def insert_message(self, *, ts_ms, task_id, kind, from_sid, to_sid, body,
                       dedup_key=None, timer_id=None, replay_of=None): ...
    def get_message(self, mid): ...
    def messages_for_sid(self, sid, after_id=0): ...
    def get_receipt(self, mid): ...
    def upsert_receipt(self, receipt: DeliveryReceipt): ...
    def receipts_by_status(self, status): ...
    def get_cursor(self, sid): ...
    def upsert_cursor(self, sid, last_read): ...
    def get_credential(self, sid): ...
    def append_event(self, ev: Event): ...


class OpencodeClient(typing.Protocol):
    """OC 注入面（access.oc.Oc 同形；调用必在 UoW 外）。"""

    def inject(self, sid, text): ...
    def inject_silent(self, sid, text): ...
    def inject_notify(self, sid, text): ...


class MessageExporter(typing.Protocol):
    """§8 派生导出：返回相对 AGORA_HOME 的消息文件路径；失败抛异常。"""

    def export(self, message: Message): ...


class MessagingService:
    """message_post/pull＋Outbox 投递（设计 §5）。"""

    def __init__(self, store: MessageStore, oc: OpencodeClient, *, home="",
                 policy=None, clock=None, injector=None,
                 exporter: MessageExporter | None = None):
        self._store = store
        self._oc = oc
        self._home = str(home)
        self._policy = policy or DeliveryPolicy()
        self._clock = clock or (lambda: epoch_ms(time.time()))
        self._injector = injector  # tests.v3_harness.FaultInjector（failpoint dispatch）
        self._exporter = exporter
        self._digest = {}  # (to_sid, task_id) -> [(mid, ts_ms, body_str)]，内存态可丢
        self._last_lease_sweep_ms = 0  # 惰性扫节流锚（见 _expire_leases）

    # ---- 写路径（§5：同事务复合提交，提交后才调 OC）----

    def post(self, from_sid, to_sid, task_id, kind, body, *,
             dedup_key=None, timer_id=None, escalate_kind=None) -> Message:
        """message_post：落库先行；dedup 撞键回读原行零新建零重投。"""
        decision = self._policy.decision(kind, escalate_kind=escalate_kind)
        self._expire_leases()  # 惰性扫：过期 inflight lease 回 pending（免后台线程）
        self._sweep()  # 过龄 digest 先冲，保投递时序
        ts = self._clock()
        with self._store.uow():
            msg = self._store.insert_message(
                ts_ms=ts, task_id=task_id, kind=kind, from_sid=from_sid,
                to_sid=to_sid, body=body, dedup_key=dedup_key,
                timer_id=timer_id)
            fresh = self._store.get_receipt(msg.id) is None
            if fresh:  # 撞键回读行必有 receipt 在案→缺席即新消息
                self._store.upsert_receipt(DeliveryReceipt(message_id=msg.id))
                self._store.append_event(Event(
                    seq=0, ts_ms=ts, actor=from_sid, event="message_posted",
                    task=task_id,
                    detail={"kind": kind, "message_id": msg.id, "to": to_sid}))
        if not fresh:
            return msg  # 幂等重试：原 id 返回，不重投（断言5）
        path = self._export_path(msg)  # §8：提交后先导出再注入
        if decision.mode is Delivery.DIGEST:
            buf = self._digest.setdefault((to_sid, task_id), [])
            buf.append((msg.id, ts, self._text(msg.body)))
            if len(buf) >= DIGEST_N:
                self._flush(to_sid, task_id)  # 满 N 条即冲（断言6）
            return msg
        self._flush(to_sid, task_id)  # 关键 kind 先冲同目标 digest（断言6）
        with self._store.uow():
            held = self._lease(msg.id)
        if held:
            self._deliver(msg, decision, path)
        return msg

    # ---- Outbox 投递（§5 receipt 生命周期）----

    def dispatch(self, mid, *, escalate_kind=None) -> bool:
        """单发投递／崩溃恢复后的重投入口（at-least-once）。消息缺席、
        已 delivered 或他人持未过期 lease→False（不抢交）。"""
        with self._store.uow():
            msg = self._store.get_message(mid)
            held = msg is not None and self._lease(msg.id)
        if not held:
            return False
        return self._deliver(
            msg, self._policy.decision(msg.kind, escalate_kind=escalate_kind),
            self._export_path(msg))

    def pull(self, sid) -> list:
        """message_pull：见模块 docstring（停止规则＋lineage 补拉＋抑制）。"""
        self._expire_leases()
        cred = self._store.get_credential(sid)
        lineage = cred.lineage if cred is not None else ()
        cur = self._store.get_cursor(sid)
        cursor = cur.last_read_ms if cur is not None else 0
        with self._store.uow():
            merged = {}
            for address in (sid, *lineage):  # 地址命中当前 sid 或血统先代
                for m in self._store.messages_for_sid(address, cursor):
                    merged[m.id] = m
            out = []
            for m in sorted(merged.values(), key=lambda m: m.id):
                r = self._store.get_receipt(m.id)
                if r is not None and r.status == "inflight":
                    break  # 遇第一条 inflight 即停：其后不返回、cursor 不越过
                out.append(m)  # receipt=delivered 不阻断消费
            if out:
                self._store.upsert_cursor(sid, max(m.id for m in out))
                for m in out:  # 抑制 Outbox 重复注入（pending→delivered）
                    r = self._store.get_receipt(m.id)
                    if r is not None and r.status == "pending":
                        self._store.upsert_receipt(dataclasses.replace(
                            r, status="delivered", last_error=None,
                            updated_ms=self._clock()))
            return out

    def recover(self):
        """启动恢复（§5「启动时 inflight→pending」）：崩溃窗滞留的 durable
        inflight 全部归位 pending（等价 lease 无限过期）。"""
        self._expire_leases(force=True)

    def _lease(self, mid) -> bool:
        """pending→inflight（attempts＋1、lease=now+30s）；delivered 或他人
        持未过期 inflight→False。身处调用方 UoW。"""
        r = self._store.get_receipt(mid)
        if r is None or r.status == "delivered":
            return False
        now = self._clock()
        if (r.status == "inflight" and r.lease_until_ms is not None
                and r.lease_until_ms > now):
            return False
        self._store.upsert_receipt(dataclasses.replace(
            r, status="inflight", attempts=r.attempts + 1,
            lease_until_ms=now + LEASE_MS, updated_ms=now))
        return True

    def _deliver(self, msg, decision, path) -> bool:
        ok, error = self._call_oc(msg, decision, self._compose(msg, path))
        self._fire("dispatch", "after", message_id=msg.id)  # OC 已回、落账前崩溃窗
        with self._store.uow():
            r = self._store.get_receipt(msg.id)
            if r is not None and r.status == "inflight":
                self._store.upsert_receipt(dataclasses.replace(
                    r, status="delivered" if ok else "pending",
                    last_error=None if ok else error,
                    lease_until_ms=None, updated_ms=self._clock()))
        return ok

    def _call_oc(self, msg, decision, text):
        self._fire("dispatch", "before", message_id=msg.id)  # inflight 已落、OC 前
        if decision.mode is Delivery.NOTIFY_EMAIL:
            channel = self._oc.inject_notify
        elif decision.mode is Delivery.DIGEST:
            channel = self._oc.inject_silent  # dispatch() 单发 note：免通知通道
        else:
            channel = self._oc.inject
        try:
            ok = channel(msg.to_sid, text)
        except Exception as exc:  # OC 异常同败口径：回 pending 留库补拉
            return False, str(exc) or type(exc).__name__
        if ok:
            return True, None
        return False, "oc inject returned False"

    def _expire_leases(self, *, force=False):
        """inflight lease 过期回 pending（§5 30s）；force＝无视为全部（启动
        恢复）。post/pull 入口惰性扫，无后台线程；receipts 表扫描按 lease
        半窗节流——30s 内不可能出现新过期项（评审环 R1 修1：免每入口全表
        扫，配 storage idx_receipts_status 索引）。"""
        now = self._clock()
        if not force and now - self._last_lease_sweep_ms < LEASE_MS // 2:
            return
        if not force:
            self._last_lease_sweep_ms = now
        with self._store.uow():
            for r in self._store.receipts_by_status("inflight"):
                if (not force and r.lease_until_ms is not None
                        and r.lease_until_ms > now):
                    continue
                self._store.upsert_receipt(dataclasses.replace(
                    r, status="pending", lease_until_ms=None, updated_ms=now))

    # ---- note digest（§5：满 5 条/120s 聚合冲刷；内存缓冲崩可丢）----

    def _sweep(self):
        now = self._clock()
        for key, buf in list(self._digest.items()):
            if buf and now - buf[0][1] >= DIGEST_T_MS:  # 最旧存龄≥120s 即冲
                self._flush(*key)

    def _flush(self, to_sid, task_id):
        """同目标待发聚 digest 经 inject_silent 免通知投递；成功即视本体
        已投（receipt→delivered，补拉不重交），失败留 pending 补拉兜底。
        批量聚投无逐条在途语义——不占 inflight lease（OC 调用窗内并发
        pull 至多把同批标 delivered，幂等同值）。"""
        with self._store.uow():
            entries = []
            for mid, _, body in self._digest.pop((to_sid, task_id), []):
                r = self._store.get_receipt(mid)
                if r is None or r.status != "delivered":  # 冲刷窗内已补拉送达剔除
                    entries.append((mid, body))
        if not entries:
            return
        mids = [mid for mid, _ in entries]
        lines = [f"[agora] {len(entries)} 条过程消息（{task_id}）",
                 f"home={self._home}", "msgs_dir=state/messages",
                 "ids=" + ",".join(str(m) for m in mids)]
        lines += [f"#{mid} {' '.join(body.split())[:120]}"
                  for mid, body in entries]  # 摘要体 ≤120 字符（#id 前缀另计）
        try:
            ok = self._oc.inject_silent(to_sid, "\n".join(lines))
        except Exception:
            ok = False  # OC 异常同败口径：本体留库补拉
        if ok:
            now = self._clock()
            with self._store.uow():
                for mid in mids:
                    r = self._store.get_receipt(mid)
                    if r is not None and r.status == "pending":
                        self._store.upsert_receipt(dataclasses.replace(
                            r, status="delivered", last_error=None,
                            updated_ms=now))

    # ---- 注入文本与派生导出（§8：引用行不悬空）----

    def _export_path(self, msg):
        """提交后先导出再注入；导出失败→None（引用行省略 msg=，不悬空）。"""
        if self._exporter is None:
            return None
        try:
            return self._exporter.export(msg)
        except Exception:
            return None

    def _compose(self, msg, path):
        lines = [f"[agora] task {msg.task_id} {msg.kind} from {msg.from_sid}",
                 f"home={self._home}"]
        if path:
            lines.append(f"msg={path}")
        lines.append(f"page=wiki/tasks/{msg.task_id}.md")
        lines.append(self._text(msg.body))
        return "\n".join(lines)

    @staticmethod
    def _text(body):
        """body 恒字节出入（§4 BLOB）；注入文本 UTF-8 宽容解码。"""
        if isinstance(body, (bytes, bytearray, memoryview)):
            return bytes(body).decode("utf-8", "replace")
        return str(body)

    def _fire(self, name, phase, **context):
        if self._injector is not None:
            self._injector.fire(name, phase, **context)
