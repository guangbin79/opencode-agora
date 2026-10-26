"""消息路由（F4，设计 §4.3）：链边寻址＋落库先行＋幂等补拉。

post 是链上唯一投递路径（cancel 终止通知也走此）：先 write_message 落库
（库在即不丢），后 inject 一次；inject 失败仅落库，对方 check_mail 补拉。
已投递集为内存态，与 mail_read 游标（快照持久化）配合：进程内 inject 成功
即不再补拉，重启后由持久化游标兜底——崩溃回退口径同 State。状态迁移不在
此——post(kind) 只管投递，状态由调用方（gate/task）驱动。
"""
import time


class NotChainEdge(Exception):
    """寻址非链边：方向非法/任务缺席/对方未登记/from_sid 非任务另一方。"""


class Router:
    """单写者路由：无锁无线程，httpd 串行调用（设计 §4.8 并发口径）。"""

    def __init__(self, store, state, oc):
        self._store = store
        self._state = state
        self._oc = oc
        self._delivered = set()

    def post(self, from_sid, to_edge: tuple, kind: str, body: str) -> int:
        to_sid = self._resolve(from_sid, to_edge)
        tid = to_edge[1]
        mid = self._store.write_message(  # 铁则：先落库后注入
            {"ts": time.time(), "task": tid, "kind": kind,
             "from": from_sid, "to": to_sid}, body)
        text = f"[agora] task {tid} {kind} from {from_sid}\n{body}"
        try:
            ok = self._oc.inject(to_sid, text)
        except Exception:  # oc 异常（凭据轮换 401/网络故障）同失败口径：落库兜底——
            ok = False     # done 链路不得因投递面异常断裂（T7 e2e 实测：raise→门不排队）
        if ok:
            self._delivered.add(mid)  # 在线送达一次；失败留库待补拉
        return mid

    def dedup_key(self, msg_id: int) -> bool:
        """已投递过返回 True（重发不再投递）；否则登记并返回 False。"""
        if msg_id in self._delivered:
            return True
        self._delivered.add(msg_id)
        return False

    def check_mail(self, sid: str) -> list:
        cursor = self._state.snapshot()["mail_read"].get(sid, 0.0)
        got = [m for m in self._store.read_messages()
               if m["to"] == sid and m["id"] not in self._delivered
               and float(m["ts"]) > cursor]
        self._state.touch_mail(sid)
        self._delivered.update(m["id"] for m in got)  # 拉取即送达（无 ack）
        return got

    def _resolve(self, from_sid, to_edge) -> str:
        d, tid = to_edge
        try:
            t = self._state.get(tid)
        except KeyError:
            raise NotChainEdge(f"{tid}: 任务不存在") from None
        if d == "up":
            to_sid, other = t.get("dispatcher_sid"), t.get("worker_sid")
        elif d == "down":
            to_sid, other = t.get("worker_sid"), t.get("dispatcher_sid")
        else:
            raise NotChainEdge(f"{to_edge}: 方向非法")
        if to_sid is None or from_sid != other:
            raise NotChainEdge(f"{from_sid}->{to_edge}: 非链边")
        return to_sid
