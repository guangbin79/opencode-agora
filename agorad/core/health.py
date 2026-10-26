"""健康探活与升级拍人（F5，设计 §4.4）：定时只探死、失控实时拍主会话。

tick 只探活动任务的直接下级会话（无全局巡检、无 stalled 判定——"僵"归
board/doctor 与人工）；死下级→recover.reinject 自愈（静默），失败→升级；
死会话若正管着在飞链（dispatcher）→不代管、直接拍人。升级正文由服务权威
合成；任务/worker 供给文本以"不可信原文"段携带（拍平换行，防伪造服务行）。
escalate 沿 dispatcher_sid 上行至根任务（根 dispatcher＝main_sid，装配保证）
经 router post——落库先行，注入即视为拍到；escalate 同时落 events.jsonl 审计行
（F8）。core 只 import stdlib，依赖全注入。
"""
import time

# 升级事件类别（R9 初始集；gate 的质量连败/闸体变更引用此收口）
KIND_QUALITY = "quality"
KIND_GATEFILES = "gatefiles"
KIND_REINJECT = "reinject-fail"
KIND_DISPATCHER_DEAD = "dispatcher-dead"

_UNTRUSTED = "---- 不可信原文 ----"
_RAW_MAX = 500
# 活动面＝探活与 last_seen 的任务集（verify_failed/reopened 归在飞，同 task 口径）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}
_IN_FLIGHT = {"dispatched", "running"}  # 自愈重注入仅限 worker 在跑的两态

_KIND_NOTE = {
    KIND_QUALITY: "同一任务连续 3 次质量失败，已自动 blocked",
    KIND_GATEFILES: "任务 diff 触及闸体（构建/测试配置）",
    KIND_REINJECT: "下级会话死亡且重注入失败",
    KIND_DISPATCHER_DEAD: "派发方会话死亡，其在飞子链失管",
}


class Health:
    """tick 由 main 定时线程驱动；单写者口径（httpd 串行调用）。"""

    def __init__(self, state, oc, router, recover, main_sid, store):
        self._state, self._oc, self._router = state, oc, router
        self._recover, self._main_sid = recover, main_sid
        self._store = store

    def tick(self):
        """探每个活动任务的直接下跳：死→自愈/上报；只管死，不判僵。"""
        tasks = self._state.snapshot()["tasks"]
        for tid, t in tasks.items():
            wsid = t.get("worker_sid")
            if t["state"] not in _ACTIVE or not wsid:
                continue
            try:
                alive = self._oc.alive(wsid)
            except Exception:  # oc 不可达≠会话死：本轮免判，勿误杀
                continue
            if alive:
                continue
            if any(x.get("dispatcher_sid") == wsid and x["state"] in _IN_FLIGHT
                   for x in tasks.values()):
                self.escalate(KIND_DISPATCHER_DEAD, tid)  # 死会话管着在飞链——拍人不代管
            elif t["state"] in _IN_FLIGHT and not self._recover.reinject(tid):
                self.escalate(KIND_REINJECT, tid)

    def touch(self, sid):
        """last_seen 刷新入口：worker/dispatcher 每次调用服务时由 httpd 调。"""
        for tid, t in self._state.snapshot()["tasks"].items():
            if sid in (t.get("worker_sid"), t.get("dispatcher_sid")):
                self._state.set_fields(tid, last_seen=time.time())

    def escalate(self, kind, tid, raw=None):
        """失控拍人：正文服务权威合成；raw 为任务/worker 供给文本（不可信）。"""
        t = self._state.get(tid)
        body = (f"失控[{kind}] 任务{tid}：{_KIND_NOTE[kind]}；"
                f"状态 {t['state']}，worker={t.get('worker_sid') or '-'}，"
                f"dispatcher={t.get('dispatcher_sid') or '-'}，"
                f"连败 {t.get('fail_streak', 0)}")
        if raw:
            flat = " ".join(str(raw).split())  # 拍平换行：供给文本不得伪造服务合成行
            body += f"\n{_UNTRUSTED}\n{flat[:_RAW_MAX]}"
        root = self._root(tid)
        rt = self._state.get(root)
        self._store.append_event({"ts": time.time(), "actor": "health",
                                  "event": "escalate", "task": tid, "kind": kind})
        self._router.post(rt.get("worker_sid"), ("up", root), "escalate", body)

    def _root(self, tid):
        """沿 dispatcher_sid 上行至根任务（根 dispatcher＝main_sid，装配保证）。"""
        tasks = self._state.snapshot()["tasks"]
        seen = set()
        while (tid not in seen
               and tasks.get(tid, {}).get("dispatcher_sid") != self._main_sid):
            seen.add(tid)
            parent = next((p for p, x in tasks.items()
                           if x.get("worker_sid")
                           == tasks[tid].get("dispatcher_sid")), None)
            if parent is None:
                break  # 链头不在册（main 缺位）——就地升级到已达的最上级
            tid = parent
        return tid
