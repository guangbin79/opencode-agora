"""装配容器（R2）：字段即依赖图——main 独占构造，transport 只 import 本模块。"""
import time
from dataclasses import dataclass

EV_MAIN_BOUND = "main_sid_bound"
EV_WORKER_REBOUND = "worker_rebound"
EV_DISPATCHER_REBOUND = "dispatcher_rebind"

# worker 换号自愈口径活动态（词汇对齐 task._ACTIVE；core 禁兄弟 import 故字面量本地定义）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}
_NESTED_OPEN = _ACTIVE | {"todo"}  # 已建未派子任务同样保留父 worker 所有权


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
        """root 牌身份钉死 daemon 身份（ag-f80309 B 案，替代 R25-A 活绑定 rebind）：
        携 X-Agora-Sid 的 root 调用只更新 main_sid 活绑定（多主会话最近活跃覆盖），
        牌面账本 sid 零改绑——污染源（每调用 rebind 会话 sid）就此根除。任务行归属
        改由 httpd 传对齐 sid 记账（TaskService actor_sid），链边按记录值判定。
        任务 worker 边会话拒绝绑定：worker 凭据缺失回落 root 牌（B1）＋携 sid 属
        误附，不得抢主会话绑定。返回本笔归属 sid（拒绑＝牌面 sid）。
        """
        if caller == sid:
            return sid
        if any(t.get("worker_sid") == caller
               for t in self.state.snapshot()["tasks"].values()):
            return sid
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
        # 快照后门线程可能已结案：零活动行不改绑（TOCTOU 复检，镜像 bind_dispatcher）
        if not any(self.state.get(tid)["state"] in _ACTIVE for tid in hit):
            return sid
        self.auth.rebind(sid, caller)
        for tid in hit:
            if self.state.get(tid)["state"] not in _ACTIVE:
                continue  # 改绑窗内结案：终态行冻结不覆写（TOCTOU 复检）
            self.state.set_fields(tid, worker_sid=caller)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_WORKER_REBOUND, "task": tid,
                                     "from": sid, "to": caller})
        # R2 波及：嵌套子任务行只覆写 dispatcher_sid 字段（worker_sid 行不串）
        nested = [tid for tid, t in tasks.items()
                  if t.get("dispatcher_sid") == sid and t["state"] in _NESTED_OPEN]
        for tid in nested:
            if self.state.get(tid)["state"] not in _NESTED_OPEN:
                continue  # 同上：终态行冻结不覆写（TOCTOU 复检）
            self.state.set_fields(tid, dispatcher_sid=caller)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_WORKER_REBOUND, "task": tid,
                                     "from": sid, "to": caller})
        return caller

    def bind_dispatcher(self, role, sid, t):
        """F21：dispatcher 侧换号改绑（镜像 bind_worker，补壳重启死号缺口）——
        root 牌 post 命中「非链边」前判愈合：登记 dispatcher_sid ≠ 调用者且该
        会话已不存在（oc 存在性检查）→ auth.rebind 尽力而为挪牌（旧号入血统；
        死号名下零牌＝无从挪——ag-f80309 B 案起 root 直连窗无窗口牌属常态，
        血统兜底对其不适用：会话邮件走 cred_issue 每窗 dispatcher 牌，F16 链
        经本愈合照续）＋活动任务行 dispatcher_sid 同步覆写＋dispatcher_rebind
        审计（含新旧 sid 与任务），本笔 post 按链边放行。守卫（不满足保持既有
        拒绝）：非 root 牌、活号（防活号互抢）、任务终态（冻结不动）、daemon
        身份行（ag-f80309 B 案：dispatcher_sid="root" 归 daemon 恒不愈合——
        rebind 不得回流触碰 root 牌）、oc 不可达≠死（recover 同口径免判）。"""
        d = t.get("dispatcher_sid")
        if role != "root" or not d or d == sid or d == "root":  # daemon 行恒不愈合
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
        # 探活窗内可能已全数结案：零活动行不改牌（TOCTOU 复检——rebind 是
        # 持久身份换绑，无活动行即无消费方，凭陈旧快照换牌＝无谓身份漂移）
        if not any(self.state.get(tid)["state"] in _ACTIVE for tid in hit):
            return
        self.auth.rebind(d, sid)
        for tid in hit:
            if self.state.get(tid)["state"] not in _ACTIVE:
                continue  # 探活窗内门体或已结案：终态行冻结不覆写（TOCTOU 复检）
            self.state.set_fields(tid, dispatcher_sid=sid)
            self.store.append_event({"ts": time.time(), "actor": sid,
                                     "event": EV_DISPATCHER_REBOUND,
                                     "task": tid, "from": d, "to": sid})
