"""同仓协作面（F22）：peer 留言板＋同仓在飞摘要。

留言复用消息库（kind=peer，头带 repo/作者任务；to="" 保持消息库全员有 to
的不变量——Router.check_mail 按 m["to"] 直取），无收件人＝不入任何信箱。
分区＝project_dir（normpath 单点归一）；写面仅 worker 牌、作者任务＝
worker_sid 命中行；四护栏：读只取最近 READ_N 条（噪音）、作者任务终态带
is_stale（陈旧降权）、单任务软限 SOFT_LIMIT 条（滥用）、留言属不可信文本
一律带 untrusted 标记（注入防线）。core 零兄弟 import（store/state 注入）。
"""
import os
import time

MAX_LEN = 500     # 单条长度上限（写面超限截断）
SOFT_LIMIT = 10   # 同作者任务留言软限（超限拒写）
READ_N = 20       # 读面条数上限（最新在前）
_TERMINAL = {"merged", "closed", "cancelled"}  # is_stale 口径：作者任务终态
_INFLIGHT_EXCL = _TERMINAL | {"blocked"}       # 在飞＝非终态/非阻塞（含 todo）
EV_PEER = "peer_post"
_LIMIT_MSG = f"留言软限 {SOFT_LIMIT} 条/任务已满——沉淀请走 knowledge index"


class Rejected(Exception):
    """peer 域拒绝（写权限/软限/无作者任务）——报因在消息。"""


def _own_task(snap, sid):
    """调用者作者任务 (tid, row)：worker_sid==sid，活动态优先（终态后读面仍可用）。"""
    hit = [(tid, t) for tid, t in snap["tasks"].items()
           if t.get("worker_sid") == sid]
    active = [kv for kv in hit if kv[1]["state"] not in _TERMINAL]
    return (active or hit or [(None, None)])[0]


class PeerBoard:
    """单写者串行调用（httpd 单线程口径）；store/state 构造注入。"""

    def __init__(self, store, state):
        self._store = store
        self._state = state

    def partition(self, sid):
        """worker 牌分区＝其任务 project_dir（normpath）；无任务行即拒。"""
        _tid, t = _own_task(self._state.snapshot(), sid)
        if t is None:
            raise Rejected(f"repo_board: {sid} 无 worker 任务行（分区不可定）")
        return os.path.normpath(t["project_dir"])

    def post(self, sid, body):
        """写面：作者任务锁 tid，软限内超长截断落库＋审计。返回 msg id。"""
        tid, t = _own_task(self._state.snapshot(), sid)
        if t is None:
            raise Rejected(f"peer 留言: {sid} 无 worker 任务行（作者不可定）")
        repo = os.path.normpath(t["project_dir"])
        peers = [m for m in self._store.read_messages()
                 if m.get("kind") == "peer" and m.get("task") == tid]
        if len(peers) >= SOFT_LIMIT:
            raise Rejected(_LIMIT_MSG)
        mid = self._store.write_message(
            {"ts": time.time(), "task": tid, "kind": "peer", "repo": repo,
             "from": sid, "to": ""}, str(body)[:MAX_LEN])
        self._store.append_event({"ts": time.time(), "actor": sid,
                                  "event": EV_PEER, "task": tid,
                                  "repo": repo, "mid": mid})
        return mid

    def board(self, snap, repo=None):
        """读面：同仓在飞四字段＋最近 READ_N 条留言。repo=None＝全仓（root/
        dispatcher 诊断面）；worker 分区由 transport 经 partition() 先定。"""
        if repo:
            repo = os.path.normpath(repo)
        now = time.time()
        tasks = [{"tid": tid, "title": t.get("title", ""), "state": t["state"],
                  "last_seen": t.get("last_seen")}
                 for tid, t in sorted(snap["tasks"].items())
                 if t["state"] not in _INFLIGHT_EXCL
                 and (repo is None or os.path.normpath(t.get("project_dir") or "")
                      == repo)]
        peers = [m for m in self._store.read_messages()
                 if m.get("kind") == "peer"
                 and (repo is None or m.get("repo") == repo)][-READ_N:]
        msgs = []
        for m in reversed(peers):  # 最新在前
            author = snap["tasks"].get(m.get("task"))
            state = author["state"] if author else None
            msgs.append({"id": m["id"], "author": m.get("task"),
                         "author_state": state,
                         "age_seconds": int(now - float(m["ts"])),
                         "is_stale": state in _TERMINAL if author else True,
                         "body": m.get("body", ""), "untrusted": True})
        return {"repo": repo, "tasks": tasks, "messages": msgs}
