"""任务生命周期（F2/F3/F11）：建/派/中止/verify 登记。

鉴权两段（R18）：auth.may 角色级粗判＋dispatcher_sid==caller 归属细判归本服务。
保险丝四件（F3）：链深<5、并发配额（活动任务计数）、can_spawn（oc 名册——会话
创建不可达即拒）、B7 after 前驱终态。中止级联下级（平表：子任务
dispatcher_sid==上级 worker_sid）、终止通知走 router.post、凭据作废、环境即回收、
merged 不动、空链幂等。core 只 import stdlib；依赖全构造注入。
"""
import pathlib
import secrets
import time

_MAX_DEPTH = 5
_QUOTA = 8  # 并发配额默认（护栏默认值，②期核定）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}
_AFTER_OK = {"merged", "closed", "cancelled"}  # B7 前驱终态（含 merged）
_FROZEN = {"cancelled", "closed", "merged"}    # cancel 不再触碰（merged 不逆转历史）

EV_CREATED = "task_created"
EV_DISPATCHED = "dispatched"
EV_CANCELLED = "cancelled"
EV_VERIFY = "verify_registered"
EV_DENIED = "auth_denied"
EV_REJECTED = "dispatch_rejected"


class Rejected(Exception):
    """task 域拒绝（鉴权/保险丝/前置校验）——报因在消息。"""


class TaskService:
    """单写者串行调用；无锁无线程（同 Router 口径）。"""

    def __init__(self, state, auth, gitx, oc, store, router, pages):
        self._state, self._auth, self._gitx, self._oc = state, auth, gitx, oc
        self._store, self._router, self._pages = store, router, pages

    # ---- F2 建任务 ----

    def create(self, goal, project_dir, after, actor_token, title=None):
        sid, _ = self._gate(actor_token, "task_create", None, None)
        tid = self._new_tid()
        if after is not None:
            try:
                self._state.get(after)
            except KeyError:
                self._reject(tid, sid, f"after: 前任务 {after} 不存在")
        depth = 1 + max((t["chain_depth"] for t in self._state.snapshot()["tasks"].values()
                         if t.get("worker_sid") == sid), default=0)
        tid = self._new_tid()
        self._state.add(tid, {
            "title": (title or goal.splitlines()[0][:40]) if (title or goal) else tid, "goal": goal,
            "project_dir": project_dir, "env_dir": None, "after": after,
            "verify": {"cmd": "", "state": "pending", "registered_by": None,
                       "registered_at": None},
            "state": "todo", "dispatcher_sid": sid, "worker_sid": None,
            "chain_depth": depth, "fail_streak": 0, "last_seen": None,
            "created": time.time(), "updated": time.time()})
        self._pages(self._store.home, tid, self._state.get(tid))
        self._store.append_event({"ts": time.time(), "actor": sid,
                                  "event": EV_CREATED, "task": tid})
        return tid

    # ---- F3 派发 ----

    def dispatch(self, tid, dispatcher_token):
        t = self._state.get(tid)
        sid, _ = self._gate(dispatcher_token, "task_dispatch", tid, t,
                            owner=t.get("dispatcher_sid"))
        if t["state"] not in ("todo", "blocked"):
            self._reject(tid, sid, f"{t['state']} 不可派发（仅 todo/blocked）")
        if t["chain_depth"] >= _MAX_DEPTH:
            self._reject(tid, sid, f"chain_depth>={_MAX_DEPTH}")
        active = sum(1 for x in self._state.snapshot()["tasks"].values()
                     if x["state"] in _ACTIVE)
        if active >= _QUOTA:
            self._reject(tid, sid, f"quota: 活动任务 {active}>={_QUOTA}")
        after = t.get("after")
        if after is not None and self._state.get(after)["state"] not in _AFTER_OK:
            self._reject(tid, sid, f"after: 前任务 {after} 未终态（B7）")
        env = t.get("env_dir") or self._gitx.build_env(t["project_dir"], tid)
        try:
            wsid = self._oc.create(env, self._boot(tid, env))
        except Exception as e:  # can_spawn 名册：会话创建失败即熔断（不 import access 异常）
            if not t.get("env_dir"):
                self._gitx.remove_env(env)
            self._reject(tid, sid, f"can_spawn: {e}")
        self._revoke_worker(t.get("worker_sid"))  # 重派先废旧牌再签新牌
        token = self._auth.issue(wsid, "worker")
        self._state.transition(tid, "dispatched", worker_sid=wsid, env_dir=env,
                               dispatcher_sid=sid)
        self._pages(self._store.home, tid, self._state.get(tid))
        self._store.append_event({"ts": time.time(), "actor": sid,
                                  "event": EV_DISPATCHED, "task": tid, "worker": wsid})
        text = f"[agora] credential {tid}\ntoken: {token}"
        try:
            ok = self._oc.inject(wsid, text)
        except Exception:  # oc 抖动同死会话口径：走落库兜底
            ok = False
        if not ok:  # 直投失败落库兜底：worker check_mail 补拉（§4.8 行先行后消息）
            self._router.post(sid, ("down", tid), "credential", text)
        return token

    # ---- F11 中止 ----

    def cancel(self, tid, actor_token):
        t = self._state.get(tid)
        sid, _ = self._gate(actor_token, "task_cancel", tid, t,
                            owner=t.get("dispatcher_sid"))
        n = self._cancel_one(tid, sid)
        self._store.append_event({"ts": time.time(), "actor": sid, "event": EV_CANCELLED,
                                  "task": tid, "note": f"cancelled={n}" if n else "noop"})
        return n

    def _cancel_one(self, tid, sid):
        """中止单任务并递归下级；返回级联取消数（merged/closed/cancelled 不动）。"""
        t = self._state.get(tid)
        total = 0
        if t["state"] not in _FROZEN:
            if not self._state.queue_cancel(tid):  # done 在门内：原子排队，门尾消费
                self._state.transition(tid, "cancelled")
                if t.get("worker_sid"):  # 快照行先行后消息（§4.8 写序）
                    self._router.post(t["dispatcher_sid"], ("down", tid), "cancel",
                                      f"task {tid} cancelled by {sid}")
                    self._revoke_worker(t["worker_sid"])
                if t.get("env_dir"):
                    self._gitx.remove_env(t["env_dir"])
                total = 1
        kids = [k for k, x in self._state.snapshot()["tasks"].items()
                if x.get("dispatcher_sid") == t.get("worker_sid")]
        return total + sum(self._cancel_one(k, sid) for k in kids)

    # ---- F2 verify 登记（登记即锁定，worker 必拒） ----

    def update_verify(self, tid, actor_token, cmd):
        t = self._state.get(tid)
        sid, _ = self._gate(actor_token, "task_update", tid, t,
                            owner=t.get("dispatcher_sid"))
        if not cmd.strip():  # 空命令＝无质量闸：登记即拒（merge 仅经 verify 绿）
            self._reject(tid, sid, "verify 命令为空——task_update 登记非空命令")
        self._state.set_fields(tid, verify={"cmd": cmd, "state": "registered",
                                            "registered_by": sid,
                                            "registered_at": time.time()})
        self._store.append_event({"ts": time.time(), "actor": sid, "event": EV_VERIFY,
                                  "task": tid})
        # 推送通知 worker：verify 已登记，消除轮询等待（狗粮摩擦 #1）
        wsid = t.get("worker_sid")
        if wsid:
            try:
                self._router.post(wsid, ("down", tid), "process",
                                  f"[agora] verify 已登记: {cmd}")
            except Exception:
                pass  # worker 不活则留库补拉（消息先落库铁则）

    # ---- 内部 ----

    def _gate(self, token, tool, tid, state, owner=None):
        """鉴权两段：牌有效→may 粗判→归属细判（R18）。返回 (sid, role)。"""
        ident = self._auth.identify(token)
        if ident is None:
            raise Rejected("token 无效")
        sid, role = ident
        if not self._auth.may(role, tool, tid, state) or (owner is not None and
                                                          owner != sid and role != "root"):
            self._store.append_event({"ts": time.time(), "actor": sid, "event": EV_DENIED,
                                      "tool": tool, "task": tid,
                                      "note": f"role={role}"})
            raise Rejected(f"{tid or ''}: {tool} 拒绝（role={role}）")
        return sid, role

    def _reject(self, tid, sid, reason):
        self._store.append_event({"ts": time.time(), "actor": sid, "event": EV_REJECTED,
                                  "task": tid, "note": reason})
        raise Rejected(f"{tid}: {reason}")

    def _new_tid(self):
        tasks = self._state.snapshot()["tasks"]
        while (tid := "gt-" + secrets.token_hex(3)) in tasks:
            pass
        return tid

    def _boot(self, tid, env):
        page = pathlib.Path(self._store.home) / "wiki" / "tasks" / f"{tid}.md"
        return (f"[agora] BOOT task {tid}\npage: {page}\nenv: {env}\n"
                "contract: 只在 env 内工作；结论经 post(done) 回填，body=完整 commit sha，结论文本走 note\n"
                "role: worker。可用: post/check_mail/wiki_read/wiki_write/task_dispatch(嵌套)/wait\n"
                "不可用(403): task_update(预登记归派发方) / task_cancel / board / doctor / enforce\n"
                "等待: Code Mode 无 setTimeout——用 agora.wait(seconds) 本地休眠后重试，勿轮询烧推理轮\n"
                "verify: 若任务页 verify.state=registered 则直接 post(done)；否则 post(kind=verify) 提案等派发方登记\n"
                "done: 每次提交后须重跑评审环使 .review-loop/.last-reviewed 锚追上 HEAD，然后 post(done, body=完整40位sha, 无附言)；结论文本另发 post(kind=note)")

    def _revoke_worker(self, wsid):
        if wsid:
            self._auth.revoke_session(wsid)
