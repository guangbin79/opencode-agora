#!/usr/bin/env python3
"""Req: F102 多实例交付端到端证明（F94-F101 交付栈全链真码）。

自包含脚本：python3 tests/e2e/multi_instance_delivery.py（零 pytest 依赖、零
网络、零凭据——origin 一律本地 bare 仓）。并发场景（a/b/i）每场景起两个真实
agora 服务实例（assemble + make_server，各自 AGORA_HOME/端口/StubOpencode/
实例身份）；其余场景单实例驱动，他写者以真实 git 操作（外部提交/远端推进）扮
演。全部动作经 agorad HTTP 面（root/worker 牌）驱动，worker 提交为真实 git
提交；门体/交付锁/ff-only/rebase/三预算/CAS 全走产品码，无一处 mock 交付路径。

场景（对应需求 a-i）：
  a 共享本地仓并发交付——仓级交付锁串行化终推，first-parent 线性含两提交；
  b 同 bare origin 双克隆并发交付——远端 ff 终裁，输家 refetch/rebase/重验；
  c 干净远端 non-ff 在三次总尝试预算内重试成功（锁内重读远端漂移）；
  d rebase 内容冲突——failed/blocked 终局、权威目标不动、不弹回、不计连败；
  e 远端已交付而本地主检出带 WIP——delivered＋local_status=pending 且保全 WIP；
  f 无 origin 本地权威——干净目标 CAS ff-only 推进 synced；脏目标保全终局失败；
  g external_branch 全 handoff——awaiting_external 驻留、不推 origin 不合默认支，
    task_deliver(delivered) 收口 closed、默认支 SHA 不变；
  h external non_ff 换基重排产生新 handoff commit，过期 ack 拒绝零改写；
  i 环境回收实例属主——一实例清理不注销他实例在册 worktree。

断言面：delivery 投影（mode/status/commit/base/attempt/local_status/error）、
权威目标 tip、first-parent 线性、外部分支/默认支保全、审计事件（rebased/
delivery_nonff）。失败也清理临时仓/家目录/服务。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agorad.main import assemble  # noqa: E402
from agorad.transport.httpd import make_server  # noqa: E402
from tests.stub_opencode import StubOpencode  # noqa: E402

_INSTANCES = []  # 全部在飞实例：失败也统一回收


# ---- 基础件 ----

def git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr.strip()}")
    return r


def commit(cwd, files, msg):
    """真实 worker 提交：写文件→add→commit，返回 40 位 sha。"""
    for name, content in files.items():
        p = Path(cwd) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(["add", "-A"], cwd)
    git(["commit", "-m", msg], cwd)
    return git(["rev-parse", "HEAD"], cwd).stdout.strip()


def init_repo(path, branch="main"):
    path.mkdir(parents=True)
    git(["init", "-b", branch], path)
    git(["config", "user.email", "e2e@t"], path)
    git(["config", "user.name", "e2e"], path)
    commit(path, {"base.txt": "base"}, "base")
    return path


def init_bare(root, name="origin.git"):
    """本地 bare origin＋种子基线提交，返回 (origin, base_sha)。"""
    origin = root / name
    origin.mkdir(parents=True)
    git(["init", "--bare", "-b", "main"], origin)
    seed = init_repo(root / f"{name}.seed")
    git(["push", str(origin.resolve()), "main:refs/heads/main"], seed)
    return origin, branch_sha(seed, "main")


def clone(origin, path):
    git(["clone", str(origin.resolve()), str(path.name)], path.parent)
    git(["config", "user.email", "e2e@t"], path)
    git(["config", "user.name", "e2e"], path)
    return path


def branch_sha(repo, branch):
    return git(["rev-parse", f"refs/heads/{branch}"], repo).stdout.strip()


def first_parent(repo, spec):
    return set(git(["rev-list", "--first-parent", spec], repo)
               .stdout.split())


def remote_refs(repo):
    out = git(["ls-remote", "origin"], repo).stdout
    return {line.split()[1]: line.split()[0]
            for line in out.splitlines() if line}


def http_call(port, path, body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Agora-Token": token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Instance:
    """一个真实 agora 服务实例：独立 home/端口/stub oc/实例身份。"""

    def __init__(self, root, tag):
        self.tag, self.tmp = tag, root
        self.home = root / f"home-{tag}"
        self.home.mkdir(parents=True)
        self.stub = StubOpencode()
        self.stub.start()
        try:
            self.app = assemble(self.home, oc_url=self.stub.base_url)
            self.token = self.app.auth.issue("main", "root")
            self.httpd = make_server(self.app)
            threading.Thread(target=self.httpd.serve_forever,
                             kwargs={"poll_interval": 0.02}, daemon=True
                             ).start()
            self.port = self.httpd.server_address[1]
        except Exception:
            self.stub.stop()  # 装配半途抛也回收监听，防部分构造泄漏
            raise
        _INSTANCES.append(self)

    def call(self, path, body, token=None):
        return http_call(self.port, path, body, token or self.token)

    def row(self, tid):
        st, r = self.call("/board", {"task": tid})
        assert st == 200, r
        return r["task"]

    def events(self, tid):
        st, r = self.call("/board", {"task": tid})
        assert st == 200, r
        return [e.get("event") for e in r["events"]]

    def worker_token(self, tid):
        p = self.home / "credentials" / f"{tid}.token"
        return json.loads(p.read_text(encoding="utf-8"))["token"]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()


def make_task(inst, repo, *, target=None, verify="true", deliverable=None):
    # 防御夹具错位：任务仓必须落在场景根内——绝不允许把服务指向意外真仓
    check(str(Path(repo).resolve()).startswith(str(inst.tmp.resolve()) + os.sep),
          f"project_dir 越出场景夹具区: {repo}")
    body = {"goal": f"e2e {inst.tag}", "project_dir": str(repo),
            "verify_cmd": verify}
    if target is not None:
        body["target_branch"] = target
    if deliverable is not None:
        body["deliverable"] = deliverable
    st, r = inst.call("/task_create", body)
    assert st == 200, r
    tid = r["task"]
    st, r = inst.call("/task_dispatch", {"task": tid})
    assert st == 200, r
    return tid


def worker_done(inst, tid, files, msg):
    """worker 侧全真流程：env 内真实提交→评审锚→HTTP done（六闸受理）。"""
    wtok = inst.worker_token(tid)
    env = Path(inst.row(tid)["env_dir"])
    sha = commit(env, files, msg)
    anchor = env / ".review-loop" / ".last-reviewed"
    anchor.parent.mkdir(exist_ok=True)
    anchor.write_text(sha, encoding="utf-8")
    st, r = inst.call("/post", {"task": tid, "kind": "done", "body": sha},
                      wtok)
    assert st == 200 and r.get("result") == "accepted", r
    return sha, env


def wait_for(inst, tid, pred, timeout=90, what=""):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = inst.row(tid)
        if pred(row):
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"[{inst.tag}] {tid} 等待超时（{what}）: state={row and row.get('state')}"
        f" delivery={row and row.get('delivery')}")


def settled(row):
    return row["state"] in ("closed", "blocked")


_LATCHES = []  # 阻塞 verify 门闩：失败路径也统一放行，防孤儿自旋进程


def blocking_verify(vd, first_blocking_run):
    """可控节奏 verify 命令（真 shell、真 runner）：第 N 次起阻塞等 go 文件。

    计数落 count；门体 verify 依次跑 env(1)/integration(2)/…——用它在确定位置
    暂停门体，制造确定性的远端漂移/外部推进窗口。等待自限时（120s 后 exit 99
    ＝verify 红），门闩目录入 _LATCHES 由 main 失败清理统一放行——绝不留
    脱管自旋进程组（runner start_new_session 下唯一杀手是本进程）。路径在
    project 外，卫生闸放行。
    """
    vd.mkdir(parents=True, exist_ok=True)
    _LATCHES.append(vd)
    return (f'n=$(cat "{vd}/count" 2>/dev/null || echo 0); '
            f'echo $((n+1)) > "{vd}/count"; '
            f'if [ $((n+1)) -ge {first_blocking_run} ]; then '
            f'i=0; while [ ! -f "{vd}/go" ]; do '
            f'sleep 0.05; i=$((i+1)); [ $i -ge 2400 ] && exit 99; done; '
            f'exit 0; fi')


def wait_count(vd, n, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if int((vd / "count").read_text().strip()) >= n:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"verify 第 {n} 次运行未到来（门体未达预期阶段）")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---- 场景 ----

def scenario_a(tmp):
    """a 共享本地仓并发交付：锁串行化、线性、双 synced。"""
    repo = init_repo(tmp / "a" / "proj")
    base = branch_sha(repo, "main")
    ia, ib = Instance(tmp / "a", "A"), Instance(tmp / "a", "B")
    check(ia.app.gitx.instance_id != ib.app.gitx.instance_id,
          "两实例身份必须互异（F96）")
    ta = make_task(ia, repo, target="main")
    tb = make_task(ib, repo, target="main")
    worker_done(ia, ta, {"a.txt": "from A"}, "A1")
    worker_done(ib, tb, {"b.txt": "from B"}, "B1")
    ra = wait_for(ia, ta, settled, what="closed")
    rb = wait_for(ib, tb, settled, what="closed")
    for row in (ra, rb):
        check(row["state"] == "closed", f"a: 终态 {row['state']} 非 closed")
        d = row["delivery"]
        check(d["mode"] == "managed" and d["status"] == "delivered"
              and d["local_status"] == "synced" and d["error"] is None
              and d["attempt"] in (1, 2),
              f"a: 投影异常 {d}")
    linear = first_parent(repo, f"{base}..refs/heads/main")
    check(len(linear) == 2 and ra["delivery"]["commit"] in linear
          and rb["delivery"]["commit"] in linear,
          "a: 权威目标 first-parent 线性必须恰好含两个交付提交")
    rebases = ("rebased" in ia.events(ta)) + ("rebased" in ib.events(tb))
    check(rebases == 1, "a: 并发同基交付恰一方须 rebase（锁串行化的证据）")
    check(base in (ra["delivery"]["base"], rb["delivery"]["base"]),
          "a: 先到者交付基线应为共享起点")


def scenario_b(tmp):
    """b 同 origin 双克隆并发交付：远端 ff 终裁，输家 rebase 重验。"""
    root = tmp / "b"
    origin, base = init_bare(root)
    ca_repo = clone(origin, root / "cloneA")
    cb_repo = clone(origin, root / "cloneB")
    ia, ib = Instance(root, "A"), Instance(root, "B")
    ta = make_task(ia, ca_repo, target="main")
    tb = make_task(ib, cb_repo, target="main")
    worker_done(ia, ta, {"b1.txt": "from A"}, "B-A1")
    worker_done(ib, tb, {"b2.txt": "from B"}, "B-B1")
    ra = wait_for(ia, ta, settled, what="closed")
    rb = wait_for(ib, tb, settled, what="closed")
    for row in (ra, rb):
        d = row["delivery"]
        check(row["state"] == "closed", f"b: 终态 {row['state']}")
        check(d["mode"] == "managed" and d["status"] == "delivered"
              and d["error"] is None and d["attempt"] in (1, 2),
              f"b: 投影异常 {d}")
        check(d["local_status"] == "pending",
              "b: 本地克隆被检出——同步必须留 pending 不强推")
    git(["fetch", "origin"], ca_repo)
    linear = first_parent(ca_repo, f"{base}..refs/remotes/origin/main")
    check(len(linear) == 2 and ra["delivery"]["commit"] in linear
          and rb["delivery"]["commit"] in linear,
          "b: origin 权威 first-parent 线性恰含两个交付提交")
    rebases = ("rebased" in ia.events(ta)) + ("rebased" in ib.events(tb))
    check(rebases == 1, "b: 输家必须 refetch/rebase/reverify（恰一方）")
    check(base in (ra["delivery"]["base"], rb["delivery"]["base"]),
          "b: 先推者交付基线应为共享远端起点")


def scenario_c(tmp):
    """c 干净远端 non-ff：锁内重读发现漂移→整链重试，预算内成功。"""
    root = tmp / "c"
    origin, base = init_bare(root)
    repo = clone(origin, root / "clone")
    ext = clone(origin, root / "ext")
    vd = root / "vc"
    ia = Instance(root, "A")
    tid = make_task(ia, repo, target="main",
                    verify=blocking_verify(vd, 2))
    c_sha, _ = worker_done(ia, tid, {"c.txt": "worker"}, "C1")
    wait_count(vd, 2)  # 集成 verify 在跑＝本轮交付基线已取（base=base）
    ext_sha = commit(ext, {"ext.txt": "external"}, "external advance")
    git(["push", "origin", "main"], ext)  # 远端在门体持基线后被推进
    (vd / "go").write_text("go")  # 放行：锁内重读将见 tip!=base
    row = wait_for(ia, tid, settled, what="closed")
    d = row["delivery"]
    check(row["state"] == "closed" and d["mode"] == "managed"
          and d["status"] == "delivered" and d["error"] is None
          and d["local_status"] == "pending",
          f"c: 投影异常 {d}")
    check(d["attempt"] == 2, f"c: 锁内远端漂移应耗一次重试（attempt=2）：{d}")
    check(d["base"] == ext_sha, "c: 成功轮基线应为外部推进后的远端 tip")
    ev = ia.events(tid)
    check("delivery_nonff" in ev and "rebased" in ev,
          f"c: 缺漂移/重放审计事件：{ev}")
    git(["fetch", "origin"], repo)
    linear = first_parent(repo, f"{base}..refs/remotes/origin/main")
    check(len(linear) == 2 and ext_sha in linear and d["commit"] in linear
          and d["commit"] != c_sha,
          "c: origin 线性含外部推进＋重放后的交付提交")


def scenario_d(tmp):
    """d 内容冲突终局：failed/blocked、目标不动、不弹回、不计连败。"""
    root = tmp / "d"
    repo = init_repo(root / "proj")
    commit(repo, {"shared.txt": "base"}, "seed shared")  # 冲突载体
    vd = root / "vd"
    ia = Instance(root, "A")
    tid = make_task(ia, repo, target="main",
                    verify=blocking_verify(vd, 1))
    worker_done(ia, tid, {"shared.txt": "worker version"}, "D1")
    wait_count(vd, 1)  # 门体停在 env verify＝交付尚未定基
    ext_sha = commit(repo, {"shared.txt": "external version"}, "external")
    (vd / "go").write_text("go")
    row = wait_for(ia, tid, settled, what="blocked")
    d = row["delivery"]
    check(row["state"] == "blocked",
          f"d: 冲突终局应 blocked（非 reopened 弹回）：{row['state']}")
    check(d["mode"] == "managed" and d["status"] == "failed"
          and d["error"] == "rebase_conflict"
          and d["attempt"] == 1 and d["local_status"] == "pending",
          f"d: 投影异常 {d}")
    check(row["fail_streak"] == 0, "d: 交付失败不计质量连败")
    check(branch_sha(repo, "main") == ext_sha,
          "d: 权威目标必须保持外部推进位不动")
    check(Path(row["env_dir"]).is_dir(), "d: 交付失败留现场（env 不回收）")
    check("reopened" not in ia.events(tid), "d: 不得弹回 worker")


def scenario_e(tmp):
    """e 远端交付＋本地 WIP：delivered＋pending，WIP 保全。"""
    root = tmp / "e"
    origin, base = init_bare(root)
    repo = clone(origin, root / "clone")
    ia = Instance(root, "A")
    tid = make_task(ia, repo, target="main")
    worker_done(ia, tid, {"e.txt": "worker"}, "E1")
    (repo / "wip.txt").write_text("user wip", encoding="utf-8")  # 本地 WIP
    row = wait_for(ia, tid, settled, what="closed")
    d = row["delivery"]
    check(row["state"] == "closed" and d["mode"] == "managed"
          and d["status"] == "delivered",
          f"e: 投影异常 {d}")
    check(d["local_status"] == "pending", "e: 本地被检出带 WIP＝pending")
    git(["fetch", "origin"], repo)
    check(git(["rev-parse", "refs/remotes/origin/main"], repo)
          .stdout.strip() == d["commit"], "e: 远端权威已含交付 commit")
    check(branch_sha(repo, "main") == base, "e: 本地检出/引用不被强推")
    check((repo / "wip.txt").read_text(encoding="utf-8") == "user wip",
          "e: 用户 WIP 必须保全")


def scenario_f(tmp):
    """f 无 origin 本地权威：CAS ff-only 推进；脏目标保全并终局失败。"""
    root = tmp / "f"
    repo = init_repo(root / "proj")
    ia = Instance(root, "A")
    # 阶段一：干净目标——CAS ff-only 推进（checked-out merge --ff-only）
    t1 = make_task(ia, repo, target="main")
    worker_done(ia, t1, {"f1.txt": "one"}, "F1")
    r1 = wait_for(ia, t1, settled, what="closed")
    d1 = r1["delivery"]
    check(r1["state"] == "closed" and d1["mode"] == "managed"
          and d1["status"] == "delivered"
          and d1["local_status"] == "synced", f"f1: 投影异常 {d1}")
    check(branch_sha(repo, "main") == d1["commit"],
          "f1: 本地权威目标应恰推进到交付 commit（CAS ff-only）")
    # 阶段二：脏目标——保全 WIP、终局 local_target_dirty
    t2 = make_task(ia, repo, target="main")
    worker_done(ia, t2, {"f2.txt": "two"}, "F2")
    (repo / "wip2.txt").write_text("dirty on purpose", encoding="utf-8")
    r2 = wait_for(ia, t2, settled, what="blocked")
    d2 = r2["delivery"]
    check(r2["state"] == "blocked" and d2["mode"] == "managed"
          and d2["status"] == "failed"
          and d2["error"] == "local_target_dirty", f"f2: 投影异常 {d2}")
    check(branch_sha(repo, "main") == d1["commit"], "f2: 脏目标零接触")
    check((repo / "wip2.txt").read_text(encoding="utf-8") == "dirty on purpose",
          "f2: 目标仓 WIP 保全")


def _external_setup(root, tag):
    """g/h 共用：origin＋默认支 main＋外部分支 ext，external_branch 任务。"""
    origin, base = init_bare(root)
    repo = clone(origin, root / "clone")
    git(["branch", "ext"], repo)
    git(["push", "origin", "ext"], repo)
    inst = Instance(root, tag)
    tid = make_task(inst, repo, target="ext", deliverable="external_branch")
    return origin, repo, base, inst, tid


def scenario_g(tmp):
    """g external handoff 全链：驻留不推不合，确认收口，默认支不动。"""
    root = tmp / "g"
    origin, repo, base, ia, tid = _external_setup(root, "A")
    before_refs = remote_refs(repo)  # 快照在 handoff 前——全程不推不合
    before_main = branch_sha(repo, "main")
    c1, env = worker_done(ia, tid, {"g.txt": "worker"}, "G1")
    row = wait_for(ia, tid,
                   lambda r: r["delivery"]["status"] == "awaiting_external",
                   what="awaiting_external")
    d = row["delivery"]
    check(row["state"] == "done", "g: handoff 驻留 done 等外部确认")
    check(d["mode"] == "external_branch" and d["commit"] == c1
          and d["base"] == base and d["attempt"] == 1
          and d["local_status"] == "not_required" and d["error"] is None,
          f"g: handoff 投影异常 {d}")
    check(env.is_dir(), "g: 驻留期 env 保留")
    check((ia.home / "credentials" / f"{tid}.token").is_file(),
          "g: 驻留期凭据保留")
    st, r = ia.call("/task_deliver", {"task": tid, "outcome": "delivered",
                                      "commit": c1})
    check(st == 200 and r.get("outcome") == "delivered", f"g: 确认应答 {r}")
    row = wait_for(ia, tid, lambda x: x["state"] == "closed", what="closed")
    check(row["delivery"]["status"] == "delivered", "g: 确认后投影 delivered")
    check(not env.is_dir(), "g: 收口后 env 回收")
    check(not (ia.home / "credentials" / f"{tid}.token").exists(),
          "g: 收口后凭据作废")
    check(remote_refs(repo) == before_refs, "g: 服务全程不推 origin")
    check(branch_sha(repo, "main") == before_main,
          "g: 默认支 SHA 全程不变（不合并不回滚）")


def scenario_h(tmp):
    """h external non_ff 换基：新 handoff commit；过期 ack 拒绝零改写。"""
    root = tmp / "h"
    origin, repo, base, ia, tid = _external_setup(root, "A")
    before_refs = remote_refs(repo)  # 快照在 handoff 前——全程不推不合
    before_main = branch_sha(repo, "main")
    c1, _ = worker_done(ia, tid, {"h.txt": "worker"}, "H1")
    wait_for(ia, tid,
             lambda r: r["delivery"]["status"] == "awaiting_external",
             what="awaiting_external")
    # 外部派发方推进钉死支（真实提交在 project 本地支上——外部基须本地可解析）
    git(["checkout", "ext"], repo)
    b1 = commit(repo, {"h-ext.txt": "external base"}, "ext advance")
    git(["checkout", "main"], repo)
    st, r = ia.call("/task_deliver", {"task": tid, "outcome": "non_ff",
                                      "commit": c1, "base": b1})
    check(st == 200 and r.get("requeued") and r.get("attempt") == 2,
          f"h: non_ff 应答重排 {r}")
    row = wait_for(ia, tid, lambda x: x["delivery"]["status"]
                   == "awaiting_external" and x["delivery"]["attempt"] == 2,
                   what="re-handoff")
    c2 = row["delivery"]["commit"]
    d = row["delivery"]
    check(c2 != c1 and d["base"] == b1 and d["local_status"] == "not_required",
          f"h: 换基 handoff 投影异常 {d}")
    check(git(["merge-base", "--is-ancestor", b1, c2], repo,
              check=False).returncode == 0, "h: 新 handoff 须落在外部新基上")
    # 过期 ack：旧 commit 确认→403 拒绝、状态零改写
    st, r = ia.call("/task_deliver", {"task": tid, "outcome": "delivered",
                                      "commit": c1})
    check(st == 403 and "过期" in r.get("error", ""), f"h: 过期确认应拒 {r}")
    row2 = ia.row(tid)
    check(row2["state"] == "done"
          and row2["delivery"]["commit"] == c2
          and row2["delivery"]["status"] == "awaiting_external",
          "h: 拒绝必须零状态改写")
    st, r = ia.call("/task_deliver", {"task": tid, "outcome": "delivered",
                                      "commit": c2})
    check(st == 200, f"h: 新 commit 确认应受理 {r}")
    wait_for(ia, tid, lambda x: x["state"] == "closed", what="closed")
    check(branch_sha(repo, "main") == before_main, "h: 默认支全程不变")
    check(remote_refs(repo) == before_refs,
          "h: 服务全程不推 origin（全部引用保持 handoff 前快照）")


def scenario_i(tmp):
    """i 实例属主清理：A 收 Env 不注销 B 的在册 worktree，B 仍可交付。"""
    root = tmp / "i"
    repo = init_repo(root / "proj")
    ia, ib = Instance(root, "A"), Instance(root, "B")
    ta = make_task(ia, repo, target="main")
    tb = make_task(ib, repo, target="main")
    env_b = Path(ib.row(tb)["env_dir"])
    worker_done(ia, ta, {"i1.txt": "A"}, "I1")  # 只跑 A——B 驻场等 A 先收
    wait_for(ia, ta, settled, what="A closed")
    env_a = Path(ia.row(ta)["env_dir"])
    check(not env_a.exists(), "i: A 交付后自身 env 已回收")
    check(env_b.is_dir(), "i: B 的 env 目录必须在盘")
    registered = [str(Path(line.split(" ", 1)[1]).resolve()) for line in
                  git(["worktree", "list", "--porcelain"], repo).stdout
                  .splitlines() if line.startswith("worktree ")]
    check(str(env_b.resolve()) in registered,
          f"i: B 的 worktree 注册不得被 A 清理摘除：{registered}")
    worker_done(ib, tb, {"i2.txt": "B"}, "I2")
    rb = wait_for(ib, tb, settled, what="B closed")
    check(rb["state"] == "closed" and rb["delivery"]["status"] == "delivered",
          f"i: B 后续交付照常 {rb['delivery']}")
    final = [str(Path(line.split(" ", 1)[1]).resolve()) for line in
             git(["worktree", "list", "--porcelain"], repo).stdout
             .splitlines() if line.startswith("worktree ")]
    check(not env_b.exists() and final == [str(repo.resolve())],
          f"i: 双实例各自收尾后无残留 worktree：{final}")


SCENARIOS = (scenario_a, scenario_b, scenario_c, scenario_d, scenario_e,
             scenario_f, scenario_g, scenario_h, scenario_i)


def stop_all():
    """统一回收：单实例 stop 抛不掩其余（含 rmtree 前放行全部 verify 门闩）。"""
    while _INSTANCES:
        try:
            _INSTANCES.pop().stop()
        except Exception:
            traceback.print_exc()
    for vd in _LATCHES:
        try:
            (vd / "go").write_text("go")
        except OSError:
            pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agora-f102-e2e-"))
    failures = []
    try:
        for fn in SCENARIOS:
            try:
                fn(tmp)
                print(f"PASS {fn.__name__}: {fn.__doc__.splitlines()[0]}")
            except Exception as e:  # noqa: BLE001——逐场景隔离，失败不掩后续
                failures.append(fn.__name__)
                print(f"FAIL {fn.__name__}: {e}")
                traceback.print_exc()
            finally:
                stop_all()
    finally:
        stop_all()
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("ALL PASS: F102 multi-instance delivery e2e")
    return 0


if __name__ == "__main__":
    sys.exit(main())
