"""装配容器（R2）：字段即依赖图——main 独占构造，transport 只 import 本模块。"""
import time
from dataclasses import dataclass

EV_MAIN_BOUND = "main_sid_bound"
EV_WORKER_REBOUND = "worker_rebound"

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
        """
        if caller != sid:
            if any(t.get("worker_sid") == caller
                   for t in self.state.snapshot()["tasks"].values()):
                return
            self.auth.rebind(sid, caller)
        if self.main_sid != caller:
            self.main_sid = caller
            self.store.append_event({"ts": time.time(), "actor": caller,
                                     "event": EV_MAIN_BOUND})

    def bind_worker(self, sid, caller):
        """gt-d55b9e：worker 牌换号自愈——持牌即身份，漂移即改绑（镜像 bind_main）。

        sid＝牌注册 sid（dispatch 时落行的 worker_sid），caller＝X-Agora-Sid
        真实续接会话。注册 sid 是某活动任务 worker_sid 且两者漂移 → rebind
        （旧 sid 入血统，token 本体不变）＋任务行 worker_sid 同步覆写（与
        recover.reinject 同口径）。守卫：终态/无主 sid 一律不动——worker 改绑
        只动 worker 牌，主会话/他牌不受累。返回改绑后 sid（调用方刷新身份）。
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
        return caller
