#!/usr/bin/env python3
"""Req: F184/F187 — dsh worker 容器化全链 e2e（形态对齐 multi_instance_delivery）。

链路：隔离 AGORA_HOME 起真 agorad（repo 源 assemble＋httpd；StubOpencode
扮 opencode 面，Dsh 适配器真装）→ service.json/root.token 落 home（shim
lift/拉旗消费面）→ task_create(harness=dsh, 最小 verify_cmd)→dispatch→
容器内 dsh worker（真 docker＋sdk profile＋F182 shim）收 BOOT/credential
→作业（建一文件含一行＋git 提交＋F111 双件锚）→post(done)→verify 门绿→
自动 merge 断言（F102 全链交付栈零 mock：门/交付/锁走产品码）。

失败路径（越界写实证——证明 worker 真在容器里、真被闸着）：第二任务
指示 write 工具写 env 外绝对路径 /work/repo/evil-f184.txt——shim 判定链
拒（gate_deny 审计落账＋dsh 会话消息含同源拒因）。

生产真形态（Req: F187）：第三场景＝完整生产 done 路径零测试侧代行——
容器 worker 自主 git add/commit（适配器按 env/.git gitdir 指针把主仓
.git 以同绝对路径 rw 挂载进容器）→写 F111 双件锚→post(kind=done,
body=容器内产生的 commit sha)→门绿→managed 自动 merge；前两场景（测试
侧代提交＋越界拒）保留为回归对照。

凭据纪律：GLM key 提取式只进本进程 env（docker -e 透传），禁打印禁
落盘；收尾 grep 守卫证据目录。证据归档 tests/e2e/dsh_delivery_out/
（事件链快照＋断言结论）。复跑：python3 tests/e2e/dsh_worker_delivery.py。
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

from agorad.access.dsh import Dsh  # noqa: E402
from agorad.main import assemble  # noqa: E402
from agorad.transport.httpd import make_server  # noqa: E402
from tests.stub_opencode import StubOpencode  # noqa: E402

OUT = Path(__file__).resolve().parent / "dsh_delivery_out"
_TIMEOUT = 600  # 场景级超时（模型多回合作业＋门体；失败诊断留档兜底）
_INSTANCES = []
# 旧双场景镜像：F182 基础镜像（node24＋python3＋bwrap＋dsh）原缺 git——
# 测试侧代行场景沿用 F184 派生层（FROM f182＋apt git）作回归对照；生产
# 真形态场景用 _PROD_IMAGE（F187 把 git 并入 dsh-shim/Dockerfile 基础配方）。
_E2E_IMAGE = "opencode-agora/f184-e2e-worker:1"
_PROD_IMAGE = "opencode-agora/f187-dsh-worker:1"


def ensure_image():
    """缺镜像才构建（幂等；apt 网络装 git 一层）。"""
    r = subprocess.run(["docker", "image", "inspect", _E2E_IMAGE],
                       capture_output=True)
    if r.returncode == 0:
        return
    d = Path(tempfile.mkdtemp(prefix="f184-img-", dir="/tmp/opencode"))
    (d / "Dockerfile").write_text(
        "FROM opencode-agora/f182-dsh-worker:1\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends git \\\n"
        "    && rm -rf /var/lib/apt/lists/* \\\n"
        "    && git --version\n", encoding="utf-8")
    r = subprocess.run(["docker", "build", "-t", _E2E_IMAGE, str(d)],
                       capture_output=True, text=True, timeout=600)
    shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        raise AssertionError(f"e2e 镜像构建失败: {r.stderr[-500:]}")


def ensure_prod_image():
    """缺镜像才构建（幂等）：dsh-shim/Dockerfile 直建——生产真形态镜像
    （F187：git 已并入基础配方；与部署注记同法直建，非派生层）。构建/
    在盘后以 git 探针验内容（防陈旧 tag 缺 git——评审环 B 册 F3 采纳：
    快败优于 600s 干等）。"""
    r = subprocess.run(["docker", "image", "inspect", _PROD_IMAGE],
                       capture_output=True)
    if r.returncode != 0:
        r = subprocess.run(["docker", "build", "-t", _PROD_IMAGE,
                            str(REPO_ROOT / "dsh-shim")],
                           capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            raise AssertionError(f"生产镜像构建失败: {r.stderr[-500:]}")
    r = subprocess.run(["docker", "run", "--rm", _PROD_IMAGE,
                        "git", "--version"], capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0 or "git version" not in (r.stdout or ""):
        raise AssertionError(
            f"生产镜像 {_PROD_IMAGE} 缺 git 面（陈旧 tag？）——"
            f"docker rmi {_PROD_IMAGE} 后重跑: {r.stderr[-200:]}")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def git(args, cwd, check_rc=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check_rc and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr.strip()}")
    return r


def init_repo(path):
    path.mkdir(parents=True)
    git(["init", "-b", "main"], path)
    git(["config", "user.email", "e2e@t"], path)
    git(["config", "user.name", "e2e"], path)
    (path / "base.txt").write_text("base", encoding="utf-8")
    git(["add", "-A"], path)
    git(["commit", "-q", "-m", "base"], path)
    return path


def http_call(port, path, body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Agora-Token": token}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Instance:
    """真 agorad v2 实例：隔离 home＋StubOpencode＋真 Dsh 适配器。"""

    def __init__(self, root, tag, image=_E2E_IMAGE):
        self.tag, self.tmp = tag, root
        self.home = root / f"home-{tag}"
        self.home.mkdir(parents=True)
        self.stub = StubOpencode()
        self.stub.start()
        try:
            self.dsh = Dsh(repo_root=str(REPO_ROOT), home=str(self.home),
                           image=image)
            self.app = assemble(self.home, oc_url=self.stub.base_url,
                                dsh=self.dsh)
            self.token = self.app.auth.issue("main", "root")
            self.httpd = make_server(self.app)
            threading.Thread(target=self.httpd.serve_forever,
                             kwargs={"poll_interval": 0.02},
                             daemon=True).start()
            self._serving = True
            self.port = self.httpd.server_address[1]
            # shim lift/拉旗消费面（生产同位）：service.json＋root.token
            self.app.store.write_ephemeral(
                ".agora/service.json",
                {"pid": os.getpid(), "port": self.port,
                 "started": time.time(), "version": "e2e-f184"})
            rt = self.home / ".agora" / "root.token"
            rt.write_text(self.token, encoding="utf-8")
            os.chmod(rt, 0o600)
        except Exception:
            self.stub.stop()
            if getattr(self, "_serving", False):  # OCR round2 #7：半构造
                self.httpd.shutdown()  # 回收已起的面（shutdown 前须真在
            if getattr(self, "httpd", None) is not None:  # serve——否则死等）
                self.httpd.server_close()
            dsh = getattr(self, "dsh", None)
            if dsh is not None:
                dsh.close()
            raise
        _INSTANCES.append(self)

    def call(self, path, body, token=None):
        return http_call(self.port, path, body, token or self.token)

    def row(self, tid):
        st, r = self.call("/board", {"task": tid})
        check(st == 200, f"board 应答 {st}: {r}")  # OCR round2 #8
        return r["task"]

    def events(self, tid=None):
        return [e for e in self.app.store.read_events(tid)]

    def stop(self):
        try:
            self.dsh.close()  # 先收容器（docker client 退出→--rm 回收）
        except Exception:
            traceback.print_exc()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.stop()


def wait_for(pred, timeout=_TIMEOUT, what=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.5)
    raise AssertionError(f"等待超时（{what}）: {last!r}")


def make_task(inst, repo, goal, verify, **extra):
    body = {"goal": goal, "project_dir": str(repo), "verify_cmd": verify,
            **extra}
    st, r = inst.call("/task_create", body)
    check(st == 200, f"task_create 应答 {st}: {r}")  # OCR round2 #8：-O 免剥
    tid = r["task"]
    inst.last_tid = tid
    st, r = inst.call("/task_dispatch", {"task": tid})
    check(st == 200, f"task_dispatch 应答 {st}: {r}")  # OCR round2 #8
    return tid


def dump_diag(inst, tag):
    """失败诊断留档（B6 兜底）：任务行/事件链/worker 转录/容器 stderr。"""
    OUT.mkdir(parents=True, exist_ok=True)
    data = {"tag": tag, "tid": getattr(inst, "last_tid", None)}
    try:
        data["row"] = inst.row(inst.last_tid) if inst.last_tid else None
    except Exception as e:  # noqa: BLE001——诊断面尽力而为
        data["row_error"] = str(e)
    data["task_events"] = [str(e)[:300] for e in inst.events(inst.last_tid)]
    data["global_events_tail"] = [str(e)[:200]
                                  for e in inst.events()][-100:]
    row = data.get("row") or {}
    wsid = row.get("worker_sid")
    if wsid:
        data["worker_sid"] = wsid
        data["messages_tail"] = inst.dsh.messages(wsid)[-25:]
        w = inst.dsh._get(wsid)
        if w is not None:
            data["session_status"] = w.status
            data["stderr_tail"] = list(w.stderr_tail)[-40:]
            data["raw_events_tail"] = [
                json.dumps(e, ensure_ascii=False)[:500]
                for e in w.events][-60:]
    (OUT / f"debug-{tag}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")


HAPPY_GOAL = (
    "唯一任务：用 bash 工具执行命令 `cd /work/task-env && printf "
    "'F184-DELIVERY\\n' > deliverable.txt && ls -l deliverable.txt`（在 "
    "/work/task-env 内创建文件 deliverable.txt，内容恰一行）。完成后调用 "
    "mcp__agora__post 工具发一条 kind=note、body=「deliverable.txt 已创建"
    "（一行 F184-DELIVERY）」的消息（task 参数＝BOOT 信封标题行里的 "
    "ag-xxxxxx）。post 返回 200 即结束回合。除上述两步外零其他动作"
    "（禁止读 /work/repo 源码、禁止探索、禁止分析本系统）。"
    "注：env 的 git 提交由派发侧收口，你无需也不得做任何 git 操作。")

FAIL_GOAL = (
    "唯一任务：用 write 工具把文本 hi 写入绝对路径 /work/repo/evil-f184.txt"
    "（必须真调用 write 工具一次，不许用 bash 或其他工具绕过）。预期该"
    "调用会失败——失败后把你收到的完整错误文本原样报告出来并结束回合。"
    "除上述动作外零其他动作（禁止读 /work/repo 源码、禁止探索、禁止分析"
    "本系统）。")


def scenario_delivery(tmp):
    """主链：dispatch→BOOT/credential→作业（容器 worker 建文件）→done→
    verify 绿→自动 merge。git 提交与 F111 锚归测试侧收口（multi_instance
    worker_done 同款形态——容器 worker 的 env worktree gitdir 指宿主主仓
    .git 绝对路径，容器内不可达＝设计级拓扑边界，已上报派发方裁定）。"""
    root = tmp / "delivery"
    repo = init_repo(root / "proj")
    inst = Instance(root, "A")
    tid = make_task(inst, repo, HAPPY_GOAL, "test -f deliverable.txt",
                    harness="dsh")
    row = inst.row(tid)
    check(str(row["worker_sid"]).startswith("session-"),
          f"worker_sid 应为 dsh 会话 id（收到 {row['worker_sid']!r}）")
    check(row["state"] == "dispatched", f"派发后状态 {row['state']}")
    env = Path(row["env_dir"])
    # shim 拉牌链路（F182 面接真 agorad）：worker 首轮工具调用→/cred_for_sid
    wait_for(lambda: any(e.get("event") == "cred_pull"
                         and str(e.get("actor", "")).startswith("session-")
                         for e in inst.events()),
             what="shim /cred_for_sid 拉牌")
    # worker 作业：容器内创建 deliverable.txt（真模型＋真容器＋真 write 闸环境）
    def delivered():
        f = env / "deliverable.txt"
        if f.is_file() and f.read_text(encoding="utf-8").strip():
            return f
        return None
    product = wait_for(delivered, what="容器 worker 建 deliverable.txt")
    check(product.read_text(encoding="utf-8").strip() == "F184-DELIVERY",
          "worker 作业文件内容恰一行（env 回收前取证）")
    # 测试侧收口（worker 的 git 面＝宿主 worktree；容器内 gitdir 不可达）：
    # 提交＋F111 双件锚＋post(done)——multi_instance worker_done 同款
    wtok = json.loads((inst.home / "credentials" / f"{tid}.token")
                      .read_text(encoding="utf-8"))["token"]
    env_base = git(["rev-parse", "HEAD"], env).stdout.strip()
    git(["add", "deliverable.txt"], env)
    git(["commit", "-q", "-m", "F184 delivery"], env)
    sha = git(["rev-parse", "HEAD"], env).stdout.strip()
    anchor = env / ".review-loop"
    anchor.mkdir(exist_ok=True)
    (anchor / ".last-reviewed").write_text(sha + "\n", encoding="utf-8")
    (anchor / ".review-base").write_text(env_base + "\n", encoding="utf-8")
    st, r = inst.call("/post", {"task": tid, "kind": "done", "body": sha},
                      wtok)
    check(st == 200 and r.get("result") == "accepted",
          f"post(done) 应答 {st}: {r}")  # OCR round2 #8
    # 门体→merged/closed（verify 门绿＋managed 交付全链产品码）
    def settled():
        r = inst.row(tid)
        return r if r["state"] in ("closed", "blocked") else None
    row = wait_for(settled, what="merged/closed 终态")
    d = row["delivery"]
    check(row["state"] == "closed",
          f"终态应 closed（收到 {row['state']}；delivery={d}）")
    check(d["mode"] == "managed" and d["status"] == "delivered"
          and d["local_status"] == "synced" and d["error"] is None,
          f"交付投影异常: {d}")
    check(d["commit"] == sha, "交付 commit 应为 worker 作业的测试侧收口提交")
    check(git(["rev-parse", "HEAD"], repo).stdout.strip() == sha,
          "本地权威目标应恰推进到交付 commit")
    check((repo / "deliverable.txt").read_text(encoding="utf-8").strip()
          == "F184-DELIVERY", "权威目标 deliverable.txt 内容恰一行")
    # worker→agorad 通道实证归场景 2（其 worker 经 mcp__agora__post 发
    # note/错误报告且 cred_pull/wiki_read/read 全链在本场景 task_events）：
    # 开工 note 是模型自选行为（非确定），不作硬断言。
    check(any(e.get("event") == "cred_pull"
              and str(e.get("actor", "")) == row["worker_sid"]
              for e in inst.events(tid)),
          "worker 应经 shim 拉牌（cred_pull＝容器 worker↔agorad 通道实证）")
    # dsh 会话消息投影（健康/用量原料面在位）
    msgs = inst.dsh.messages(row["worker_sid"])
    check(any(m.get("type") == "assistant" for m in msgs),
          "dsh 会话消息投影应含 assistant 消息")
    return {"tid": tid, "worker_sid": row["worker_sid"], "commit": sha,
            "delivery": d, "messages": len(msgs)}


def scenario_oob_deny(tmp):
    """失败路径：env 外写被 shim 拒（worker 真在容器里、真被闸着）。"""
    root = tmp / "oob"
    repo = init_repo(root / "proj")
    inst = Instance(root, "B")
    tid = make_task(inst, repo, FAIL_GOAL, "true", harness="dsh",
                    verify_base_exempt=True)  # verify=true 恒绿（本场景不进门）
    row = inst.row(tid)
    wsid = row["worker_sid"]

    def deny_evidence():
        evs = [e for e in inst.events()
               if e.get("event") == "gate_deny"
               and "evil-f184.txt" in str(e.get("note", ""))]
        deny_msg = ""
        for m in inst.dsh.messages(wsid):
            for part in m.get("content") or []:
                if isinstance(part, dict) and "evil-f184.txt" in str(
                        part.get("text", "")) and ("外" in str(
                            part.get("text", "")) or "被拦" in str(
                            part.get("text", ""))):
                    deny_msg = str(part.get("text"))
        return (evs and deny_msg) and {"gate_deny": evs[0].get("note"),
                                       "deny_text": deny_msg[:200]}
    got = wait_for(deny_evidence, what="gate_deny 审计＋拒因回流")
    check("write" in got["gate_deny"] and "evil-f184.txt" in
          got["gate_deny"], f"审计应点名工具与目标: {got['gate_deny']}")
    # 目标在 ro 挂载的 /work/repo（git repo 内＝B3 豁免面外）——拒因＋审计
    # 双证：闸先于文件系统（pre-execute 判定链，与 opencode 版同源拒文案）
    inst.call("/task_cancel", {"task": tid, "force": True})
    return {"tid": tid, "worker_sid": wsid, "evidence": got}


PROD_GOAL = (
    "唯一任务：在 /work/task-env（一个 git worktree）内完成一次真实提交"
    "并申报交付，严格按以下三步。第一步：用 bash 工具执行命令 "
    "`cd /work/task-env && printf 'F187-PROD\\n' > prod.txt && git add "
    "prod.txt && git commit -q -m 'F187 production git face'`。第二步："
    "用 bash 工具执行命令 `cd /work/task-env && mkdir -p .review-loop && "
    "git rev-parse HEAD > .review-loop/.last-reviewed && git rev-parse "
    "HEAD~1 > .review-loop/.review-base`（两锚文件内容各自恰一行 40 位 "
    "sha）。第三步：用 bash 工具执行命令 `cd /work/task-env && git "
    "rev-parse HEAD` 记下输出的 40 位 sha，然后调用 mcp__agora__post 工具"
    "发 kind=done、body=该 40 位 sha（task 参数＝BOOT 信封标题行里的 "
    "ag-xxxxxx）。除上述三步外零其他动作（禁止读 /work/repo 源码、禁止"
    "探索、禁止分析本系统）。git 身份已由仓配置提供，无需任何 git config "
    "操作。")


def scenario_production_git_face(_tmp):
    """生产真形态（Req: F187）：容器 worker 自主 git add/commit→F111 双件
    锚→post(kind=done, body=容器内 commit sha)→门绿→managed 自动 merge
    ——测试侧零 git 代行。env 侧取证在终态前（managed merge 后 env 回收，
    repo/交付投影是持久证面）；仓根用非 /tmp 根（shim B3 TMP_ROOT 豁免面
    会让写闸判定失真；生产 env 在 repo 同级天然满足）；镜像＝本仓
    dsh-shim/Dockerfile 直建（git 已并入基础配方）。"""
    root = Path(tempfile.mkdtemp(prefix="agora-f187-e2e-",
                                 dir=str(Path.home())))
    try:
        repo = init_repo(root / "proj")
        base = git(["rev-parse", "HEAD"], repo).stdout.strip()
        inst = Instance(root, "C", image=_PROD_IMAGE)
        tid = make_task(inst, repo, PROD_GOAL, "test -f prod.txt",
                        harness="dsh")
        row = inst.row(tid)
        check(str(row["worker_sid"]).startswith("session-"),
              f"worker_sid 应为 dsh 会话 id（收到 {row['worker_sid']!r}）")
        env = Path(row["env_dir"])

        def worker_evidence():
            """容器 worker 自主产出齐件：env 分支推进＋F111 双件锚落盘。"""
            try:
                r = git(["rev-parse", "HEAD"], env, check_rc=False)
            except OSError:
                return None
            head = r.stdout.strip()
            if not head or head == base:
                return None
            lr = env / ".review-loop" / ".last-reviewed"
            rb = env / ".review-loop" / ".review-base"
            if not (lr.is_file() and rb.is_file()):
                return None
            return head
        # env 侧取证必须在终态等待前：managed 交付 merge 后 env 即回收
        # （run3 实证——closed 后 env 已不在盘，repo/target 才是持久证面）
        head = wait_for(worker_evidence,
                        what="容器 worker 自主 commit＋F111 双件锚")
        # 容器内自主提交证据（测试侧零 git 写——唯一写者＝容器 worker）
        check(git(["rev-parse", f"{head}^"], env).stdout.strip() == base,
              "交付 commit 的父提交应为任务起点（恰一提交推进）")
        check((env / "prod.txt").read_text(encoding="utf-8").strip()
              == "F187-PROD", "worker 作业文件内容恰一行（env 回收前取证）")
        # F111 双件锚证据（worker 在容器内写）
        anchor = env / ".review-loop"
        last_reviewed = (anchor / ".last-reviewed").read_text(
            encoding="utf-8").strip()
        review_base = (anchor / ".review-base").read_text(
            encoding="utf-8").strip()
        check(last_reviewed == head,
              ".review-loop/.last-reviewed 应等于分支 HEAD（worker 写）")
        check(review_base == base,
              ".review-loop/.review-base 应等于任务起点（worker 写）")
        # 终态：done→verify 门绿→managed 自动 merge（env 随交付回收）
        def settled():
            r = inst.row(tid)
            return r if r["state"] in ("closed", "blocked") else None
        row = wait_for(settled, what="生产 done 路径终态"
                       "（容器 worker 自主 commit→锚→done→门→merge）")
        d = row["delivery"]
        check(row["state"] == "closed",
              f"终态应 closed（收到 {row['state']}；delivery={d}）")
        check(d["mode"] == "managed" and d["status"] == "delivered"
              and d["error"] is None, f"交付投影异常: {d}")
        sha = d["commit"]
        check(sha == head, "交付 commit 应为容器 worker 自主提交的 commit")
        # 权威目标 ff 推进＋交付物同源（merge 后以 repo 为证）
        check(git(["rev-parse", "HEAD"], repo).stdout.strip() == sha,
              "本地权威目标应恰推进到容器内产生的交付 commit")
        check((repo / "prod.txt").read_text(encoding="utf-8").strip()
              == "F187-PROD", "权威目标 prod.txt 内容恰一行")
        # 通道实证：worker 经 shim 拉牌（容器 worker↔agorad）
        check(any(e.get("event") == "cred_pull"
                  and str(e.get("actor", "")) == row["worker_sid"]
                  for e in inst.events(tid)),
              "worker 应经 shim 拉牌（cred_pull＝容器 worker↔agorad 通道实证）")
        events = [e.get("event") for e in inst.events(tid)]
        result = {"tid": tid, "worker_sid": row["worker_sid"], "commit": sha,
                  "base": base, "anchor_last_reviewed": last_reviewed,
                  "anchor_review_base": review_base, "delivery": d,
                  "events": events,
                  "messages": len(inst.dsh.messages(row["worker_sid"]))}
    except Exception:
        print(f"生产场景工作区保留待诊断: {root}")
        raise
    shutil.rmtree(root, ignore_errors=True)  # 成功路径回收（失败留诊断）
    return result


SCENARIOS = (scenario_delivery, scenario_oob_deny,
             scenario_production_git_face)


def extract_glm_key():
    """提取式（任务页纪律）：opencode 凭据库只读拉活键——只进进程 env。"""
    code = ("import sqlite3,json,os; c=sqlite3.connect("
            '"file:"+os.path.expanduser('
            '"~/.local/share/opencode/opencode.db")+"?mode=ro",uri=True); '
            'print(json.loads(c.execute("SELECT value FROM credential WHERE '
            'integration_id=\\"zai-coding-plan\\" AND active=1")'
            '.fetchone()[0])["key"])')
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=15)
    key = (r.stdout or "").strip()
    if key:
        os.environ["GLM_API_KEY"] = key
    return bool(key)


def main():
    if not extract_glm_key():
        print("FAIL GLM key 提取为空（opencode 凭据库不可用）", file=sys.stderr)
        return 3
    Path("/tmp/opencode").mkdir(parents=True, exist_ok=True)  # OCR round2 #6
    ensure_image()  # 派生镜像（含 git）幂等构建
    ensure_prod_image()  # 生产真形态镜像（dsh-shim/Dockerfile 直建，F187）
    tmp = Path(tempfile.mkdtemp(prefix="agora-f184-e2e-",
                                dir="/tmp/opencode"))
    results, failures = [], []
    leaked = False
    try:
        for fn in SCENARIOS:
            try:
                results.append((fn.__name__, fn(tmp)))
                print(f"PASS {fn.__name__}: {fn.__doc__.splitlines()[0]}")
            except Exception as e:  # noqa: BLE001——逐场景隔离
                failures.append(fn.__name__)
                print(f"FAIL {fn.__name__}: {e}")
                traceback.print_exc()
                for inst in list(_INSTANCES):  # 失败诊断留档先于回收
                    try:
                        dump_diag(inst, fn.__name__)
                    except Exception:
                        traceback.print_exc()
            finally:
                while _INSTANCES:
                    try:
                        _INSTANCES.pop().stop()
                    except Exception:
                        traceback.print_exc()
    finally:
        while _INSTANCES:
            try:
                _INSTANCES.pop().stop()
            except Exception:
                traceback.print_exc()
        # 证据留档（B6：输出/事件链快照）；GLM key 守卫——零落盘
        OUT.mkdir(parents=True, exist_ok=True)
        report = {"generated": time.strftime("%F %T"),
                  "results": results, "failures": failures}
        (OUT / "e2e-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        key = os.environ.get("GLM_API_KEY", "")
        if key:  # OCR round2 #9：key 经 stdin 喂 grep（argv 免泄）
            leaked = subprocess.run(
                ["grep", "-rqFf", "-", str(OUT)], input=key, text=True,
                capture_output=True).returncode == 0
        # 出口判定统一在 finally 外（OCR round2 #10：in-finally return
        # 吞在飞异常（KeyboardInterrupt 等）＋尾部分支死码）
    if leaked:
        print("FAIL GLM key 泄入证据目录——清除后重跑", file=sys.stderr)
        return 3
    if failures:
        print(f"工作区保留待诊断: {tmp}")
        print(f"FAILED: {', '.join(failures)}")
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS: F184 dsh worker delivery e2e")
    return 0


if __name__ == "__main__":
    sys.exit(main())
