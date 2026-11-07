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
正文＝会话行同源服务合成（零 worker 文本），(tid,60s) 窗吞 merged→closed
门尾相邻双发（merged 崩窗滞留仍有一封）。
"""
import time

_DIGEST_N = 5    # F42：阈值可调
_DIGEST_T = 120  # F42：阈值可调

_TERMINAL_MAIL = {"merged", "closed", "blocked", "cancelled"}  # F63 邮件终态集
_MAIL_WINDOW = 60  # F63：窗去重秒数——只吞 merged→closed 门尾相邻对（见下）


class NotChainEdge(Exception):
    """寻址非链边：方向非法/任务缺席/对方未登记/from_sid 非任务另一方。"""


class Router:
    """单写者路由：无锁无线程，httpd 串行调用（设计 §4.8 并发口径）。"""

    def __init__(self, store, state, oc, same_lineage=None, clock=None, mail=None):
        self._store = store
        self._state = state
        self._oc = oc
        self._same_lineage = same_lineage  # F16 血统判定（core 禁兄弟 import，装配注入）
        self._delivered = set()
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
             silent: bool = False) -> int:
        to_sid = self._resolve(from_sid, to_edge)
        return self._deliver(from_sid, to_sid, to_edge[1], kind, body, silent)

    def notify_dispatcher(self, tid, kind, body):
        """服务侧结果回传：子任务尚无 worker 链边时也能通知其父 dispatcher。"""
        to_sid = self._state.get(tid).get("dispatcher_sid")
        if to_sid is None:
            raise NotChainEdge(f"{tid}: dispatcher 未登记")
        return self._deliver("agorad", to_sid, tid, kind, body)

    def notify_transition(self, tid, to, detail="", body=None):
        """生命周期迁移→dispatcher 实时单行（ag-acae3b）：终态（merged/closed/
        blocked/cancelled）与 reopened 拒因摘要。kind=result 走弹通道直投——
        F42 缓冲只收 note，天然旁路（分级）；去重键含迁移时点（state.updated）：
        同迁移重放（状态未再动，gate 重试/崩溃重排窗口）不重推，跨迁移照推——
        裸 (tid,to,detail) 键会吞 worker 修复后再败的合法重复（评审环 B 册）。
        F63 终态邮件必达：to∈终态集且注入 mail 时外呼一封，正文＝会话行同源
        服务合成（tid＋终态＋detail/sha，零 worker 文本插值）；独立于会话投递
        ——dispatcher 未登记/落库 I/O 败不吞邮件兜底（必达恰在故障场景兑现）。
        (tid,60s) 窗只吞 merged→closed 门尾相邻对（merged 发信 60s 内到达的
        closed 视为同一事件的门尾重复——生产观测 merged 后必随 closed，双邮件
        纯噪声；崩窗滞留 merged 仍恰一封到达）：其余终态互不吞——blocked→重派
        →merged 跨回合、blocked→cancelled 快速终局各自必达。mail 返回 False
        （确认败）不占窗，closed 可重试。body 显式给定时整体覆盖缺省合成行：
        终态接线点保既有会话文案逐字节不变。通知面任何异常（未登记/落库 I/O）
        静默尽力而为：不得断裂门链。"""
        try:
            key = (tid, to, self._state.get(tid).get("updated"))
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
                        if self._mail(tid, text) is not False:  # None/True 视同已交
                            if to == "merged":  # 仅 merged 发信占窗：closed 照此判邻
                                self._mailed[tid] = now
                    except Exception:
                        pass
            return mid
        except Exception:  # 同 _deliver 注入口径：投递面异常不抛、不断链
            return None

    def _deliver(self, from_sid, to_sid, tid, kind, body, silent=False):
        self._sweep()  # F42：过龄待发先冲，保投递时序
        mid = self._store.write_message(  # 铁则：先落库后注入
            {"ts": time.time(), "task": tid, "kind": kind,
             "from": from_sid, "to": to_sid}, body)
        if kind == "note" and to_sid == self._state.get(tid).get("dispatcher_sid"):
            buf = self._pending.setdefault((to_sid, tid), [])
            buf.append((mid, self._clock(), body))
            if len(buf) >= _DIGEST_N:  # F42 a：满条数即冲
                self._flush(to_sid, tid)
            return mid
        self._flush(to_sid, tid)  # F42 a：关键消息先冲同目标 digest，本体保持实时
        text = f"[agora] task {tid} {kind} from {from_sid}\n{body}"
        try:
            # 取属性入 try：oc 缺 inject_silent 时同落兜底网（评审环 B 册）
            ok = (self._oc.inject_silent if silent else self._oc.inject)(to_sid, text)
        except Exception:  # oc 异常（凭据轮换 401/网络故障）同失败口径：落库兜底——
            ok = False     # done 链路不得因投递面异常断裂（T7 e2e 实测：raise→门不排队）
        if ok:
            self._delivered.add(mid)  # 在线送达一次；失败留库待补拉
        return mid

    def _sweep(self):
        """F42 a：最旧待发存龄 ≥_DIGEST_T 即冲（无后台线程，投递时惰性扫）。"""
        now = self._clock()
        for key, buf in list(self._pending.items()):
            if now - buf[0][1] >= _DIGEST_T:
                self._flush(*key)

    def _flush(self, to_sid, tid):
        """F42 b/c：同目标待发聚 digest 经 inject_silent 免通知投递；失败则
        本体留库待补拉，成功即标记已投（check_mail 不重交）。"""
        buf = [(m, ts, b) for m, ts, b in self._pending.pop((to_sid, tid), ())
               if m not in self._delivered]  # F46 H-2：缓冲窗口内补拉已交的 mid 对账剔除
        if not buf:
            return
        lines = [f"[agora] {len(buf)} 条过程消息（{tid}）"]
        lines += [" ".join(b.split())[:120] for _, _, b in buf]  # 每条一行 ≤120 字符
        try:  # 取属性入 try：oc 缺 inject_silent 时同落兜底网（评审环 B 册）
            ok = self._oc.inject_silent(to_sid, "\n".join(lines))
        except Exception:  # 同失败口径：不抛、不断链（T7 e2e 实测）
            ok = False
        if ok:
            self._delivered.update(m for m, _, _ in buf)

    def dedup_key(self, msg_id: int) -> bool:
        """已投递过返回 True（重发不再投递）；否则登记并返回 False。"""
        if msg_id in self._delivered:
            return True
        self._delivered.add(msg_id)
        return False

    def check_mail(self, sid: str) -> list:
        cursor = self._state.snapshot()["mail_read"].get(sid, 0.0)
        got = [m for m in self._store.read_messages()
               if (m["to"] == sid or (self._same_lineage is not None
                                      and self._same_lineage(sid, m["to"])))
               and m["id"] not in self._delivered and float(m["ts"]) > cursor]
        self._state.touch_mail(sid)
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
