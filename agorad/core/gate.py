"""verify 门与自动合并：任务环境验收→集成环境试合并/验收→目标支最终推进。

done 受理仍执行状态、verify、commit 格式与评审锚前置校验。门体先在任务 env 跑
verify，再在目标分支当前 HEAD 的 detached integration worktree 内 ff-only 试合并
并重跑 verify；试合并遇真 non-ff 先门内 detached rebase 干净自愈（F32：重放后
集成 verify 跑在新内容上，绿则 sha 级 ff 推进），同步冲突才弹回；只有全绿且目标
主检出两次干净检查通过，才推进真实目标分支。集成
失败不再依赖 reset --hard 回滚，用户未提交工作与目标分支均不受测试副作用影响。
managed 任务（Req: F100，delivery.mode=managed＋钉死目标支）一律走本地权威
交付（_deliver_managed_local，ag-b5f2e9 用户裁定废除 F98 远端优先）：本地钉死
支 tip 定基＋锁内 advance_local_target CAS ff-only 推进，origin 在位不改变
定基/交付面（零 fetch 零 push），脏/占用目标＝交付终局失败；交付败走
delivery failed/blocked 专用终局（非质量口径）；启动恢复面 recover_managed
收敛崩溃残留（failed 防重试、权威已含补终态——恢复探针 F109 起一律本地，
零网络）。
external_branch 任务（Req: F101，外部开发分支交付）走 _deliver_external：本地
钉死支 tip（或 task_deliver(non_ff) 携带的外部基）定基集成→rebase→集成 verify，
绿即驻留 done＋delivery.status=awaiting_external——不合目标支、不推 origin、
不回收 env/凭据，交付确认权在外部派发方（task_deliver：delivered 收口
merged/closed、non_ff 经 F110 祖先预判直接 diverged 终局、failed 终局 blocked）。

默认 runner 以独立进程组执行命令，双顶（F36）：软顶到点提醒派发方一次不杀继续
跑，硬顶（缺省 4×软顶）到点杀全组返回 124；rc=124 分类路由派发方（不计 worker
连败、任务驻留 done），避免单个挂死验证永久堵住串行门。命令自身死（rc=126/127，
或 rc=2＋输出含 No such file or directory，F81/F82）同款驻留分类——escalate
通知通道拍派发方，不弹回 worker 自愈。
崩溃重排先探测目标支是否
已含任务 commit：已含说明上轮最终推进已完成，只补 merged/closed 终态，不重验后
假 reopened。gate_env 与 target_branch 的原始分支位随任务快照持久化，集成环境和
分支位均可跨重启收敛。

全程 in-gate 门闩、pending-cancel 门尾原子结算、三连败 blocked 与事件审计保持
不变。F112：门按仓分池——同仓任务经 per-repo 车道串行（交付锁本就仓级），
跨仓任务并行进门（httpd 消费循环按 project_dir 分发车道）。F116：门体全程
再套跨进程资源闸（repo 锁＋AGORA_HOME 全局槽，access/gate_resources 经构造
注入）——排队不占 verify/看门狗预算，waiting 投影与三类 gate_resource_* 事件
可观测。F122：全部 runner 执行点经 _run_with_artifact 落完整执行产物
（ArtifactStore 构造注入），rc<0 或 rc∈[129,165] 的 signal 类机械失败走
_verify_infra（驻留 done、不计连败、路由派发方）——非质量败不烧连败。
core 只 import stdlib，依赖全构造注入。
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

# 升级 kind 与 health 的类别词汇对齐（core 禁 import 兄弟模块，字面量本地定义）
KIND_QUALITY = "quality"
KIND_GATEFILES = "gatefiles"
KIND_VERIFY_DEAD = "verify-dead"
KIND_VERIFY_INFRA = "verify-infra"  # Req: F122 signal 类机械失败升级渠道

_TAIL = 2000
_VERIFY_DEAD_RAW_TAIL = 400  # health 头截 500：先取尾并留 exit= 前缀/拍平余量
# F36 软硬双顶轮询步长：硬顶判死及时性 0.5s 级（秒）
_VERIFY_POLL = 0.5
# 闸体＝verify 依赖面（构建/测试配置）：任务 diff 触及即升级拍人
_GATE_PATTERNS = ("Makefile", "*.mk", "pyproject.toml", "setup.py", "setup.cfg",
                  "requirements*.txt", "package.json", "package-lock.json",
                  "go.mod", "Cargo.toml", "tox.ini", "noxfile.py",
                  ".github/workflows/*")

# ag-54bd98 幻影合并双守卫共用弹回文案：env boot 分支无交付可并（G1 空集重放/
# G2 落账 commit 不在目标支），worker 确认交付在 boot 分支后重 post(done) 自愈
_PHANTOM_BOUNCE = ("合并前置失败：env 分支相对目标无新增提交——确认交付提交在"
                   " env boot 分支上后重新 post(done, commit)")

# ag-9a680b：local_target_dirty 三形细分（v3 gatedelivery.py 平移件，core
# 禁 import 兄弟模块故自持）——umbrella reason 字符串不变（downstream 解析
# 口径），sub-form 只进 note 明细。事故家族：gt-fc6fb2（未跟踪 cron 产物撞
# ff-merge 被误报 non-ff 盲试循环）、ag-e35a42、2026-11-25 两例 agent 构建树。
_DIRTY_CAP = 20              # 三形明细路径行上限（通知面防过载）
_DIRTY_NOTE_TAIL = 2000      # 脏面明细 note 截断（_TAIL 同族；入参自带 20 行上限）
_DIRTY_DISPOSITION = (       # untracked_only 形固定处置句（任务页原文，一字不改）
    "safe disposition: append paths to .git/info/exclude (local-only ignore) "
    "or mv them out (byte-preserving quarantine) - never rm; tracked changes "
    "always require human adjudication")


def _dirty_face(records, incoming=()):
    """porcelain 脏面 → 三形判定（纯函数）：tracked_dirty（M/A/D/R/C 等
    已跟踪改动——人工裁决）、untracked_collision（未跟踪路径与 incoming
    合并内容相撞——gt-fc6fb2「untracked working tree files would be
    overwritten by merge」家族，绝不入 non-ff 重试）、untracked_only（纯
    未跟踪零相撞——exclude/mv 处置）。porcelain 未跟踪目录折叠（?? dir/）
    按前缀判撞（实证：git 对 ignored 路径径直覆写，撞面必为非忽略未跟踪）。
    返回 (form, tracked, untracked, colliding)。"""
    tracked = [r[3:] for r in records if not r.startswith("?? ")]
    untracked = [r[3:] for r in records if r.startswith("?? ")]
    colliding = [u for u in untracked if any(
        i == u.rstrip("/") or i.startswith(u.rstrip("/") + "/")
        for i in incoming)]
    if tracked:
        form = "tracked_dirty"
    elif colliding:
        form = "untracked_collision"
    else:
        form = "untracked_only"
    return form, tracked, untracked, colliding


def _capped(paths):
    uniq = list(dict.fromkeys(paths))[:_DIRTY_CAP]  # 保序去重＋行上限
    if len(paths) > _DIRTY_CAP:
        uniq.append(f"…共 {len(paths)} 条，仅列前 {_DIRTY_CAP} 行")
    return uniq


def dirty_face_detail(records, incoming=()):
    """三形明细 note（delivery_failed 的 note 通道送达派发方）。records 空
    ＝脏面不在主检出 porcelain（持有目标支的其他 worktree/竞窗已消）——
    如实降级不猜形。"""
    if not records:
        return ("dirty form: unknown——目标仓 porcelain 取数全净（脏面可能"
                "在持有目标支的其他 worktree）；人工 git worktree list 判读")
    form, tracked, untracked, colliding = _dirty_face(records, incoming)
    if form == "tracked_dirty":
        lines = [f"dirty form: tracked_dirty——{len(tracked)} 处已跟踪改动"
                 "（M/A/D/R/C），requires human adjudication（绝不自动清洗/"
                 "stash/rm）:"]
        lines += _capped(tracked)
        if untracked:  # 混合脏面：未跟踪随行面一并点名（处置归下句）
            lines.append(f"另有 {len(untracked)} 条未跟踪路径随行: "
                         + ", ".join(_capped(untracked)))
        return "\n".join(lines)
    if form == "untracked_collision":
        lines = ["dirty form: untracked_collision——未跟踪路径与合并内容相撞"
                 "（git: untracked working tree files would be overwritten "
                 "by merge——gt-fc6fb2 家族，非 non-ff，禁整链重试盲试）。",
                 # 处置句先行（评审环 R2）：路径清单再长也不丢「禁 exclude」
                 # 指引——相撞路径 exclude 后合并会静默覆写丢字节（实证）
                 "disposition: mv them out (byte-preserving quarantine) "
                 "- never rm；相撞路径禁走 exclude——排除后合并会静默"
                 "覆写丢字节（实证 git 对 ignored 路径径直 clobber）",
                 f"colliding untracked paths (cap {_DIRTY_CAP}):"]
        lines += _capped(colliding)
        return "\n".join(lines)
    lines = [f"dirty form: untracked_only——{len(untracked)} 条未跟踪路径，"
             "与合并内容零相撞。",
             # 处置句先行（评审环 R2）：路径清单再长也不把规格原句挤出帽口
             _DIRTY_DISPOSITION,
             f"untracked paths (cap {_DIRTY_CAP}):"]
    lines += _capped(untracked)
    return "\n".join(lines)


def dirty_face_warning(records):
    """派发时脏仓预警行（ag-9a680b）：必含「target repo is dirty」＋三形
    摘要＋路径计数——warn-only，派发不受拦（终局拦截归交付门 dirty 面）。
    派发时无 incoming 可判碰撞：纯未跟踪按 untracked_only 汇总（保守）。"""
    if not records:
        return ""
    form, tracked, untracked, _ = _dirty_face(records)
    paths = tracked + untracked
    sample = ", ".join(paths[:3])
    return (f"target repo is dirty: {form}（{len(tracked)} tracked + "
            f"{len(untracked)} untracked path(s): {sample}"
            + (f" 等 {len(paths)} 条" if len(paths) > 3 else "")
            + "）——merge gate 将按 local_target_dirty 终局拦截")


# F32 门环境消毒：服务自身态三变量不随 env 泄入 verify 子进程链——test_install.sh
# 类自测拉起的 daemon 经 main.py os.environ.get("AGORA_PORT","0") 硬绑服务在用端口即崩。
# AGORA_HOME 明示不剥：无事故面，夹具本就自设各自值。
_GATE_ENV_STRIP = ("AGORA_PORT", "AGORA_OC_URL", "AGORA_OC_PASSWORD")

# ag-67650e 门构建 cgroup 隔离：verify 子进程落独立 slice，oomd 连坐不再击杀
# daemon 单元（11-23 事故：验收构建与 daemon 同 cgroup，5.8G 构建触发 oomd 杀
# 整单元）。unit 模板（MemoryMax/ManagedOOMMemoryPressure）见
# docs/agorad-oomd-isolation.md——宿主 unit 更新由派发方合并后处理。
_GATE_SLICE = "agora-gate.slice"

# Req: F98 交付锁有界等待：等锁超时＝一次失败尝试，绝不无限阻塞串行门
# （数值沿用原 push 网络上界口径——F109 网络面已删，常量仅作交付锁等待上界）
_LOCK_WAIT = 300
_LOCK_POLL = 0.05
# P0-1 门级看门狗硬下限（ag-9c4cbb）：缺省 deadline=max(4×生效 verify 预算, 600s)
_WATCHDOG_FLOOR = 600


class SpawnTracker:
    """P0-1 门运行期子进程组登记簿（ag-9c4cbb；F112 按 owner 线程分域）。

    gate 路径全部 spawn（verify runner＋gitx._git）以 start_new_session 自立
    进程组并登记 pgid；门看门狗到点按域整组收杀——孤儿孙进程（ssh/传输）
    不再持管道挂死门线程（2026-11-16 生产 wedge：ag-9e055d 门线程 blocked
    1.5h）。F112（门按仓分池）起按 owner 线程分域：activate() 认主＝登记
    当前线程域，add/killed 只认本线程域，kill_thread(ident) 收杀指定域
    （看门狗定时器线程触发，owner 域精确收杀不波及并行车道）；kill_all()
    收杀当前线程域（同线程调用兼容形态）。HTTP 线程并发的 _git（dispatch
    建 env 等）不入册（其线程未开域）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_thread = {}   # owner ident -> set(pgids)
        self._killed = set()   # 已收杀的 owner ident

    def activate(self):
        """开域（run_guarded 起点）：当前线程登记为 owner，清场复位。"""
        with self._lock:
            self._by_thread[threading.get_ident()] = set()
            self._killed.discard(threading.get_ident())
        return threading.get_ident()

    def deactivate(self):
        with self._lock:
            self._by_thread.pop(threading.get_ident(), None)
            self._killed.discard(threading.get_ident())

    def add(self, pgid):
        with self._lock:
            dom = self._by_thread.get(threading.get_ident())
            if dom is not None:
                dom.add(pgid)

    def discard(self, pgid):
        with self._lock:
            for dom in self._by_thread.values():
                dom.discard(pgid)

    def killed(self):
        """本线程域已被告警收杀（runner 据此让位 124 分类，不伪装质量败）。"""
        with self._lock:
            return threading.get_ident() in self._killed

    def kill_thread(self, ident):
        """到点收杀指定 owner 域：标记 killed＋SIGKILL 其全部在册进程组。"""
        with self._lock:
            self._killed.add(ident)
            pgids = list(self._by_thread.get(ident) or ())
            self._by_thread[ident] = set()
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # 与正常退出竞速：组已消失即无需补杀

    def kill_all(self):
        """同线程兼容形态：收杀当前线程域。"""
        self.kill_thread(threading.get_ident())


def _roster_from_file(path):
    """F35 名册解析：按行 strip，丢空行与 # 注释行；缺文件/不可读（OSError 与
    非 UTF-8 解码败）＝空名册＝现状全净。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _roster_gate_patterns(roster):
    """F35 名册并入闸体检测面：目录形条目（/ 结尾）展开「本目录＋子树」两条，其余逐字。"""
    out = []
    for entry in roster:
        if entry.endswith("/"):
            base = entry.rstrip("/")
            out += [base, base + "/*"]
        else:
            out.append(entry)
    return out


def _auto_ignore_patterns(records):  # Req: F97
    """全脏面皆「根级未跟踪隐藏目录」（`?? .dir/` 整目录形态）→返回目录名册
    条目；任一条不匹配（跟踪改动/非隐藏/根级散文件/嵌套新文件）→None 不豁免。
    目录判定吃 porcelain 的尾 `/`：git 对未跟踪目录整目录折叠正是该形态。"""
    out = []
    for rec in records:
        if not rec.startswith("?? "):
            return None
        path = rec[3:]
        base = path[:-1] if path.endswith("/") else ""
        if (not base or "/" in base or not base.startswith(".")
                or base in (".", "..")
                or any(ord(c) < 32 or c == "\x7f" for c in base)
                or any(c in "\\*?[]\ufffd" for c in base)):
            return None  # 控制字符写劈名册行、glob 元字符/替换符入册即活模式
                         # （`.*`→全仓未跟踪面致盲、`.tmp*`→过配漏配），不自动豁免
        if base + "/" not in out:
            out.append(base + "/")
    return out or None


def _append_roster(path, patterns):  # Req: F97
    """中央名册幂等追加：字节面比对防重、既有文本无尾换行先补防粘连
    （apply_ignore_roster 同款纪律）；缺文件＝新建，父目录缺＝建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = p.read_bytes() if p.exists() else b""
    have = set(data.splitlines())
    missing = [e for e in patterns if e.encode("utf-8") not in have]
    if not missing:
        return
    if data and not data.endswith(b"\n"):
        data += b"\n"
    p.write_bytes(data + "\n".join(missing).encode("utf-8") + b"\n")


def _cmd_dead(rc, out):
    """Req: F82 命令死判定单点（三运行点共用）：rc∈{126,127}（shell 自身退出码
    语义）或 rc==2 且输出含 ENOENT 文案（ag-c49b21 原始形态：命令在、文件参数
    缺失）。文案仅作 rc==2 的加严条件、不独立触发（rc=1 带同文案不入死类）；
    locale 非 C 漏报＝退化回活差老路径，无害。"""
    return rc in (126, 127) or (rc == 2 and "No such file or directory" in out)


def classify_failure(rc, out="", error=None):  # Req: F122
    """机械失败分型纯单源（路由与 artifact meta 共用；与 _cmd_dead 同居本模块
    ——core 禁兄弟 import）：runner 异常→runner_exception；rc==0→green；
    rc==124→overrun（F36 硬顶）；_cmd_dead 语义→command_dead（F81/F82 复用
    同函数）；rc<0 或 rc∈[129,165]→signal（subprocess 负值＝真信号死、
    shell 128+n 形态：137 SIGKILL/139 SIGSEGV/134 SIGABRT；128 与 255 歧义大
    仍归 content）；其余非零→content_failed。"""
    if error is not None:
        return "runner_exception"
    if rc == 0:
        return "green"
    if rc == 124:
        return "overrun"
    if _cmd_dead(rc, out):
        return "command_dead"
    if rc < 0 or 129 <= rc <= 165:
        return "signal"
    return "content_failed"


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # Req: F123 export 注入键


def _with_shell_exports(cmd, env):
    """Req: F123 环境变量 export 前缀注入：`export K=V ...; <cmd>`——对整条
    shell 命令链生效。禁用 `VAR=x cmd` 前缀赋值：只作用于首条命令
    （`cd x && pytest` 形态下 pytest 读不到）。KEY/VALUE 安全校验＋
    shlex.quote（禁换行/分号注入），坏值抛 ValueError（调用方全为内部
    定值：变量名常量＋git sha 值）。"""
    if not env:
        return cmd
    parts = []
    for key, value in env.items():
        value = str(value)
        if not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"export 注入变量名非法: {key!r}")
        if not value or re.search(r"[\s\n\r;`$\"'\\]", value):
            raise ValueError(f"export 注入值非法: {key}")
        parts.append(f"{key}={shlex.quote(value)}")
    return f"export {' '.join(parts)}; {cmd}"


def _scoped_cmd(cmd):
    """ag-67650e 门构建 cgroup 隔离：verify 命令包进 systemd-run --user --scope
    落 agora-gate.slice（与 daemon 不同 cgroup，oomd 连坐不再杀服务单元）。

    systemd-run 缺失直接回退现状直跑；user bus 不可达（cron/CI 态拉起 daemon）
    由命令内探针运行时回退——两分支都不改 verify 语义/超时/输出捕获/退出码分类：
    --scope 透传 stdio/env/cwd 且命令树留在原进程组（killpg proc.pid 杀伤口径
    不变，探针实证）。两分支同走 sh -c 引号体：cmd 内保留字（fi/else）不破壳。
    """
    if not shutil.which("systemd-run"):
        return cmd
    body = shlex.quote(cmd)
    probe = ("systemd-run --user --scope --quiet "
             f"--slice={_GATE_SLICE} sh -c true 2>/dev/null")
    scope = (f"systemd-run --user --scope --quiet --slice={_GATE_SLICE} "
             f"sh -c {body}")
    return f"if {probe}; then {scope}; else sh -c {body}; fi"


def _subprocess_runner(env_dir, cmd, timeout=300, ceiling=None, on_soft=None,
                       tracker=None):
    """真 runner：独立进程组跑 verify，F36 软硬双顶。

    软顶＝timeout：到点不杀，on_soft() 恰一次（None 跳过），继续等；硬顶＝ceiling
    （缺省 4×软顶）：到点 SIGKILL 全组 rc=124。循环 communicate（CPython 契约：
    TimeoutExpired 后重试不丢已读输出）。F32 门环境消毒：剥除 _GATE_ENV_STRIP
    三枚服务自身态变量后以显式副本传子进程（不改 os.environ 本体）。输出管道显式
    UTF-8＋errors=replace 解码（ag-830b1b：子进程非 UTF-8 字节输出曾于 communicate
    内 UnicodeDecodeError 门崩——非法字节降级 U+FFFD，rc 与输出尾照常捕获）。
    tracker（P0-1）：spawn 入 SpawnTracker 登记簿（门看门狗兜底收杀）；看门狗已
    收杀（tracker.killed）即让位 124 分类，不把杀组伪装成质量败或普通退出。
    """
    ceiling = 4 * timeout if ceiling is None else ceiling
    child_env = {k: v for k, v in os.environ.items() if k not in _GATE_ENV_STRIP}
    proc = subprocess.Popen(_scoped_cmd(cmd), shell=True, cwd=env_dir,
                            env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    if tracker is not None:
        tracker.add(proc.pid)
    try:
        soft_fired, start = False, time.monotonic()
        while True:
            now = time.monotonic()
            waits = [_VERIFY_POLL, ceiling - (now - start)]
            if not soft_fired:
                waits.append(timeout - (now - start))
            try:
                out, err = proc.communicate(timeout=max(min(waits), 0.001))
                if tracker is not None and tracker.killed():
                    return 124, ((out or "") + (err or "")
                                 + "\nverify 进程组被门看门狗终止（gate_overrun）")
                return proc.returncode, (out or "") + (err or "")
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                if not soft_fired and elapsed >= timeout:
                    soft_fired = True  # 软顶拍不杀：提醒恰一次（进程已退则免提醒）
                    if proc.poll() is None and on_soft is not None:
                        try:
                            on_soft()
                        except Exception:  # 提醒链故障不废硬顶杀保证：杀分支必须可达
                            pass
                if elapsed >= ceiling:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # 超时与正常退出竞速：进程组已消失即无需补杀
                    try:  # 有限宽限收尾：逃逸后代持管道时弃读，防门线程挂死
                        out, err = proc.communicate(timeout=2 * _VERIFY_POLL)
                    except subprocess.TimeoutExpired:
                        for pipe in (proc.stdout, proc.stderr):
                            if pipe:
                                pipe.close()
                        out, err = "", "（逃逸进程仍持有输出管道，已弃采）"
                    overrun = "" if soft_fired else f"（软顶 {timeout:g}s 未提醒：硬顶≤软顶）"
                    return 124, ((out or "") + (err or "") +
                                 f"\nverify 超硬顶（>{ceiling:g}s{overrun}）"
                                 "——已终止验证进程组；可用 AGORA_VERIFY_CEILING 调整上限")
    finally:
        if tracker is not None:
            tracker.discard(proc.pid)


class Gate:
    """accept_done 单写者串行受理；run 为后台线程体（httpd 包 Thread 调用）。"""

    def __init__(self, state, gitx, router, oc, read_anchor, health, auth,
                 store, runner=None, verify_timeout=300, verify_ceiling=None,
                 on_blocked=None, roster_path=None, quick_max_files=3,
                 quick_max_lines=30, clean_ignore_env="",
                 page_writer=None, spawn_tracker=None,
                 resources=None, artifact_store=None,
                 infra_grace=1800.0):  # F35 名册；F65 quick；ag-f5aa4d 豁免 env；ag-0f60fd 页面刷新；F116 跨进程资源闸；F122 执行产物存储；Req: F130 infra 宽限
        self._state, self._gitx, self._router, self._oc = state, gitx, router, oc
        self._read_anchor, self._health, self._auth = read_anchor, health, auth
        self._store, self._on_blocked = store, on_blocked
        self._pages = page_writer  # ag-0f60fd：门段任务页刷新面（_audit 单点挂钩）
        self._roster_path = roster_path
        self._clean_ignore_env = clean_ignore_env  # ag-f5aa4d 数据层豁免 env 面
        self._verify_timeout, self._verify_ceiling = verify_timeout, verify_ceiling
        self._quick_max_files, self._quick_max_lines = quick_max_files, quick_max_lines
        self._runner = runner  # None＝run() 按 tid 构造默认 runner（F36 软顶提醒绑任务）
        self._resources = resources  # F116：None＝无跨进程闸（单测直构），assemble 恒注入
        self._artifacts = artifact_store  # Req: F122：None＝单测直构直通零落盘，main 生产注入
        self._infra_grace = infra_grace  # Req: F130：verify_infra 驻留宽限（秒）
        # P0-1（ag-9c4cbb）：门运行期 spawn 登记簿（与 Gitx 同实例注入＝门线程
        # 内全部子进程组可被看门狗整组收杀）；killed 标记即 run() 异常路径的
        # 让位判据（activate() 每轮复位——重派重跑的门内崩不被旧轮标记误吞）
        self._spawns = spawn_tracker if spawn_tracker is not None else SpawnTracker()

    # ---- done 受理（前置六拒，拒收落审计并附动作指引） ----

    def accept_done(self, tid, worker_sid, commit):
        t = self._state.get(tid)
        if t["state"] != "running":  # 含二次 done：防重入门
            return self._deny_done(
                tid, "state",
                f"任务 {tid} 当前 {t['state']} 非 running——"
                "board 查看状态，仅 running 受理 done")
        if t["verify"]["state"] != "registered":
            return self._deny_done(
                tid, "verify-unregistered",
                f"任务 {tid} verify 未登记——"
                "post 通知派发方 task_update 登记 verify 命令，"
                "登记后重新 post(done, commit)")
        if not t["verify"]["cmd"].strip():  # 兜底：空命令＝无质量闸，拒入门
            return self._deny_done(
                tid, "verify-empty",
                f"任务 {tid} verify 命令为空——"
                "task_update 登记非空命令后重新 post(done, commit)")
        if t.get("deliverable") == "out_of_band":  # ag-af96bb②：review-only 豁免分支
            return self._accept_out_of_band(tid, worker_sid, commit)
        c = commit.lower()
        if len(c) != 40 or any(ch not in "0123456789abcdef" for ch in c):
            return self._deny_done(  # F13：body-format 先行，参数错不再伪装成锚问题
                tid, "body-format",
                f"done body 须为纯 40 位十六进制完整 commit sha（收到 {commit[:20]!r}）"
                "——无前缀无附言；结论文本另发 post(kind=note)")
        commit = c  # 归一一次：锚比较与落库均用小写，大写 sha 不再伪装成锚问题（OCR）
        open_children = self._open_children(worker_sid)
        if open_children:
            return self._deny_done(
                tid, "children",
                f"嵌套子任务未结案：{', '.join(sorted(open_children))}——"
                "等待 result 回传，或由你取消子任务；成功/有意跳过后再 post(done)")
        # 锚检查＝合并前置（ag-084a22 A）：锚存在 ∧ 锚==done commit ∧ 评审环绿，
        # 不满足即拒收（无条件——评审环缺失不再放行入门）——死锁驻留态不可达。
        # Req: F114 门审任务（review_cmd 在案）跳过整段锚申报面：合并权威＝
        # 门亲自跑 review_cmd，worker 的「评审过」申报不再是被信任的输入。
        if (t.get("review_cmd") or "").strip():
            a = None  # F114：无锚路径——vacuous/子任务闸照常，锚三检＋链根全免
        else:
            a = self._read_anchor(t["env_dir"])  # 锚＝worktree 分支 HEAD 且无 blocker/high
        if not (t.get("review_cmd") or "").strip():
            if a is None:
                return self._deny_anchor(
                    tid, f"任务 {tid} 合并前置失败：锚缺失/落后（.review-loop/"
                    ".last-reviewed 缺失或不等于分支 HEAD，或 .review-loop/"
                    ".review-base 链根"
                    "缺失——F111 双件齐全才算在锚）——跑评审环重建锚（==HEAD 且"
                    "评审环绿）后重新 post(done, commit)")
            if a.get("head") != commit:
                return self._deny_anchor(
                    tid, f"任务 {tid} 合并前置失败：{commit} ≠ 分支 HEAD "
                    f"{a.get('head')}——以 env 内完整 HEAD sha 重新 post(done, commit)")
            # Req: F111 评审链根绑定任务起点：锚 base 必须等于 env_base——链根
            # 漂移/他链混入＝评审范围未覆盖任务全量交付；env_base 缺席（升级前
            # 在飞任务）＝判据不可确立，带痕放行（F107 同款降级口径）
            if t.get("env_base") and a.get("base") != t["env_base"]:
                return self._deny_anchor(
                    tid, f"任务 {tid} 合并前置失败：评审链根 {a.get('base')} ≠ "
                         f"任务起点 {t['env_base']}——评审范围未覆盖任务全量交付，"
                         "以任务起点为链根重跑评审环后重新 post(done, commit)")
        try:  # Req: F69 vacuous-done；谓词病（锚伪造 sha/目标支被删）不拦受理：
            # F109：探针只查本地支 ref（零网络）——「reopen 期间人工上游合入」
            # 不在受理时识别，由集成段幻影守卫/外部树等价逃生门兜底
            vacuous = self._gitx.contains(t["project_dir"], commit,
                                          t.get("target_branch"))
        except RuntimeError:  # gitx 失败契约（_git/contains 同款）；编程错误照崩不静默
            self._audit(tid, "zero_commit_probe_failed")  # 带痕放行：run() 同款异常面兜底
            vacuous = False
        if vacuous and not self._vacuous_delivered(tid, t, commit):
            return self._deny_anchor(  # 真零提交（空集且无在案交付）＝锚拒同款弹回
                tid, f"任务 {tid} 零提交 done 拒收：本任务相对目标分支无新增提交"
                "——若无需改动或活已被上游完成，post(kind=question, body=no-op "
                "结论与证据) 申报——需派发方处置（cancel 归档/确认）；question "
                "唤醒派发方、note 不唤醒勿用；勿空回填 done", tag="vacuous-done")
        if a is not None and not a.get("clean"):  # F114：门审任务 a=None 全免
            return self._deny_anchor(
                tid, f"任务 {tid} 合并前置失败：评审环未绿——修复 blocker/high "
                "后重新 post(done, commit)")
        usage = self._collect_usage(worker_sid)  # oc 可失败原语先于 transition：败则零账面
        self._state.transition(tid, "done", commit=commit,
                               rebased_from=None)  # F32：受理即清陈旧留痕（门内直通对齐处亦清）
        if usage is not None:
            self._state.set_fields(tid, usage=usage)
        self._audit(tid, "done", commit=commit)
        return "accepted"

    def _open_children(self, worker_sid):
        """嵌套归属闸（受理共用）：申报 worker 名下未结案子任务 id 列表。"""
        children = [(cid, x) for cid, x in self._state.snapshot()["tasks"].items()
                    if x.get("dispatcher_sid") == worker_sid]
        return [cid for cid, x in children
                if x["state"] not in ("merged", "closed", "cancelled")
                or str(x.get("cancel_reason", "")).startswith("dependency_failed")]

    def _vacuous_delivered(self, tid, t, commit):
        """ag-1ae0f1 空集放行判据：rev-list(目标..HEAD)=空集不再自动等价零提交。
        任一成立＝有效交付放行：(a) env HEAD 相对任务起点 base 有前进（base 为
        祖先且 ≠HEAD——交付提交挂目标支自身的 hotfix 形态）；(b) task 行既有
        commit 字段（前次受理留痕，如 reopen 期间人工合入）在目标支可达——与
        门体 run() 崩溃重排直通探针同语义。均不成立＝真零提交照拒。env_base
        缺席（升级前在飞任务）＝判据 (a) 不可确立，按旧口径拒；判据谓词病＝
        「未确立」不放行（带痕审计），与主探针 fail-open 分工：拒收须正据。"""
        prev = t.get("commit")
        if prev == commit:
            return True  # (b) 等价：空集主探针已证本 commit 目标支可达
        try:
            base = t.get("env_base")
            if base and commit != base \
                    and self._gitx.is_ancestor(t["project_dir"], base, commit):
                return True  # (a)
            if prev and self._gitx.contains(t["project_dir"], prev,
                                            t.get("target_branch")):
                return True  # (b)
        except RuntimeError:  # gitx 失败契约；编程错误照崩不静默
            self._audit(tid, "zero_commit_probe_failed")
        return False

    def _accept_out_of_band(self, tid, worker_sid, ref):
        """ag-af96bb② review-only（deliverable=out_of_band）done 受理豁免分支：
        零提交＋交付物在案——sha 形状/锚/F69 vacuous 三检跳过（禁改仓任务结构性
        无提交无锚）；state/verify/children 三闸照常；交付物引用＝done body 本体
        （邮件直达派发方＋done 审计 note 留痕），verify 仍跑。F69 语义不变：
        未标记任务照走既有六拒。"""
        ref = ref.strip()
        if not ref:
            return self._deny_done(
                tid, "body-format",
                f"任务 {tid} review-only done body 须为交付物引用（收到空 body）"
                "——post(done, body=路径/链接/结论)")
        open_children = self._open_children(worker_sid)
        if open_children:
            return self._deny_done(
                tid, "children",
                f"嵌套子任务未结案：{', '.join(sorted(open_children))}——"
                "等待 result 回传，或由你取消子任务；成功/有意跳过后再 post(done)")
        usage = self._collect_usage(worker_sid)  # oc 可失败原语先于 transition：败则零账面
        self._state.transition(tid, "done")  # 零提交：无 sha 入行
        if usage is not None:
            self._state.set_fields(tid, usage=usage)
        self._audit(tid, "done", note=ref[:200])
        return "accepted"

    def _collect_usage(self, worker_sid):
        """done 受理点 token 用量采集（F57）：assistant 消息 tokens/cost 求和落行。

        usage={input,output,reasoning,cache_read,cache_write,cost:float,msgs}
        （msgs＝全消息数）；oc 异常/空会话/无 assistant→None＝usage 缺席——
        经济性数据永不阻塞验收门；/message 单页缺省 50 条＝长会话用量已知低估。"""
        try:
            msgs = self._oc.messages(worker_sid)
        except Exception:  # 降级缺席照受理（含旧注入面无 messages 的 AttributeError）
            return None
        if not msgs:
            return None
        usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
                 "cache_write": 0, "cost": 0.0, "msgs": len(msgs)}

        def _num(v, zero):
            return v if isinstance(v, (int, float)) else zero  # 值漂移该字段按 0 计，采集不炸

        seen = False
        for m in msgs:
            if not isinstance(m, dict) or m.get("type") != "assistant":
                continue
            tk = m.get("tokens")
            if not isinstance(tk, dict):  # 形状漂移守卫：该条不计，采集不炸
                continue
            seen = True
            for k in ("input", "output", "reasoning"):
                usage[k] += _num(tk.get(k), 0)
            cache = tk.get("cache")
            if isinstance(cache, dict):
                usage["cache_read"] += _num(cache.get("read"), 0)
                usage["cache_write"] += _num(cache.get("write"), 0)
            usage["cost"] += _num(m.get("cost"), 0.0)
        return usage if seen else None  # 无 assistant＝无计量，不设键

    def _deny_anchor(self, tid, msg, tag="anchor"):
        """ag-084a22 A/C：锚拒收＝合并前置败——计连败（3 连败 blocked 拍人）＋带因拒回。"""
        self._state.fail_streak_bump(tid)
        self._escalate_if_blocked(tid)
        return self._deny_done(tid, tag, msg)

    def _deny_done(self, tid, tag, msg):
        """F13：done 拒收统一出口——先审计 done_rejected（note=tag）再带因拒回。
        ag-d03e0e 拒因落消息面：拒因原文持久化为任务消息（from=agorad、
        kind=note、只落库不注入——worker 同回合已收同步 MCP 报错；单线程
        HTTP 环零网络 I/O），check_mail/消息库可查，fail_streak 不再是唯一
        痕迹；留痕面尽力而为（F54）：败不吞同步拒回。"""
        reason = f"rejected:{tag}: {msg}"
        self._audit(tid, "done_rejected", note=tag)
        try:
            self._router.notify_worker(tid, "note", reason)
        except Exception:
            pass
        return reason

    def _ensure_clean_auto(self, tid, target, exempt, auto_ignored,
                           roster=None):  # Req: F97
        """ensure_clean＋根级未跟踪隐藏目录自动豁免：全脏面皆可豁免→追加中央
        名册、幂等写 exclude、同轮重试一次；混合脏面原样上抛（不部分豁免）。
        roster 原位并入自动条目——F35 纪律：入册路径必须进闸体检测面。"""
        try:
            self._gitx.ensure_clean(target, exempt)
        except RuntimeError as e:
            patterns = _auto_ignore_patterns(getattr(e, "records", ()))
            if not patterns or not self._roster_path:
                raise
            _append_roster(self._roster_path, patterns)
            self._gitx.apply_ignore_roster(target, patterns)
            self._audit(tid, "auto_ignore", note=" ".join(patterns))
            auto_ignored.extend(patterns)
            if roster is not None:
                roster.extend(p for p in patterns if p not in roster)
            self._gitx.ensure_clean(target, exempt)

    # ---- 门体（后台线程体） ----

    def run_guarded(self, tid, deadline=None):
        """P0-1 门级看门狗（ag-9c4cbb，httpd consume 唯一门入口）：整体硬顶包
        run()。到点 kill_all 本门运行期登记的全部子进程组（杀组解链 communicate）
        ＋审计 gate_overrun；run() 返回后终局 done→blocked（delivery failed，
        error=gate_overrun——非质量败不弹 worker 不计连败，处置权在派发方），门
        队列继续下一任务。deadline 缺省 max(4×生效 verify 预算, 600s)，注入即
        测试秒级跑。shortcut: run() 卡在无子进程的纯 Python 环节（锁/死循环）时
        看门狗只留痕不能解卡——恢复需重启；生产 wedge 形态（孤儿持管道）已由
        P0-2 从源头消灭。F116：资源闸（repo 锁＋全局槽）在定时器起表前
        获取——排队时间不计看门狗/verify 预算（软硬顶自获锁后起算）。"""
        try:
            t = self._state.get(tid)
        except KeyError:
            raise  # 崩溃归 httpd gate_crashed 留痕
        if deadline is None:
            budget = t["verify"].get("timeout") or self._verify_timeout
            deadline = max(4 * budget, _WATCHDOG_FLOOR)
        fired = threading.Event()
        owner = threading.get_ident()  # F112：车道线程域——定时器线程收杀定向

        def _fire():
            fired.set()
            self._spawns.kill_thread(owner)
            try:
                self._audit(tid, "gate_overrun")
            except Exception:
                pass  # 审计面尽力而为：杀组解链不因留痕败而回退

        res = self._acquire_resources(tid, t)  # F116：跨进程 repo 锁→全局槽
        try:
            self._spawns.activate()
            timer = threading.Timer(deadline, _fire)
            timer.daemon = True
            try:
                timer.start()
                try:
                    self.run(tid)
                finally:
                    timer.cancel()
                try:
                    if fired.is_set() and self._state.get(tid)["state"] == "done":
                        self._overrun_final(tid, deadline)
                except Exception:  # 终局竞窗（任务已被处置/账面 I/O 病）：留痕已在，不强写
                    pass
            finally:
                self._spawns.deactivate()
        finally:
            self._release_resources(tid, res)

    def _acquire_resources(self, tid, t):
        """F116 跨进程资源闸：repo 锁→全局槽（恒此序，无环）。排队前先落
        waiting 投影＋wait 事件（board 可观测），阻塞期间任务状态仍 done、
        fail_streak 冻结；获锁即 acquire 事件＋投影翻转。闸面异常照崩走
        httpd gate_crashed 留痕（不伪装质量败）。resources=None（单测直构）
        ＝零闸直通。"""
        if self._resources is None:
            return None
        repo, limit = t["project_dir"], self._resources.max_concurrent
        self._state.set_gate_resource(tid, waiting=True, resource=repo,
                                      limit=limit)
        try:  # wait 审计入清理域：任何路径 waiting 都有翻转（不留幽灵排队）
            self._audit(tid, "gate_resource_wait", repo=repo, limit=limit)
            handle = self._resources.acquire(repo)
        except BaseException:
            self._state.set_gate_resource(
                tid, waiting=False, resource=repo, limit=limit,
                error="acquire_failed")
            raise
        self._state.set_gate_resource(
            tid, waiting=False, resource=repo, limit=limit, slot=handle.slot,
            acquired_at=handle.acquired_at, waited=round(handle.waited, 3))
        try:  # 获锁观测尽力而为：败不丢 handle（调用方 finally 仍释放）
            self._audit(tid, "gate_resource_acquire", repo=repo, limit=limit,
                        slot=handle.slot, duration=round(handle.waited, 3))
        except Exception:
            pass
        return handle

    def _release_resources(self, tid, handle):
        """F116 出队释放（finally 面）：取消/除籍/崩溃路径同款全释放——
        flock 本就随进程死由内核回收，这里是正常路径与观测收口。"""
        if handle is None:
            return
        handle.release()
        held = time.monotonic() - (handle.acquired or time.monotonic())
        try:  # 释放面观测尽力而为：败不掩 run() 原发异常（_fire 同口径）
            self._state.set_gate_resource(
                tid, waiting=False, resource=handle.repo, limit=handle.limit,
                slot=handle.slot, acquired_at=handle.acquired_at,
                released_at=time.time())
            self._audit(tid, "gate_resource_release", repo=handle.repo,
                        limit=handle.limit, duration=round(held, 3))
        except Exception:
            pass

    def _overrun_final(self, tid, deadline):
        """P0-1 overrun 终局：delivery failed(error=gate_overrun)＋done→blocked
        （F98 交付失败同款专用边）。"""
        t = self._state.get(tid)
        d = dict(t.get("delivery") or {})
        d.update(status="failed", error="gate_overrun")
        self._state.set_fields(tid, delivery=d)
        self._state.transition(tid, "blocked")
        try:
            self._router.notify_transition(
                tid, "blocked",
                body=f"任务 {tid} 看门狗超限（gate_overrun，>{deadline:g}s）——"
                     "已 blocked（非质量败，不计连败）；进程组已终止，处置后重派或取消")
        except Exception:  # 通知面尽力而为（F54 口径）——不逆转终局
            pass

    def run(self, tid):
        t = self._state.get(tid)
        if t["state"] != "done":
            return  # F36 R2：驻留取消直结后，重启残留队列里的非 done 任务不跑门
        branch = t.get("target_branch")  # 合入目标支（外部工作流）；缺省＝仓默认支
        explicit = branch is not None  # ag-bf61e7：分支位还原面只挂显式支（缺省支任务不还原）
        env, target, cmd = t["env_dir"], t["project_dir"], t["verify"]["cmd"]
        integration = t.get("gate_env") or f"{env}__gate"
        origin = t.get("gate_origin_branch")  # 崩溃重排沿用首轮分支位，不采信漂移后的现场
        budget = t["verify"].get("timeout") or self._verify_timeout  # F51 两级取值
        ceiling = (4 * budget if t["verify"].get("timeout")
                   or self._verify_ceiling is None
                   else self._verify_ceiling)  # 任务级硬顶固定 4×；服务级 F36 旋钮缺省 4×
        runner = self._runner
        if runner is None:  # F36 默认 runner：软顶提醒闭包绑本任务＋生效预算
            runner = lambda env, cmd: _subprocess_runner(  # noqa: E731
                env, cmd, timeout=budget, ceiling=ceiling,
                on_soft=lambda: self._notify_verify_soft(tid, budget),
                tracker=self._spawns)
        else:  # F51：注入 runner 经 kwargs 观察生效预算（硬顶＝4×生效值单测缝）
            runner = lambda env, cmd: self._runner(env, cmd, timeout=budget,
                                                   ceiling=ceiling)
        auto_ignored = []  # Req: F97 本轮自动豁免清单（merged 通知外带）
        self._state.set_fields(tid, in_gate=True, gate_env=integration,
                               verify_infra_grace=None)  # Req: F130 转 in_gate 撤销宽限计时
        try:  # F54 门启动拍：两通道各自尽力而为（同 notify_transition 口径），一侧败不压另一侧
            self._post(tid, "已入验证门：verify 运行中——绿/败另有消息")
        except Exception:
            pass
        try:
            self._router.notify_dispatcher(  # F-4 崩溃重排会重发一次，罕见窗口可接受
                tid, "result", f"任务 {tid} 已入验证门（verify 运行中）")
        except Exception:
            pass
        try:
            if t.get("deliverable") == "out_of_band":  # ag-af96bb② review-only：只验不合
                if not self._base_probe(tid, t, target, integration,
                                        runner, cmd):  # Req: F107 探针不豁免 review-only
                    return
                self._phase(tid, "verify")  # P1b 阶段审计
                rc, out = self._run_with_artifact(tid, "verify", env, cmd,
                                                  runner)  # Req: F122
                if rc == 124:
                    self._verify_overrun(tid, out, budget, ceiling)
                    return
                if _cmd_dead(rc, out):  # Req: F81/F82 命令自身死：分类路由，不弹 worker
                    self._verify_dead(tid, rc, out)
                    return
                if classify_failure(rc, out) == "signal":  # Req: F122
                    self._verify_infra(tid, rc, out)
                    return
                if rc != 0:
                    self._quality_fail(tid, "verify_failed",
                                       "verify 失败——修复后重新 post(done, "
                                       f"交付物引用)\n输出尾部:\n{out[-_TAIL:]}"
                                       f"{self._artifact_line(tid)}")
                    return
                self._state.fail_streak_reset(tid)  # 全绿清连败（R12，_succeed 同口径）
                self._state.transition(tid, "merged")  # 复用门尾 merged→closed 结算链
                self._audit(tid, "merged", note="out_of_band")  # 零合入留痕
                self._router.notify_transition(  # ag-acae3b 直投；closed 门尾邻对窗吞
                    tid, "merged", "review-only 交付（verify 绿，零合入）")
                self._store.remove_credential(tid)  # F43：先删牌——env 回收败不得拦断
                self._reclaim_env(tid, t["env_dir"], "gate:out_of_band")  # Req: ag-0e6ca3
                self._post(tid, "review-only 交付在案（零合入）——环境已回收")
                return
            # Req: F100 managed 判据拆分：delivery.mode=managed＋钉死目标支即入
            # 交付协议。ag-b5f2e9 用户裁定：managed 一律本地权威交付
            # （_deliver_managed_local，F98 远端优先废除——有 origin 的仓
            # local=pending 账面根因）。legacy delivery=None 行保持本地门不变
            delivery = t.get("delivery") or {}
            managed = delivery.get("mode") == "managed" and branch is not None
            external = delivery.get("mode") == "external_branch"  # Req: F101
            if external and delivery.get("status") == "awaiting_external":
                # Req: F101 外部确认驻留：done+awaiting_external 重排（重启 done
                # 残留恰一次）直通——不重跑 handoff、不发明结果、不过期等待
                return
            if external and delivery.get("status") == "delivered" \
                    and t["state"] == "done":
                # Req: F101 崩窗收敛：task_deliver(delivered) 已落账而 merged 未
                # 达——补迁移与回收，门尾（finally _tail）照常结算 merged→closed
                self._state.fail_streak_reset(tid)
                self._state.transition(tid, "merged", commit=t.get("commit"))
                self._audit(tid, "merged", commit=t.get("commit"),
                            note="external_delivered_recovery")
                self._router.notify_transition(
                    tid, "merged",
                    f"@{(t.get('commit') or '')[:7]} external delivered"
                    "（崩窗补终态）")
                self._store.remove_credential(tid)  # F43：先删牌——env 回收败不得拦断
                self._reclaim_env(tid, t["env_dir"], "gate:external_recovery")  # Req: ag-0e6ca3
                return
            if (managed or external) and delivery.get("status") == "failed":
                # Req: F100 崩窗收敛：failed 已落账而 blocked 迁移未达——补迁移，
                # 绝不自动重试交付（重试唯经派发方显式重派重置投影）
                if t["state"] == "done":
                    self._state.transition(tid, "blocked")
                    self._audit(tid, "delivery_failed_recovery")
                return
            if managed:  # 崩溃窗口补终态：本地目标支已含任务 commit 即收尾，不重验不重推
                # ag-b5f2e9 权威恒＝本地钉死支（origin 在位不问远端）。终闸 B 册：
                # 探针严格问 refs/heads/<branch>（F109 起 baseline 亦恒本地），
                # origin 含而本地不含交本地交付自愈（rebase 重放＋advance 真推进），
                # 绝不零推进谎报 synced
                try:
                    landed = self._gitx.is_ancestor(
                        target, t["commit"], f"refs/heads/{branch}")
                except RuntimeError:
                    landed = False  # 探针病照走投递路径（其内自有失败面）
                if landed:
                    self._finish_local_delivered(
                        tid, branch, t["commit"], None,
                        delivery.get("attempt", 0))
                    return
            elif external:
                pass  # Req: F101 外部权威在派发方（task_deliver）——无本地/远端 landed 补终态探针
            elif self._gitx.contains(target, t["commit"], branch):
                # kill 落在“目标已推进、merged 未落库”窗口：任务提交已在目标支即只补终态，
                # 绝不重跑集成 verify 后把已合法合并的任务假 reopened，更不需要 reset --hard。
                self._succeed(tid)
                return
            # ag-bf61e7 缺省支实名解析：基线与推进的单一事实源＝实时分支 ref，而非
            # 检出 HEAD——检出位漂移（回滚 checkout/人工切支）时 HEAD 判据会以陈旧位
            # 误判 non-ff，且 detached 下 merge --ff-only 只挪 HEAD、分支 ref 不动
            # （merged 落账但内容静默丢失）。detached 拒判如实上抛走门内 except；
            # 置于 contains 补终态探针之后——kill 窗口补终态优先于拒判（终闸 B 册）。
            # managed 恒钉死实名支，不进此路。
            if branch is None:
                branch = self._gitx.symref(target)
                if branch == "HEAD":
                    raise RuntimeError(
                        "目标检出已脱离分支（detached HEAD），无法解析缺省目标支"
                        "——恢复分支检出后重新 post(done, commit)")
            # F35：首道干净检查前把名册幂等写入目标仓 info/exclude——exclude 后
            # 名册路径不进 status，未跟踪名册路径在谓词外隐身（跟踪改动豁免归
            # clean-ignore，两机制分工见 README）。
            roster = _roster_from_file(self._roster_path) if self._roster_path else []
            if roster:
                self._gitx.apply_ignore_roster(target, roster)
            if not (managed or external):  # Req: F98/F101 交付协议任务不写主检出：干净硬闸/分支位记录零接触
                # ag-f5aa4d 数据层豁免：声明（仓根 clean-ignore ∪ 部署 env）传入硬闸，
                # 跟踪中的数据层心跳不再制造结构性脏（两处检查共用同一份豁免面）。
                exempt = self._gitx.clean_ignore(target, self._clean_ignore_env)
                self._ensure_clean_auto(tid, target, exempt, auto_ignored,
                                        roster)  # 主检出写前硬闸＋F97 自动豁免
                if explicit:  # 显式支才采分支位（缺省支实名解析不扩还原面）
                    origin = origin or self._gitx.symref(target)
                    self._state.set_fields(tid, gate_origin_branch=origin)
            if self.gate_files_touched(tid, list(_GATE_PATTERNS)
                                       + _roster_gate_patterns(roster),
                                       branch=branch):  # 闸体变更：拍人不拦门（ag-bf61e7 实名支同源）
                self._health.escalate(KIND_GATEFILES, tid)
            warn = self._gitx.diff_check(env, branch)  # 冲突标记机械检查，先于用户 verify
            if warn:  # gt-83185d R3：非空走 verify_failed 口径（截 2000 与 verify 败一致）
                self._quality_fail(tid, "verify_failed",
                                   "diff 检查发现冲突标记/空白错误——清理后重新"
                                   f" post(done, commit)\n输出尾部:\n{warn[-_TAIL:]}")
                return
            if t.get("quick"):  # F65 包络门：verify 前机械量累计 diff，超顶按 verify 红弹回
                files, lines, detail = self._gitx.diff_stats(env, branch)
                if files > self._quick_max_files or lines > self._quick_max_lines:
                    self._quality_fail(
                        tid, "verify_failed",
                        f"quick envelope exceeded: {files} files "
                        f"(max {self._quick_max_files}) / {lines} lines "
                        f"(max {self._quick_max_lines})"
                        "——缩减改动后重新 post(done, commit)"
                        f"\n输出尾部:\n{detail[-_TAIL:]}")
                    return
            if not self._base_probe(tid, t, target, integration,
                                    runner, cmd):  # Req: F107 base-red 探针
                return
            self._phase(tid, "verify")  # P1b 阶段审计
            rc, out = self._run_with_artifact(tid, "verify", env, cmd,
                                              runner)  # Req: F122
            if rc == 124:  # F36 超硬顶：分类路由派发方，不 transition、不弹 worker、不计连败
                self._verify_overrun(tid, out, budget, ceiling)
                return
            if _cmd_dead(rc, out):  # Req: F81/F82 命令自身死（不存在/不可执行/缺文件）≠活不达标
                self._verify_dead(tid, rc, out)
                return
            if classify_failure(rc, out) == "signal":  # Req: F122
                self._verify_infra(tid, rc, out)
                return
            if rc != 0:
                self._quality_fail(tid, "verify_failed",
                                   f"verify 失败——修复后重新 post(done, commit)"
                                   f"\n输出尾部:\n{out[-_TAIL:]}"
                                   f"{self._artifact_line(tid)}")
                return
            if (t.get("review_cmd") or "").strip():  # Req: F114 门审路径
                if not self._gate_review(tid, t, env, runner):
                    return
            else:
                a = self._read_anchor(env)  # 合并前锚重验：堵 accept→run 窗口 TOCTOU
                # 锚比对对象＝rebased_from or commit（F32 裁定①）：崩溃重排的 done 任务
                # （commit=R、锚=E）重入门用留痕 E 判过，才能走到再 rebase，不死锁。
                if not a or a.get("head") != (t.get("rebased_from") or t.get("commit")) \
                        or not a.get("clean") \
                        or (t.get("env_base") and a.get("base") != t["env_base"]):
                    self._audit(tid, "review_absent_no_merge")
                    self._quality_fail(  # ag-084a22 B：自动 reopened 回 worker 自愈
                        tid, "reopened",  # （无出边的驻留 done 态封死）
                        "合并前置失败：verify 绿但合并时 .review-loop 锚缺失/落后/"
                        "未绿/链根漂移（F111：.review-loop/.review-base ≠ 任务"
                        "起点）——重建锚（.last-reviewed ==HEAD 且评审环绿且 "
                        ".review-loop/.review-base == 任务"
                        "起点）后重新 post(done, commit)",
                        reason="锚缺失/落后/未绿/链根漂移")
                    return
            if managed:  # Req: F100 交付协议（ag-b5f2e9 一律本地权威）：集成基＝
                # 本地钉死支 tip，主检出经 advance 守卫推进，origin 零接触
                self._deliver_managed_local(tid, branch, env, integration,
                                            target, runner, cmd, budget,
                                            ceiling)
                return
            if external:  # Req: F101 外部交付：绿即驻留 awaiting_external 等确认
                self._deliver_external(tid, branch, env, integration,
                                       target, runner, cmd, budget, ceiling)
                return
            # 集成阶段全部落在 detached worktree：试合并/测试失败都不会改目标主检出。
            self._phase(tid, "integration")  # P1b 阶段审计
            self._gitx.build_integration(target, integration, branch)
            before = self._gitx.head(integration)
            e = t.get("rebased_from") or t.get("commit")  # done sha＝交付权威
            r = None  # R＝F32 重放后任务提交（None＝ff 试合并直通）
            # ag-28f327：ff 源＝done sha——worker 回报且已过 verify 的 commit 是
            # 交付权威；env 名分支可停旧基线（worker 自建工作分支交付），作源则
            # 零增量推进＝假终态/reopen 循环（ag-f77d90/ag-2f6c8a 判例）。
            if self._gitx.ff_sha(integration, e) != "ok":
                # F32 真非 ff：门内 detached rebase 干净自愈（env 分支与锚不动），
                # 同步冲突才弹回（不计连败）；环境病如实上抛走门内 except。
                if self._gitx.rebase_onto(env, integration) == "conflict":
                    self._non_ff(tid, branch, before, conflict=True,
                                 auto_ignored=auto_ignored)
                    return
                # E＝锚对齐的 env 提交：崩溃重排时留痕值优先于落库残值 R
                r = self._gitx.head(integration)
                if r == before:
                    # ag-54bd98 G1 幻影形态②（ag-c3de0f/ag-e75d89）：空集重放
                    # R==onto＝env boot 分支零交付——原锚（run() 起点快照）不在
                    # 目标支即非崩溃窗口，禁以 set_fields(commit=R) 伪装推进。
                    # F109：对账以本地钉死支为准（零网络）。ag-f77d90 的「env 外
                    # 操作污染本地 ref」面随 legacy 行消亡——worker 动共享分支属
                    # 协议违反，插件闸＋审计兜底；新协议行本就走 is_ancestor 本地判定
                    landed = self._gitx.contains(target, e, branch)
                    if not landed:
                        self._audit(tid, "empty_rebase_noop")
                        self._quality_fail(tid, "reopened", _PHANTOM_BOUNCE,
                                           reason="空集 rebase 无交付")
                        return
                    # 崩溃补终态窗口（权威已含交付）：账面保真 done sha，禁以
                    # 集成头覆写——差异入 audit（ag-28f327 set_fields 保真裁定）
                    self._audit(tid, "already_delivered",
                                **{"done": e, "head": r})
                    if t.get("commit") != e or t.get("rebased_from"):
                        self._state.set_fields(tid, commit=e, rebased_from=None)
                else:
                    self._state.set_fields(tid, commit=r, rebased_from=e)  # 先落库再推进（崩溃幂等）
                    self._audit(tid, "rebased", **{"from": e, "to": r})
            report = self._gitx.materialize_checkout(integration, target)  # Req: ag-0d2daa
            if report:  # 检出后依赖物化：推进后按任务 pin 预置（含 gitlink bump 的任务 pin）
                self._audit(tid, "checkout_materialize", note=report)
            irc, iout = self._run_with_artifact(
                tid, "integration_verify", integration,
                _with_shell_exports(cmd, {"AGORA_INTEGRATION_BASE": before}),
                runner, extra={"integration_base": before})  # Req: F122/F123
            if irc == 124:  # F36 集成超硬顶：同款分类路由，目标分支未改动
                self._audit(tid, "verify_overrun")
                self._router.notify_dispatcher(
                    tid, "result",
                    f"集成 verify 超硬顶，目标分支未改动——进程组已终止"
                    f"（非质量败，无需 worker 重试）；生效预算 {budget:g}s"
                    f"（硬顶 {ceiling:g}s）；输出尾部:\n{iout[-_TAIL:]}"
                    f"{self._artifact_line(tid)}")
                return
            if _cmd_dead(irc, iout):  # Req: F81/F82 集成阶段命令死：同款分类路由
                self._verify_dead(tid, irc, iout)
                return
            if classify_failure(irc, iout) == "signal":  # Req: F122
                self._verify_infra(tid, irc, iout)
                return
            if irc != 0:
                where = "rebase 后集成 verify 失败" if r else "集成 verify 失败"
                self._quality_fail(tid, "reopened",
                                   f"{where}，目标分支未改动——修复后重新"
                                   f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}"
                                   f"{self._artifact_line(tid)}",
                                   reason=where)
                return
            # 集成验证可能耗时较长，最终推进前复查，关闭“开始干净、结束时变脏”窗口。
            auto_before = len(auto_ignored)
            self._ensure_clean_auto(tid, target, exempt, auto_ignored,
                                   roster)
            if len(auto_ignored) != auto_before:  # 终检才入册：二次进闸体检测面
                if self.gate_files_touched(tid, list(_GATE_PATTERNS)
                                           + _roster_gate_patterns(roster),
                                           branch=branch):
                    self._health.escalate(KIND_GATEFILES, tid)
            self._phase(tid, "publish")  # P1b 阶段审计：进入最终推进
            if r is not None:  # 重放推进：sha 级 ff（env 分支仍在 E，不可再作合并源）
                if self._gitx.ff_sha(target, r, branch) != "ok":
                    self._non_ff(tid, branch, self._gitx.head(target, branch),
                                 auto_ignored=auto_ignored)
                    return
            else:
                if t.get("rebased_from"):
                    # 崩溃态②残值遇直通（目标支回退使 E 可 ff）：直通合入的即
                    # done sha（E）——落账对齐之并清留痕——防 merged 账面 sha 与
                    # 验证内容脱节（反目标）
                    self._state.set_fields(tid, commit=e, rebased_from=None)
                # ag-28f327：推进源＝done sha——env 名分支零增量推进＝假终态
                if self._gitx.ff_sha(target, e, branch) != "ok":
                    self._non_ff(tid, branch, self._gitx.head(target, branch),
                                 auto_ignored=auto_ignored)
                    return
            self._succeed(tid, auto_ignored)
        except Exception as e:  # 门内崩（checkout 拒/分支失/脏工作区）：转 reopened 通知
            if self._spawns.killed():
                pass  # P0-1 看门狗杀组解链（含 _git 阶段）：终局归 run_guarded（blocked/gate_overrun），不误弹 reopened
            elif self._state.get(tid)["state"] == "done":
                self._state.transition(tid, "reopened")
                self._audit(tid, "reopened")
                self._post(tid, f"门内异常：{e}——处理目标分支后重新 post(done, commit)")
                self._router.notify_transition(  # ag-acae3b 摘要直投（异常长文归 worker）
                    tid, "reopened", "门内异常")
            else:  # 终态后异常（如环境回收败）：不逆转门结果，但留痕＋实时提示派发方
                self._audit(tid, "reclaim_failed", note=str(e)[:200])
                t = self._state.get(tid)
                self._router.post(t["worker_sid"], ("up", tid), "milestone",
                                  f"环境回收失败：{e}——任务终态不变，残留目录需人工清理")
        finally:
            try:
                self._gitx.remove_integration(integration, target)
                self._state.set_fields(tid, gate_env=None)
            except Exception as e:  # 回收失败不掩盖门结果；gate_env 保留供下轮/人工定位
                self._audit(tid, "reclaim_failed", note=f"integration: {e}"[:200])
            if origin:
                if not self._gitx.checkout(target, origin):
                    self._audit(tid, "restore_failed")  # 还原败留痕并保留原分支位供下轮复用
                else:
                    self._state.set_fields(tid, gate_origin_branch=None)
            self._tail(tid)

    def _delivery_fail(self, tid, attempt, error, note="",
                       conflict_files=None, note_cap=200):  # Req: F98/F105
        """交付失败终局：delivery.status=failed＋error 机器可读（报表失败桶与
        任务明细可见）；done→blocked（本需求唯一新增合法边）。非质量败——
        不计 fail_streak、不弹回 worker、不自动取消/重派；env 留现场，处置权
        在派发方。conflict_files（Req: F105）＝终局冲突清单通道——ff-only 后
        已无调用方供值（managed 终局不采集），留作 reserved，投影与通知面
        据此惰性；note 非空＝附进 blocked 通知（note_cap 截断——缺省 200
        与审计同口径；ag-9a680b 脏面三形明细走专用有界 note（入参自带 20
        行上限）显式抬帽至 _DIRTY_NOTE_TAIL——push_failed 尾巴无界防过载
        口径对其余调用方不变）。"""
        t = self._state.get(tid)
        d = dict(t.get("delivery") or {})
        d.update(status="failed", error=error, attempt=attempt)
        if conflict_files is not None:  # Req: F105 排障内置（今日靠人工 merge-tree）
            d["conflict_files"] = list(conflict_files)
        self._state.set_fields(tid, delivery=d)
        self._state.transition(tid, "blocked")
        self._audit(tid, "delivery_failed", error=error,
                    note=str(note)[:note_cap])
        body = f"任务 {tid} 交付失败（{error}）——已 blocked（非质量问题，" \
               "不计连败）；核对远端/网络后重派或取消"
        if conflict_files:
            body += "；冲突文件: " + ", ".join(conflict_files)
        if note:
            body += "；" + str(note)[:note_cap]
        try:
            self._router.notify_transition(tid, "blocked", body=body)
        except Exception:  # 通知面尽力而为（F54 同口径）——不逆转终局
            pass

    def _dirty_face_note(self, tid, target, base, deliver):
        """dirty 终局三形明细取数（ag-9a680b，v3 _dirty_detail 平移件）：
        porcelain 脏面＋incoming（base..deliver diff 路径面）碰撞判定；取数
        病如实降级留痕（形未知不猜——排除误导处置）。碰撞面取数败按零撞面
        汇总并附甄别提示（exclude 处置仅对零撞面安全）。"""
        try:
            records = self._gitx.status_records(target)
        except Exception as e:
            self._audit(tid, "dirty_face_probe_failed", note=str(e)[:200])
            return (f"dirty form: unknown——目标仓 porcelain 取数失败"
                    f"（{str(e)[:120]}）；人工 git status 判读")
        incoming, degraded = (), False
        try:
            incoming = tuple(self._gitx.diff_paths(target, base, deliver))
        except Exception as e:
            self._audit(tid, "dirty_face_collision_probe_failed",
                        note=str(e)[:200])
            degraded = True
        note = dirty_face_detail(records, incoming)
        if degraded:
            # caveat 前置（评审环 R2）：尾部 append 会被 20 条长路径挤出
            # 帽口——前置保证「禁 exclude」甄别先于路径清单与处置句在场
            note = ("碰撞面取数失败：这些未跟踪路径是否与交付内容相撞未证"
                    "——处置前先人工核对；相撞路径只能 mv 出，禁 exclude"
                    "（排除后合并会静默覆写丢字节）\n" + note)
        return note

    def _acquire_delivery_lock(self, lock):  # Req: F98
        """有界等锁：非阻塞探测＋短步长轮询，到点让位＝一次失败尝试，
        绝不无限阻塞串行门（等待上界沿用原 push 网络口径，F109 后纯本地）。"""
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            if lock.acquire(blocking=False):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL)

    def _deliver_managed_local(self, tid, branch, env, integration, target,
                               runner, cmd, budget=None,
                               ceiling=None):  # Req: F100
        """managed no-origin 本地权威交付：本地钉死支 tip 定基集成→ff/rebase→
        集成 verify→仓级锁内有界发布（advance_local_target CAS ff-only 推进）。
        至多 3 次总尝试（含首次）——干净 non-ff（他写者推进）与等锁超时消耗
        预算后整链重走（重取基/重排/重验）；rebase 内容冲突即终局 failed/
        blocked；脏/占用目标是交付终局失败（local_target_dirty/
        local_target_busy）而非 pending——本地即权威，推进未落地不得报
        delivered。"""
        t = self._state.get(tid)
        for attempt in range(1, 4):
            try:
                base = self._gitx.local_branch_sha(target, branch)
            except RuntimeError as e:
                self._delivery_fail(tid, attempt, "local_base", str(e))
                return
            self._phase(tid, "integration", attempt=attempt)  # P1b 阶段审计
            self._gitx.build_integration_at(target, integration, base_sha=base)
            before = self._gitx.head(integration)
            r = None  # R＝重放后交付 sha（None＝ff 直通）
            if self._gitx.merge_ff(env, integration) != "ok":
                if self._gitx.rebase_onto(env, integration) == "conflict":
                    self._delivery_fail(tid, attempt, "rebase_conflict")
                    return
                e = t.get("rebased_from") or t.get("commit")
                r = self._gitx.head(integration)
                if r == before and not self._gitx.is_ancestor(target, e, base):
                    # 幻影空集重放（ag-54bd98 G1 同判，本地版）：env 无交付且
                    # 本地基亦不含——弹回 worker 自愈（质量口径，与 legacy 一致）
                    self._audit(tid, "empty_rebase_noop")
                    self._quality_fail(tid, "reopened", _PHANTOM_BOUNCE,
                                       reason="空集 rebase 无交付")
                    return
                self._state.set_fields(tid, commit=r, rebased_from=e)
                self._audit(tid, "rebased", **{"from": e, "to": r})
            deliver = self._gitx.head(integration)  # ff 直通＝env 头；重放＝R
            report = self._gitx.materialize_checkout(integration, target)
            if report:
                self._audit(tid, "checkout_materialize", note=report)
            irc, iout = self._run_with_artifact(
                tid, "integration_verify", integration,
                _with_shell_exports(cmd, {"AGORA_INTEGRATION_BASE": base}),
                runner, extra={"integration_base": base})  # Req: F122/F123
            if irc == 124:  # F36 集成超硬顶：同款分类路由，目标支未动
                self._audit(tid, "verify_overrun")
                self._router.notify_dispatcher(
                    tid, "result",
                    f"集成 verify 超硬顶，本地目标支未动——进程组已终止"
                    f"（非质量败，无需 worker 重试）；生效预算 {budget:g}s"
                    f"（硬顶 {ceiling:g}s）；输出尾部:\n{iout[-_TAIL:]}"
                    f"{self._artifact_line(tid)}")
                return
            if _cmd_dead(irc, iout):
                self._verify_dead(tid, irc, iout)
                return
            if classify_failure(irc, iout) == "signal":  # Req: F122
                self._verify_infra(tid, irc, iout)
                return
            if irc != 0:
                where = "rebase 后集成 verify 失败" if r else "集成 verify 失败"
                self._quality_fail(tid, "reopened",
                                   f"{where}，本地目标支未动——修复后重新"
                                   f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}"
                                   f"{self._artifact_line(tid)}",
                                   reason=where)
                return
            # 发布：仓级交付锁＋有界等待（F96/F98 同口径）
            lock = self._gitx.delivery_lock(target)
            if not self._acquire_delivery_lock(lock):
                self._audit(tid, "delivery_lock_timeout", attempt=attempt)
                continue
            try:
                tip = self._gitx.local_branch_sha(target, branch)  # 锁内重读本地
                if tip != base:  # 他写者已推进：释放后整链重试（重排重验）
                    self._audit(tid, "delivery_nonff", attempt=attempt,
                                note=f"{base[:12]}→{tip[:12]}")
                    continue
                self._phase(tid, "publish", attempt=attempt)  # P1b 阶段审计
                st = self._gitx.advance_local_target(target, branch, deliver,
                                                     base)
            except RuntimeError as e:  # 锁内缺支/环境病＝交付终局（F98 评审 D2
                # 同口径）——绝不走门内异常 reopened 弹回 worker
                self._delivery_fail(tid, attempt, "local_base", str(e))
                return
            finally:
                lock.release()
            if st == "synced":  # 本地权威落地＝唯一成功出口
                self._finish_local_delivered(tid, branch, deliver, base,
                                             attempt)
                return
            if st == "non-ff":  # 锁内 CAS 落空/分叉：整链重试
                self._audit(tid, "delivery_nonff", attempt=attempt, note=st)
                continue
            if st == "dirty":  # ag-9a680b：三形细分随 note（umbrella 不变）
                self._delivery_fail(
                    tid, attempt, "local_target_dirty",
                    note=self._dirty_face_note(tid, target, base, deliver),
                    note_cap=_DIRTY_NOTE_TAIL)
                return
            # failed：终局失败——本地权威未推进，绝不伪装 pending
            self._delivery_fail(tid, attempt, "local_target_busy")
            return
        self._delivery_fail(tid, 3, "retry_exhausted",
                            "3 次总尝试内本地目标支持续被推进或交付锁不可得")

    def _finish_local_delivered(self, tid, branch, sha, base, attempt):  # Req: F100
        """本地权威交付落地后的唯一成功出口：投影落账（local_status 恒 synced
        ——本地就是权威）→merged 终态与既有门尾回收链（凭据/env）。base=None＝
        崩溃窗口补终态（保留在案基线与尝试计数）。"""
        t = self._state.get(tid)
        d = dict(t.get("delivery") or {})
        d.update(status="delivered", commit=sha, local_status="synced",
                 error=None, attempt=attempt, delivered_at=time.time())  # Req: F110 未出版超龄判据
        if base is not None:
            d["base"] = base
        try:  # Req: F110 出版对账：merged≠已推送 origin——ahead>0 随行提醒自推；
            # Req: F171 前移至 delivery 组装处：ahead 投影 origin_ahead（邮件人话
            # 渲染取用）；异常/非 int 省略键（同 F54 降级口径）
            pub = self._gitx.publication(t["project_dir"], branch)
            if type(pub.get("ahead")) is int:  # bool 非 int 语义（B/C 册 R1）
                d["origin_ahead"] = pub["ahead"]
        except Exception:  # 投影不可得降级省略，不阻塞交付通知（F54 口径）
            pub = {}
        self._state.fail_streak_reset(tid)  # 全绿清连败（R12，_succeed 同口径）
        self._state.set_fields(tid, delivery=d)
        self._state.transition(tid, "merged", commit=sha)
        extra = {"rebased_from": t["rebased_from"]} if t.get("rebased_from") else {}
        self._audit(tid, "merged", commit=sha, **extra)
        detail = (f"@{sha[:7]} local=synced——沉淀: 聚本任务 note/task page "
                  "走 knowledge ingest")
        if pub.get("ahead"):  # Req: F110（pub 已在组装处取，行为等价）
            detail += (f"——origin 未同步（ahead={pub['ahead']}），"
                       "请派发方自推")
        self._router.notify_transition(tid, "merged", detail)  # Req: F60/F110
        self._store.remove_credential(tid)  # F43：先删牌——env 回收败不得拦断
        self._reclaim_env(tid, t["env_dir"], "gate:finish_local_delivered")  # Req: ag-0e6ca3
        self._post(tid, f"merged @{sha}——本地目标支已交付，环境已回收")

    def _deliver_external(self, tid, branch, env, integration, target,
                          runner, cmd, budget=None, ceiling=None):  # Req: F101
        """external_branch 外部交付 handoff：钉死本地支 tip（或 non_ff 重排队
        携带的外部确认基）定基集成→ff-only 合入→集成 verify→绿即驻留
        awaiting_external——不合目标支、不推 origin、不回收 env/凭据（外部
        派发方持有远端交付权，确认经 task_deliver）。非 ff 即终局 failed/
        blocked（ag-f12220 ff-only：自动变基调和已废除——变基轮 env 与交付
        分支各执同批改动的不等重写副本时，门内 rebase 只会两副本对撞循环
        耗会话；处置权交人工——内容超集 force-with-lease 推送/取消重开/
        人工调和）；集成 verify 败保持质量 reopened 语义；超时/命令死分类
        路由同款。"""
        d = self._state.get(tid).get("delivery") or {}
        attempt = d.get("attempt") or 1  # non_ff 重排队已带尝试号；首跑＝1
        base = d.get("base")  # non_ff 重排队的外部基；None＝本地钉死支 tip
        if base is None:
            if branch is None:
                self._delivery_fail(tid, attempt, "local_base",
                                    "target_branch 未钉死——external_branch"
                                    "不回退仓默认支")
                return
            try:
                base = self._gitx.local_branch_sha(target, branch)
            except RuntimeError as e:
                self._delivery_fail(tid, attempt, "local_base", str(e))
                return
        self._phase(tid, "integration", attempt=attempt)  # P1b 阶段审计
        try:  # 评审 R1：外部基由派发方供给——本地不可解析（未 fetch/外部改写
            # 历史）＝交付面终局，不走门内异常 reopened 弹回 worker（F98 D2 口径）
            self._gitx.build_integration_at(target, integration,
                                            base_sha=base)
        except (RuntimeError, ValueError) as e:
            self._delivery_fail(tid, attempt, "external_base", str(e))
            return
        if self._gitx.merge_ff(env, integration) != "ok":  # ff-only
            self._delivery_fail(tid, attempt, "diverged",
                                "交付分支与 worker 线索分叉（非 ff，自动变基"
                                "已废除）——env 为内容超集时外部 force-with-"
                                "lease 推送，否则取消重开（env 按分支现状重建）"
                                "或人工调和")
            return
        deliver = self._gitx.head(integration)  # ff 直通＝integration 头
        report = self._gitx.materialize_checkout(integration, target)
        if report:
            self._audit(tid, "checkout_materialize", note=report)
        irc, iout = self._run_with_artifact(
            tid, "integration_verify", integration,
            _with_shell_exports(cmd, {"AGORA_INTEGRATION_BASE": base}),
            runner, extra={"integration_base": base})  # Req: F122/F123
        if irc == 124:  # F36 集成超硬顶：同款分类路由，目标支未动
            self._audit(tid, "verify_overrun")
            self._router.notify_dispatcher(
                tid, "result",
                f"集成 verify 超硬顶，目标支未动——进程组已终止"
                f"（非质量败，无需 worker 重试）；生效预算 {budget:g}s"
                f"（硬顶 {ceiling:g}s）；输出尾部:\n{iout[-_TAIL:]}"
                f"{self._artifact_line(tid)}")
            return
        if _cmd_dead(irc, iout):
            self._verify_dead(tid, irc, iout)
            return
        if classify_failure(irc, iout) == "signal":  # Req: F122
            self._verify_infra(tid, irc, iout)
            return
        if irc != 0:
            self._quality_fail(tid, "reopened",
                               f"集成 verify 失败，目标支未动——修复后重新"
                               f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}"
                               f"{self._artifact_line(tid)}",
                               reason="集成 verify 失败")
            return
        self._phase(tid, "publish", attempt=attempt)  # P1b 阶段审计：handoff 即外部交付发布
        self._handoff_external(tid, branch, deliver, base, attempt)

    def _handoff_external(self, tid, branch, sha, base, attempt):  # Req: F101
        """external_branch 绿 handoff 唯一出口：投影 awaiting_external（commit/
        base/attempt 机器可读，local_status=not_required——外部权威，本地无
        同步义务）＋任务驻留 done＋env/凭据保留；通知面带齐四要素（commit/
        target branch/base/attempt），确认权在外部派发方（task_deliver）。"""
        t = self._state.get(tid)
        d = dict(t.get("delivery") or {})
        d.update(status="awaiting_external", commit=sha, base=base,
                 attempt=attempt, local_status="not_required", error=None,
                 conflict_files=None,  # Req: F105 上轮终局清单残值必清（B1）
                 handoff_at=time.time())  # Req: F110 驻留超龄判据（health 扫）
        self._state.fail_streak_reset(tid)  # 全绿清连败（R12，_succeed 同口径）
        self._state.set_fields(tid, delivery=d)
        self._audit(tid, "awaiting_external", commit=sha, base=base,
                    attempt=attempt)
        try:
            self._router.notify_dispatcher(
                tid, "result",
                f"任务 {tid} handoff 就绪（awaiting_external）：commit={sha} "
                f"target={branch} base={base} attempt={attempt}"
                "——外部确认经 task_deliver(delivered/non_ff/failed)")
        except Exception:  # 通知面尽力而为（F54 口径）——不逆转驻留
            pass
        try:  # 评审 R1：state 仍 done——_post 裸抛会被门内 except 误判门体
            # 异常而 reopened，逆转已驻留的 handoff（通知面尽力而为不逆转）
            self._post(tid, f"handoff 就绪 @{sha[:7]}（base {base[:7]}，"
                            f"第 {attempt} 次）——等待外部派发方 task_deliver 确认，"
                            "环境保留")
        except Exception:
            pass

    def recover_managed(self):  # Req: F100
        """启动恢复面：managed 交付投影非终态任务的崩溃收敛。

        failed＋done＝补 blocked 迁移，绝不自动重试（gate.run 同守卫双保险）；
        pending＋done＋commit＝权威判定一律本地钉死支（F109 零网络）：
        已含即按已交付收口（零推送零推进）＋门尾结算；未含留给门重排队列
        （make_server done 残留恰一次）续跑既有交付——不发明 commit、不建
        重复工作；缺 commit/权威不可得＝审计留痕交门重排链路兜底，不猜。
        逐任务隔离：单任务恢复败只留痕不拦后续收敛（recover.startup 同口径）；
        收口半途抛（如 env 回收败）时 merged 已落账仍补 _tail 闭合，防僵尸行。
        external_branch/out_of_band/legacy delivery=None 零接触。"""
        for tid, t in self._state.snapshot()["tasks"].items():
            finishing = False
            try:
                d = t.get("delivery") or {}
                if d.get("mode") != "managed" or not t.get("target_branch"):
                    continue
                if d.get("status") == "failed":
                    if t["state"] == "done":  # 崩窗：failed 落账而 blocked 未达
                        self._state.transition(tid, "blocked")
                        self._audit(tid, "delivery_failed_recovery")
                    continue  # 终局：恢复面绝不重试
                if d.get("status") != "pending":
                    if d.get("status") != "delivered":  # 未知态＝可上报恢复错误
                        self._audit(tid, "delivery_recover_unknown_status",
                                    note=str(d.get("status"))[:200])
                    continue
                if t["state"] != "done":
                    continue  # 在飞/续修态归各自车道（reinject/门）
                if not t.get("commit"):
                    self._audit(tid, "delivery_recover_missing_commit")
                    continue
                branch, commit = t["target_branch"], t["commit"]
                try:
                    # F109：权威判定一律本地钉死支（零网络）——F98 时代「已推
                    # 远端未落 merged」形态随远端权威废除消亡，不再向 origin 对账
                    landed = self._gitx.contains(t["project_dir"], commit, branch)
                except Exception as e:  # 权威不可得：留门重排链路兜底
                    self._audit(tid, "delivery_recover_probe_failed",
                                note=str(e)[:200])
                    continue
                if not landed:
                    continue  # 门重排（make_server done 残留）续跑：不重复排队
                self._audit(tid, "delivery_recovered")
                finishing = True
                self._finish_local_delivered(tid, branch, commit, None,
                                             d.get("attempt", 0))
                self._tail(tid)  # merged→closed 终局结算（含 pending-cancel 消费）
            except Exception as e:  # 逐任务隔离：不拦后续任务收敛
                try:
                    self._audit(tid, "delivery_recover_task_failed",
                                note=str(e)[:200])
                except Exception:  # 审计自身 I/O 病吞掉兜底——隔离不变式
                    pass  # 先于留痕（R2 终闸：防 audit 抛逃出中断整轮收敛）
                if finishing:  # 收口半途抛：merged 已落账仍补闭合防僵尸行
                    try:
                        self._tail(tid)
                    except Exception:
                        pass

    def _succeed(self, tid, auto_ignored=()):
        """唯一成功出口：目标支已含任务 commit，补终态、审计与任务环境回收。"""
        t = self._state.get(tid)
        if not self._gitx.contains(t["project_dir"], t.get("commit"),
                                   t.get("target_branch")):
            # ag-54bd98 G2 幻影形态①（ag-40a1fa）：ff 幂等假成功——merged 落账
            # 的 commit 从未进目标支（门记 merged+closed 而交付静默丢失）。
            # 锚一致性终检：merged ⇒ 目标支真含落账 commit，否则弹回自愈。
            self._audit(tid, "phantom_merge_blocked")
            self._quality_fail(tid, "reopened", _PHANTOM_BOUNCE,
                               reason="目标支不含落账 commit")
            return
        self._state.fail_streak_reset(tid)  # 全绿清连败（R12）
        self._state.transition(tid, "merged", commit=t.get("commit"))
        # F32：rebased_from 从 state 读（裁定②）——崩溃补终态路径才有值可取
        extra = {"rebased_from": t["rebased_from"]} if t.get("rebased_from") else {}
        self._audit(tid, "merged", commit=t.get("commit"), **extra)
        detail = (f"@{(t.get('commit') or '')[:7]}——沉淀: "
                  "聚本任务 note/task page 走 knowledge ingest")  # Req: F60
        if auto_ignored:  # Req: F97：完成通知告知自动忽略清单
            detail += (f"——自动忽略根级未跟踪隐藏目录: {' '.join(auto_ignored)}")
        self._router.notify_transition(  # ag-acae3b：merged 实时直达 dispatcher
            tid, "merged", detail)
        self._store.remove_credential(tid)  # F43：先删牌——env 回收败不得拦断（merged 路径牌无其他作废点）
        self._reclaim_env(tid, t["env_dir"], "gate:succeed")  # Req: ag-0e6ca3
        self._post(tid, f"merged @{t.get('commit')}——环境已回收")
        # F109：origin 同步与远端任务支清理归派发方自理——门尾零网络

    def _non_ff(self, tid, branch, before, conflict=False, auto_ignored=()):
        """F32 同步冲突或最终推进窄竞态遇真 non-ff：重开不计质量连败，目标主检出保持不动。"""
        t = self._state.get(tid)
        # 窄竞态守卫：试合并/推进窗口内 commit 已被人工推进目标支，则补终态成功，绝不重开
        if self._gitx.contains(t["project_dir"], t.get("commit"), branch):
            self._succeed(tid, auto_ignored)
            return
        self._state.transition(tid, "reopened")
        self._audit(tid, "reopened")
        why, act = (("同步冲突", "解冲突后") if conflict else ("non-ff 拒并", "同步后"))
        self._router.notify_transition(tid, "reopened", why)  # ag-acae3b 摘要直投
        self._post(tid, f"{why}："
                        f"{branch or self._gitx.symref(t['project_dir'])}@{before}"
                        f" vs branch@{self._gitx.head(t['env_dir'])}——{act}"
                        "重新 post(done, commit)")

    def gate_files_touched(self, tid, patterns=None, branch=None):
        t = self._state.get(tid)
        return bool(t.get("env_dir")) and self._gitx.touches_gate(
            t["env_dir"], patterns if patterns is not None else _GATE_PATTERNS,
            branch if branch is not None else t.get("target_branch"))

    # ---- 内部 ----

    def _notify_verify_soft(self, tid, timeout):
        """F36 软顶提醒线：不杀只提醒恰一次（gt-961483/gt-018676 误报诱导 cancel
        前科→文案显式仍在跑、无需操作）；F51 文案报任务级生效预算。"""
        self._audit(tid, "verify_soft_timeout")
        self._router.notify_dispatcher(
            tid, "milestone",
            f"任务 {tid} verify 超软顶（>{timeout:g}s）——仍在跑、"
            "无需操作、确认挂死才 cancel")

    def _run_with_artifact(self, tid, phase, env_dir, cmd, runner,
                           extra=None):  # F122；extra Req: F123
        """统一执行面：runner 结果无论 rc 多少全量落 artifact（完整 output＋
        meta）；runner 抛异常也先落 runner_exception 再原样上抛（不得伪装
        content failed）。store=None＝直通。返回 (rc, out) 原样——调用方契约
        与既有分类路由零改动。extra 透传 meta（integration/review base 等
        机器判读面）。"""
        if self._artifacts is None:
            return runner(env_dir, cmd)
        started = time.time()
        try:
            rc, out = runner(env_dir, cmd)
        except Exception as e:
            self._record_artifact(tid, phase, env_dir, cmd, None, "",
                                  started, error=e, extra=extra)
            raise
        self._record_artifact(tid, phase, env_dir, cmd, rc, out, started,
                              extra=extra)
        return rc, out

    def _record_artifact(self, tid, phase, env_dir, cmd, rc, out, started,
                         error=None, extra=None):  # F122；extra Req: F123
        """落盘＋任务行 gate_artifact 投影（只存最新一条）＋gate_artifact_written
        事件。best-effort 三段逐段隔离：store 写败审计 gate_artifact_write_failed
        不改门状态机（投影缺席即通知面 artifact unavailable 语义）。"""
        text = out if isinstance(out, str) else ""
        kind = classify_failure(rc, text, error)
        try:
            art = self._artifacts.record(
                tid, phase, env_dir, cmd, rc, text, started, time.time(),
                error=str(error) if error is not None else None,
                extra=extra, failure_kind=kind)
        except Exception:
            try:  # 兜底审计自身尽力而为（OCR#2）：append_event 与 store 共享
                # 失败域（磁盘满/权限），再抛会把 runner 已完成的执行变门级异常
                self._audit(tid, "gate_artifact_write_failed", phase=phase)
            except Exception:
                pass
            return None
        try:
            self._state.set_fields(tid, gate_artifact={
                "path": art["path"], "phase": art["phase"], "rc": art["rc"],
                "failure_kind": art["failure_kind"],
                "duration_s": art["duration_s"], "bytes": art["bytes"]})
        except Exception:
            pass
        try:
            self._audit(tid, "gate_artifact_written", phase=art["phase"],
                        rc=art["rc"], failure_kind=art["failure_kind"],
                        bytes=art["bytes"], path=art["path"])
        except Exception:
            pass
        return art

    def _artifact_line(self, tid):  # F122
        """通知附行：最新 artifact 目录路径（meta.json 所在）；投影缺席＝空串。"""
        p = (self._state.get(tid).get("gate_artifact") or {}).get("path")
        return f"\nartifact={p}" if p else ""

    def _verify_overrun(self, tid, out, budget, ceiling):
        """F36 超硬顶分类路由：审计＋派发方告知（普通与 review-only 门共用文案）。"""
        self._audit(tid, "verify_overrun")
        self._router.notify_dispatcher(
            tid, "result",
            f"verify 超硬顶——进程组已终止（非质量败，无需 worker 重试）；"
            f"生效预算 {budget:g}s（硬顶 {ceiling:g}s）；"
            f"输出尾部:\n{out[-_TAIL:]}{self._artifact_line(tid)}")

    def _verify_infra(self, tid, rc, out):  # Req: F122；宽限兜底 Req: F130
        """signal 类机械失败分类（137 SIGKILL/139 SIGSEGV/134 SIGABRT 等）：
        与活不达标分开——任务驻留 done、不计连败、不弹回 worker（基础设施疑点
        worker 无从修）；审计 verify_infra_failed＋KIND_VERIFY_INFRA 升级
        （通知通道唤醒派发方，8122a2e 同 verify-dead 渠道）；worker 只收最小
        等待说明；通知明确「机械失败/基础设施疑点，非质量败」。通知面尽力而为
        （F54 口径）——败不改变驻留语义。
        Req: F130 宽限兜底：首败登记宽限计时（health tick 到点 dispatcher 仍
        失效零动作→自动重排恰一次）；兜底标记在身再败＝终局 blocked
        （verify_infra_backstop_exhausted，gate_overrun→blocked 同构）——
        防 dispatcher 失效下的无限驻留（ag-aa8798 判例 13h 死锁）。"""
        self._audit(tid, "verify_infra_failed", rc=rc)
        if self._state.get(tid).get("verify_infra_backstop"):
            self._state.transition(
                tid, "blocked", blocked_reason="verify_infra_backstop_exhausted")
            try:
                self._health.escalate(
                    KIND_VERIFY_INFRA, tid,
                    raw=f"exit={rc} backstop exhausted"
                        f"{self._artifact_line(tid)}")
            except Exception:
                pass
            try:
                self._router.notify_transition(
                    tid, "blocked",
                    body=f"任务 {tid} verify 机械失败再发（宽限兜底重排后仍 "
                         f"signal，exit={rc}）——已 blocked"
                         f"（verify_infra_backstop_exhausted，非质量败）；"
                         f"处置后重派或取消{self._artifact_line(tid)}")
            except Exception:  # 通知面尽力而为（F54 口径）——不逆转终局
                pass
            return
        self._state.set_fields(tid, verify_infra_grace={  # 驻留后登记宽限
            "deadline": time.time() + self._infra_grace,
            "since": time.time()})
        try:
            self._health.escalate(
                KIND_VERIFY_INFRA, tid,
                raw=f"exit={rc} {out[-_VERIFY_DEAD_RAW_TAIL:]}"
                    f"{self._artifact_line(tid)}")
        except Exception:
            pass
        try:
            self._router.notify_dispatcher(
                tid, "result",
                f"任务 {tid} verify 机械失败（exit {rc}：signal 类——"
                "SIGKILL/SIGSEGV/SIGABRT，疑似 OOM/基础设施）——"
                "机械失败/基础设施疑点，非质量败：不计连败、无需 worker "
                f"重试，任务驻留 done 待处置{self._artifact_line(tid)}")
        except Exception:
            pass
        try:
            self._post(tid, f"verify 机械失败（exit {rc}：signal 类，疑似"
                            "基础设施/OOM）——非质量败，无需自愈重试；"
                            "已升级派发方处置，等待通知即可")
        except Exception:
            pass

    def _verify_dead(self, tid, rc, out):
        """Req: F81 命令自身死分类（exit 126/127）：与活不达标分开——驻留 done、
        不计连败、不下发失败尾部（不弹回 worker 自愈：命令死了 worker 无从修）；
        escalate verify-dead 走通知通道唤醒派发方（8122a2e inject_notify/
        synthetic resume:true 复用勿新造；上行至根——嵌套链 verify 写面 worker
        永拒，只有主会话能处置），输出尾以不可信原文随行；worker 留最小等待说明。
        通知面尽力而为（F54 同口径）——败不改变驻留语义。"""
        self._audit(tid, "verify_dead", rc=rc)
        try:
            self._health.escalate(
                KIND_VERIFY_DEAD, tid,
                raw=f"exit={rc} {out[-_VERIFY_DEAD_RAW_TAIL:]}"
                    f"{self._artifact_line(tid)}")  # Req: F122
        except Exception:
            pass
        try:
            self._post(tid, f"verify 命令自身死（exit {rc}：命令不存在/不可执行）"
                            "——非活不达标，无需自愈重试；已升级派发方处置，"
                            f"等待通知即可{self._artifact_line(tid)}")
        except Exception:
            pass

    def _gate_review(self, tid, t, env, runner):  # Req: F114
        """门审：verify 绿后在任务 env 亲自跑 review_cmd——合并权威在服务，
        worker 无「评审过」申报面。退出码契约与 verify 同构：rc==0＝绿
        （结果缓存 cmd+HEAD 双键，幂等跳过重审）；rc!=0＝评审红——**独立
        连败计数 review_fail_streak**（与质量 fail_streak 解耦：评审器误报
        不烧穿 worker 三连败），弹回 worker 附输出尾；3 次分歧＝blocked＋
        升级派发方人工判读（修代码或调 review_cmd——真代码问题与真误报
        都不该由 worker 无限扛）。超硬顶/命令死同 verify 分类：驻留 done
        路由派发方，不计任何连败。env_base 缺席（升级前在飞）＝评审范围
        不可划定，带痕放行（F107 同款降级口径）。评审以 env HEAD（done
        交付链）为准；legacy rebase 重放内容等价由集成 verify 重跑保障。"""
        cmd = t["review_cmd"]
        base = t.get("env_base")
        if not base:
            self._audit(tid, "review_base_unavailable")
            return True
        head = self._gitx.head(env)
        cache = t.get("review_cache") or {}
        if cache.get("cmd") == cmd and cache.get("commit") == head \
                and cache.get("ok"):
            self._audit(tid, "gate_review_cached")
            return True
        # 评审范围注入：AGORA_REVIEW_BASE 经 export 前缀对整条命令链生效
        # （Req: F123——VAR=x cmd 前缀赋值只作用首条，`cd x && pytest` 形态
        # 下 pytest 读不到）；登记文本保持干净，卫生闸查的是登记面。
        self._phase(tid, "review")  # P1b 阶段审计
        rc, out = self._run_with_artifact(
            tid, "review", env,
            _with_shell_exports(cmd, {"AGORA_REVIEW_BASE": base}),
            runner, extra={"review_base": base})  # Req: F122/F123
        if rc == 124:
            self._audit(tid, "review_overrun")
            self._router.notify_dispatcher(
                tid, "result",
                f"任务 {tid} 门审超硬顶——进程组已终止（非质量败，无需 "
                f"worker 重试）；输出尾部:\n{out[-_TAIL:]}"
                f"{self._artifact_line(tid)}")
            return False
        if _cmd_dead(rc, out):  # Req: F81/F82 同款分类：命令死≠评审红
            self._audit(tid, "review_dead", rc=rc)
            try:
                self._health.escalate(
                    KIND_VERIFY_DEAD, tid,
                    raw=f"exit={rc} {out[-_VERIFY_DEAD_RAW_TAIL:]}"
                        f"{self._artifact_line(tid)}")  # Req: F122
            except Exception:
                pass
            try:
                self._post(tid, f"门审命令自身死（exit {rc}：命令不存在/不可"
                                "执行）——非活不达标，无需自愈重试；已升级"
                                "派发方处置，等待通知即可")
            except Exception:
                pass
            return False
        if classify_failure(rc, out) == "signal":  # Req: F122
            # 评审跑被信号杀（oomd 等）＝机械失败——驻留 done、不计
            # review_fail_streak、不弹 worker，KIND_VERIFY_INFRA 升级派发方。
            self._audit(tid, "review_infra", rc=rc)
            try:
                self._health.escalate(
                    KIND_VERIFY_INFRA, tid,
                    raw=f"exit={rc} {out[-_VERIFY_DEAD_RAW_TAIL:]}"
                        f"{self._artifact_line(tid)}")
            except Exception:
                pass
            try:
                self._router.notify_dispatcher(
                    tid, "result",
                    f"任务 {tid} 门审机械失败（exit {rc}：signal 类，疑似 "
                    "OOM/基础设施）——机械失败/基础设施疑点，非质量败；"
                    f"任务驻留 done 待处置{self._artifact_line(tid)}")
            except Exception:
                pass
            try:
                self._post(tid, f"门审机械失败（exit {rc}：signal 类，疑似"
                                "基础设施/OOM）——非活不达标，无需自愈重试；"
                                "已升级派发方处置，等待通知即可")
            except Exception:
                pass
            return False
        if rc != 0:
            self._review_fail(
                tid, f"门审未通过——按评审发现修复后重新 post(done, commit)"
                     f"\n输出尾部:\n{out[-_TAIL:]}{self._artifact_line(tid)}")
            return False
        self._state.set_fields(
            tid, review_cache={"cmd": cmd, "commit": head, "ok": True,
                               "ran_at": time.time()},
            review_fail_streak=0)  # 绿即复位（评审分歧是轮次态不是任务态）
        self._audit(tid, "gate_reviewed", commit=head)
        return True

    def _review_fail(self, tid, body):  # Req: F114
        """评审红弹回：review_fail_streak 独立计数——3 次分歧转 blocked＋
        升级派发方（人工判读「代码问题 vs 评审器误报」），worker 不无限扛
        评审分歧；未达阈值照常 reopened 回 worker 附发现。"""
        t = self._state.get(tid)
        n = (t.get("review_fail_streak") or 0) + 1
        self._state.set_fields(tid, review_fail_streak=n)
        if n >= 3:
            self._state.transition(tid, "blocked")
            self._audit(tid, "blocked", note="review_disagreement")
            try:
                self._health.escalate(KIND_QUALITY, tid)
            except Exception:
                pass
            try:
                self._router.notify_dispatcher(
                    tid, "result",
                    f"任务 {tid} 门审与 worker 三轮分歧（review_disagreement）"
                    "——已 blocked；人工判读：代码问题修代码，评审器误报调 "
                    "review_cmd 或取消重派")
            except Exception:
                pass
            try:
                self._post(tid, body)
            except Exception:
                pass
            return
        self._state.transition(tid, "reopened")
        self._audit(tid, "review_failed")
        self._post(tid, body)

    def _base_probe(self, tid, t, target, integration, runner,
                    cmd):  # Req: F107
        """base-red 探针（FAIL_TO_PASS 最小机械近似）：env verify 前在任务起点
        base（env_base）的 detached 集成树上跑一次 verify——红＝有判别力放行
        （门流程零变化）；绿且无豁免＝硬拒弹回（裁决②）。豁免：建单旗标
        verify_base_exempt（裁决① A）／quick 自动豁免（裁决③）。base 阶段命令
        死（126/127）＝判别红放行（ag-d68faf：缺文件形态——真死命令由 env 阶段
        F81/F82 分类兜底）；超硬顶（F36）／机械败＝分类路由派发方——驻留 done、
        不计连败、不弹 worker。结果缓存任务行 probe={result,cmd,base,ran_at}
        （cmd+base 双键：换命令或重派换基即失效重探），reopen／崩溃重排不重跑；
        env_base 缺席（升级前在飞任务）＝探针不可确立 fail-open 放行。"""
        base = t.get("env_base")
        if t.get("verify_base_exempt") or t.get("quick") or not base:
            return True
        probe = t.get("probe") or {}
        if probe.get("cmd") == cmd and probe.get("base") == base:
            if probe.get("result") == "red":
                return True
            self._probe_green(tid)
            return False
        self._phase(tid, "base_probe")  # P1b 阶段审计（实跑才发：豁免/缓存零噪音）
        try:
            self._gitx.build_integration_at(target, integration, base_sha=base)
            self._gitx.materialize_checkout(integration, target)  # ag-0d2daa 同款：submodule/LFS 物化后探针红才可信
            rc, out = self._run_with_artifact(tid, "base_probe", integration,
                                              cmd, runner)  # Req: F122
        except RuntimeError as e:  # env_base 对象缺失/引用被回收＝机械面不可用
            self._audit(tid, "base_probe_failed", note=str(e)[:200])
            try:
                self._router.notify_dispatcher(
                    tid, "result",
                    f"任务 {tid} base-red 探针无法运行：{e}——核对 env_base "
                    "对象在位后重排，或换验收命令／豁免"
                    "（非质量败，无需 worker 重试）")
            except Exception:
                pass
            return False
        if rc == 124:  # 超硬顶：F36 语义（驻留 done、不 bump、不弹 worker）
            self._audit(tid, "base_probe_overrun")
            try:
                self._router.notify_dispatcher(
                    tid, "result",
                    f"任务 {tid} base-red 探针超硬顶——进程组已终止"
                    f"（非质量败，无需 worker 重试）；"
                    f"输出尾部:\n{out[-_TAIL:]}{self._artifact_line(tid)}")
            except Exception:
                pass
            return False
        # ag-d68faf 修订：base 阶段命令死（126/127 等）＝判别红——缺文件形态
        # （verify 引用任务随后才建的测试脚本）在 base 上恰红；真死命令由 env
        # 阶段 F81/F82 分类兜底（_cmd_dead 不在此处适用）。
        red = rc != 0
        self._state.set_fields(tid, probe={
            "result": "red" if red else "green", "cmd": cmd, "base": base,
            "ran_at": time.time()})
        self._audit(tid, "base_probe_red" if red else "base_probe_green",
                    rc=rc)
        if not red:
            self._probe_green(tid)
        return red

    def _probe_green(self, tid):  # Req: F107 裁决②
        """base 绿硬拒弹回：验收命令不区分任务前后（空洞 verify 假绿面）——
        worker 无豁免权，拒因引导换命令或经派发方豁免。"""
        self._quality_fail(
            tid, "reopened",
            "base-red 探针拒收：verify 命令在任务起点 base 上即绿（无判别力，"
            "F107）——换能区分任务前后的验收命令，或由派发方豁免（task_create "
            "verify_base_exempt=true／quick）；worker 自己无豁免权——"
            "post(kind=question) 请派发方处置后重新 post(done, commit)",
            reason="base-red 探针：验收命令在 base 上绿")

    def _quality_fail(self, tid, to, body, reason=""):
        """质量败（verify 败/集成败/合并前锚败）：迁移＋审计＋实时回 worker＋计连败
        （3 连败拍人）。reopened 时向 dispatcher 推拒因摘要单行（ag-acae3b：原文归
        worker）；通知面尽力而为（F54 同口径）——败不得断连败计数或改写门结果。"""
        self._state.transition(tid, to)
        self._audit(tid, to)
        self._post(tid, body)
        try:
            if to == "reopened":
                self._router.notify_transition(tid, "reopened", reason)
        except Exception:
            pass
        self._state.fail_streak_bump(tid)
        self._escalate_if_blocked(tid)

    def _escalate_if_blocked(self, tid):
        """连败拍人公共尾（ag-084a22 C：受理锚拒与门体质量败同口径）。
        blocked 已由 fail_streak_bump 落账；升级/通知面逐通道尽力而为（F54 同
        口径，run() 门启动拍同款）——一侧败不压另一侧，不断裂受理链或门链。"""
        if self._state.get(tid)["state"] != "blocked":
            return
        try:
            self._audit(tid, "blocked")
        except Exception:
            pass
        try:
            self._health.escalate(KIND_QUALITY, tid)
        except Exception:
            pass
        try:
            self._router.notify_transition(  # F63：终态并线（会话行原文不变＋邮件必达）
                tid, "blocked", body=f"任务 {tid} 已 blocked；请重派或取消")
        except Exception:
            pass
        if self._on_blocked is not None:  # 链尾取消（task.stop_after_failed）不随通知败跳过
            try:
                self._on_blocked(tid)
            except Exception:
                pass

    def _reclaim_env(self, tid, env_dir, site):  # Req: ag-0e6ca3
        """env 回收前置审计：任务 id＋resolve 后路径＋调用点标签先落事件流再
        remove_env——0f7d7f 事故（仓外 glob rm+prune 冒名服务回收）事后无从
        定谳，自此服务侧每次回收在事件流可与服务外删除区分。审计尽力而为
        （B 册终闸：审计失败拦断回收＝凭据已删/终态已落而 env 无痕滞留），
        顺序仍先审计后动手——正常路径先落账，审计面病只缺观测不缺回收。"""
        try:
            self._audit(tid, "env_removed",
                        env=str(Path(env_dir).resolve()), site=site)
        except Exception:
            pass  # F54 尽力而为口径：观测面病不断权威回收链
        self._gitx.remove_env(env_dir)

    def _tail(self, tid):
        """门走完：pending-cancel 经 State.gate_tail 原子结算（merged 不逆转历史）；副作用随终态。"""
        t = self._state.gate_tail(tid)
        if t["state"] == "cancelled":
            self._audit(tid, "cancelled")
            if t.get("env_dir"):  # 同 T7 直路径口径：cancelled 即环境回收
                try:
                    self._reclaim_env(tid, t["env_dir"], "gate:tail_cancel")  # Req: ag-0e6ca3
                except Exception as e:  # 回收败留痕后继续：终止通知＋凭据作废不得被拦断
                    self._audit(tid, "reclaim_failed", note=str(e)[:200])
            self._store.remove_credential(tid)  # F43：门尾取消同删牌文件
            if t.get("worker_sid"):  # 同 T7 直路径：终止通知＋凭据作废
                self._router.post(t["dispatcher_sid"], ("down", tid), "cancel",
                                  f"task {tid} cancelled at gate tail")
                self._auth.revoke_session(t["worker_sid"])
            self._router.notify_transition(  # F63：终态并线（会话行原文不变＋邮件必达）
                tid, "cancelled", body=f"任务 {tid} 已取消")
        elif t["state"] == "closed":
            self._audit(tid, "closed")
            self._router.notify_transition(  # F63：终态并线（会话行原文不变＋邮件必达）
                tid, "closed",
                body=f"任务 {tid} 已 closed；commit={t.get('commit') or '-'}")

    def _audit(self, tid, event, **extra):
        """F8 门段审计：迁移即 events.jsonl 一行（事件名＝迁移后状态名）。"""
        self._store.append_event({"ts": time.time(), "actor": "gate",
                                  "event": event, "task": tid, **extra})
        if self._pages is not None:  # ag-0f60fd：门段全部事件经此单点刷新任务页
            try:  # 页面写面尽力而为（F54 同口径）：败不断门结果
                self._pages(self._store.home, tid, self._state.get(tid))
            except Exception:
                pass

    def _phase(self, tid, name, **extra):
        """P1b 门阶段审计（ag-9e055d 门楔死 1.5h 人工 forensics）：阶段迁移即
        phase_<name> 事件（verify/base_probe/integration/publish）——楔死可按
        末次阶段事件定位；health 门内判僵以本面为活性基线。尽力而为：败不断门。"""
        try:
            self._audit(tid, f"phase_{name}", **extra)
        except Exception:
            pass

    def _post(self, tid, body):
        t = self._state.get(tid)
        self._router.post(t["dispatcher_sid"], ("down", tid), "milestone", body)
