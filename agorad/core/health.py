"""健康探活与升级拍人（F5，设计 §4.4）：定时探死＋边沿判僵、失控实时拍主会话。

tick 只探活动任务的直接下级会话（无全局巡检）；死下级→recover.reinject 自愈
（静默），失败→升级；死会话若正管着在飞链（dispatcher）→不代管、直接拍人。
边沿判僵（F5 决策记录 2026-10-28，只提醒不处置）：tick 顺带按 doctor 同口径
（20min 无活动，report.stale_candidates 构造注入）算僵态候选，新进候选集→
stalled 升级＋可选邮件（email.json opt-in）；离开候选集复位，oc 不可达轮次
免判；"僵"的处置仍归人工。升级正文由服务权威合成；任务/worker 供给文本以
"不可信原文"段携带（拍平换行，防伪造服务行）。escalate 沿 dispatcher_sid
上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）经 router post——
落库先行，注入即视为拍到；escalate 同时落 events.jsonl 审计行（F8）。
core 只 import stdlib，依赖全注入。
"""
import json
import shlex
import subprocess
import time

# 升级事件类别（R9 初始集；gate 的质量连败/闸体变更引用此收口）
KIND_QUALITY = "quality"
KIND_GATEFILES = "gatefiles"
KIND_REINJECT = "reinject-fail"
KIND_DISPATCHER_DEAD = "dispatcher-dead"
KIND_STALLED = "stalled"

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
    KIND_STALLED: "在飞超 20min 无活动（僵态候选），请人工关注",
}


def _subprocess_runner(cmd, input_text):
    """默认邮件外呼：正文走 stdin，30s 超时；非零退出/超时抛异常由调用方吞。"""
    r = subprocess.run(cmd, input=input_text, text=True, timeout=30,
                       capture_output=True)
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r


class Health:
    """tick 由 main 定时线程驱动；单写者口径（httpd 串行调用）。"""

    def __init__(self, state, oc, router, recover, app, store,
                 runner=None, stale_candidates=None):
        """app 按引用注入：main_sid 是活绑定（R25-A），escalate 每次取当前值。

        stale_candidates＝report.stale_candidates（core 禁兄弟 import，构造注入）；
        runner＝邮件外呼缝（gate 同款），缺省真 subprocess。
        """
        self._state, self._oc, self._router = state, oc, router
        self._recover, self._app = recover, app
        self._store = store
        self._runner = runner or _subprocess_runner
        self._stale_candidates = stale_candidates
        self._stale_seen = set()  # 僵态候选边沿记忆（内存态，崩溃回退＝重拍一次）

    def tick(self):
        """探每个活动任务的直接下跳：死→自愈/上报；边沿判僵只提醒不处置。"""
        tasks = self._state.snapshot()["tasks"]
        oc_down = False
        for tid, t in tasks.items():
            wsid = t.get("worker_sid")
            if t["state"] not in _ACTIVE or not wsid:
                continue
            try:
                alive = self._oc.alive(wsid)
            except Exception:  # oc 不可达≠会话死：本轮免判（含僵态），勿误杀
                oc_down = True
                continue
            if alive:
                continue
            if any(x.get("dispatcher_sid") == wsid and x["state"] in _IN_FLIGHT
                   for x in tasks.values()):
                self.escalate(KIND_DISPATCHER_DEAD, tid)  # 死会话管着在飞链——拍人不代管
            elif t["state"] in _IN_FLIGHT and not self._recover.reinject(tid):
                self.escalate(KIND_REINJECT, tid)
        if not oc_down:
            self._stale_scan(tasks)

    def _stale_scan(self, tasks):
        """边沿判僵（F5 决策记录 2026-10-28）：新进候选集→stalled 拍人。

        _stale_seen 每轮同步为当前候选集（离开即移除，之后再僵再拍）。
        """
        if self._stale_candidates is None:
            return
        cand = self._stale_candidates({"tasks": tasks}, time.time())
        for tid, _ in cand:
            if tid not in self._stale_seen:  # 边沿：在候选集而不在记忆集
                self.escalate(KIND_STALLED, tid)
                self._stale_seen.add(tid)  # 拍到才记：escalate 失败下轮重试不丢报
        self._stale_seen &= {tid for tid, _ in cand}  # 离开候选集即移除（复位再拍）

    def _notify_email(self, tid, body_text):
        """可选邮件外呼（F5 决策记录 2026-10-28，opt-in）：未配置＝零行为变化。

        email.json 缺失/缺字段→静默跳过；失败/超时吞异常（邮件尽力而为），
        成败各落一行审计事件。
        """
        try:
            cfg = json.loads((self._store.home / ".agora" / "email.json")
                             .read_text(encoding="utf-8"))
            to, command = cfg["to"], cfg["command"]
        except Exception:
            return
        try:
            self._runner(shlex.split(command), body_text)
            event = "email_sent"
        except Exception:
            event = "email_failed"
        # shortcut: 逐任务同步外呼，批量同僵最坏 N×30s 阻塞 tick 线程；规模化再聚合单邮件
        self._store.append_event({"ts": time.time(), "actor": "health",
                                  "event": event, "task": tid, "to": to})

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
        if kind == KIND_STALLED:  # 僵态邮件例外（§6 决策记录）：仅此 kind 外呼
            self._notify_email(tid, body)

    def _root(self, tid):
        """沿 dispatcher_sid 上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）。"""
        tasks = self._state.snapshot()["tasks"]
        seen = set()
        while (tid not in seen
               and tasks.get(tid, {}).get("dispatcher_sid") != self._app.main_sid):
            seen.add(tid)
            parent = next((p for p, x in tasks.items()
                           if x.get("worker_sid")
                           == tasks[tid].get("dispatcher_sid")), None)
            if parent is None:
                break  # 链头不在册（main 缺位）——就地升级到已达的最上级
            tid = parent
        return tid
