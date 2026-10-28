"""会话牌（token→sid/role）与 enforce 旗标：独立持久化＋角色判定矩阵（设计 §4.9）。

牌不入 tasks 快照行与消息库，经 write_ephemeral("tokens") 独立落盘，kill -9 后
新实例从盘恢复；enforce 旗标只内存持有、重启默认 on，开/关与拒绝均写审计
（events.jsonl 经注入 store.append_event）。Store 构造注入，core 只 import stdlib。
"""
import secrets
import time

# worker 工具白名单＝矩阵唯一放行集；task_update/task_cancel/enforce 不在其中
WORKER_TOOLS = {"post", "check_mail", "repo_board", "wiki_read", "wiki_write",
                "task_dispatch"}
# dispatcher 通用面＝worker 面＋建任务/观测；task_update/task_cancel 为 task 域单列
DISPATCHER_TOOLS = WORKER_TOOLS | {"task_create", "board", "doctor"}

# 血统封顶（gt-83185d R1）：牌血统最多留最近 16 条曾用名（FIFO 最旧出局），
# 防持牌调用每次携随机新 X-Agora-Sid 把血统表与落盘无限撑大
LINEAGE_CAP = 16

# 审计事件名常量
EV_ENFORCE_ON = "enforce_on"
EV_ENFORCE_OFF = "enforce_off"
EV_AUTH_DENIED = "auth_denied"
EV_ROOT_ROTATED = "root_rotated"


class Auth:
    """每会话一牌：root/dispatcher/worker；多主会话各持 root 牌（角色非单例）。"""

    def __init__(self, store):
        self._store = store
        data = store.read_ephemeral("tokens") or {}
        self._tokens = {t: list(v) for t, v in data.items()}  # token -> [sid, role]
        self._enforce = True  # 重启默认 on（旗标不落盘）

    def issue(self, sid, role):
        token = secrets.token_hex()
        self._tokens[token] = [sid, role, []]
        self._persist()
        return token

    @staticmethod
    def _lineage(v):
        """牌血统集（F16 曾用名）；旧格式两元组缺字段＝空集。"""
        return v[2] if len(v) > 2 else []

    def same_lineage(self, current_sid, historical_sid):
        """F16：current_sid 现持牌的血统集含 historical_sid（换号链边连续）。"""
        return any(historical_sid in self._lineage(v)
                   for v in self._tokens.values() if v[0] == current_sid)

    def identify(self, token):
        v = self._tokens.get(token)
        return (v[0], v[1]) if v else None

    def rebind(self, old_sid, new_sid):
        """按 sid 改绑牌到新会话（值 [new_sid, role]，token 本体不变）——
        重注入后旧牌即续用；old_sid 入血统集随牌落盘（F16 换号链边连续）。
        返回牌文本（供 inject 送达），无牌 None。"""
        for tok, v in self._tokens.items():
            if v[0] == old_sid:
                v[0] = new_sid
                if len(v) < 3:  # 旧格式起档
                    v.append([])
                if old_sid not in v[2]:
                    v[2].append(old_sid)
                del v[2][:-LINEAGE_CAP]  # 封顶：最旧出局
                self._persist()
                return tok
        return None

    def revoke(self, token):
        if token not in self._tokens:
            return False
        del self._tokens[token]
        self._persist()
        return True

    def revoke_session(self, sid):
        """按 sid 作废该会话全部牌（worker 单牌语义；cancel/重派/门尾取消共用）。"""
        hit = [t for t, v in self._tokens.items() if v[0] == sid]
        for t in hit:
            del self._tokens[t]
        if hit:
            self._persist()
        return bool(hit)

    def rotate_root(self):
        # doctor 一键轮换：全部旧 root 牌即废，新牌沿用最近 root 的 sid；其余角色不受累
        roots = [t for t, v in self._tokens.items() if v[1] == "root"]
        sid = self._tokens[roots[-1]][0] if roots else "root"
        for t in roots:
            del self._tokens[t]
        token = self.issue(sid, "root")
        self._store.append_event({"ts": time.time(), "actor": "root",
                                  "event": EV_ROOT_ROTATED, "note": f"revoked={len(roots)}"})
        return token

    def may(self, role, tool, tid, state):
        """矩阵：root→任意；worker→白名单；dispatcher→通用面＋task 域（须有任务行）。

        state＝目标任务快照行（State.snapshot()["tasks"][tid]），无行即非己任；
        归属终审（dispatcher_sid==caller sid）由持 actor_token 的 TaskService 负责。
        """
        if role == "root":
            return True
        if role == "worker":
            return tool in WORKER_TOOLS
        if role == "dispatcher":
            if tool in ("task_update", "task_cancel"):
                return state is not None
            return tool in DISPATCHER_TOOLS
        return False

    def set_enforce(self, on, role):
        """仅 root 生效；开/关与非 root 拒绝均写 events 审计行。"""
        if role != "root":
            self._store.append_event({"ts": time.time(), "actor": role,
                                      "event": EV_AUTH_DENIED, "note": "enforce"})
            return False
        self._enforce = bool(on)
        self._store.append_event({"ts": time.time(), "actor": role,
                                  "event": EV_ENFORCE_ON if self._enforce else EV_ENFORCE_OFF})
        return True

    def enforce_on(self):
        return self._enforce

    def _persist(self):
        self._store.write_ephemeral("tokens", self._tokens)
