"""健康探活与升级拍人（F5，设计 §4.4）：定时探死＋边沿判僵、失控实时拍主会话。

tick 只探活动任务的直接下级会话（无全局巡检）；死下级→recover.reinject 自愈
（静默），失败→升级。父 worker 若正管着在飞链也优先重注入，并同步改绑嵌套
子任务；只有无法重注入的失管 dispatcher 才直接拍人。
worker 凭据自愈已推改拉（F25 修正）：inject_silent 推送通道证伪（v2.2.0
synthetic 不触发 prompt 钩子），改由插件 hook 缓存未命中经 /cred_for_sid 拉取。
边沿判僵（F5 决策记录 2026-10-28，只提醒不处置）：tick 顺带按 doctor 同口径
（20min 无活动，report.stale_candidates 构造注入）算僵态候选，新进候选集→
stalled 升级＋可选邮件（email.json opt-in）；离开候选集复位，oc 不可达轮次
免判；"僵"的处置仍归人工。升级正文由服务权威合成；任务/worker 供给文本以
"不可信原文"段携带（拍平换行，防伪造服务行）。escalate 沿 dispatcher_sid
上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）经 router post——
落库先行，注入即视为拍到（stalled 例外：synthetic 免通知通道，不弹窗）；
escalate 同时落 events.jsonl 审计行（F8）。
core 只 import stdlib，依赖全注入。
"""
import json
import shlex
import subprocess
import time
from pathlib import Path

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
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r


class Health:
    """tick 由 main 定时线程驱动；单写者口径（httpd 串行调用）。"""

    def __init__(self, state, oc, router, recover, app, store,
                 runner=None, stale_candidates=None, env_activity=None,
                 draft_writer=None):
        """app 按引用注入：main_sid 是活绑定（R25-A），escalate 每次取当前值。

        stale_candidates＝report.stale_candidates（core 禁兄弟 import，构造注入）；
        env_activity＝report._env_activity（F59 同款构造注入）；draft_writer＝
        pages.write_stalled_draft（F59 草稿页写面）；runner＝邮件外呼缝（gate
        同款），缺省真 subprocess。"""
        self._state, self._oc, self._router = state, oc, router
        self._recover, self._app = recover, app
        self._store = store
        self._runner = runner or _subprocess_runner
        self._stale_candidates = stale_candidates
        self._env_activity = env_activity
        self._draft_writer = draft_writer
        self._stale_seen = set()  # 僵态候选边沿记忆（内存态，崩溃回退＝重拍一次）

    def tick(self):
        """探每个活动任务的直接下跳：死→自愈/上报；边沿判僵只提醒不处置。"""
        tasks = self._state.snapshot()["tasks"]
        oc_down = False
        busy = set()  # updated>idle＝回合进行中：僵态本轮免判（doctor 口径减 busy 免决）
        for tid, t in tasks.items():
            wsid = t.get("worker_sid")
            if t["state"] not in _ACTIVE or not wsid:
                continue
            try:
                data = self._oc.session(wsid)
            except Exception:  # oc 不可达≠会话死：本轮免判（含僵态），勿误杀
                oc_down = True
                continue
            if data is None:
                manages = any(x.get("dispatcher_sid") == wsid
                              and x["state"] in _IN_FLIGHT for x in tasks.values())
                if t["state"] in _IN_FLIGHT:
                    if self._recover.reinject(tid):
                        continue  # 父 worker 一并恢复；recover 同步改绑嵌套子任务
                    self.escalate(KIND_DISPATCHER_DEAD if manages else KIND_REINJECT, tid)
                elif manages:
                    self.escalate(KIND_DISPATCHER_DEAD, tid)
            else:
                tm = data.get("time") or {}  # 缺形状→不 busy→照判（安全回退）
                if (tm.get("updated") or 0) > (tm.get("idle") or 0):
                    busy.add(tid)
        if not oc_down:
            self._stale_scan(tasks, busy)

    def _stale_scan(self, tasks, busy):
        """边沿判僵（F5 决策记录 2026-10-28）：新进候选集→stalled 拍人。

        免判口径（二批）：busy（updated>idle，回合进行中）本轮从候选集剔除；
        离开 busy 再超阈重新边沿触发。_stale_seen 每轮同步为当前候选集
        （离开即移除，之后再僵再拍）。
        """
        if self._stale_candidates is None:
            return
        cand = [(tid, b) for tid, b
                in self._stale_candidates({"tasks": tasks}, time.time())
                if tid not in busy]
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
        """last_seen 刷新入口：仅 worker 服务调用刷——dispatcher 查看≠worker
        干活，查看不续命（二批决策；018676 真僵晚拍 40min 实证）。"""
        for tid, t in self._state.snapshot()["tasks"].items():
            if sid == t.get("worker_sid"):
                self._state.touch_seen(tid)  # 心跳只进内存：快照仅随真实迁移追加

    def _last_activity(self, sid):
        """末次活动摘要（F52）：role＋tool 名/标题＋距今秒；失败/缺失→None
        （尽力而为，同 email 口径：获取吞掉照常拍人）。"""
        try:
            m = self._oc.last_message(sid)
            if not m:
                return None
            role = m.get("role") or m.get("type") or "?"  # 2.2.2 落 type，旧版 role 兼容
            tool = ""
            for p in m.get("content") or []:
                if isinstance(p, dict) and p.get("type") == "tool" and p.get("name"):
                    title = (p.get("state") or {}).get("title")
                    tool = f"{p['name']}/{title}" if title else p["name"]
                    break
            created = (m.get("time") or {}).get("created") or 0
            age = max(0, int(time.time() * 1000 - created) // 1000) if created else None
            note = f"末次活动 {role}" + (f" tool={tool}" if tool else "") \
                + (f" 距今{age}s" if age is not None else "")
            return " ".join(note.split())  # 摘要含供给文本（tool 名/标题）：同 raw 口径拍平防伪造服务行
        except Exception:
            return None

    def _stalled_evidence(self, tid, t, note):
        """卡住证据包（F59）：只收事实读数零根因结论；单项取不到整项省略。

        返回（证据行集, 草稿页相对路径）；草稿写面失败→路径 None——
        降级纪律：拍人/草稿互不阻塞，通知照发。"""
        ev = {"tid": tid, "state": t["state"]}
        if t.get("model"):
            ev["model"] = t["model"]  # F55 字段，缺席省
        if t.get("last_seen"):
            ev["last_seen_age_s"] = max(0, int(time.time() - t["last_seen"]))
        if t.get("worker_sid"):
            try:  # busy＝F28 判据 updated>idle；oc 不可达省
                tm = (self._oc.session(t["worker_sid"]) or {}).get("time") or {}
                ev["busy"] = (tm.get("updated") or 0) > (tm.get("idle") or 0)
            except Exception:
                pass
        latest = self._env_activity(t.get("env_dir")) if self._env_activity else 0.0
        if latest > 0.0:  # env 缺席/被回收（活性 0.0）省
            ev["env_active_seconds_ago"] = max(0, int(time.time() - latest))
        vstate = (t.get("verify") or {}).get("state")
        if vstate:
            ev["verify_state"] = vstate
        ev["fail_streak"] = t.get("fail_streak", 0)
        if note:
            ev["last_msg"] = note  # F52 摘要原样并入，不重复取
        lines = [f"{k}: {' '.join(str(v).split())}" for k, v in ev.items()]
        draft = None
        if self._draft_writer:
            try:
                page = self._draft_writer(self._store.home, tid,
                                          t.get("dispatcher_sid"), lines)
                draft = str(Path(page).relative_to(self._store.home))
            except Exception:
                pass  # 草稿照写失败不阻塞拍人
        return lines, draft

    def escalate(self, kind, tid, raw=None):
        """失控拍人：正文服务权威合成；raw 为任务/worker 供给文本（不可信）。

        stalled 走免通知通道（synthetic，落转录不弹窗，gt-5bc312 用户裁定），
        其余 kind 照常会话注入；stalled 边沿附末次活动摘要（F52，失败降级）
        ＋证据包小节与草稿页路径（F59，降级纪律：证据永不阻塞拍人）。"""
        t = self._state.get(tid)
        body = (f"失控[{kind}] 任务{tid}：{_KIND_NOTE[kind]}；"
                f"状态 {t['state']}，worker={t.get('worker_sid') or '-'}，"
                f"dispatcher={t.get('dispatcher_sid') or '-'}，"
                f"连败 {t.get('fail_streak', 0)}")
        note = None
        if kind == KIND_STALLED and t.get("worker_sid"):
            note = self._last_activity(t["worker_sid"])  # 加一次取末条（F52 裁定）
            if note:
                body += f"；{note}"
        if kind == KIND_STALLED:  # F59 证据包：逐行 key: value，缺失项整行省略
            lines, draft = self._stalled_evidence(tid, t, note)
            if lines:
                body += "\n证据包\n" + "\n".join(lines)
            if draft:
                body += f"\n草稿: {draft}"
        if raw:
            flat = " ".join(str(raw).split())  # 拍平换行：供给文本不得伪造服务合成行
            body += f"\n{_UNTRUSTED}\n{flat[:_RAW_MAX]}"
        root = self._root(tid)
        rt = self._state.get(root)
        self._store.append_event({"ts": time.time(), "actor": "health",
                                  "event": "escalate", "task": tid, "kind": kind})
        self._router.post(rt.get("worker_sid"), ("up", root), "escalate", body,
                          silent=(kind == KIND_STALLED))  # stalled 免通知通道：不弹窗（gt-5bc312）
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
