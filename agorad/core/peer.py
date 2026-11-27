"""同仓协作面（F22）：peer 留言板＋同仓在飞摘要。

留言复用消息库（kind=peer，头带 repo/作者任务；to="" 保持消息库全员有 to
的不变量——Router.check_mail 按 m["to"] 直取），无收件人＝不入任何信箱。
分区＝project_dir（normpath 单点归一）；写面仅 worker 牌、作者任务＝
worker_sid 命中行；四护栏：读只取最近 READ_N 条（噪音）、作者任务终态带
is_stale（陈旧降权）、单任务软限 SOFT_LIMIT 条（滥用）、留言属不可信文本
一律带 untrusted 标记（注入防线）。core 零兄弟 import（store/state 注入）。

Req: F173 留言类型化：首行前缀解析 tag（认领/此路不通/确认，全角半角冒号
等价）落 frontmatter；claim 去前缀首 token 为路径，撞车＝同仓他人非终态
同 path claim（纯读回执不拒写不落状态）；读面 tag 过滤＋fails 尾舱 FAIL_N
条。解析/路径提取为模块级纯函数（v3 compose 复用）。
"""
import os
import time

MAX_LEN = 500     # 单条长度上限（写面超限截断）
SOFT_LIMIT = 10   # 同作者任务留言软限（超限拒写）
READ_N = 20       # 读面条数上限（最新在前）
FAIL_N = 50       # 此路不通尾舱条数（fail 全量保留，最新在前）
TAGS = ("claim", "fail", "fact")   # repo_board tag 参数值域（读面过滤）
_TERMINAL = {"merged", "closed", "cancelled"}  # is_stale 口径：作者任务终态
_INFLIGHT_EXCL = _TERMINAL | {"blocked"}       # 在飞＝非终态/非阻塞（含 todo）
EV_PEER = "peer_post"
_LIMIT_MSG = f"留言软限 {SOFT_LIMIT} 条/任务已满——沉淀请走 knowledge index"

# 首行前缀→tag（全角/半角冒号等价）；命中即剥离前缀
_TAG_PREFIXES = (("认领:", "claim"), ("认领：", "claim"),
                 ("此路不通:", "fail"), ("此路不通：", "fail"),
                 ("确认:", "fact"), ("确认：", "fact"))


def parse_tag(body):
    """Req: F173 tag 解析：首行前缀命中 → (tag, 去前缀正文)；否则 ("note", 原文)。"""
    s = str(body)
    for prefix, tag in _TAG_PREFIXES:
        if s.startswith(prefix):
            return tag, s[len(prefix):]
    return "note", s


def claim_path(rest):
    """Req: F173 claim 路径：去前缀正文首个空白 token；含 / 或 . 视为路径，否则 None。"""
    tokens = str(rest).split()
    if not tokens:
        return None
    first = tokens[0]
    return first if ("/" in first or "." in first) else None


class Rejected(Exception):
    """peer 域拒绝（写权限/软限/无作者任务）——报因在消息。"""


def _repo_key(path: str) -> str:
    """F90 分区键：realpath 归一尾斜杠/符号链接（存量消息 repo 同口径）。"""
    return os.path.realpath(path)


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
        return _repo_key(t["project_dir"])

    def post(self, sid, body):
        """写面：作者任务锁 tid，软限内超长截断落库＋审计。Req: F173 落
        tag frontmatter＋claim 撞车回执；返回 {"id", "collision"}（撞车
        仅提醒不拒写——写库永远先成功）。"""
        snap = self._state.snapshot()
        tid, t = _own_task(snap, sid)
        if t is None:
            raise Rejected(f"peer 留言: {sid} 无 worker 任务行（作者不可定）")
        repo = _repo_key(t["project_dir"])
        peers = [m for m in self._store.read_messages()
                 if m.get("kind") == "peer" and m.get("task") == tid]
        if len(peers) >= SOFT_LIMIT:
            raise Rejected(_LIMIT_MSG)
        text = str(body)[:MAX_LEN]
        tag, rest = parse_tag(text)  # Req: F173
        mid = self._store.write_message(
            {"ts": time.time(), "task": tid, "kind": "peer", "repo": repo,
             "from": sid, "to": "", "tag": tag}, text)
        self._store.append_event({"ts": time.time(), "actor": sid,
                                  "event": EV_PEER, "task": tid,
                                  "repo": repo, "mid": mid})
        collision = None
        if tag == "claim":
            path = claim_path(rest)
            if path is not None:
                collision = self._collision(snap, tid, repo, path)
        return {"id": mid, "collision": collision}

    def _collision(self, snap, tid, repo, path):
        """Req: F173 撞车回执（纯读不落状态）：落库后扫同仓 claim——四条件
        同仓/他人/作者非终态/path 逐字相等；同 path 多人取最新一条。"""
        hit = None
        for m in self._store.read_messages():
            if (m.get("kind") != "peer" or m.get("task") == tid
                    or (m.get("tag") or "note") != "claim"):
                continue
            if _repo_key(m.get("repo") or "") != repo:
                continue
            author = snap["tasks"].get(m.get("task"))
            if author is None or author["state"] in _TERMINAL:
                continue
            if claim_path(parse_tag(m.get("body", ""))[1]) == path:
                hit = {"with_task": m.get("task"), "path": path,
                       "author_env": author.get("env_dir")}
        return hit

    def board(self, snap, repo=None, tag=None):
        """读面：同仓在飞五字段（req_id＝F83 撞号改号双查面，无号行 None）
        ＋最近 READ_N 条留言。repo=None＝全仓（root/
        dispatcher 诊断面）；worker 分区由 transport 经 partition() 先定。
        Req: F173：tag 过滤（值域 TAGS，非法值 ValueError→400）、messages
        元素带 tag、claim 带 author_env、fails 尾舱恒在（不受 tag 影响）。"""
        if tag is not None and tag not in TAGS:
            raise ValueError(
                f"repo_board tag 须为 {'/'.join(TAGS)} 之一（收到 {tag!r}）")
        if repo:
            repo = _repo_key(repo)
        now = time.time()
        tasks = [{"tid": tid, "title": t.get("title", ""), "state": t["state"],
                  "last_seen": t.get("last_seen"), "req_id": t.get("req_id")}
                 for tid, t in sorted(snap["tasks"].items())
                 if t["state"] not in _INFLIGHT_EXCL
                 and (repo is None
                       or _repo_key(t.get("project_dir") or "") == repo)]
        pool = [m for m in self._store.read_messages()
                if m.get("kind") == "peer"
                and (repo is None
                     or _repo_key(m.get("repo") or "") == repo)]
        shown = (pool if tag is None
                 else [m for m in pool if (m.get("tag") or "note") == tag])
        msgs = [self._row(snap, m, now) for m in reversed(shown[-READ_N:])]
        fails = [self._row(snap, m, now) for m in reversed(
            [m for m in pool if (m.get("tag") or "note") == "fail"][-FAIL_N:])]
        return {"repo": repo, "tasks": tasks, "messages": msgs, "fails": fails}

    @staticmethod
    def _row(snap, m, now):
        """留言投影（messages/fails 同构）：Req: F173 加 tag＋claim 的 author_env。"""
        author = snap["tasks"].get(m.get("task"))
        state = author["state"] if author else None
        out = {"id": m["id"], "author": m.get("task"),
               "author_state": state,
               "age_seconds": int(now - float(m["ts"])),
               "is_stale": state in _TERMINAL if author else True,
               "body": m.get("body", ""),
               "tag": m.get("tag") or "note",  # 存量无 tag 键归一 note
               "untrusted": True}
        if out["tag"] == "claim" and author and author.get("env_dir"):
            out["author_env"] = author["env_dir"]
        return out
