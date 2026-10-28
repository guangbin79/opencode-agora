"""装配容器（R2）：字段即依赖图——main 独占构造，transport 只 import 本模块。"""
import time
from dataclasses import dataclass

EV_MAIN_BOUND = "main_sid_bound"
EV_WORKER_REBOUND = "worker_rebound"
EV_DISPATCHER_REBOUND = "dispatcher_rebind"

# worker 换号自愈口径活动态（词汇对齐 task._ACTIVE；core 禁兄弟 import 故字面量本地定义）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}


@dataclass
class App:
    """聚合根：core 服务＋注入的 access 件＋装配元数据＋换号自愈绑定面（R25-A/gt-d55b9e）。"""
    store: object
    state: object
    auth: object
    router: object
    task: object
    peer: object
    gate: object
    health: object
    recover: object
    report: object
    oc: object
    gitx: object
    main_sid: str

    def bind_main(self, sid, caller):
        """R25-A：root 牌调用登记主会话真实 sid（多主会话最近活跃覆盖）。

        sid＝牌当前 sid（占位 "main" 或重启前旧绑定），caller＝X-Agora-Sid 真实
        会话——改绑牌使后续 task 行 dispatcher 落真实 sid，拍人链边直达真会话；
        重启后 main_sid 复位与牌不同步亦经此自愈。任务 worker 边会话拒绝绑定：
        worker 凭据缺失回落 root 牌（B1）＋携 sid 属误附，不得抢主会话绑定。
        返回对齐后 sid（拒绑＝原 sid）——本笔请求后续归属判定即以新 sid 走。
        """
        if caller != sid:
            if any(t.get("worker_sid") == caller
                   for t in self.state.snapshot()["tasks"].values()):
                return sid
            self.auth.rebind(sid, caller)
        if self.main_sid != caller:
            self.main_sid = caller
            self.store.append_event({"ts": time.time(), "actor": caller,
                                     "event": EV_MAIN_BOUND})
        return caller

    def bind_worker(self, sid, caller):
        """gt-d55b9e：worker 牌换号自愈——持牌即身份，漂移即改绑（镜像 bind_main）。

        sid＝牌注册 sid（dispatch 时落行的 worker_sid），caller＝X-Agora-Sid
        真实续接会话。注册 sid 是某活动任务 worker_sid 且两者漂移 → rebind
        （旧 sid 入血统，token 本体不变）＋任务行 worker_sid 同步覆写（与
        recover.reinject 同口径）。改绑波及 authorship（gt-83185d R2）：worker
        作为派发方的活动嵌套子任务行 dispatcher_sid 一并覆写为 caller——
        嵌套任务所有权在换号后保持，不被归属终审 dispatcher_sid==caller 卡死。
        守卫：终态/无主 sid 一律不动；worker_sid 行与 dispatcher_sid 行分别
        覆写对应字段不串；已占 caller sid 拒绑（下）原样——worker 改绑只动
        worker 牌，主会话/他牌不受累。返回改绑后 sid（调用方刷新身份）。
        """
        if caller == sid:
            return sid
        tasks = self.state.snapshot()["tasks"]
        # 已占 sid 不抢绑（镜像 bind_main 守卫）：X-Agora-Sid 是自证头，worker 可从
        # 任务页读到他任务 sid——caller 已是主会话/任一任务 dispatcher/worker 即拒
        # （防跨任务冒充；真续接 sid 全新必不撞）。
        if caller == self.main_sid or any(
                caller in (t.get("worker_sid"), t.get("dispatcher_sid"))
                for t in tasks.values()):
            return sid
        hit = [tid for tid, t in tasks.items()
               if t.get("worker_sid") == sid and t["state"] in _ACTIVE]
        if not hit:
            return sid
        self.auth.rebind(sid, caller)
        for tid in hit:
            self.state.set_fields(tid, worker_sid=caller)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_WORKER_REBOUND, "task": tid,
                                     "from": sid, "to": caller})
        # R2 波及：嵌套子任务行只覆写 dispatcher_sid 字段（worker_sid 行不串）
        nested = [tid for tid, t in tasks.items()
                  if t.get("dispatcher_sid") == sid and t["state"] in _ACTIVE]
        for tid in nested:
            self.state.set_fields(tid, dispatcher_sid=caller)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_WORKER_REBOUND, "task": tid,
                                     "from": sid, "to": caller})
        return caller

    def bind_dispatcher(self, role, sid, t):
        """F21：dispatcher 侧换号改绑（镜像 bind_worker，补壳重启死号缺口）——
        root 牌 post 命中「非链边」前判愈合：登记 dispatcher_sid ≠ 调用者且该
        会话已不存在（oc 存在性检查）→ auth.rebind（旧号入血统）＋活动任务行
        dispatcher_sid 同步覆写＋dispatcher_rebind 审计（含新旧 sid 与任务），
        本笔 post 按链边放行。守卫（不满足保持既有拒绝）：非 root 牌、活号
        （防活号互抢）、任务终态（冻结不动）、oc 不可达≠死（recover 同口径免判）。"""
        d = t.get("dispatcher_sid")
        if role != "root" or not d or d == sid:
            return
        tasks = self.state.snapshot()["tasks"]
        hit = [tid for tid, x in tasks.items()
               if x.get("dispatcher_sid") == d and x["state"] in _ACTIVE]
        if not hit:
            return  # 终态不触发改绑
        # 已占 sid 不抢绑（镜像 bind_worker 防跨任务冒充；真续接号全新必不撞；
        # main_sid 不在守卫内：root 牌对齐后 sid==main_sid 是常态非冒充）。
        if any(sid in (x.get("worker_sid"), x.get("dispatcher_sid"))
               for x in tasks.values()):
            return
        try:
            if self.oc.alive(d):
                return  # 登记会话仍活不抢绑
        except Exception:  # oc 不可达≠会话死：本轮免判（recover.startup 同口径）
            return
        self.auth.rebind(d, sid)
        for tid in hit:
            if self.state.get(tid)["state"] not in _ACTIVE:
                continue  # 探活窗内门体或已结案：终态行冻结不覆写（TOCTOU 复检）
            self.state.set_fields(tid, dispatcher_sid=sid)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_DISPATCHER_REBOUND,
                                     "task": tid, "from": d, "to": sid})
