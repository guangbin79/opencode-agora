"""消息路由（F4，设计 §4.3）：链边寻址＋落库先行＋幂等补拉。

post 是链上唯一投递路径（cancel 终止通知也走此）：先 write_message 落库
（库在即不丢），后 inject 一次；inject 失败仅落库，对方 check_mail 补拉。
已投递集为内存态，与 mail_read 游标（快照持久化）配合：进程内 inject 成功
即不再补拉，重启后由持久化游标兜底——崩溃回退口径同 State。会话换号重绑定后，
check_mail 沿牌血统补拉发往旧 sid 的离线消息，嵌套结果不因恢复而丢失。状态迁移
不在此——post(kind) 只管投递，状态由调用方（gate/task）驱动。

F42 分级批量注入：worker→dispatcher 的 kind=note 进内存缓冲，聚合 digest 经
inject_silent 免通知投递（落转录不调度回合），其余 kind 保持实时注入。缓冲为
服务内存态，崩溃丢 digest 可接受——note 本体已先行落库（§4.8 行先行），重启后
经 check_mail 可拉。冲刷触发：满 _DIGEST_N 条／最旧存龄 ≥_DIGEST_T 秒（无后台
线程，任一投递时惰性扫）／同目标关键消息先冲后发。digest 送达即视本体已投
（补拉不重交），失败则本体留库待补拉（离线口径不变）。

生命周期通知（ag-acae3b）：终态迁移（merged/closed/blocked/cancelled）与
reopened 拒因摘要经 notify_transition 以 kind=result 实时直投 dispatcher——
缓冲只收 note，天然旁路（终态优先级最高）；(tid,to,detail) 去重防重试窗口重推。
F63 终态邮件必达：终态迁移经注入 mail 缝外呼公共 send_email（health 单实现），
(tid,60s) 窗吞 merged→closed 门尾相邻双发（merged 崩窗滞留仍有一封）。
邮件正文经 _human_mail 人话渲染（Req: F171——邮件与会话行分家：会话注入
路径逐字节不变，原始通知行兜底保留合成行，零 worker 文本插值原则不变）。
"""
import math
import threading
import time
from pathlib import Path

_DIGEST_N = 5    # F42：阈值可调
_DIGEST_T = 120  # F42：阈值可调

_REF_LEN = 240  # Req: F125 引用触发长度阈值（超过即挂引用行）
_REF_KINDS = frozenset({"result", "question", "escalate", "verify"})
_REF_MARKS = ("输出尾部", "失败", "异常", "超时", "artifact=")

_TERMINAL_MAIL = {"merged", "closed", "blocked", "cancelled"}  # F63 邮件终态集
_MAIL_WINDOW = 60  # F63：窗去重秒数——只吞 merged→closed 门尾相邻对（见下）

_SILENT_REPLAY = frozenset({"note"})  # F75 补交分级：silent 免弹窗面
_NOTIFY_REPLAY = frozenset({"escalate"})  # ag-3016f1：escalate 补交走通知通道（唤醒）

_MAIL_WORD = {"merged": "已合并", "closed": "已收口",  # Req: F171 终态邮件状态词
              "blocked": "受阻", "cancelled": "已取消"}


def _human_mail(tid, to, text, task):  # Req: F171
    """终态邮件人话渲染（邮件面与会话行分家）：首行＝邮件主题
    [agora] 状态词：{title}（{tid}）（title 缺失回退 tid 不重复）；正文＝概述行
    ＋事实行（sha 在案才给）＋merged 专属（本地同步/远端领先 push 提醒＋收尾
    沉淀提醒）＋原始通知行兜底。纯字符串拼接，不查 git 不 IO 不抛；to 已由
    notify_transition 的 _TERMINAL_MAIL 闸住（函数内不防）。title 可源自
    worker 嵌套建单（tasksvc 只压平空白）——剥控制字符＋截 120 字符净化
    （评审 C 册 R1：ANSI 转义/信封单行健壮性）。"""
    title = ("".join(c for c in str(task.get("title") or tid)
                     if not ("\x00" <= c <= "\x1f"
                             or "\x7f" <= c <= "\x9f"))[:120]) or tid
    subject = (f"[agora] {_MAIL_WORD[to]}：{title}（{tid}）" if title != tid
               else f"[agora] {_MAIL_WORD[to]}：{tid}")
    lines = []
    if to == "merged":
        lines.append(f"「{title}」已通过验证并自动合并。")
    elif to == "closed":
        lines.append("任务收口（环境已回收）。")
    elif to == "blocked":
        lines.append("任务受阻——需重派（dispatch）或取消（cancel）。")
    else:
        lines.append("任务已取消。")
    delivery = task.get("delivery") or {}
    sha = task.get("commit") or delivery.get("commit")
    if sha:  # 事实行整行 sha 闸：缺则省略
        branch = delivery.get("target_branch") or task.get("target_branch")
        repo = str(task.get("project_dir") or "").rstrip("/").rsplit("/", 1)[-1]
        lines.append(f"· 仓库/分支：{repo} · {branch}（提交 {str(sha)[:7]}）")
    if to == "merged":
        ahead = delivery.get("origin_ahead")  # gate 交付投影（Req: F171）
        lines.append(f"· 本地已同步；远端尚未推送（领先 {ahead} 个提交）"
                     "→ 需要你手动 git push"
                     if type(ahead) is int and ahead > 0  # bool 非 int 语义（B/C 册）
                     else "· 本地已同步")
        lines.append("· 收尾提醒：本任务笔记/任务页做一次知识沉淀（knowledge ingest）")
    lines.append(f"· 原始通知：{text}")
    return subject + "\n\n" + "\n".join(lines)


def _wants_refs(kind, body, task):
    """Req: F125 引用优先触发：长正文／关键 kind 带诊断标记／产物在案带
    输出尾。短 ack/milestone 零引用噪音。"""
    if len(body) > _REF_LEN:
        return True
    if kind in _REF_KINDS and any(m in body for m in _REF_MARKS):
        return True
    return bool((task.get("gate_artifact") or {}).get("path")
                and "输出尾部" in body)


def _reference_lines(tid, mid, task, home):
    """Req: F125 稳定引用行：外层截断不可控，完整内容的指针必须最先出现。
    消息库 body 保持原文——引用只进注入文本。"""
    lines = [f"home={home}", f"msg=state/messages/{mid:08d}.md",
             f"page=wiki/tasks/{tid}.md"]
    art = (task.get("gate_artifact") or {}).get("path")
    if art:  # 相对 AGORA_HOME 优先；不在其下＝绝对原样
        try:
            art = str(Path(art).relative_to(home))
        except ValueError:
            pass
        lines.append(f"artifact={art}")
    return "\n".join(lines)


# Req: F174 no-op 申报前缀（全/半角冒号等价，F173 口径）；v3 同构副本在
# agorad/messaging.py——分层守卫（transport 仅 core、serverv3 不入 core）
# 禁共享 import，纯函数双件是分层税
_NOOP_PREFIXES = ("no-op:", "no-op：")


def parse_noop(body):
    """Req: F174：首行（第一个换行前）以 no-op:/no-op： 开头 → (True, 首行)；
    否则 (False, None)。首行剥离 C0/C1 控制字符（_human_mail 同款）——返回值
    直入 cancel_reason 通知面，防行伪造（评审 C 册）。"""
    first = str(body).split("\n", 1)[0]
    for p in _NOOP_PREFIXES:
        if first.startswith(p):
            clean = "".join(c for c in first
                            if not ("\x00" <= c <= "\x1f"
                                    or "\x7f" <= c <= "\x9f"))
            return True, clean
    return False, None


class NotChainEdge(Exception):
    """寻址非链边：方向非法/任务缺席/对方未登记/from_sid 非任务另一方。"""


class Router:
    """投递面三线程写者（httpd 串行面＋gate 门尾＋health 升级/修复，§4.8 演化：
    消息号配给经 Store._mid_lock 串行——Req: F75 评审残留 D3；交付状态经
    _delivery_lock 串行，网络注入锁外——Req: F87）。"""

    def __init__(self, store, state, oc, same_lineage=None, clock=None, mail=None):
        self._store = store
        self._state = state
        self._oc = oc
        self._same_lineage = same_lineage  # F16 血统判定（core 禁兄弟 import，装配注入）
        self._delivery_lock = threading.RLock()  # F87：落库/补拉/冲刷的复合状态串行
        self._delivered = set()
        self._inflight = set()  # F87：在线注入持有交付权的 msg id（网络调用锁外）
        self._pending = {}  # F42：(to_sid, tid) -> [(mid, ts, body)]，内存态可丢
        self._notified = set()  # ag-acae3b 生命周期通知去重：(tid, to, detail)
        self._clock = clock or time.time  # 假时钟注入口（测试）
        self._mail = mail  # F63 终态邮件外呼缝（公共 send_email 闭包，None＝不启用）
        self._mailed = {}  # F63：(tid) -> 上次邮件 clock 时点，60s 窗去重

    def is_dispatcher(self, sid, t):
        """sid 是否本任务派发方：现 sid 相等，或血统含旧 dispatcher_sid
        （F16 dispatcher 换号链边连续；血统判定缺失＝仅相等）。"""
        d = t.get("dispatcher_sid")
        return (sid == d
                or (d is not None and self._same_lineage is not None
                    and self._same_lineage(sid, d)))

    def post(self, from_sid, to_edge: tuple, kind: str, body: str,
             silent: bool = False, notify: bool = False) -> int:
        to_sid = self._resolve(from_sid, to_edge)
        return self._deliver(from_sid, to_sid, to_edge[1], kind, body,
                             silent, notify)

    def record(self, from_sid, tid, kind, body):
        """Req: F174 no-op 申报落库面：落库不注入（notify_worker 的链边镜像）
        ——申报原文留案供翻案（check_mail 可补拉），唤醒由 cancel result 通知
        接管。返回 mid；dispatcher 未登记＝无收件人沿 notify_dispatcher 口径
        抛 NotChainEdge。"""
        to_sid = self._state.get(tid).get("dispatcher_sid")
        if to_sid is None:
            raise NotChainEdge(f"{tid}: dispatcher 未登记")
        with self._delivery_lock:
            return self._store.write_message(
                {"ts": time.time(), "task": tid, "kind": kind,
                 "from": from_sid, "to": to_sid}, body)

    def notify_dispatcher(self, tid, kind, body):
        """服务侧结果回传：子任务尚无 worker 链边时也能通知其父 dispatcher。"""
        to_sid = self._state.get(tid).get("dispatcher_sid")
        if to_sid is None:
            raise NotChainEdge(f"{tid}: dispatcher 未登记")
        return self._deliver("agorad", to_sid, tid, kind, body)

    def notify_worker(self, tid, kind, body):
        """服务侧 worker 定向留痕（notify_dispatcher 的只落库镜像）。
        ag-d03e0e done 拒收可见化：受理段拒因原文经此落任务消息（from=
        agorad）。只落库不注入——调用方身处单线程串行 HTTP 环（B 册 R2：
        OC 挂起时同步注入会 head-of-line 阻断全部工具面），worker 同回合
        已收同步 MCP 报错，此处为消息库/check_mail 留痕面（未投递消息由
        check_mail 补拉即送达）。worker 未登记＝无收件人，返回 None。"""
        to_sid = self._state.get(tid).get("worker_sid")
        if to_sid is None:
            return None
        with self._delivery_lock:
            return self._store.write_message(
                {"ts": time.time(), "task": tid, "kind": kind,
                 "from": "agorad", "to": to_sid}, body)

    def notify_wait_resolved(self, tid, dep_tid, dep_state):
        """Req: F124 受控等待解除双通知（task 域登记即解析与 health tick
        解析共用）：等待任务 worker 注入 process 通知（依赖已结束及结果，
        可继续执行或按裁定处置——merged/closed＝成功；cancelled/blocked＝
        需处置）＋dispatcher 一条简短 result。通知面尽力而为（F54 口径），
        不抛、不阻塞解析面。"""
        try:
            t = self._state.get(tid)
            verdict = ("成功——可继续执行" if dep_state in ("merged", "closed")
                       else "未成功——需处置（按裁定继续或取消）")
            text = (f"受控等待解除：任务 {tid} 等待的依赖 {dep_tid} 已结束"
                    f"（{dep_state}，{verdict}）")
            if t.get("worker_sid"):
                self.post(t["dispatcher_sid"], ("down", tid), "process", text)
            self.notify_dispatcher(tid, "result", text)
        except Exception:
            pass

    def notify_transition(self, tid, to, detail="", body=None):
        """生命周期迁移→dispatcher 实时单行（ag-acae3b）：终态（merged/closed/
        blocked/cancelled）与 reopened 拒因摘要。kind=result 走弹通道直投——
        F42 缓冲只收 note，天然旁路（分级）；去重键含迁移时点（state.updated）：
        同迁移重放（状态未再动，gate 重试/崩溃重排窗口）不重推，跨迁移照推——
        裸 (tid,to,detail) 键会吞 worker 修复后再败的合法重复（评审环 B 册）。
        F63 终态邮件必达：to∈终态集且注入 mail 时外呼一封，正文经 _human_mail
        人话渲染（Req: F171——主题行用 title，原始通知行兜底合成行，零 worker
        文本插值原则保留）；独立于会话投递
        ——dispatcher 未登记/落库 I/O 败不吞邮件兜底（必达恰在故障场景兑现）。
        (tid,60s) 窗只吞 merged→closed 门尾相邻对（merged 发信 60s 内到达的
        closed 视为同一事件的门尾重复——生产观测 merged 后必随 closed，双邮件
        纯噪声；崩窗滞留 merged 仍恰一封到达）：其余终态互不吞——blocked→重派
        →merged 跨回合、blocked→cancelled 快速终局各自必达。mail 返回 False
        （确认败）不占窗，closed 可重试。body 显式给定时整体覆盖缺省合成行：
        终态接线点保既有会话文案逐字节不变。通知面任何异常（未登记/落库 I/O）
        静默尽力而为：不得断裂门链。"""
        try:
            task = self._state.get(tid)
            key = (tid, to, task.get("updated"))
            if key in self._notified:
                return None
            self._notified.add(key)
            text = body if body is not None else f"任务 {tid} {to} {detail}".rstrip()
            try:  # 会话面尽力而为：败不吞邮件兜底（F63）
                mid = self.notify_dispatcher(tid, "result", text)
            except Exception:
                mid = None
            if self._mail is not None and to in _TERMINAL_MAIL:
                now = self._clock()
                last = self._mailed.get(tid)
                if to != "closed" or last is None or now - last >= _MAIL_WINDOW:
                    try:  # 外缝异常不抛（send_email 内吞已返 False）
                        # Req: F171 邮件面人话渲染——会话行 text 逐字节不变
                        if self._mail(tid, _human_mail(tid, to, text, task)) \
                                is not False:  # None/True 视同已交
                            if to == "merged":  # 仅 merged 发信占窗：closed 照此判邻
                                self._mailed[tid] = now
                    except Exception:
                        pass
            return mid
        except Exception:  # 同 _deliver 注入口径：投递面异常不抛、不断链
            return None

    def _deliver(self, from_sid, to_sid, tid, kind, body,
                 silent=False, notify=False):
        self._sweep()  # F42：过龄待发先冲，保投递时序
        if kind == "note" and to_sid == self._state.get(tid).get("dispatcher_sid"):
            with self._delivery_lock:  # 落库＋入缓冲原子；未冲刷仍可提前补拉
                mid = self._store.write_message(  # 铁则：先落库后注入
                    {"ts": time.time(), "task": tid, "kind": kind,
                     "from": from_sid, "to": to_sid}, body)
                buf = self._pending.setdefault((to_sid, tid), [])
                buf.append((mid, self._clock(), body))
                flush_now = len(buf) >= _DIGEST_N
            if flush_now:  # F42 a：满条数即冲；网络注入在锁外
                self._flush(to_sid, tid)
            return mid
        self._flush(to_sid, tid)  # F42 a：关键消息先冲同目标 digest，本体保持实时
        with self._delivery_lock:
            mid = self._store.write_message(  # 铁则：先落库后注入
                {"ts": time.time(), "task": tid, "kind": kind,
                 "from": from_sid, "to": to_sid}, body)
            self._inflight.add(mid)  # 可见即持交付权：check_mail 不抢交（F87）
        # Req: F125 引用优先——[agora] 头之后、正文之前插稳定引用行；短消息
        # 不加（防噪音），消息库 body 与 check_mail 返回保持原文
        task = self._state.get(tid)
        text = f"[agora] task {tid} {kind} from {from_sid}\n"
        if _wants_refs(kind, body, task):
            text += _reference_lines(tid, mid, task, self._store.home) + "\n"
        text += body
        try:
            # 取属性入 try：oc 缺对应通道时同落兜底网（评审环 B 册）；notify 优先于
            # silent（ag-3016f1 通知通道＝synthetic resume:true，二者互斥不叠加）
            inject = (self._oc.inject_notify if notify
                      else self._oc.inject_silent if silent else self._oc.inject)
            ok = inject(to_sid, text)
        except Exception:  # oc 异常（凭据轮换 401/网络故障）同失败口径：落库兜底——
            ok = False     # done 链路不得因投递面异常断裂（T7 e2e 实测：raise→门不排队）
        with self._delivery_lock:
            self._inflight.discard(mid)
            if ok:
                self._delivered.add(mid)  # 在线送达一次；失败留库待补拉
        return mid

    def _sweep(self):
        """F42 a：最旧待发存龄 ≥_DIGEST_T 即冲（无后台线程，投递时惰性扫）。"""
        now = self._clock()
        with self._delivery_lock:
            due = [key for key, buf in self._pending.items()
                   if now - buf[0][1] >= _DIGEST_T]
        for key in due:
            self._flush(*key)

    def _flush(self, to_sid, tid):
        """F42 b/c：同目标待发聚 digest 经 inject_silent 免通知投递；失败则
        本体留库待补拉，成功即标记已投（check_mail 不重交）。"""
        with self._delivery_lock:
            buf = [(m, ts, b) for m, ts, b
                   in self._pending.pop((to_sid, tid), ())
                   if m not in self._delivered]  # F46 H-2：缓冲窗内已补拉的 mid 剔除
            if not buf:
                return
            self._inflight.update(m for m, _, _ in buf)  # 冲刷期不补拉（F87）
            # Req: F125 digest 头部全文引用优先：home/msgs_dir/ids 先于摘要
            # （摘要仍可截断；#id 定位全文文件 state/messages/<id 补零8位>.md，
            #  与 _reference_lines 的 msg= 行同口径——store 落盘即 {mid:08d}.md）
            lines = [f"[agora] {len(buf)} 条过程消息（{tid}）",
                     f"home={self._store.home}",
                     "msgs_dir=state/messages",
                     "ids=" + ",".join(str(m) for m, _, _ in buf)]
            lines += [f"#{m} " + " ".join(b.split())[:120]
                      for m, _, b in buf]  # 摘要体 ≤120 字符（另加 #id 前缀）
        try:  # 取属性入 try：oc 缺 inject_silent 时同落兜底网（评审环 B 册）
            ok = self._oc.inject_silent(to_sid, "\n".join(lines))
        except Exception:  # 同失败口径：不抛、不断链（T7 e2e 实测）
            ok = False
        with self._delivery_lock:
            for m, _, _ in buf:
                self._inflight.discard(m)
            if ok:
                self._delivered.update(m for m, _, _ in buf)

    def dedup_key(self, msg_id: int) -> bool:
        """已投递过返回 True（重发不再投递）；否则登记并返回 False。"""
        with self._delivery_lock:
            if msg_id in self._delivered:
                return True
            self._delivered.add(msg_id)
            return False

    def replay_root_backlog(self, tid, to_sid):
        """F75 积压补交：to=='root'（错址窗口）的消息生成补交副本——原件一字
        不改；副本 to=修复后真实派发者、task/kind/from/body 原样、新增
        replay_of=<原消息id> 幂等锚（重启/重复扫描按锚判已补交，绝不重复生成）；
        ts 取当前保证越过派发者既有 check_mail 游标。分级投递同既有面：
        question/verify/done 等走真实 prompt 唤醒，note 走 silent 免弹窗、
        escalate 走 notify 通知通道（ag-3016f1 同主通道口径）；注入失败副本
        留库由 check_mail 补拉。只恢复通信，不替派发者做
        业务决策。返回补交数。"""
        msgs = self._store.read_messages()
        done = {m.get("replay_of") for m in msgs}
        n = 0
        for m in msgs:
            if (m.get("task") != tid or m.get("to") != "root"
                    or str(m["id"]) in done):
                continue
            with self._delivery_lock:  # 副本可见即持交付权（F87）
                mid = self._store.write_message(
                    {"ts": time.time(), "task": tid, "kind": m.get("kind"),
                     "from": m.get("from"), "to": to_sid,
                     "replay_of": m["id"]}, m.get("body", ""))
                self._inflight.add(mid)
            n += 1
            # Req: F125 补交注入同款引用优先格式（不复制 _deliver 投递状态面）
            body = m.get("body", "")
            task = self._state.get(tid)
            text = f"[agora] task {tid} {m.get('kind')} from {m.get('from')}\n"
            if _wants_refs(m.get("kind"), body, task):
                text += _reference_lines(tid, mid, task,
                                         self._store.home) + "\n"
            text += body
            try:  # 取属性入 try：oc 缺对应通道时同落兜底网（_flush 口径）
                inject = (self._oc.inject_notify
                          if m.get("kind") in _NOTIFY_REPLAY
                          else self._oc.inject_silent
                          if m.get("kind") in _SILENT_REPLAY
                          else self._oc.inject)
                ok = inject(to_sid, text)
            except Exception:  # oc 异常同失败口径：留库补拉
                ok = False
            with self._delivery_lock:
                self._inflight.discard(mid)
                if ok:
                    self._delivered.add(mid)  # 在线送达一次；失败留库待补拉
        return n

    def check_mail(self, sid: str) -> list:
        with self._delivery_lock:  # 过滤＋标记原子：inflight 消息让位在线注入
            cursor = self._state.snapshot()["mail_read"].get(sid, 0.0)
            inbox = [m for m in self._store.read_messages()
                     if m["to"] == sid or (
                         self._same_lineage is not None
                         and self._same_lineage(sid, m["to"]))]
            got = [m for m in inbox
                   if m["id"] not in self._delivered
                   and m["id"] not in self._inflight
                   and float(m["ts"]) > cursor]
            if got:
                # 游标不得越过本收件更老 inflight：其注入失败后仍须补拉（F87）
                inflight_ts = [float(m["ts"]) for m in inbox
                               if m["id"] in self._inflight]
                advance = max(float(m["ts"]) for m in got)
                if inflight_ts:
                    cap = min(inflight_ts)
                    advance = min(advance, math.nextafter(cap, -math.inf))
                self._state.touch_mail(sid, advance)
                self._delivered.update(m["id"] for m in got)  # 拉取即送达（无 ack）
            return got

    def _resolve(self, from_sid, to_edge) -> str:
        d, tid = to_edge
        try:
            t = self._state.get(tid)
        except KeyError:
            raise NotChainEdge(f"{tid}: 任务不存在") from None
        if d == "up":  # worker 边不加血统：recover 已覆写 worker_sid，无换号缺口
            to_sid, ok = t.get("dispatcher_sid"), from_sid == t.get("worker_sid")
        elif d == "down":
            to_sid, ok = t.get("worker_sid"), self.is_dispatcher(from_sid, t)
        else:
            raise NotChainEdge(f"{to_edge}: 方向非法")
        if to_sid is None or not ok:
            raise NotChainEdge(f"{from_sid}->{to_edge}: 非链边")
        return to_sid
