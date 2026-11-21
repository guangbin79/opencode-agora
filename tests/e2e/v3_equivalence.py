#!/usr/bin/env python3
"""Req: F143 — v2/v3 stub 等价 e2e 差分器（opencode-agora v3 P4-T13b）。

同一操作脚本分别驱动 v2 栈（agorad.main.assemble＋transport.httpd）与 v3 栈
（agorad.compose.V3Stack＋serverv3 legacy 根路由——adapter 已归一 v2 形状），
逐操作 diff 外部可见面：HTTP 状态＋响应 JSON、消息导出件（state/messages/*.md
语义解析——§11「frontmatter/JSON 格式不逐字节一致」不入字节比较）、events
语义投影（actor/event/task 序列）、stub OC 注入文本。双隔离 sandbox（root/v2
与 root/v3 互斥路径），StubOpencode 扮 OC、真实 git 临时仓，零网络（仅回环）。

断言映射（任务页 ag-972029；差异分类含 dispatcher 2026-11-21 逐类裁定）：1 同
序列驱动——S1 managed 全链/S2 out_of_band/S3 external（含 crash-B 重启）/S4
cancel×gate_tail/S5 task_wait 依赖解除/S6 worker 死亡→reinject→lineage 补拉/
S7 crash-A（done/verify 间 SIGKILL 子进程栈重启）；2 外部可见对比＝逐操作
（status/json/messages/events/injects）；3 差异分类（裁定版）——intended＝
§11 五项（typed/毫秒、at-least-once、格式化、last_seen、迁移游标）＋A Outbox
消息化（credential 指针持久消息＋message_posted 审计行＋消息 id 漂移——§5
本义，v2 直投零落库即被废除的丢失窗）＋B 通知文案/命名/kind/from（统一于
DeliveryPolicy 不承诺逐字节；语义面由覆盖矩阵守门）＋C 事件 actor 归因/新增
审计行/行序；已知缺陷四键（--known-defects 旗列出时容忍、未列出或清单外仍
fail）：board_detail_projection（v2 全字段明细行 vs v3 投影）、
worker_reinject_missing（v2 会话重建 vs v3 消息重试）、gate_lane_audit_missing
（gate_resource_*/门审计键 v3 缺席——F116 车道面未平移）、
notification_coverage_gap（task×阶段×通道通知覆盖矩阵缺口）；4 崩溃恢复差分
＝S3/S7（最终任务状态/交付 ref/凭据回收等价，消息重复计数差属 intended）；
5 信封与凭据差分＝BOOT（含 reinject 标记归 E 类）/credential 指针经
tests/fixtures/v3_envelope.json 三形态正则双侧解析一致＋/cred_for_sid 拉取面
对齐；6 性能烟测＝逐操作双侧时延＋home 体积/文件数/DB 体积记录入报告（不设
门槛）；7 报告＝JSON 落 report/ 目录（含每操作双侧原始响应）＋stdout 摘要；
8 退出码＝零 unintended（含未旗已知缺陷）→0，否则→1 且打印报告路径；9 卫生
＝零网络/双 sandbox 互斥/结束清理（报告恒保留）/不触碰生产 AGORA_HOME；
10 回归＝脚本独立可执行（pytest 不收集），被引用模块零破坏。

用法：python3 tests/e2e/v3_equivalence.py [--known-defects k1,k2,...]
（--known-defects 缺省＝严格模式：已知缺陷类差异亦 fail——T13c 修复单验收面；
旗列名键容忍未出现，清单外差异仍 fail。）

崩溃窗口径（不改生产代码、v2 侧无 failpoint 的确定性替代）：crash-A＝verify
门闩暂停期 SIGKILL 子进程栈（done 残留重排面——v2 门资源 home 级 flock 随
进程消亡，in-process 重组必死锁）；crash-B＝external awaiting_external
驻留点重组（verify 绿/handoff 落账后、closed 前——managed 的 merged→closed
在两栈门体内同步原子，无第三窗）。门闩自限（1100s→exit 124＝两栈「硬顶终止
不迁移」分类）：旧栈门线程只出不进不回写账本；S7 收口后放行 go1，进程组零
残留。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agorad.access.gitx import Gitx  # noqa: E402
from agorad.access.oc import Oc  # noqa: E402
from agorad.main import assemble, instance_identity  # noqa: E402
from agorad.transport.httpd import make_server  # noqa: E402
from agorad import compose, serverv3  # noqa: E402
from tests.stub_opencode import StubOpencode  # noqa: E402

ENVELOPE = json.loads((REPO_ROOT / "tests" / "fixtures" / "v3_envelope.json")
                      .read_text(encoding="utf-8"))

TIME_KEYS = frozenset({
    "ts", "created", "updated", "last_seen", "registered_at", "started",
    "duration_s", "age_seconds", "env_active_seconds_ago", "ran_at",
    "first_created", "last_created", "stale_age_s", "last_seen_age_s"})
# 通知类消息：文案/from/kind 命名面＝B intended；语义覆盖由矩阵守门
NOTIFY_KINDS = frozenset({"milestone", "result", "process", "cancel",
                          "escalate"})
USER_KINDS = frozenset({"note", "question", "answer", "done", "verify",
                        "verify_reject", "peer"})
# 裁定版已知缺陷键（--known-defects 旗列出时容忍；未列出/清单外仍 fail）
KNOWN_DEFECT_CLASSES = frozenset({
    "board_detail_projection",    # D：v2 全字段明细行 vs v3 15 键投影
    "worker_reinject_missing",    # E：v2 会话重建 vs v3 消息重试
    "gate_lane_audit_missing",    # C-lane：F116 车道/门审计面 v3 未平移
    "notification_coverage_gap",  # B：task×阶段×通道覆盖矩阵缺口
})
TRANSITION_EVENTS = frozenset({
    "dispatched", "done", "merged", "closed", "cancelled", "blocked",
    "reopened", "verify_failed"})
_GATE_RES_EVENTS = ("gate_resource_acquire", "gate_resource_wait",
                    "gate_resource_release")
_V3_ONLY_EVENT_KEYS = frozenset({"kind", "message_id", "to"})  # A 类细节键
_HEX40 = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_HEX32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])")  # 32/64 位牌值
_TS_PATH = re.compile(r"\d{8}T\d{6}")  # gate-artifacts 目录名时钟（侧各自钟）
_RESERVED_SID = re.compile(r"[a-z]+")  # 纯字母 sid（main/root）＝通用词，禁全串替换
_BOOT_PAT = ENVELOPE["patterns"]["boot_msg"]
_CRED_PAT = ENVELOPE["patterns"]["cred_pointer"]


def _is_envelope_text(text):
    """BOOT/credential 指针文本——断言5 经 fixture 正则解析对比，不入
    injects 字节多重集（避免同一差异双计＋BOOT 逐字节噪声）。"""
    if re.search(_BOOT_PAT["source"], text):
        return True
    return bool(re.search(_CRED_PAT["source"], text,
                          re.MULTILINE if "m" in _CRED_PAT["flags"] else 0))
_SID_HEADER = "ses-f143-rebind"  # S6 换号面（两侧重名＝可归一）
_ROOTS = []  # 全部在飞 Side：失败也统一回收（断言9）


def git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr.strip()}")
    return r


def commit_files(cwd, files, msg):
    for name, content in files.items():
        p = Path(cwd) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(["add", "-A"], cwd)
    git(["commit", "-m", msg], cwd)
    return git(["rev-parse", "HEAD"], cwd).stdout.strip()


def init_repo(path):
    path.mkdir(parents=True)
    git(["init", "-b", "main"], path)
    git(["config", "user.email", "e2e@t"], path)
    git(["config", "user.name", "e2e"], path)
    return commit_files(path, {"base.txt": "base"}, "base")


def latch_cmd(vd):
    """可控 verify 门闩：第 n 次运行等 go{n}；1100s 自限 exit 124（两栈「硬顶
    终止不迁移」分类——重启后被弃的旧门线程只出不进零账本污染；放行＝运行方
    按 count 写 go{n}）。"""
    vd.mkdir(parents=True, exist_ok=True)
    return (f'n=$(cat "{vd}/count" 2>/dev/null || echo 0); '
            f'echo $((n+1)) > "{vd}/count"; '
            f'i=0; while [ ! -f "{vd}/go$((n+1))" ]; do '
            f'sleep 0.05; i=$((i+1)); [ $i -ge 22000 ] && exit 124; done; '
            f'exit 0')


def latch_count(vd):
    try:
        return int((vd / "count").read_text().strip())
    except (OSError, ValueError):
        return 0


def latch_release(vd, start=1, upto=8):
    for n in range(start, upto + 1):
        (vd / f"go{n}").write_text("go", encoding="utf-8")


# ---- 归一化与差分分类 ----

# sid 语义键：只有这些字段的值做精确 sid 归一（"main" 同时是 v2 root sid
# 与常见分支名——泛字段精确替换会把 branch:"main" 吞成 ROOT，B 册 R1）
_SID_KEYS = frozenset({"dispatcher_sid", "worker_sid", "actor", "from",
                       "to", "root_sid", "worker", "author", "sid", "caller"})


class Normalizer:
    """一侧专用：真实值→别名（tid/sid/env/sha/home）、时间键蒙版、hex 蒙版。"""

    def __init__(self, home="", root=""):
        self.tid, self.sid, self.env, self.sha = {}, {}, {}, {}
        self._sha_rev = {}
        self.home = str(home) if home else ""
        self.root = str(root) if root else ""

    def reg(self, table, real, alias):
        if real is not None:
            table[str(real)] = alias
        if table is self.sha:
            self._sha_rev[alias] = str(real)
        return alias

    def sha_real(self, alias):
        return self._sha_rev[alias]

    def value(self, s):
        """单值字段（actor/from/to）：精确 sid 命中优先（保留字免吞）。"""
        s = str(s)
        return self.sid[s] if s in self.sid else self._text(s)

    def _text(self, s):
        for table in (self.env, self.sha, self.tid):
            for k in sorted(table, key=len, reverse=True):
                if k in s:  # 具体别名先行（env 路径内嵌 tid/root，先整体替换）
                    s = s.replace(k, table[k])
        # sandbox 根蒙版（含 home/repos/env——home 恒为 root 前缀，无需独立蒙版）
        if self.root and self.root in s:
            s = s.replace(self.root, "<SANDBOX>")
        s = _TS_PATH.sub("<TSPATH>", s)  # artifact 目录名内嵌时钟（YYYYMMDDTHHMMSS）
        for k, alias in self.sid.items():
            if _RESERVED_SID.match(k):  # B 册 R1：main/root 等保留字只替换
                # 信封语境（"from <sid>"）——防分支名 "main" 等通用词被吞
                s = s.replace(f"from {k}", f"from {alias}")
            elif k in s:  # worker 会话号＝唯一 hex 串，可全串替换
                s = s.replace(k, alias)
        s = _HEX40.sub("<SHA>", s)
        return _HEX32.sub("<TOKEN>", s)

    def walk(self, obj, key=None):
        if isinstance(obj, dict):
            return {k: self.walk(v, k) for k, v in sorted(obj.items())}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key in _SID_KEYS and obj in self.sid:  # sid 语义键精确命中
                return self.sid[obj]
            return self._text(obj)
        if key in TIME_KEYS and isinstance(obj, (int, float)) \
                and not isinstance(obj, bool):
            return "<T>"
        return obj


def leaf_diffs(a, b, path="$", out=None):
    """深比较：同值异型数值→typed 弱差异；不等→强差异（含缺席键）。"""
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append((f"{path}.{k}", a.get(k, "<缺席>"),
                            b.get(k, "<缺席>"), False))
            else:
                leaf_diffs(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path + ".len", len(a), len(b), False))
        for i, (x, y) in enumerate(zip(a, b)):
            leaf_diffs(x, y, f"{path}[{i}]", out)
    elif isinstance(a, bool) or isinstance(b, bool) or \
            not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        if a != b or type(a) is not type(b):
            out.append((path, a, b, False))
    elif a != b:
        out.append((path, a, b, False))
    elif type(a) is not type(b):
        out.append((path, a, b, True))
    return out


def classify(leaves):
    """叶子差异→分类清单；清单外一律 unintended（§11 不私自扩）。键缺席＝
    结构差异恒 unintended（A 册 R1：时间豁免只救数值，不救缺席）。"""
    out = []
    for path, va, vb, typed in leaves:
        key = re.split(r"[.\[\]]", path)[-1]
        if va == "<缺席>" or vb == "<缺席>":
            cls = "unintended"
        elif typed:
            cls = "typed字段/毫秒时间"
        elif key == "last_seen":
            cls = "last_seen节流粒度"
        elif key in TIME_KEYS:
            cls = "typed字段/毫秒时间"
        else:
            cls = "unintended"
        out.append((cls, path, va, vb))
    return out


def seq_diffs(a, b):
    """序列差分：集合差→unintended（extra/missing）；仅计数差→at-least-once；
    仅顺序差→unintended（§11 无顺序豁免）；全等→[]。"""
    ja = [json.dumps(x, sort_keys=True, ensure_ascii=False) for x in a]
    jb = [json.dumps(x, sort_keys=True, ensure_ascii=False) for x in b]
    if ja == jb:
        return []
    ca, cb = Counter(ja), Counter(jb)
    out = []
    for k in sorted(set(ca) - set(cb)):
        out.append(("unintended", "extra", json.loads(k), None))
    for k in sorted(set(cb) - set(ca)):
        out.append(("unintended", "missing", None, json.loads(k)))
    if out:
        return out
    if ca != cb:
        dup = {k: (ca[k], cb[k]) for k in set(ca) | set(cb) if ca[k] != cb[k]}
        return [("at-least-once重复投递", "multiplicity", dup, None)]
    return [("unintended", "order", a, b)]


# ---- 裁定版族分类器（2026-11-21 dispatcher 逐类裁定） ----

def _msg_semantic(rec):
    """消息语义键：id/ts 剔除（id 漂移→A 单列；ts 时间面）。"""
    return {k: v for k, v in rec.items() if k not in ("id", "ts")}


def _is_cred_pointer(rec):
    return str(rec.get("body", "")).startswith("[agora] credential")


def message_list_diffs(va, vb, where, with_ids):
    """消息族（裁定 A/B）：用户/通知均按语义键（id/ts 剔除）比较——用户严格、
    通知文案→B、credential 指针→A、多重计数→at-least-once；id 漂移→A；
    通知覆盖语义归矩阵。"""
    def split(lst):
        users = [m for m in lst if m.get("kind") in USER_KINDS]
        notify = [m for m in lst if m.get("kind") not in USER_KINDS]
        return users, notify

    ua, na = split(va)
    ub, nb = split(vb)
    out = []
    sem_ua = [_msg_semantic(m) for m in ua]
    sem_ub = [_msg_semantic(m) for m in ub]
    for cls, w, x, y in seq_diffs(sem_ua, sem_ub):
        out.append((cls, f"{where}.user.{w}", x, y))
    sa = Counter(json.dumps(_msg_semantic(m), sort_keys=True,
                            ensure_ascii=False) for m in na)
    sb = Counter(json.dumps(_msg_semantic(m), sort_keys=True,
                            ensure_ascii=False) for m in nb)
    n0 = len(out)
    for k in sorted(set(sa) - set(sb)):
        rec = json.loads(k)
        cls = ("Outbox消息化(A)" if _is_cred_pointer(rec)
               else "通知文案/命名(B)")
        out.append((cls, f"{where}.notify.extra", rec, None))
    for k in sorted(set(sb) - set(sa)):
        rec = json.loads(k)
        cls = ("Outbox消息化(A)" if _is_cred_pointer(rec)
               else "通知文案/命名(B)")
        out.append((cls, f"{where}.notify.missing", None, rec))
    if len(out) == n0 and sa != sb:  # 键集相等仅计数差＝at-least-once
        out.append(("at-least-once重复投递", f"{where}.notify.multiplicity",
                    sum(sa.values()), sum(sb.values())))
    if with_ids:  # id 漂移：语义多重集相等而 id 计数不等（A——消息化挪位）
        for name, la_, lb_ in (("user", ua, ub), ("notify", na, nb)):
            csa = Counter(json.dumps(_msg_semantic(m), sort_keys=True,
                                     ensure_ascii=False) for m in la_)
            csb = Counter(json.dumps(_msg_semantic(m), sort_keys=True,
                                     ensure_ascii=False) for m in lb_)
            if csa != csb:
                continue
            ia = Counter(m.get("id") for m in la_)
            ib = Counter(m.get("id") for m in lb_)
            if ia != ib:
                out.append(("Outbox消息化(A)", f"{where}.{name}.id-drift",
                            dict(ia), dict(ib)))
    return out


def event_rows_diffs(ra, rb, where):
    """事件族（裁定 A/C/lane/E）：桶＝(event, task)；v3-only message_posted→A；
    v2-only gate_resource_*/reinjected→lane/E 缺陷；配对行 actor 差→C；配对
    gate 行细节键缺席/值差→lane 缺陷；v3-only 细节键→A；计数差→at-least-once。"""
    out = []

    def bucket(rows):
        d = {}
        for r in rows:
            d.setdefault((r.get("event"), r.get("task")), []).append(r)
        return d

    ba, bb = bucket(ra), bucket(rb)
    for key in sorted(set(ba) | set(bb), key=str):
        la, lb = ba.get(key, []), bb.get(key, [])
        ev = key[0]
        if not la:
            if ev == "message_posted":
                cls = "Outbox消息化(A)"
            elif all(r.get("actor") in ("gate", "agorad") for r in lb):
                # v3 新增门/迁移审计行（gate_enter/verify_green/closed 等）
                # ——与裁定「message_posted 新增行」同族（C），已在报告注明
                cls = "门/迁移审计行新增(C)"
            else:
                cls = "unintended"
            out.extend((cls, f"{where}[{key}].v3only", None, r) for r in lb)
            continue
        if not lb:
            if ev in _GATE_RES_EVENTS:
                cls = "gate_lane_audit_missing"
            elif ev == "reinjected":
                cls = "worker_reinject_missing"
            elif all(r.get("actor") in ("gate", "agorad") for r in la):
                # v2 侧门/启动审计行（gate_latch_reset 等）——审计行词汇差
                # 与裁定「新增审计行」（C）同族双向
                cls = "门/迁移审计行新增(C)"
            else:
                cls = "unintended"
            out.extend((cls, f"{where}[{key}].v2only", r, None) for r in la)
            continue
        for x, y in zip(la, lb):  # 配对行（同桶按序）
            if x.get("actor") != y.get("actor"):
                out.append(("事件actor/行序(C)", f"{where}[{key}].actor",
                            x.get("actor"), y.get("actor")))
            da = {k: v for k, v in x.items()
                  if k not in ("ts", "actor", "event", "task")}
            db_ = {k: v for k, v in y.items()
                   if k not in ("ts", "actor", "event", "task")}
            for k in sorted(set(da) | set(db_)):
                if da.get(k, "<缺席>") == db_.get(k, "<缺席>"):
                    continue
                if k in _V3_ONLY_EVENT_KEYS and k not in da:
                    out.append(("Outbox消息化(A)",
                                f"{where}[{key}].{k}", "<缺席>", db_[k]))
                elif ev == "worker_rebound" and k == "from":
                    # v2 reinject 新建会话 sid（未登记 hex）vs v3 原会话——
                    # 会话重建面（裁定 E）的直接痕迹
                    out.append(("worker_reinject_missing",
                                f"{where}[{key}].{k}", da.get(k, "<缺席>"),
                                db_.get(k, "<缺席>")))
                elif x.get("actor") == "gate":
                    out.append(("gate_lane_audit_missing",
                                f"{where}[{key}].{k}", da.get(k, "<缺席>"),
                                db_.get(k, "<缺席>")))
                else:
                    out.append(("unintended", f"{where}[{key}].{k}",
                                da.get(k, "<缺席>"), db_.get(k, "<缺席>")))
        # 尾行（B 册终闸 F1：行数不等时 zip 后的尾部逐行核查——同侧既有行的
        # 重复＝at-least-once；gate/agorad 审计行尾＝C 新增行族（可见非吞没，
        # 与空桶分支同口径）；迁移事件尾行/其他 actor＝unintended 严格）
        seen = {json.dumps(r, sort_keys=True, ensure_ascii=False)
                for r in la[:min(len(la), len(lb))]}
        seen |= {json.dumps(r, sort_keys=True, ensure_ascii=False)
                 for r in lb[:min(len(la), len(lb))]}

        def tail_cls(r):
            dup = json.dumps(r, sort_keys=True, ensure_ascii=False) in seen
            if dup:
                return "at-least-once重复投递"
            if r.get("actor") in ("gate", "agorad") \
                    and r.get("event") not in TRANSITION_EVENTS:
                return "门/迁移审计行新增(C)"
            return "unintended"

        for r in la[min(len(la), len(lb)):]:
            out.append((tail_cls(r), f"{where}[{key}].v2tail", r, None))
        for r in lb[min(len(la), len(lb)):]:
            out.append((tail_cls(r), f"{where}[{key}].v3tail", None, r))
        if len(la) != len(lb):
            out.append(("at-least-once重复投递", f"{where}[{key}].multiplicity",
                        len(la), len(lb)))
    return out


def task_row_diffs(ta, tb, where):
    """board 明细 task 行（裁定 D）：任意深度键缺席→投影缺陷；共有键值差严格
    （时间键已在归一化面蒙版）。"""
    out = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k not in a or k not in b:
                    out.append(("board_detail_projection", f"{path}.{k}",
                                a.get(k, "<缺席>"), b.get(k, "<缺席>")))
                else:
                    walk(a[k], b[k], f"{path}.{k}")
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                out.append(("unintended", path + ".len", len(a), len(b)))
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")
        elif a != b:
            out.append(("unintended", path, a, b))

    if isinstance(ta, dict) and isinstance(tb, dict):
        walk(ta, tb, where)
    elif ta != tb:
        out.append(("unintended", where, ta, tb))
    return out


_INJECT_HEAD = re.compile(r"\[agora\] task (\S+) (\S+) from (\S+)")


def _inject_body(text):
    """注入信封正文：剥头行＋home=/msg=/page= 引用行后的部分。"""
    lines = text.split("\n")
    i = 1
    while i < len(lines) and lines[i].startswith(("home=", "msg=", "page=")):
        i += 1
    return "\n".join(lines[i:])


def pair_channel_diffs(mdiffs, idiffs):
    """§5 投递通道差配对：同一用户消息一侧经在线注入送达（inject-only）、
    另一侧经拉取补送（pull-only）——语义已覆盖，通道差归 at-least-once。"""
    m_idx = [i for i, d in enumerate(mdiffs)
             if d[0] == "unintended" and d[1].endswith(".user.missing")]
    i_idx = [i for i, d in enumerate(idiffs)
             if d[0] == "unintended" and d[1].endswith(".user.extra")]
    used = set()
    for i in i_idx:
        body = _inject_body(idiffs[i][2]) if isinstance(idiffs[i][2], str) \
            else None
        if body is None:
            continue
        for m in m_idx:
            if m in used or not isinstance(mdiffs[m][3], dict):
                continue
            if mdiffs[m][3].get("body") == body:
                used.add(m)
                idiffs[i] = ("at-least-once重复投递", idiffs[i][1],
                             idiffs[i][2], idiffs[i][3])
                mdiffs[m] = ("at-least-once重复投递", mdiffs[m][1],
                             mdiffs[m][2], mdiffs[m][3])
                break
    return mdiffs, idiffs


def inject_diffs(ta, tb, where):
    """注入文本族：通知类信封→B 文案；用户类信封严格；计数差→at-least-once。"""
    def kind_of(text):
        m = _INJECT_HEAD.search(text)
        return m.group(2) if m else None

    ua = [t for t in ta if kind_of(t) in USER_KINDS or kind_of(t) is None]
    ub = [t for t in tb if kind_of(t) in USER_KINDS or kind_of(t) is None]
    na = [t for t in ta if kind_of(t) not in USER_KINDS and kind_of(t)]
    nb = [t for t in tb if kind_of(t) not in USER_KINDS and kind_of(t)]
    out = []
    n0 = len(out)
    for cls, w, x, y in seq_diffs(ua, ub):
        out.append((cls, f"{where}.user.{w}", x, y))
    ca = Counter(na)
    cb = Counter(nb)
    for k in sorted(set(ca) - set(cb)):
        out.append(("通知文案/命名(B)", f"{where}.notify.extra", k, None))
    for k in sorted(set(cb) - set(ca)):
        out.append(("通知文案/命名(B)", f"{where}.notify.missing", None, k))
    if len(out) == n0 and ca != cb:  # 键集相等仅计数差＝at-least-once
        out.append(("at-least-once重复投递", f"{where}.notify.multiplicity",
                    sum(ca.values()), sum(cb.values())))
    return out


# ---- 一侧栈 ----

class Side:
    """v2 或 v3 单侧：独立 sandbox（home/repos/latch）＋StubOpencode＋HTTP。"""

    def __init__(self, root, kind):
        self.kind = kind
        self.root = root / kind  # 双 sandbox 互斥路径（断言9）
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        for sub in ("state", "state/messages", "wiki", ".agora",
                    "credentials"):  # 骨架（v3 Storage 开库即需 state/）
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.repos = self.root / "repos"
        self.norm = Normalizer(home=str(self.home), root=str(self.root))
        for s in ("main", "root"):  # 两侧保留词都注册（在程 v2 用 main、
            self.norm.reg(self.norm.sid, s, "ROOT")  # 子进程 v2 与 v3 用 root）
        self.stub = StubOpencode()
        self.stub.start()
        self.token = None
        self.wtok = {}            # 任务别名 -> worker token
        self.env = {}             # 任务别名 -> env 目录
        self.wsid = {}            # 任务别名 -> worker OC 会话 sid
        self.app = self.stack = self._serv = None
        try:
            self.start_stack()
        except Exception:
            self.dispose()
            raise
        _ROOTS.append(self)

    def start_stack(self):
        if self.kind == "v2":
            self.app = assemble(self.home, oc_url=self.stub.base_url)
            if self.token is None:
                self.token = self.app.auth.issue("main", "root")
            self.app.recover.startup()  # main() 同序启动面（首启＝无待恢复）
            for step in (lambda: self.app.gate.recover_managed(),
                         lambda: self.app.health.repair_dispatcher_routes()):
                try:
                    step()
                except Exception as e:  # B 册 R5：恢复面失败留痕（生产同吞，
                    # 差分器侧可观测）——下游差异归因不失真
                    print(f"[{self.kind}] v2 恢复面失败: {e}", file=sys.stderr,
                          flush=True)
            self._serv = make_server(self.app)
        else:
            self.stack = compose.V3Stack(
                self.home, oc=Oc(self.stub.base_url),
                gitx=Gitx(instance_id=instance_identity(self.home)))
            self.stack.start()
            if self.token is None:
                self.token = (self.stack.root_token
                              or self.stack.auth.token_for("root"))
            self._serv = serverv3.make_server_v3(self.stack)
        threading.Thread(target=self._serv.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self._serv.server_address[1]

    def stop_stack(self):
        if self._serv is not None:
            try:
                self._serv.shutdown()
                self._serv.server_close()
            except Exception:
                pass
            self._serv = None
        if self.stack is not None:
            try:
                self.stack.close()
            except Exception:
                pass
            self.stack = None
        self.app = None  # v2 旧 app 弃置（旧门线程按门闩口径只出不进）

    def restart(self):
        """崩溃注入＝进程级重启的服务内模拟：停服务→同 home 重组→恢复面重跑。"""
        self.stop_stack()
        self.start_stack()

    def dispose(self):
        self.stop_stack()
        if self.stub.httpd is not None:
            self.stub.stop()

    # -- HTTP --

    def http(self, path, body, token=None, sid_header=None):
        headers = {"Content-Type": "application/json",
                   "X-Agora-Token": token or self.token}
        if sid_header:
            headers["X-Agora-Sid"] = sid_header
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), headers=headers, method="POST")
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                st, payload = r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            st, payload = e.code, json.loads(e.read() or b"{}")
        return st, payload, (time.monotonic() - t0) * 1000.0

    def detail(self, alias):
        st, r, _ = self.http("/board", {"task": self.real_tid(alias)})
        if st != 200:
            raise AssertionError(f"[{self.kind}] board {alias}: {st} {r}")
        return r

    def row(self, alias):
        return self.detail(alias)["task"]

    def real_tid(self, alias):
        rev = {v: k for k, v in self.norm.tid.items()}
        if alias not in rev:
            raise KeyError(alias)
        return rev[alias]

    def cred_file(self, alias):
        return self.home / "credentials" / f"{self.real_tid(alias)}.token"

    def env_dir(self, alias):
        return Path(self.env[alias])

    # -- 外部可见面（断言2） --

    def messages(self):
        """state/messages/*.md 语义解析（§11-3：字节格式不入比较；id＝内部分配
        不入键，id 面经 check_mail 响应 JSON 对比）。"""
        out = []
        d = self.home / "state" / "messages"
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            head, _, body = p.read_text(encoding="utf-8").partition("\n\n")
            rec = {"body": self.norm._text(body)}
            for line in head.splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    if k == "id":
                        continue  # 内部分配序——id 面经 check_mail JSON 对比
                    if k in ("from", "to") and v in self.norm.sid:
                        v = self.norm.sid[v]  # sid 语义键精确替换（免吞分支名）
                    rec[k] = ("<T>" if k in TIME_KEYS else self.norm._text(v))
            out.append(rec)
        return out

    def raw_message_bodies(self):
        d = self.home / "state" / "messages"
        return [p.read_text(encoding="utf-8").partition("\n\n")[2]
                for p in sorted(d.glob("*.md"))] if d.is_dir() else []

    def events(self):
        """events 语义投影：经 board 明细 HTTP 面（双侧同形投影），已知任务
        全量归一化行（actor/event/task＋细节键）——供事件族分类器（裁定 C 拆分
        与 A/lane/E 缺陷键）。"""
        out = []
        for alias in sorted(set(self.norm.tid.values())):
            for e in self.detail(alias).get("events", []):
                row = {"actor": self.norm.value(e.get("actor")),
                       "event": e.get("event"),
                       "task": self.norm.value(e.get("task"))}
                for k, v in e.items():
                    if k in ("ts", "actor", "event", "task"):
                        continue
                    row[k] = ("<T>" if k in TIME_KEYS
                              else self.norm.value(v) if k in _SID_KEYS
                              else self.norm._text(str(v)))
                out.append(row)
        return out

    def coverage_matrix(self):
        """B 裁定·通知覆盖矩阵：{(任务别名, 迁移事件, 通道)→是否有通知}。
        通道＝dispatcher/worker（消息 to 归属）；窗＝本迁移 ts 至下一迁移 ts；
        通知＝kind∈NOTIFY_KINDS 或 credential（A 类持久指针）到该通道。"""
        out = {}
        raw_ts = self._notify_ts()  # 一次扫描（任务间共享，勿入循环）
        for alias in sorted(set(self.norm.tid.values())):
            raw = self.detail(alias)  # detail 收别名（内部自行转真 tid）
            evs = sorted((e for e in raw.get("events", [])
                          if e.get("event") in TRANSITION_EVENTS
                          and isinstance(e.get("ts"), (int, float))),
                         key=lambda e: e["ts"])
            row = raw.get("task", {})
            disp, worker = row.get("dispatcher_sid"), row.get("worker_sid")
            if not disp or not worker:
                continue
            for j, e in enumerate(evs):
                lo = e["ts"]
                hi = evs[j + 1]["ts"] if j + 1 < len(evs) else float("inf")
                for ch, sid in (("disp", disp), ("worker", worker)):
                    out[(alias, e["event"], ch)] = any(
                        lo <= ts < hi for ts, to in raw_ts if to == sid)
        return out

    def _notify_ts(self):
        """[(原始 ts, to_sid)]：通知类消息（矩阵专用，不经蒙版）。"""
        out = []
        d = self.home / "state" / "messages"
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            head, _, _ = p.read_text(encoding="utf-8").partition("\n\n")
            rec = {}
            for line in head.splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    rec[k] = v
            if rec.get("kind") in (NOTIFY_KINDS | {"credential"}):
                try:
                    out.append((float(rec["ts"]), rec.get("to", "")))
                except (KeyError, ValueError):
                    continue
        return out

    def raw_injects(self):
        out = []
        with self.stub.lock:
            for sid in sorted(self.stub.sessions):
                out.extend(self.stub.sessions[sid]["injects"])
        return out

    def visible(self):
        return {"messages": self.messages(), "events": self.events(),
                "injects": [self.norm._text(t) for t in self.raw_injects()
                            if not _is_envelope_text(t)]}


# ---- 子进程侧：真崩溃面（S7 crash-A 专用） ----

_V3_LAUNCHER = """
import os, sys
sys.path.insert(0, {repo!r})
from agorad.access.gitx import Gitx
from agorad.access.oc import Oc
from agorad.main import instance_identity
from agorad import compose, serverv3
home = os.environ["AGORA_HOME"]
stack = compose.V3Stack(
    home, oc=Oc(os.environ["AGORA_OC_URL"]),
    gitx=Gitx(instance_id=instance_identity(home)))
stack.start()
if stack.root_token:
    print(f"AGORA_ROOT_TOKEN: {{stack.root_token}}", flush=True)
sys.exit(serverv3.serve_forever_v3(
    stack, int(os.environ.get("AGORA_PORT", "0")),
    health_interval=float(os.environ.get("AGORA_HEALTH_INTERVAL", "300"))))
"""


class SideSubproc(Side):
    """栈跑子进程、SIGKILL＝真崩溃：门线程/DB 连接/home 级 flock（v2 门资源
    闸）随进程消亡——in-process 重组无法复现的窗口（v2 旧门线程持闸不放手，
    新门恒死等）。重启＝kill＋同 home 重生（root 牌沿用首启值）。verify 门闩
    子进程经 start_new_session 存活，1100s 自限 exit 124，孤儿有界。"""

    def start_stack(self):
        env = {**os.environ, "AGORA_HOME": str(self.home),
               "AGORA_OC_URL": self.stub.base_url, "AGORA_PORT": "0",
               "AGORA_HEALTH_INTERVAL": "0.3",
               "PYTHONPATH": str(REPO_ROOT)}
        cmd = ([sys.executable, "-m", "agorad.main"] if self.kind == "v2"
               else [sys.executable, "-c",
                     _V3_LAUNCHER.format(repo=str(REPO_ROOT))])
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace")
        self._lines = []
        threading.Thread(target=self._drain, daemon=True).start()
        self._await_ready()

    def _drain(self):
        lines = self._lines  # B 册 R4：绑定当次列表——旧线程尾巴不混入新列表
        for line in self.proc.stdout:
            lines.append(line.rstrip("\n"))

    def _await_ready(self, timeout=40):
        import socket
        deadline = time.time() + timeout
        sj = self.home / ".agora" / "service.json"
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"[{self.kind}] 子进程栈即退: {self._lines[-5:]}")
            try:
                port = json.loads(sj.read_text(encoding="utf-8"))["port"]
                with socket.create_connection(("127.0.0.1", port), 0.5):
                    self.port = port
                    break
            except (OSError, ValueError, KeyError):
                time.sleep(0.05)
        else:
            raise AssertionError(f"[{self.kind}] 子进程栈就绪超时")
        if self.token is None:
            deadline = time.time() + timeout  # B 册 R3：token 窗独立计时
            while time.time() < deadline:
                for line in self._lines:
                    if line.startswith("AGORA_ROOT_TOKEN: "):
                        self.token = line.split(": ", 1)[1].strip()
                        return
                time.sleep(0.05)
            raise AssertionError(f"[{self.kind}] root 牌未打印（首启）")

    def stop_stack(self):
        if getattr(self, "proc", None) is not None:
            self.proc.kill()  # SIGKILL：真崩溃（flock/线程/连接随进程消亡）
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None


# ---- 差分驱动 ----

class Diff:
    """双侧同序列执行＋逐操作对比分类（报告与退出码唯一权威）。"""

    def __init__(self, root, side_cls=Side, known=frozenset()):
        self.root = root
        self.known = frozenset(known)  # --known-defects 旗列（容忍的缺陷键）
        self.sides = {k: side_cls(root, k) for k in ("v2", "v3")}
        self.report = {"meta": {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "root": str(root),
                                "known_defects_flag": sorted(self.known),
                                "sides": {k: str(s.home)
                                          for k, s in self.sides.items()}},
                       "ops": [], "envelope": [], "perf": {},
                       "intended": [], "known_defects": [], "unintended": []}
        self._tid_n = 0

    # -- 参数解析（占位符→侧内真值） --

    def _resolve(self, side, val):
        if not isinstance(val, str):
            return val
        if val.startswith("$T"):
            return side.real_tid(val[1:])
        if val.startswith("$C:"):
            return side.norm.sha_real(val[3:])
        if val.startswith("$REPO:"):
            return str(side.repos / val.split(":", 1)[1])
        if val.startswith("$LATCH:"):
            return latch_cmd(side.root / "latch" / val.split(":", 1)[1])
        return val

    def _token(self, side, tok):
        if tok is None or tok == "root":
            return side.token
        return side.wtok[tok]  # ("worker", 任务别名)

    # -- 核心执行 --

    def op(self, label, path, body, tok=None, sid_header=None):
        """双侧同调用：记录 status/归一化 json/时延＋可见面对比（断言2）。"""
        rec = {"label": label, "raw": {}, "norm": {}, "ms": {}, "tok": tok}
        for kind, side in self.sides.items():
            resolved = {k: self._resolve(side, v) for k, v in body.items()}
            st, payload, ms = side.http(path, resolved,
                                        self._token(side, tok), sid_header)
            rec["raw"][kind] = {"status": st, "json": payload}
            rec["norm"][kind] = {"status": st, "json": side.norm.walk(payload)}
            rec["ms"][kind] = round(ms, 3)
        rec["diffs"] = self._compare_response(rec)
        self._record(label, rec, rec["diffs"])
        self.face(f"{label} 后可见面")
        return rec

    def _compare_response(self, rec):
        a, b = rec["norm"]["v2"], rec["norm"]["v3"]
        diffs = []
        if a["status"] != b["status"]:
            # §11 扩册（dispatcher 2026-11-21 裁定①）：worker 牌终态生命周期
            # ——终态撤牌侧 401「无牌或牌无效」vs v2 滞留簿记 200，整族 intended
            # （牌随任务生死 §4/F43；v2 松面＝被废除遗留）。仅 worker 牌调用面。
            pair = {a["status"], b["status"]}
            bad = (b if b["status"] == 401 else a)["json"]
            err401 = isinstance(bad, dict) and bad.get("error") == "无牌或牌无效"
            if pair == {200, 401} and err401 and rec.get("tok") not in \
                    (None, "root"):
                # C 册终闸 F2：绑定终态（活任务提前吊销同签名不吞）＋error
                # 字段精确匹配（勿全文子串——响应回显可伪造触发）
                alias = rec["tok"]
                if all(self.sides[k].row(alias).get("state") in
                       ("closed", "cancelled", "merged", "blocked")
                       for k in ("v2", "v3")):
                    return [("牌终态生命周期(§11扩册)", "$.status+body",
                             a["status"], b["status"])]
            diffs.append(("unintended", "$.status", a["status"], b["status"]))
        ja, jb = a["json"], b["json"]
        # check_mail 面：messages 列表走消息族分类（id 漂移→A；通知→B）
        if isinstance(ja, dict) and isinstance(jb, dict) \
                and "messages" in ja and "messages" in jb:
            rest_a = {k: v for k, v in ja.items() if k != "messages"}
            rest_b = {k: v for k, v in jb.items() if k != "messages"}
            mdiffs = message_list_diffs(ja["messages"], jb["messages"],
                                        "$.messages", with_ids=True)
            diffs.extend(self._pair_pull_channel(mdiffs))
            diffs.extend(classify(leaf_diffs(rest_a, rest_b)))
            return diffs
        # board 单任务明细面：events 走事件族分类；task 行键缺席→D；
        # classification 值差→D；其余严格（B 册终闸 F2：双侧键齐才进——
        # 单侧缺键＝形状分歧，落通用严格比较防 KeyError 中断）
        if isinstance(ja, dict) and isinstance(jb, dict) \
                and all("events" in j and "task" in j for j in (ja, jb)):
            rest_a = {k: v for k, v in ja.items()
                      if k not in ("events", "task", "classification")}
            rest_b = {k: v for k, v in jb.items()
                      if k not in ("events", "task", "classification")}
            diffs.extend(event_rows_diffs(ja["events"], jb["events"],
                                          "$.events"))
            diffs.extend(task_row_diffs(ja.get("task"), jb.get("task"),
                                        "$.task"))
            if ja.get("classification") != jb.get("classification"):
                diffs.append(("board_detail_projection", "$.classification",
                              ja.get("classification"),
                              jb.get("classification")))
            diffs.extend(classify(leaf_diffs(rest_a, rest_b)))
            return diffs
        leaves = leaf_diffs(ja, jb)
        for path, va, vb, typed in list(leaves):  # $.message＝消息 id 漂移→A
            if path == "$.message":
                leaves.remove((path, va, vb, typed))
                diffs.append(("Outbox消息化(A)", path, va, vb))
        diffs.extend(classify(leaves))
        return diffs

    def _pair_pull_channel(self, diffs):
        """check_mail 响应面的 §5 通道差：用户消息单侧多出/缺失、语义在两侧
        消息文件面都在、且抑制侧（响应缺席侧）确有该消息的在线注入送达
        （C 册终闸 F1：文件双在≠已送达——否则持久化但投递链整体失效的回归
        会被静默吞掉）→投递通道互替→at-least-once。"""
        file_sets, inject_texts = {}, {}
        for kind, side in self.sides.items():
            file_sets[kind] = {
                json.dumps(_msg_semantic(m), sort_keys=True,
                           ensure_ascii=False)
                for m in side.messages() if m.get("kind") in USER_KINDS}
            inject_texts[kind] = side.raw_injects()
        out = []
        for cls, where, x, y in diffs:
            if cls == "unintended" and ".user." in where \
                    and where.endswith((".extra", ".missing")):
                rec = x if x is not None else y
                if isinstance(rec, dict):
                    key = json.dumps(_msg_semantic(rec), sort_keys=True,
                                     ensure_ascii=False)
                    # extra＝v3 响应缺席（v3 抑制）；missing＝v2 响应缺席（v2 抑制）
                    suppressed = "v3" if x is not None else "v2"
                    delivered = any(rec.get("body", None) is not None
                                    and rec["body"] in t
                                    for t in inject_texts[suppressed])
                    if key in file_sets["v2"] and key in file_sets["v3"] \
                            and delivered:
                        out.append(("at-least-once重复投递",
                                    where + "(通道差)", x, y))
                        continue
            out.append((cls, where, x, y))
        return out

    def face(self, label):
        """可见面对比（裁定版）：消息族/事件族/注入族＋通知覆盖矩阵。"""
        va, vb = self.sides["v2"].visible(), self.sides["v3"].visible()
        mdiffs = message_list_diffs(va["messages"], vb["messages"],
                                    f"{label}.messages", with_ids=False)
        ediffs = event_rows_diffs(va["events"], vb["events"],
                                  f"{label}.events")
        idiffs = inject_diffs(va["injects"], vb["injects"],
                              f"{label}.injects")
        mdiffs, idiffs = pair_channel_diffs(mdiffs, idiffs)
        bodies = {k: {m.get("body") for m in s.messages()
                      if m.get("kind") in USER_KINDS}
                  for k, s in self.sides.items()}
        idiffs = [(("at-least-once重复投递", d[1] + "(通道差)", d[2], d[3])
                   if d[0] == "unintended" and d[1].endswith(".user.extra")
                   and isinstance(d[2], str)
                   and _inject_body(d[2]) in bodies["v2"]
                   and _inject_body(d[2]) in bodies["v3"] else d)
                  for d in idiffs]
        diffs = mdiffs + ediffs + idiffs
        ma = self.sides["v2"].coverage_matrix()
        mb = self.sides["v3"].coverage_matrix()
        for key in sorted(set(ma) | set(mb), key=str):
            if ma.get(key) != mb.get(key):
                diffs.append(("notification_coverage_gap",
                              f"{label}.coverage[{key}]", ma.get(key),
                              mb.get(key)))
        self._record(label, {"v2": va, "v3": vb}, diffs)

    def _record(self, label, rec, diffs):
        rec["label"], rec["diffs"] = label, diffs
        fatal = [d for d in diffs
                 if d[0] == "unintended"
                 or (d[0] in KNOWN_DEFECT_CLASSES and d[0] not in self.known)]
        rec["verdict"] = ("match" if not diffs else
                          "unintended" if fatal else
                          "known_defect" if any(d[0] in KNOWN_DEFECT_CLASSES
                                                for d in diffs) else "intended")
        self.report["ops"].append(rec)
        for cls, where, x, y in diffs:
            entry = {"op": label, "where": where, "v2": x, "v3": y,
                     "class": cls}
            if cls == "unintended":
                self.report["unintended"].append(entry)
            elif cls in KNOWN_DEFECT_CLASSES:
                (self.report["known_defects"] if cls in self.known
                 else self.report["unintended"]).append(entry)
            else:
                self.report["intended"].append(entry)

    # -- 仓与任务面 --

    def repo(self, name):
        shas = {}
        for side in self.sides.values():
            shas[side.kind] = init_repo(side.repos / name)
        for side in self.sides.values():
            side.norm.reg(side.norm.sha, shas[side.kind], f"B{name}")
        return name

    def external_repo(self, name):
        for side in self.sides.values():
            root = side.repos / name
            origin = root / "origin.git"
            origin.mkdir(parents=True)
            git(["init", "--bare", "-b", "main"], origin)
            seed = root / "seed"
            base = init_repo(seed)
            git(["push", str(origin.resolve()), "main:refs/heads/main"], seed)
            git(["clone", str(origin.resolve()), "clone"], root)
            clone = root / "clone"
            git(["config", "user.email", "e2e@t"], clone)
            git(["config", "user.name", "e2e"], clone)
            git(["branch", "ext"], clone)
            git(["push", "origin", "ext"], clone)
            side.norm.reg(side.norm.sha, base, f"B{name}")

    def create(self, scenario, repo, *, target=None, deliverable=None,
               verify="true", latch=None):
        """建单：原始响应先取→tid 别名登记→后归一比较（防未登记裸 tid 假差）。"""
        alias = f"T{self._tid_n}"
        self._tid_n += 1
        body = {"goal": f"goal {alias} {scenario}",
                "title": f"{scenario} {alias}",
                "project_dir": f"$REPO:{repo}",
                "verify_cmd": f"$LATCH:{latch}" if latch else verify,
                "verify_base_exempt": True}
        if target:
            body["target_branch"] = target
        if deliverable:
            body["deliverable"] = deliverable
        rec = {"label": f"create {alias}", "raw": {}, "norm": {}, "ms": {}}
        for kind, side in self.sides.items():
            resolved = {k: self._resolve(side, v) for k, v in body.items()}
            st, payload, ms = side.http("/task_create", resolved)
            rec["raw"][kind] = {"status": st, "json": payload}
            rec["ms"][kind] = round(ms, 3)
            side.norm.reg(side.norm.tid, payload.get("task"), alias)
        rec["norm"] = {k: {"status": v["status"],
                           "json": self.sides[k].norm.walk(v["json"])}
                       for k, v in rec["raw"].items()}
        self._record(f"create {alias}", rec, self._compare_response(rec))
        self.face(f"create {alias} 后可见面")
        return alias

    def dispatch(self, alias):
        """派发＋登记侧内真值（worker sid/env/token）＋cred_for_sid 拉取面对照。
        先取原始响应并登记别名、后比较可见面（防未登记裸 sid 假差）。"""
        raws = {}
        for kind, side in self.sides.items():
            st, payload, ms = side.http("/task_dispatch",
                                        {"task": side.real_tid(alias)})
            raws[kind] = {"status": st, "json": payload, "ms": round(ms, 3)}
            wsid = side.row(alias).get("worker_sid")
            side.norm.reg(side.norm.sid, wsid, f"W{alias}")
            side.wsid[alias] = wsid
            cst, r, _ = side.http("/cred_for_sid", {"sid": wsid})
            if cst != 200 or not r.get("env_dir") or not r.get("token"):
                raise AssertionError(
                    f"[{kind}] cred_for_sid {alias}: {cst} {r}")
            # C 册 R1：env_dir 是被测栈的外部响应——越出本侧沙箱即拒
            # （防栈 bug 指向生产/对侧路径时差分器代为落盘＋跑 git）
            if not Path(r["env_dir"]).resolve().is_relative_to(
                    side.root.resolve()):
                raise AssertionError(
                    f"[{kind}] env_dir 越出沙箱: {r['env_dir']}")
            side.env[alias] = r.get("env_dir")
            side.wtok[alias] = r.get("token")
            if side.env.get(alias):
                side.norm.reg(side.norm.env, side.env[alias], f"ENV{alias}")
            raws[kind]["cred"] = {"status": cst, "json": r}
        rec = {"label": f"dispatch {alias}",
               "raw": {k: {"status": v["status"], "json": v["json"]}
                       for k, v in raws.items()},
               "norm": {k: {"status": v["status"],
                            "json": self.sides[k].norm.walk(v["json"])}
                        for k, v in raws.items()},
               "ms": {k: v["ms"] for k, v in raws.items()}}
        self._record(f"dispatch {alias}", rec, self._compare_response(rec))
        cred_rec = {"label": f"cred_pull {alias}",
                    "raw": {k: v["cred"] for k, v in raws.items()},
                    "norm": {k: {"status": v["cred"]["status"],
                                 "json": self.sides[k].norm.walk(
                                     v["cred"]["json"])}
                             for k, v in raws.items()}, "ms": {}}
        cred_diffs = self._compare_response(
            {"norm": {k: cred_rec["norm"][k] for k in ("v2", "v3")}})
        self._record(f"cred_pull {alias}", cred_rec, cred_diffs)
        self.face(f"dispatch {alias} 后可见面")
        return rec

    def worker_open(self, alias):
        return self.op(f"worker 开工 {alias}", "/post",
                       {"task": f"${alias}", "kind": "note",
                        "body": f"开工 {alias}"}, tok=alias)

    def worker_commit(self, alias, files, msg):
        shas = {}
        for side in self.sides.values():
            env = side.env_dir(alias)
            sha = commit_files(env, files, msg)
            anchor = env / ".review-loop" / ".last-reviewed"
            anchor.parent.mkdir(exist_ok=True)
            anchor.write_text(sha, encoding="utf-8")
            base = env / ".review-loop" / ".review-base"
            if not base.exists():
                base.write_text(git(["rev-parse", "HEAD~1"], env)
                                .stdout.strip(), encoding="utf-8")
            shas[side.kind] = sha
        for side in self.sides.values():
            side.norm.reg(side.norm.sha, shas[side.kind], alias)
        return f"$C:{alias}"

    def done(self, alias, body):
        return self.op(f"worker done {alias}", "/post",
                       {"task": f"${alias}", "kind": "done", "body": body},
                       tok=alias)

    def wait(self, alias, pred, what, timeout=120):
        """双侧轮询至谓词真（原始行——异步门体收敛，非差分面）。"""
        for kind, side in self.sides.items():
            deadline = time.time() + timeout
            row = None
            while time.time() < deadline:
                row = side.row(alias)
                if pred(row):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(
                    f"[{kind}] {alias} 等待超时（{what}）: "
                    f"state={row and row.get('state')} "
                    f"delivery={row and row.get('delivery')}")

    def wait_latch(self, name, n, timeout=30):
        for kind, side in self.sides.items():
            vd = side.root / "latch" / name
            deadline = time.time() + timeout
            while time.time() < deadline:
                if latch_count(vd) >= n:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(f"[{kind}] 门闩 {name} 第 {n} 次未运行")

    def release_latch(self, name, start=1, upto=8):
        for side in self.sides.values():
            latch_release(side.root / "latch" / name, start=start, upto=upto)

    def restart(self):
        for side in self.sides.values():
            side.restart()

    def tick(self):
        for side in self.sides.values():
            if side.kind == "v2":
                if side.app is None:  # SideSubproc 侧无进程内句柄
                    raise AssertionError("tick 仅适用于进程内栈")
                side.app.health.tick()
            else:
                if side.stack is None:
                    raise AssertionError("tick 仅适用于进程内栈")
                side.stack.health.tick()

    def kill_worker(self, alias):
        for side in self.sides.values():
            side.stub.kill(side.wsid[alias])

    # -- 信封三形态（断言5） --

    def envelope(self, label):
        pats = ENVELOPE["patterns"]

        def search(name, text):
            p = pats[name]
            return re.search(p["source"], text,
                             re.MULTILINE if "m" in p["flags"] else 0)

        parsed = {}
        for kind, side in self.sides.items():
            boots, creds = [], []
            for text in side.raw_injects() + side.raw_message_bodies():
                mb, me = search("boot_msg", text), search("boot_env", text)
                if mb and me:  # A 册 R2：tid 槽取 boot_msg 组（非 and 链尾）
                    boots.append([side.norm._text(mb.group(1)),
                                  side.norm._text(me.group(1)),
                                  "(reinject)" in text])  # E 裁定标记
                c = search("cred_pointer", text)
                if c:
                    tid, file = c.group(1), c.group(2)
                    legit = Path(file) == (side.home / "credentials"
                                           / f"{tid}.token")
                    creds.append([side.norm._text(tid),
                                  side.norm._text(file), legit])
            parsed[kind] = {"boot": boots, "cred_pointer": creds}
        diffs = []
        for key in ("boot", "cred_pointer"):
            for cls, where, x, y in seq_diffs(parsed["v2"][key],
                                              parsed["v3"][key]):
                if key == "boot" and where in ("extra", "missing"):
                    # v2 多出的 (reinject) BOOT＝会话重建面（裁定 E 缺陷）
                    rec = x if where == "extra" else y
                    cls = ("worker_reinject_missing" if rec and rec[2]
                           else "unintended")
                elif cls == "at-least-once重复投递" and key == "cred_pointer":
                    cls = "Outbox消息化(A)"  # 裁定 A：指针消息化/重复投递
                elif cls == "unintended":
                    cls = "unintended"
                diffs.append((cls, f"{label}.{key}.{where}", x, y))
        # fixture 样本三形态自检（双侧解析器同源＝Python re；样本可解析入报告）
        section_pat = {"credential_pointer": "cred_pointer",
                       "credential_token_legacy": "cred_token_legacy"}
        samples_ok = True
        for s in ENVELOPE["boot"]:
            if not (search("boot_msg", s["text"])
                    and search("boot_env", s["text"])):
                samples_ok = False
        for key, pat in section_pat.items():
            for s in ENVELOPE[key]:
                if not search(pat, s["text"]):
                    samples_ok = False
        self.report["envelope"].append({"label": label, "parsed": parsed,
                                        "fixture_samples_parse": samples_ok,
                                        "diffs": diffs})
        if not samples_ok:
            self.report["unintended"].append(
                {"op": label, "where": "fixture_samples",
                 "v2": None, "v3": None, "class": "unintended"})
        for cls, where, x, y in diffs:
            entry = {"op": label, "where": where, "v2": x, "v3": y,
                     "class": cls}
            if cls == "unintended":
                self.report["unintended"].append(entry)
            elif cls in KNOWN_DEFECT_CLASSES:
                (self.report["known_defects"] if cls in self.known
                 else self.report["unintended"]).append(entry)
            else:
                self.report["intended"].append(entry)

    # -- 性能烟测（断言6） --

    def perf(self):
        for kind, side in self.sides.items():
            try:  # B 册 R2：rglob→stat 窗内生产写者 replace 可致 ENOENT
                files = [p for p in side.home.rglob("*") if p.is_file()]
                total = 0
                for p in files:
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass  # 窗内消失＝体积近似（烟测不设门槛）
                perf = {"op_ms": {}, "home_bytes": total,
                        "home_files": len(files)}
                db = side.home / "state" / "agora.db"
                perf["db_bytes"] = db.stat().st_size if db.exists() else 0
                ledger = side.home / "state" / "tasks.jsonl"
                perf["ledger_bytes"] = (ledger.stat().st_size
                                        if ledger.exists() else 0)
            except OSError as e:
                perf = {"error": str(e)}
            for rec in self.report["ops"]:
                if rec.get("ms", {}).get(kind) is not None:
                    perf.setdefault("op_ms", {})[rec["label"]] = \
                        rec["ms"][kind]
            self.report["perf"][kind] = perf

    # -- 物面对照（凭据回收/env 回收——断言4 物理面） --

    def physical(self, label, aliases):
        rec = {"label": label, "v2": {}, "v3": {}}
        for kind, side in self.sides.items():
            rec[kind] = {a: {"cred": side.cred_file(a).exists(),
                             "env": side.env_dir(a).is_dir()}
                         for a in aliases}
        diffs = []
        for a in aliases:
            for k in ("cred", "env"):
                if rec["v2"][a][k] != rec["v3"][a][k]:
                    diffs.append(("unintended", f"{label}.{a}.{k}",
                                  rec["v2"][a][k], rec["v3"][a][k]))
        self._record(label, rec, diffs)


# ---- 场景（断言1/4） ----

def s1_managed(d):
    d.repo("r1")
    t = d.create("s1-managed", "r1")
    d.op("board 默认", "/board", {})
    d.dispatch(t)
    d.envelope(f"s1 {t}")
    d.worker_open(t)
    sha = d.worker_commit(t, {"s1.txt": "s1 work"}, "S1 work")
    d.done(t, sha)
    d.wait(t, lambda r: r["state"] == "closed", "closed")
    d.op("board 明细", "/board", {"task": f"${t}"})
    d.op("dispatcher check_mail", "/check_mail", {})
    d.op(f"worker check_mail {t}", "/check_mail", {}, tok=t)
    d.physical("s1 物面", [t])


def s2_out_of_band(d):
    d.repo("r2")
    t = d.create("s2-oob", "r2", deliverable="out_of_band")
    d.dispatch(t)
    d.worker_open(t)
    d.done(t, "交付物引用: docs/x.md（review-only）")
    d.wait(t, lambda r: r["state"] == "closed", "closed")
    d.op("board 明细", "/board", {"task": f"${t}"})
    d.physical("s2 物面", [t])


def s3_external_crash_b(d):
    """external 全链＋crash-B：awaiting_external（verify 绿/handoff 落账后、
    closed 前）注入重启——最终状态/交付 ref/凭据回收对照（断言4）。"""
    d.external_repo("r3")
    t = d.create("s3-ext", "r3/clone", target="ext",
                 deliverable="external_branch")
    d.dispatch(t)
    d.worker_open(t)
    sha = d.worker_commit(t, {"s3.txt": "s3 work"}, "S3 work")
    d.done(t, sha)
    d.wait(t, lambda r: r.get("delivery", {}).get("status")
           == "awaiting_external", "awaiting_external")
    d.physical("s3 驻留物面", [t])
    d.restart()  # crash-B：merge 后（handoff 落账）closed 前重启
    d.op("task_deliver", "/task_deliver",
         {"task": f"${t}", "outcome": "delivered", "commit": f"$C:{t}"})
    d.wait(t, lambda r: r["state"] == "closed", "closed")
    d.op("board 明细", "/board", {"task": f"${t}"})
    d.op("dispatcher check_mail", "/check_mail", {})
    d.physical("s3 收口物面", [t])


def s4_cancel_gate_tail(d):
    d.repo("r4")
    t = d.create("s4-cancel", "r4", latch="l4")
    d.dispatch(t)
    d.worker_open(t)
    sha = d.worker_commit(t, {"s4.txt": "s4 work"}, "S4 work")
    d.done(t, sha)
    d.wait_latch("l4", 1)  # 门体停在 env verify＝pending_cancel 排队窗
    d.op("task_cancel(门内)", "/task_cancel", {"task": f"${t}", "force": True})
    d.release_latch("l4")  # 放行：门尾结算（v2 语义＝merged 历史不逆转，
    # pending 取消在门尾被吸收——终态若双侧异形由差分面揭示）
    d.wait(t, lambda r: r["state"] in ("closed", "cancelled", "blocked"),
           "门尾结算")
    d.op("board 明细", "/board", {"task": f"${t}"})
    d.physical("s4 物面", [t])


def s5_task_wait(d):
    d.repo("r5")
    a = d.create("s5-dep-a", "r5")
    b = d.create("s5-dep-b", "r5")
    d.dispatch(a)
    d.dispatch(b)
    d.worker_open(a)
    d.worker_open(b)
    d.op("task_wait(B→A)", "/task_wait", {"task": f"${b}", "wait_on": f"${a}"})
    sha = d.worker_commit(a, {"s5a.txt": "a"}, "S5 A")
    d.done(a, sha)
    d.wait(a, lambda r: r["state"] == "closed", "A closed")
    d.tick()  # 依赖解除：health tick 清 waiting＋通知双方（服务级面）
    d.op("board 明细 B", "/board", {"task": f"${b}"})
    d.op("worker check_mail B", "/check_mail", {}, tok=b)
    d.physical("s5 物面", [a, b])


def s6_worker_death_lineage(d):
    d.repo("r6")
    t = d.create("s6-death", "r6")
    d.dispatch(t)
    d.worker_open(t)
    d.op("dispatcher note→worker", "/post",
         {"task": f"${t}", "kind": "note", "body": "note before death"})
    d.op(f"worker check_mail {t}", "/check_mail", {}, tok=t)
    d.kill_worker(t)  # worker OC 会话死亡（双侧同点）
    d.op("dispatcher question→死 worker", "/post",
         {"task": f"${t}", "kind": "question", "body": "q to dead worker"})
    d.restart()  # reinject 面：恢复重注入（会话死→仍待拉取）
    d.op("worker 换号 check_mail（lineage 补拉）", "/check_mail", {},
         tok=t, sid_header=_SID_HEADER)
    d.envelope(f"s6 {t}")
    d.op("board 明细", "/board", {"task": f"${t}"})
    d.physical("s6 物面", [t])


def s7_crash_a(d):
    """crash-A：done 受理后、verify 完成前（门闩暂停窗）SIGKILL 子进程栈——
    done 残留重排恰一次、最终 closed/交付 ref/凭据回收对照（断言4）。子进程
    真崩溃：v2 home 级门资源 flock 随进程消亡，in-process 重组无法复现。"""
    d7 = Diff(d.root / "s7sub", side_cls=SideSubproc, known=d.known)
    try:
        d7.repo("r7")
        t = d7.create("s7-crash-a", "r7", latch="l7")
        d7.dispatch(t)
        d7.worker_open(t)
        sha = d7.worker_commit(t, {"s7.txt": "s7 work"}, "S7 work")
        d7.done(t, sha)
        d7.wait_latch("l7", 1)  # 门体停在 verify＝崩溃窗
        d7.physical("s7 崩溃前物面", [t])
        d7.restart()  # SIGKILL＋同 home 重生（旧门闩 go1 永不放行）
        d7.release_latch("l7", start=2)
        d7.wait(t, lambda r: r["state"] == "closed", "closed")
        d7.op("board 明细", "/board", {"task": f"${t}"})
        d7.op("dispatcher check_mail", "/check_mail", {})
        d7.physical("s7 收口物面", [t])
        d7.release_latch("l7")  # A 册 R3：收口后放行 go1——孤儿 verify shell
        # 即退（父进程已死，无人消费其输出），进程面零残留
    finally:
        d7.perf()
        d.report["ops"].extend(d7.report["ops"])
        d.report["envelope"].extend(d7.report["envelope"])
        d.report["intended"].extend(d7.report["intended"])
        d.report["known_defects"].extend(d7.report["known_defects"])
        d.report["unintended"].extend(d7.report["unintended"])
        d.report["perf"]["s7sub"] = d7.report["perf"]


SCENARIOS = (s1_managed, s2_out_of_band, s3_external_crash_b, s4_cancel_gate_tail,
             s5_task_wait, s6_worker_death_lineage, s7_crash_a)


def dispose_all():
    while _ROOTS:
        try:
            _ROOTS.pop().dispose()
        except Exception:
            traceback.print_exc()


def release_stale_latches():
    """场景异常收尾：放行全部门闩，防旧门持闸毒害后续场景（s7＝子进程真崩溃，
    释放 go1 对孤儿 shell 无害——父进程已死）。"""
    for side in _ROOTS:
        vd_root = side.root / "latch"
        if vd_root.is_dir():
            for vd in vd_root.iterdir():
                if vd.is_dir():
                    try:
                        latch_release(vd)
                    except OSError:
                        pass


def parse_known_defects(argv):
    """--known-defects k1,k2,...（缺省空＝严格模式）；支持 --known-defects=k
    等号形与重复旗并集（B 册终闸 F4：静默丢弃/静默严格＝旗语义误行为）；
    非法键 fail-fast。"""
    known = set()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--known-defects":
            if i + 1 >= len(argv):
                raise SystemExit("--known-defects 需要逗号分隔键列表参数")
            known |= {k.strip() for k in argv[i + 1].split(",") if k.strip()}
            i += 2
            continue
        if arg.startswith("--known-defects="):
            known |= {k.strip() for k in arg.split("=", 1)[1].split(",")
                      if k.strip()}
        i += 1
    bad = known - KNOWN_DEFECT_CLASSES
    if bad:
        raise SystemExit(f"--known-defects 非法键: {sorted(bad)}"
                         f"（合法: {sorted(KNOWN_DEFECT_CLASSES)}）")
    return known


def main():
    known = parse_known_defects(sys.argv[1:])
    base = Path("/tmp/opencode") if Path("/tmp/opencode").is_dir() else None
    root = Path(tempfile.mkdtemp(prefix="agora-f143-e2e-", dir=base))
    d = None
    try:
        d = Diff(root, known=known)
        for fn in SCENARIOS:
            name = fn.__name__
            try:
                fn(d)
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001——逐场景隔离，失败不掩后续
                release_stale_latches()
                d.report["unintended"].append(
                    {"op": name, "where": "harness_exception",
                     "v2": None, "v3": None, "class": "unintended",
                     "detail": f"{e}\n{traceback.format_exc()}"})
                print(f"FAIL {name}: {e}")
                traceback.print_exc()
        try:
            d.perf()
        except Exception as e:  # B 册 R2：perf 异常不毁报告落盘契约
            d.report["perf"]["error"] = str(e)
        report_path = root / "report" / "v3_equivalence_report.json"
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(d.report, ensure_ascii=False,
                                              indent=1, default=str),
                                   encoding="utf-8")
        except OSError as e:  # 报告写败＝带因退出（不静默吞交付物）
            print(f"报告写入失败: {report_path}: {e}", file=sys.stderr)
            return 1
        unintended = d.report["unintended"]
        intended = d.report["intended"]
        defects = d.report["known_defects"]
        print(f"\n== F143 v2/v3 等价差分（known-defects 旗="
              f"{sorted(known) or '严格'}）：intended={len(intended)} "
              f"known_defects={len(defects)} unintended={len(unintended)} ==")
        by_cls = Counter(e["class"] for e in defects)
        for cls, n in sorted(by_cls.items()):
            print(f"  [known-defect] {cls} ×{n}")
        for e in unintended[:40]:
            print(f"  [unintended] {e['op']} {e['where']}: "
                  f"v2={str(e['v2'])[:120]} v3={str(e['v3'])[:120]}")
        if len(unintended) > 40:
            print(f"  ……另有 {len(unintended) - 40} 条见报告")
        if not unintended:
            print(f"ALL EQUIVALENT: F143 v2/v3 stub e2e\n报告: {report_path}")
            return 0
        print(f"报告: {report_path}")
        return 1
    finally:
        dispose_all()  # B 册 R6：报告恒保留（断言7）；全绿只清 sandbox 面
        for sub in ("v2", "v3", "s7sub"):  # 断言9：结束清理（双 sandbox＋子进程）
            shutil.rmtree(root / sub, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
