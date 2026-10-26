"""崩溃恢复协调（F6，设计 §4.6）：启动复查重注入、死 worker 自愈原语。

startup：State 构造已恢复末行快照，此处对 in-flight 任务探活，仅
dispatched/running 重建 worker 会话（终态/blocked/verify_failed 等续修态
复查后一律不注入）。reinject：oc.create 同 BOOT 语义（任务页＋环境＋行为
合同），新 sid 覆写 worker_sid；凭据不动——牌独立持久化、identify 仍有效；
审计一行 reinjected。任何失败返回 False 不抛：先 create 成功才覆写行，
不半写。core 只 import stdlib，依赖全构造注入（store 供任务页路径与审计）。
"""
import time

_REINJECTABLE = {"dispatched", "running"}

EV_REINJECTED = "reinjected"


class Recover:
    """startup 由 main 装配期调用一次；reinject 供 health.tick 与启动共用。"""

    def __init__(self, state, oc, gitx, store):
        self._state, self._oc, self._gitx = state, oc, gitx
        self._store = store

    def startup(self):
        for tid, t in self._state.snapshot()["tasks"].items():
            if t["state"] not in _REINJECTABLE or not t.get("worker_sid"):
                continue
            try:
                alive = self._oc.alive(t["worker_sid"])
            except Exception:  # oc 不可达≠会话死：本轮免判，tick 再兜
                continue
            if not alive:
                self.reinject(tid)

    def reinject(self, tid) -> bool:
        """死 worker 重建：成功 True；复查不通过/创建失败一律 False。"""
        try:
            t = self._state.get(tid)
        except KeyError:
            return False
        if t["state"] not in _REINJECTABLE or not t.get("env_dir"):
            return False  # 复查：终态/blocked/verify_failed(等续修) 不重注入
        try:
            self._gitx.head(t["env_dir"])  # 环境在册探针：worktree 已失不盲注
            wsid = self._oc.create(t["env_dir"], self._boot(tid, t))
        except Exception:
            return False
        self._state.set_fields(tid, worker_sid=wsid)  # 新 sid 覆写；凭据不动
        self._store.append_event({"ts": time.time(), "actor": "agorad",
                                  "event": EV_REINJECTED, "task": tid,
                                  "worker": wsid})
        return True

    def _boot(self, tid, t):
        page = self._store.home / "wiki" / "tasks" / f"{tid}.md"
        return (f"[agora] BOOT task {tid} (reinject)\npage: {page}\n"
                f"env: {t['env_dir']}\n"
                "contract: 会话曾死亡已重建——先 check_mail 补拉断点上下文，"
                "继续任务页目标；结论经 post(done) 回填")
