"""装配容器（R2）：字段即依赖图——main 独占构造，transport 只 import 本模块。"""
import time
from dataclasses import dataclass

EV_MAIN_BOUND = "main_sid_bound"


@dataclass
class App:
    """聚合根：core 服务＋注入的 access 件＋装配元数据（main_sid/root 之外零逻辑）。"""
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
