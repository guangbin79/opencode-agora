"""健康探活与升级拍人（F5，设计 §4.4）：定时探死＋边沿判僵、失控实时拍主会话。

tick 只探活动任务的直接下级会话（无全局巡检）；死下级→recover.reinject 自愈
（静默），失败→升级。父 worker 若正管着在飞链也优先重注入，并同步改绑嵌套
子任务；只有无法重注入的失管 dispatcher 才直接拍人。
worker 凭据自愈已推改拉（F25 修正）：inject_silent 推送通道证伪（v2.2.0
synthetic 不触发 prompt 钩子），改由插件 hook 缓存未命中经 /cred_for_sid 拉取。
边沿判僵（F5 决策记录 2026-10-28；F70 自检提示＋F160 僵死梯子）：tick 顺带按 doctor
同口径（20min 无活动，report.stale_candidates 构造注入）算僵态候选，新进候选集→
stalled 升级＋worker prompt 自检一次（不强制打断，失败不重试）＋可选邮件
（email.json opt-in）。F160 机械兜底：自检词强制产出锚（结论必须
post(kind=note) 一条）；nudge 后一个退避窗内零产出（last_seen 未前移且无
worker 新消息）→自动重派（ag-d1d8c1 stalled 重派边：废旧牌＋新会话接管，
fail_streak 记梯子爬升）；连败达限/无重派边/重派被拒只 escalate(kind=zombie)
不再自动重开；自动动作全程审计＋邮件告知。离开候选集复位，oc 不可达轮次
免判；同任务重复升级（复位后再僵再拍）间隔×4 指数退避、封顶 24h，正文带
连续升级计数（ag-0f537d 降噪，ag-4628f9 宿主排队忙/闲抖动误报实证）；
"僵"的处置仍归人工。env 已物理消失的候选改拍一次性 env-gone（F72，
ag-724152 /tmp 清理事故）：每丢失周期一次、持续丢失不拍 stalled 不注自检，
恢复在盘回常规 stalled 边沿，终态出账。升级正文由服务权威合成；任务/worker 供给文本以
"不可信原文"段携带（拍平换行，防伪造服务行）。escalate 沿 dispatcher_sid
上行至根任务（根 dispatcher＝主会话 sid，R25-A 活绑定）经 router post——
落库先行，注入即视为拍到（stalled/env-gone/verify-dead 例外：synthetic notify
通道 resume:true——落消息并调度回合＝弹消息通知、无选择框，ag-3016f1 勘误
gt-5bc312 原降噪裁定只免选择框不免通知）；
escalate 同时落 events.jsonl 审计行（F8）。
core 只 import stdlib，依赖全注入。
"""
import json
import math
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
KIND_ENV_GONE = "env-gone"
KIND_ZOMBIE = "zombie"  # Req: F160 僵死梯子到顶（nudge 后零产出且不可再自动重开）
KIND_VERIFY_DEAD = "verify-dead"
KIND_VERIFY_INFRA = "verify-infra"  # Req: F122 signal 类机械失败（非质量败）
KIND_EXT_STALE = "external-stale"  # Req: F110 external handoff 驻留超龄
KIND_UNPUBLISHED = "unpublished"  # Req: F110 managed 已交付未出版

_UNTRUSTED = "---- 不可信原文 ----"
_RAW_MAX = 500
# 重复升级退避（ag-0f537d）：首档＝4×tick 节奏（默认 AGORA_HEALTH_INTERVAL 300s），
# 逐档再×4，封顶 24h
_STALE_GAP0 = 1200.0
_STALE_CAP = 86400.0
# Req: F160 连败达限口径（对齐 state.fail_streak_bump 硬编码 3；core 禁兄弟
# import 故本地字面量）：僵死梯子重派爬升到限只升级不再自动重开
_FAIL_STREAK_LIMIT = 3
# 活动面＝探活与 last_seen 的任务集（verify_failed/reopened 归在飞，同 task 口径）
_ACTIVE = {"dispatched", "running", "done", "verify_failed", "reopened", "blocked"}
_IN_FLIGHT = {"dispatched", "running"}  # 自愈重注入仅限 worker 在跑的两态

_KIND_NOTE = {
    KIND_QUALITY: "同一任务连续 3 次质量失败，已自动 blocked",
    KIND_GATEFILES: "任务 diff 触及闸体（构建/测试配置）",
    KIND_REINJECT: "下级会话死亡且重注入失败",
    KIND_DISPATCHER_DEAD: "派发方会话死亡，其在飞子链失管",
    KIND_STALLED: "在飞超 20min 无活动（僵态候选），请人工关注",
    KIND_ENV_GONE: "任务环境已不在盘——项目仓仍在则重派可重建 env（F71），"
                   "项目仓已亡则 cancel 归档",
    KIND_VERIFY_DEAD: "verify 命令自身死（exit 126/127：命令不存在/不可执行）"
                      "——非活不达标，worker 无从自愈；请修正验收命令或取消",
    KIND_VERIFY_INFRA: "verify 机械失败（signal 类：SIGKILL/SIGSEGV 等，疑似"
                       " OOM/基础设施）——非质量败不计连败，完整产物在 "
                       "gate_artifact 投影指向目录；请核对基础设施后重派或取消",
    KIND_EXT_STALE: "external handoff 驻留 awaiting_external 超龄（缺省 30min）"
                    "——外部派发方未确认交付，可能派发链路断裂，请人工核对",
    KIND_UNPUBLISHED: "managed 交付已 merged 但 origin 未同步（本地支领先未推）"
                      "——请派发方尽快自推，防 origin 长期缺交付",
    KIND_ZOMBIE: "nudge 自检后一个退避窗零产出——僵死梯子已到顶（连败达限或"
                 "该状态无自动重派边），不再自动重开；请人工处置（取消归档"
                 "或修复后重派）",
}

# F70/F73 共用提示：单源防 board 手动面与 health 自动面漂移
# Req: F160 产出锚——结论必须 post(kind=note) 一条：零产出可机械判定
SELF_CHECK_PROMPT = ("核对任务目标、当前动作与程序状态。若正常，简述当前进展后"
                     "继续；若异常或偏离目标，先安全停止当前动作，说明原因，"
                     "再选择安全的恢复方式；不要机械重启。无论结论如何，必须先"
                     " post(kind=note) 回发一条自检结论（产出锚）——退避窗内"
                     "零产出将被自动重派接管。")


def _subprocess_runner(cmd, input_text):
    """默认邮件外呼：正文走 stdin，30s 超时；非零退出/超时抛异常由调用方吞。"""
    r = subprocess.run(cmd, input=input_text, text=True, timeout=30,
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r


def send_email(store, tid, body_text, runner=None):
    """可选邮件外呼公共函数（F63 自 F5 决策记录抽出，msg/health 单实现）：opt-in。

    email.json 缺失/缺字段→静默跳过；失败/超时吞异常（邮件尽力而为），
    成败各落一行审计事件（actor=health：邮件基建归属本模块）。runner＝外呼缝
    （Health 与 main 装配的 Router 终态邮件共用），缺省真 subprocess。
    返回 bool：True＝发送成功；False＝未配置或发送失败（Router 据此不占去重窗）。
    """
    try:
        cfg = json.loads((store.home / ".agora" / "email.json")
                         .read_text(encoding="utf-8"))
        to, command = cfg["to"], cfg["command"]
    except Exception:
        return False  # 未配置＝零行为（F5 opt-in），返回值仍如实报未发
    try:
        (runner or _subprocess_runner)(shlex.split(command), body_text)
        event = "email_sent"
    except Exception:
        event = "email_failed"
    # shortcut: 同步外呼 ≤30s/封，终态与僵态皆稀有事件——health tick 批量同僵最坏
    # N×30s 阻塞定时线程、msg 终态邮件阻塞 httpd/gate 单写线程同量级（可接受
    # 上限），规模化再改异步聚合单邮件
    store.append_event({"ts": time.time(), "actor": "health",
                        "event": event, "task": tid, "to": to})
    return event == "email_sent"  # False＝确认败（含未配置）：调用方可据此重试


class Health:
    """tick 由 main 定时线程驱动；消息写面三线程并发（httpd/gate/health），
    消息号配给经 Store._mid_lock 串行（Req: F75 评审残留 D3）。"""

    def __init__(self, state, oc, router, recover, app, store,
                 runner=None, stale_candidates=None, env_activity=None,
                 draft_writer=None, gate_stale_after=1200.0,
                 gitx=None, ext_stale_after=1800.0, unpublished_after=3600.0):
        """app 按引用注入：main_sid 是活绑定（R25-A），escalate 每次取当前值。

        stale_candidates＝report.stale_candidates（core 禁兄弟 import，构造注入）；
        env_activity＝report._env_activity（F59 同款构造注入）；draft_writer＝
        pages.write_stalled_draft（F59 草稿页写面）；runner＝邮件外呼缝（gate
        同款），缺省真 subprocess；gate_stale_after＝P1b 门内 done 无阶段审计
        进展的拍人阈（秒，缺省 20min，测试可注入）。"""
        self._state, self._oc, self._router = state, oc, router
        self._recover, self._app = recover, app
        self._store = store
        self._runner = runner or _subprocess_runner
        self._stale_candidates = stale_candidates
        self._env_activity = env_activity
        self._draft_writer = draft_writer
        self._gate_stale_after = gate_stale_after
        # Req: F110 交付终局观测注入：gitx.publication（零网络 refs-only）；
        # ext_stale_after＝awaiting_external 驻留超龄阈（缺省 30min）；
        # unpublished_after＝managed delivered 未出版超龄阈（缺省 1h）
        self._gitx = gitx
        self._ext_stale_after = ext_stale_after
        self._unpublished_after = unpublished_after
        self._ext_esc = {}  # tid->(连拍次数 n, 末拍时刻)：external-stale 退避
        self._unpub_esc = {}  # tid->(n, last)：unpublished 退避
        # tid->(layer, commit, branch)：content-settled 已审边沿（评审环册B
        # 防事件重落）；ag-0bdf33 权威备忘持久化在任务行 delivery.settled
        # （daemon 重启随 tasks.jsonl 加载），此内存账只护持久化失败后的
        # 本进程重落
        self._settled_audit = {}
        self._stale_seen = set()  # 僵态候选边沿记忆（内存态，崩溃回退＝重拍一次）
        self._stale_esc = {}  # tid->(连拍次数 n, 末拍时刻)：重复升级退避（内存态）
        self._env_gone_seen = set()  # env 丢失周期边沿记忆（F72，内存态同上）
        # Req: F160 僵死梯子布防账（内存态）：tid->(nudge 时刻, updated 基线,
        # 任务链末条消息 ts 基线, 退避窗秒, nudge 时 worker_sid, nudge 时
        # state)——nudge 成功才布防，窗尽零产出即机械兜底；产出判据＝新消息
        # 或业务行更新（ag-d17587 修正：last_seen 前移系插件轮询噪音，不单独
        # 作判据）；状态迁移离集/产出/换 worker（reinject 或改绑车道已接管）/
        # 动作即销账（仅 touch/busy 新鲜化离集不销——窗尽照判；崩溃回退＝
        # 梯子从头爬）
        self._nudged = {}

    def tick(self):
        """F75 错址修复扫描先行（启动＋每轮双触发），再探每个活动任务的直接
        下跳：死→自愈/上报；边沿判僵拍人＋F70 worker 自检提示，不处置。"""
        self.repair_dispatcher_routes()
        self._wait_scan()  # Req: F124 受控等待解析先行——快照在解析后取（解除即恢复活性口径）
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
                    r = self._recover.reinject(tid, expect_wsid=wsid)
                    if r is not False:
                        continue  # True＝重建成功；None＝身份已易主让位（他车道接管，免误报 escalate，round-3）
                    self.escalate(KIND_DISPATCHER_DEAD if manages else KIND_REINJECT, tid)
                elif manages:
                    self.escalate(KIND_DISPATCHER_DEAD, tid)
            else:
                tm = data.get("time") or {}  # 缺形状→不 busy→照判（安全回退）
                if (tm.get("updated") or 0) > (tm.get("idle") or 0):
                    busy.add(tid)
        if not oc_down:
            self._stale_scan(tasks, busy)
        self._delivery_watch(tasks)  # Req: F110 交付终局观测（与 oc 活性无关）
        self._infra_backstop_scan(tasks)  # Req: F130 verify_infra 宽限兜底

    def _wait_scan(self):
        """Req: F124 受控等待解析：幂等扫描 waiting_on 任务（账本侧事实，
        与 oc 活性无关）——依赖进入 merged/closed/cancelled/blocked 任一
        终态即清 waiting_on＋审计 wait_resolved（附 dependency_state）＋
        双通知（worker process/dispatcher result，Router.notify_wait_resolved
        与 task 域登记即解析同源）。随既有 tick 驱动，不新建轮询线程；
        解析后同轮判僵即恢复常规活性口径（等待时长不伪造 last_seen）。
        等待任务自身已终态则跳过——字段保留作历史不再参与判定/通知；
        清除经 State.clear_waiting_on_if 锁内复核，判定窗内的并发换登记
        不被旧清除抹掉（评审环 round1 HIGH-1/HIGH-2）。"""
        for tid, t in self._state.snapshot()["tasks"].items():
            dep = t.get("waiting_on")
            if not dep:
                continue
            if t["state"] in ("merged", "closed", "cancelled", "blocked"):
                continue  # 等待任务自身已终态：waiting_on 留作历史（Req: F124）
            try:
                dep_state = self._state.get(dep)["state"]
            except KeyError:
                continue  # 依赖不在册（task_wait 验存在，账面不可能）：免判
            if dep_state in ("merged", "closed", "cancelled", "blocked"):
                if not self._state.clear_waiting_on_if(tid, dep):
                    continue  # 并发换登记：新等待不抹（下轮按新依赖重判）
                self._store.append_event(
                    {"ts": time.time(), "actor": "health",
                     "event": "wait_resolved", "task": tid,
                     "dependency": dep,
                     "note": f"dependency_state={dep_state}"})
                self._router.notify_wait_resolved(tid, dep, dep_state)

    def _stale_scan(self, tasks, busy):
        """边沿判僵（F5 决策记录 2026-10-28）＋重复升级退避（ag-0f537d）。

        免判口径（二批）：busy（updated>idle，回合进行中）本轮从候选集剔除；
        离开 busy 再超阈重新边沿触发。_stale_seen 每轮同步为当前候选集
        （离开即移除，之后再僵再拍）；重复升级间隔×4 指数退避封顶 24h——
        首拍不变，宿主排队忙/闲抖动反复再僵时降噪（ag-4628f9 实证）；
        退避窗内边沿不拍不记（下轮到期重试，拍到才记不丢报）。
        拍人后向 worker 注入一次 prompt 自检提示（F70）：模型自行纠偏，不强制
        打断；注入失败同轮不重试、不阻断拍人。nudge 成功即布防 F160 僵死梯子
        （_zombie_scan：退避窗内零产出→自动重派/到顶升级）。
        env 在册不在盘的候选改拍一次性 env-gone（F72，ag-724152）：每丢失周期
        只拍一次；持续丢失不拍 stalled、不注入自检（env 已失无从自纠）；
        恢复在盘回常规 stalled 边沿；终态出账。
        """
        if self._stale_candidates is None:
            return
        cand = [(tid, b) for tid, b
                in self._stale_candidates({"tasks": tasks}, time.time())
                if tid not in busy]
        now = time.time()
        # P1b（ag-9e055d 门楔死 1.5h）：done＋门内任务以阶段审计为活性基线
        # ——上次 phase_* 事件超过 gate_stale_after 无进展＝门内僵（含门线程
        # 挂死），并入候选集走同一边沿/退避；无 phase 事件（升级前在飞/崩窗
        # 残留）回落 last_seen/created 基线，不静默免检。
        gate_phase = {}  # tid→当前门阶段名（escalate 正文带出）
        for tid, t in tasks.items():
            if t["state"] == "done" and t.get("in_gate") and tid not in busy:
                phase, last = self._gate_phase(tid)
                # R1（ocr#2）：phase 基线与任务基线取新——重排再入门窗（旧轮
                # phase 事件在案、新轮首事件未发）不以旧轮事件误拍；门中真
                # 楔死时本轮 phase ts 恒新于 last_seen，判僵不受影响
                last = max(last or 0.0, t.get("last_seen") or 0.0,
                           t.get("created", 0.0))
                if now - last > self._gate_stale_after:
                    gate_phase[tid] = phase or "unknown"
                    cand.append([tid, last])
        renudged = set()  # 本轮补 nudge（布防中再入僵）：同 tick 免窗尽判定
        for tid, _ in cand:
            env_dir = tasks[tid].get("env_dir")
            if env_dir and not Path(env_dir).is_dir():  # F72 在盘判定（勿用 env_activity：0.0 分不清缺失与不活跃）
                if tid not in self._env_gone_seen:  # 每个丢失周期只拍一次
                    self.escalate(KIND_ENV_GONE, tid)
                    self._env_gone_seen.add(tid)  # 拍到才记（同 _stale_seen 语义）
                continue
            if tid not in self._stale_seen:  # 边沿：在候选集而不在记忆集
                n, last = self._stale_esc.get(tid, (0, 0.0))
                if now - last < self._stale_gap(n):
                    continue  # 退避窗内：抑制本轮拍人，不记边沿下轮重试
                self.escalate(KIND_STALLED, tid, streak=n + 1,
                              phase=gate_phase.get(tid))
                self._stale_esc[tid] = (n + 1, time.time())
                self._stale_seen.add(tid)  # 拍到才记：escalate 失败下轮重试不丢报
                wsid = tasks[tid].get("worker_sid")
                # 门内僵＝门侧楔死，worker 无从自纠：免 F70 自检注入
                if wsid and tid not in gate_phase:
                    # 布防中再入僵只补 nudge 不覆写布防（ag-d17587：微光
                    # 反复离集/入集若每次重开窗＝窗尽永不可达；F70 再提示保留）
                    nudged_ok = self.nudge_worker(tid)
                    if nudged_ok and tid not in self._nudged:
                        # Req: F160 产出锚布防：nudge 已达 worker，退避窗内
                        # 零产出（无该任务新消息且无业务行更新——last_seen
                        # 前移不算，ag-d17587 修正）→机械兜底
                        self._nudged[tid] = (
                            time.time(), float(tasks[tid].get("updated") or 0.0),
                            self._task_msg_ts(tid) or 0.0,  # 读故障基线 0.0＝宽松
                            self._stale_gap(self._stale_esc[tid][0]), wsid,
                            tasks[tid].get("state"))
                    elif nudged_ok and tid in self._nudged:
                        renudged.add(tid)  # nudge 达布防中任务：给生效窗（B 册 suggestion；R4：仅送达才顺延）
        self._zombie_scan(tasks, {tid for tid, _ in cand}, busy, renudged)
        self._stale_seen &= {tid for tid, _ in cand}  # 离开候选集即移除（复位再拍）
        # 终态任务出账：防长驻 daemon 无界增长（终闸 B1；在飞任务退避记忆保留）
        self._stale_esc = {tid: v for tid, v in self._stale_esc.items()
                           if tasks.get(tid, {}).get("state") in _ACTIVE}
        # F72 出账：env 回到在盘或任务终态/除籍即结束丢失周期（下周期可再拍）；
        # 经任务全集判在失——busy 轮离开候选集不误出账（防忙闲抖动重拍）
        self._env_gone_seen = {tid for tid in self._env_gone_seen
                               if tasks.get(tid, {}).get("state") in _ACTIVE
                               and tasks[tid].get("env_dir")
                               and not Path(tasks[tid]["env_dir"]).is_dir()}

    # ---- Req: F160 僵死梯子机械兜底 ----

    def _zombie_scan(self, tasks, cand_ids, busy_ids=(), renudged=()):
        """nudge 后一个退避窗内零产出（无该任务新消息且无业务行更新——
        last_seen 前移不单独构成判据，ag-d17587 修正：插件后台轮询刷
        touch 噪音实证）＝worker 对自检产出锚无响应：自动重派（running 走 ag-d1d8c1 stalled
        重派边＝废旧牌＋新会话接管、env 不回收零数据损失；fail_streak 记梯子
        爬升）；连败达限/无重派边/重派被拒只 escalate(kind=zombie) 不再自动
        重开。动作全程审计＋邮件告知；状态迁移离集或已产出＝销账（仅 touch/
        busy 新鲜化离集不销——last_seen 非判据，窗尽照判，ag-d17587 修正；
        busy 回合进行中或同 tick 刚补 nudge 时窗尽顺延不销不拍，终闸 B 册），
        再僵从边沿重爬梯；动作面半失败回填布防下轮重试
        （拍到才记口径，防候选期内永久静默——评审环 R2 B 册）。"""
        now = time.time()
        for tid in list(self._nudged):
            ts, upd_base, msg_base, gap, wsid_base, state_base = self._nudged[tid]
            t = tasks.get(tid)
            if tid not in cand_ids:
                # 离集两形（ag-d17587 修正）：状态迁移离队（done/blocked 门侧
                # 接管等）＝静默解除；仅新鲜化离队（touch 微光/busy）不销布防
                # ——last_seen 前移非有效判据，窗尽照常零产出判定
                if t is None or t.get("state") != state_base:
                    del self._nudged[tid]  # 状态离队＝解除布防（零产出判定失效）
                    continue
            if now - ts < gap:
                continue  # 退避窗内：等产出锚
            if tid in busy_ids or tid in renudged:
                # 窗尽顺延（终闸 B 册）：回合进行中（busy）硬判会撞
                # redispatch_stalled 的 busy fail-closed→zombie 误报＋梯级
                # 误消费；本轮刚补 nudge（布防中再入僵）同理给生效窗——
                # 均不销不拍，下轮再判
                continue
            entry = self._nudged.pop(tid)  # 本梯级消费：动作与否都销账（防逐 tick 重拍）
            if t is None or t.get("state") not in _ACTIVE:
                continue  # 窗内已终态/除籍（人工 cancel 等）：免动作
            if t.get("worker_sid") != wsid_base:
                continue  # worker 已易主（reinject 重建/改绑/人工重派）：他车道接管
            msg_ts = self._task_msg_ts(tid)
            if msg_ts is None:
                # 读故障回填（评审环 R3）：持续候选任务边沿不再触发，纯销账
                # ＝进程生命周期内梯子闩死——回填下轮重读；None 期零动作，
                # 宁漏杀不误杀方向不变
                self._nudged[tid] = entry
                continue
            # 产出判据（ag-d17587 修正）：新消息（任意 from，含 note）或同态
            # 业务行更新（updated 前移且状态未迁移——迁移走离集/易主/新鲜行
            # 预检各自收口，不在此静默销账）；last_seen 不参与（仅 stalled 入口）
            if (msg_ts > msg_base
                    or (float(t.get("updated") or 0.0) > upd_base
                        and t.get("state") == state_base)):
                continue  # 已产出（① 的 note 锚或业务事件）：梯子收起
            age = max(0, int(now - (t.get("last_seen") or t.get("created") or 0.0)))
            try:
                why = self._auto_redispatch(tid, t, window_s=int(gap), age_s=age)
                if why is not None:
                    self.escalate(KIND_ZOMBIE, tid, raw=why)
            except Exception as e:
                self._nudged[tid] = entry  # 半失败回填：动作下轮重试不丢报
                try:  # 失败留痕（评审环 R3）：静默重试环不可观测，同 rejected 口径
                    self._store.append_event(
                        {"ts": time.time(), "actor": "health",
                         "event": "auto_redispatch_error", "task": tid,
                         "note": f"zero_output retry: {e}"[:200]})
                except Exception:
                    pass
                continue

    def _auto_redispatch(self, tid, t, window_s, age_s):
        """机械重派单点：连败达限/状态无重派边先判（只升级），重派经
        TaskService.redispatch_stalled daemon 权威面（token 闸旁路同
        recover.reinject 口径）。成功即 fail_streak＋1（梯子爬升记账）＋
        auto_redispatch 审计＋dispatcher 通知＋邮件；失败拒因随 zombie 升级
        raw 上行（auto_redispatch_rejected 审计留痕）。返回 None＝已重派/
        无需升级，否则＝升级 raw。"""
        note = f"zero_output window_s={window_s} last_seen_age_s={age_s}"
        # 预检用新鲜行（评审环 R2 B 册）：布防窗可达数小时，快照行可被门侧
        # 走完 running→done→verify×3→blocked——机械重开质量封锁任务＝击穿
        # 「达限只 escalate」语义；行读失败回落快照行（报因上行，不空转）
        try:
            t = self._state.get(tid) or t
        except Exception:
            pass
        n = t.get("fail_streak", 0)
        if n >= _FAIL_STREAK_LIMIT:
            return f"{note} fail_streak={n} 达限——停止自动重开"
        if t.get("state") != "running":
            return f"{note} state={t.get('state')} 无自动重派边（仅 running stalled）"
        ts = getattr(self._app, "task", None) if self._app is not None else None
        if ts is None or not hasattr(ts, "redispatch_stalled"):
            return f"{note} no redispatch face wired"
        try:
            ts.redispatch_stalled(tid)
        except Exception as e:
            self._store.append_event(
                {"ts": time.time(), "actor": "health",
                 "event": "auto_redispatch_rejected", "task": tid,
                 "note": f"{note} {e}"[:200]})
            return f"{note} redispatch rejected: {e}"
        try:
            # transition(dispatched) 清零后回填：梯子爬升与质量连败共用计数
            # （达限即停自动重开；set_fields 不触发 fail_streak_bump 的自动
            # blocked——重派后立即 blocked 会废掉整条自愈车道）。写前重读新鲜行值
            # （C 册：快照 n 到写点隔整轮 tick，门侧并发 bump 可被旧值覆盖）
            fresh = self._state.get(tid) or t  # 行缺席回落预检行（R3：None 炸记账）
            # transition(dispatched) 已把 fresh 清零（再 dispatch 清连败）：
            # 保留预检累计值爬梯（R4：纯回读恒 0＝梯子恒 1 震荡，达限 zombie
            # 永不可达）；max 仅让位复位后到达的门侧并发 bump
            n = max(n, fresh.get("fail_streak", 0))
            self._state.set_fields(tid, fail_streak=n + 1)
        except Exception:
            pass  # 记账败不回滚重派（下轮爬升重新计，崩溃回退口径同内存态）
        # 重派不刷基线（F15）→任务留在候选集且 _stale_seen 未复位，边沿
        # 永不再触发＝梯子爬不上去（评审环 R1）：丢弃边沿记忆，_stale_esc
        # 退避在本梯级窗尽时恰好到期，下轮重新拍僵＋nudge 新 worker——梯子
        # 才能逐级爬到 _FAIL_STREAK_LIMIT（替换 worker 持续零产出场景）。
        self._stale_seen.discard(tid)
        self._store.append_event(
            {"ts": time.time(), "actor": "health",
             "event": "auto_redispatch", "task": tid,
             "note": f"{note} fail_streak={n}->{n + 1}"})
        body = (f"僵死梯子（F160）：任务 {tid} nudge 后退避窗 {window_s}s 零产出，"
                f"已自动重派新 worker 接管（连败 {n + 1}/{_FAIL_STREAK_LIMIT}，"
                f"达限即停自动重开）")
        try:
            self._router.notify_dispatcher(tid, "process", body)
        except Exception:
            pass  # 通知面尽力而为（F54 口径）：审计已落不回滚
        try:
            send_email(self._store, tid, body, runner=self._runner)
        except Exception:
            pass  # 邮件面尽力而为（R4）：send_email 末尾审计落库异常不得把
                 # 已完成接管回填成半失败重试（下轮易主静默销账＝邮件永失）
        return None

    def _task_msg_ts(self, tid):
        """该任务任务链末条消息 ts（F160 产出基线；ag-d17587 修正放宽：
        任意 from——worker 上行 question/note/done/verify 与派发方回发
        皆任务面产出；kind!=escalate 仍排除——escalate 行 from 被 router
        按 "up" 边归 worker、系服务合成非产出。store 读回 ts 恒为 str
        （F92 frontmatter 值一律 str）——float 收敛防异型比较炸 tick。
        坏 ts 行（非数值 str/异型/非有限值 nan·inf——float 原生可解析，
        终闸 B 册 R1）沿缺 ts＝0.0 宽松口径收敛（B 册
        suggestion：行在不炸健康面，时序语义不另造；NaN 基线会令产出
        判据恒假＝误杀活 worker，inf 反向漏杀，同归 0.0 两向都修）。
        返回 None＝消息面读故障（窗尽判定按已产出跳过——宁漏杀不误杀，
        评审环 R2 B 册）；无匹配消息＝0.0（布防侧作基线＝宽松正确）。"""
        try:
            rows = self._store.read_messages()
        except Exception:
            return None
        m = next((m for m in reversed(rows)
                  if m.get("task") == tid and m.get("kind") != "escalate"), None)
        if m is None:
            return 0.0
        try:
            v = float(m.get("ts") or 0.0)
        except (TypeError, ValueError):  # 坏 ts＝缺 ts：0.0 收敛不炸 tick
            return 0.0
        return v if math.isfinite(v) else 0.0  # nan/inf 绕得过 float()——同归 0.0

    def _delivery_watch(self, tasks):
        """Req: F110 交付终局观测：external handoff 超龄未确认＋managed 未出版。

        与 worker 活性无关（done/merged/closed 也扫）——拍的是派发方：
        external-stale＝done＋awaiting_external 且 handoff_at 超龄（issue-agent
        等外部派发方失联的兜底告警）；unpublished＝managed delivered 且
        publication ahead>0 且超龄（origin 长期缺交付的提醒）。重复升级按
        _stale_gap 退避；确认/出版/除籍即出账，再犯再拍。content-settled
        备忘持久化（ag-0bdf33）：delivery.settled 在案＝永久出账（daemon
        重启自 tasks.jsonl 加载，免三层判定与升级）。"""
        now = time.time()
        for tid, t in tasks.items():
            d = t.get("delivery") or {}
            if t["state"] == "done" and d.get("status") == "awaiting_external":
                ts = d.get("handoff_at")
                if ts and now - ts > self._ext_stale_after:
                    n, last = self._esc_book(self._ext_esc, tid,
                                             KIND_EXT_STALE)
                    if now - last >= self._stale_gap(n):
                        self.escalate(KIND_EXT_STALE, tid, streak=n + 1,
                                      raw=f"commit={d.get('commit')} "
                                          f"age={int(now - ts)}s")
                        self._ext_esc[tid] = (n + 1, now)
            else:
                self._ext_esc.pop(tid, None)
            if (t["state"] in ("merged", "closed")
                    and d.get("mode") == "managed"
                    and d.get("status") == "delivered"
                    and now - (d.get("delivered_at") or t.get("updated")
                               or 0.0) > self._unpublished_after):
                # ag-0bdf33 备忘持久化：delivery.settled 在案（daemon 启动
                # 自 tasks.jsonl 加载，重启不失忆）＝内容已落定的永久出账
                # ——探针免全部三层判定与升级（publication 亦免，零 gitx 面）
                try:
                    pub = (self._gitx.publication(t["project_dir"],
                                                  t.get("target_branch"))
                           if self._gitx and not d.get("settled")
                           else None)
                    # 祖先对账（ag-6c673c 误报）＋补丁等价对账（ag-4c6136
                    # 误报）＋内容落定（ag-b0efd4 孪生残留；ag-0bdf33 窗口
                    # 改锚——内容落定窗自交付时间起算，不随并行提交数漂移）：
                    # 分支 ahead 可能来自无关提交；交付 commit 也可能被派发
                    # 方 rebase 改 sha 后出版；rebase 孪生还可能上下文漂移/
                    # 纯二进制交付致 patch-id 两层失明——sha、补丁与内容
                    # 落定均不在 origin 才拍（三漏才复读轰炸）
                    unpublished = bool(
                        pub and pub.get("origin_ref") and d.get("commit")
                        and not self._gitx.published_on_origin(
                            t["project_dir"], d["commit"], pub["branch"])
                        and not self._gitx.patch_equivalent_on_origin(
                            t["project_dir"], d["commit"], pub["branch"])
                        and not self._content_settled(
                            tid, t["project_dir"], d["commit"],
                            pub["branch"],
                            d.get("delivered_at") or t.get("updated")
                            or 0.0))
                except Exception:
                    pub, unpublished = None, False  # 投影/对账不可得本轮免判（下轮重试）
                if unpublished:
                    n, last = self._esc_book(self._unpub_esc, tid,
                                             KIND_UNPUBLISHED)
                    if now - last >= self._stale_gap(n):
                        self.escalate(KIND_UNPUBLISHED, tid, streak=n + 1,
                                      raw=f"branch={pub.get('branch')} "
                                          f"ahead={pub['ahead']} "
                                          f"commit={d.get('commit')} "
                                          f"origin_ref={pub.get('origin_ref')}")
                        self._unpub_esc[tid] = (n + 1, now)
                else:
                    self._unpub_esc.pop(tid, None)  # 已出版/无 origin：出账
            else:
                self._unpub_esc.pop(tid, None)
        for book in (self._ext_esc, self._unpub_esc):
            for tid in list(book):
                if tid not in tasks:  # 除籍出账：防长驻 daemon 无界增长
                    del book[tid]
        for tid in list(self._settled_audit):
            if tid not in tasks:
                del self._settled_audit[tid]

    def _content_settled(self, tid, project_dir, commit, branch,
                         since=None) -> bool:
        """第三层「内容落定」（ag-b0efd4，unpublished 孪生残留兜底；
        ag-0bdf33 备忘持久化＋时间窗锚）：祖先与 patch-id 两层均未命中后，
        per-file blob 落定（二进制可判，同 blob 强于同补丁）或新增行包含性
        （上下文漂移孪生）任一命中即视同已出版——停升级并把备忘持久化到
        任务行 delivery.settled={layer,commit,branch,at}（tasks.jsonl 随
        daemon 重启加载，探针命中即永久出账——重启失忆不再复发复读），
        同时落 content-settled 审计行（判定层＋commit/branch 证据指针，
        注明 persisted）；判定窗口＝自交付时间起的 reachable 窗口（since
        锚，不随并行提交数漂移）；判定异常上抛交调用方本轮免判（下轮
        重试）；持久化/审计败尽力而为不逆转判定（F54 口径）。"""
        layer = None
        if self._gitx.blob_settled_on_origin(project_dir, commit, branch,
                                             since=since):
            layer = "blob"
        elif self._gitx.lines_settled_on_origin(project_dir, commit, branch,
                                                since=since):
            layer = "lines"
        if not layer:
            self._settled_audit.pop(tid, None)  # 边沿回落：重臂（首落定前
            # 判定 miss 照常升级；已持久化任务探针先跳不至此——备忘＝
            # 永久闩，重启/判定回落均不复读）
            return False
        key = (layer, commit, branch)
        if self._settled_audit.get(tid) == key:
            return True  # 已审边沿：驻留任务每轮 tick 不重落（防事件无界增长）
        persisted = False
        try:
            d = dict((self._state.get(tid) or {}).get("delivery") or {})
            d["settled"] = {"layer": layer, "commit": commit,
                            "branch": branch, "at": time.time()}
            self._state.set_fields(tid, delivery=d)
            persisted = True
        except Exception:
            pass  # 持久化尽力而为不逆转判定（F54 口径）：备忘不置位＝下轮
            # 重落（审计行随之重发一条，append-only 账本可接受）
        try:
            self._store.append_event({"ts": time.time(), "actor": "health",
                                      "event": "content-settled",
                                      "task": tid, "layer": layer,
                                      "commit": commit, "branch": branch,
                                      "note": "persisted" if persisted
                                      else "memo_not_persisted"})
        except Exception:
            pass  # 审计行尽力而为（F54）：败不拦判定；持久化已先行——备忘
            # 在案即权威（探针短路后审计缺行不再可重试，如实接受）
        if persisted:
            self._settled_audit[tid] = key  # 持久化成功才记边沿：败则下轮重试
        return True

    def _infra_backstop_scan(self, tasks):  # Req: F130
        """verify_infra 驻留宽限兜底：到点仍 done∧非门内∧非 awaiting_external
        ＝派发方失效零动作（ag-aa8798 判例 13h 死锁）——服务自动重排恰一次
        （reverify 同款投 gate_q，审计 verify_infra_backstop/actor=health）；
        重排后再败由门体转 blocked（exhausted）。owner 活着且动作即撤销计时
        （用户裁定：服务端兜底仅在派发方失效时出手）：reverify（task 面）、
        dispatch/cancel/终态（State.transition）、转 in_gate（gate.run 入门）、
        worker 心跳（last_seen>since）五面各自撤销。"""
        now = time.time()
        q = getattr(self._app, "gate_q", None)
        for tid, t in tasks.items():
            g = t.get("verify_infra_grace")
            if not g:
                continue
            if t["state"] != "done" or t.get("in_gate"):
                continue  # 非驻留态：计时由迁移/入门面撤销，此为竞态兜底
            d = t.get("delivery") or {}
            if d.get("status") == "awaiting_external":
                continue  # 外部交付驻留＝设计内稳态（F101），永不触发
            if (t.get("last_seen") or 0.0) > (g.get("since") or 0.0):
                # 锁内复核再清（R3）：窗内可能已装新宽限/任务已除籍——陈旧
                # 清除不得误伤新周期（clear_infra_grace_if 自吞 KeyError 面）
                self._state.clear_infra_grace_if(tid, g.get("since"))
                continue  # 宽限窗内 worker 动作：撤销计时
            if now < (g.get("deadline") or 0.0) or q is None:
                continue  # 未到点／队列缺席（无 httpd 装配）：下轮再判
            # 落账走锁内复核（R2：陈旧快照 TOCTOU——窗内 reverify/入门/心跳
            # 已动作即 False 免投队）；True 才审计＋投队＝恰一次且不抹 owner 动作
            if not self._state.mark_infra_backstop(tid, ts=now):
                continue
            try:
                self._store.append_event({"ts": now, "actor": "health",
                                          "event": "verify_infra_backstop",
                                          "task": tid})
            except Exception:  # R3 审计尽力而为（F54 口径）：败不吞唯一投队
                pass
            q.put(tid)

    def repair_dispatcher_routes(self):
        """F75 存量错址修复：active 顶层任务 dispatcher_sid=='root' 且 root_sid
        为真实会话（CLI 无 sid 派发覆写所致）→ 归属行回写 root_sid＋
        dispatcher_route_repaired 审计（含 from/to 与成因）＋积压补交
        （router.replay_root_backlog）。幂等：修后行不再匹配、补交按 replay_of
        锚去重；嵌套（链边归父 worker）、终态、root_sid 缺席或 =='root'（真
        headless）一律不动——不按 main_sid/最近活跃会话猜归属，防多窗口抢绑。
        返回修复数。"""
        n = 0
        for tid, t in self._state.snapshot()["tasks"].items():
            rs = t.get("root_sid")
            if (t["state"] in _ACTIVE and t.get("chain_depth", 1) == 1
                    and t.get("dispatcher_sid") == "root" and rs and rs != "root"):
                try:
                    # 先补交后回写（评审 B 册 D1）：replay 中途写败→行未修→下轮
                    # 仍匹配重试（replay_of 锚防重复副本）；先修后补会孤儿剩余积压
                    self._router.replay_root_backlog(tid, rs)
                    self._state.set_fields(tid, dispatcher_sid=rs)
                    self._store.append_event(
                        {"ts": time.time(), "actor": "health",
                         "event": "dispatcher_route_repaired", "task": tid,
                         "note": f"from=root to={rs} "
                                 "cause=cli_dispatch_without_sid"})
                    # 回写记账后再扫一次（评审环：首扫含阻塞 inject，窗口可达
                    # 秒级，窗内落库的 to=root 上行此轮即补——锚幂等不产重复副本）
                    self._router.replay_root_backlog(tid, rs)
                except Exception:  # 单任务修复面败不废整轮 tick：下轮重试，探活照跑
                    continue
                n += 1
        return n

    def nudge_worker(self, tid, text=None):
        """F70/F73 单一注入面：缺省＝自检，text＝board 派发方自定义提示。

        不强制打断（prompt 由 opencode 排队/转向）；无 worker 或注入异常
        返回 False，是否审计/报因归调用方。"""
        wsid = self._state.get(tid).get("worker_sid")
        if not wsid:
            return False
        body = (f"[agora] 僵态自检（任务 {tid}）：{SELF_CHECK_PROMPT}"
                if not text else
                f"[agora] dispatcher prompt（任务 {tid}）\n{text}")
        try:
            return bool(self._oc.inject(wsid, body))
        except Exception:
            return False

    def _esc_book(self, book, tid, kind):
        """退避账本查取（miss 时经事件面重建，agorad 重启退避续命）：
        n=该 kind 历史升级次数、last=末次升级 ts；无事件记 (0, 0.0)。
        仅在将要拍人的分支调用——出账路径不重建，防逐 tick 扫事件面。"""
        ent = book.get(tid)
        if ent is None:
            try:
                evs = [e for e in self._store.read_events(tid)
                       if e.get("event") == "escalate" and e.get("kind") == kind]
            except Exception:
                evs = []  # 事件面读故障按首拍处理，不阻塞拍人（降级纪律同既有）
            ent = (len(evs), evs[-1].get("ts", 0.0)) if evs else (0, 0.0)
            book[tid] = ent
        return ent

    def _stale_gap(self, count):
        """已连拍 count 次后的再拍退避窗（秒）：首档 1200s 逐档×4，封顶 24h。"""
        # 指数钳 15：count 数百时 4**count 先于 min 爆 float 上限（OverflowError
        # 每轮重抛会静默杀掉整条升级链，终闸 B2）
        return min(_STALE_GAP0 * 4 ** min(max(count - 1, 0), 15), _STALE_CAP)

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
        # ag-3016f1 链向补证（ag-acc22e 死锁）：末条链边消息方向——worker 上行
        # 未应答时卡点可能在 dispatcher 侧；取不到整行省略（降级纪律同既有）
        try:
            # to 空＝peer 留言（非链边）不入选（评审环 C 册：防误标 worker→dispatcher）
            last = next(m for m in reversed(self._store.read_messages())
                        if m.get("task") == tid and m.get("to"))
            frm = last.get("from")
            ev["last_chain_msg"] = (
                "worker→dispatcher 未应答——卡点可能在 dispatcher 侧"
                if frm and frm == t.get("worker_sid")
                else f"{frm or '-'}→{last.get('to') or '-'}")
        except Exception:
            # 无链边消息（StopIteration）／消息面读故障：整行省略不阻塞拍人（评审环 B 册）
            pass
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

    def _gate_phase(self, tid):
        """P1b 门阶段进度读数：（当前阶段名, 末次 phase_* 审计 ts）。

        事件面读故障/无 phase 事件＝(None, None)——证据缺席由调用方回落任务
        基线，不阻塞判僵（降级纪律同 F59 证据包）。"""
        try:
            evs = [e for e in self._store.read_events(tid)
                   if str(e.get("event", "")).startswith("phase_")]
        except Exception:
            return None, None
        if not evs:
            return None, None
        last = evs[-1]
        ts = last.get("ts") or 0.0
        return str(last["event"])[len("phase_"):], ts or None

    def escalate(self, kind, tid, raw=None, streak=None, phase=None):
        """失控拍人：正文服务权威合成；raw 为任务/worker 供给文本（不可信）。

        stalled/env-gone/verify-dead 走通知通道（synthetic resume:true——落合成消息
        并调度回合：弹消息通知、无选择框，idle dispatcher 被唤醒；gt-5bc312 勘误
        ag-3016f1，原裁定只免选择框；F72 env-gone、F80 verify-dead 同款），其余
        kind 照常真实
        会话注入；stalled 边沿附末次活动摘要（F52，失败降级）＋证据包小节
        与草稿页路径（F59，降级纪律：证据永不阻塞拍人；env-gone 正文自足
        不给）。streak＝同任务连续升级计数（退避节奏见 _stale_scan），正文随带。"""
        t = self._state.get(tid)
        body = (f"失控[{kind}] 任务{tid}：{_KIND_NOTE[kind]}；"
                f"状态 {t['state']}，worker={t.get('worker_sid') or '-'}，"
                f"dispatcher={t.get('dispatcher_sid') or '-'}，"
                f"连败 {t.get('fail_streak', 0)}")
        if streak:
            body += f"，第 {streak} 次升级"
        if phase:  # P1b 门内僵：当前门阶段随行（服务侧事件面事实，非供给文本）
            body += f"，门阶段 {phase}"
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
        quiet = kind in (KIND_STALLED, KIND_ENV_GONE, KIND_VERIFY_DEAD,
                         KIND_VERIFY_INFRA,  # Req: F122 signal 类同走通知通道
                         KIND_EXT_STALE, KIND_UNPUBLISHED,  # Req: F110 同走通知通道＋邮件
                         KIND_ZOMBIE)  # Req: F160 梯子到顶：通知通道唤醒＋邮件
        self._router.post(rt.get("worker_sid"), ("up", root), "escalate", body,
                          notify=quiet)  # stalled/env-gone/verify-dead/verify-infra 通知通道：synthetic resume:true 唤醒 dispatcher（无选择框；gt-5bc312 勘误 ag-3016f1；F72/F80/F122 同款）
        if quiet:  # 邮件例外（§6 决策记录）：stalled/env-gone（F72）＋verify-dead（F80：命令死需人工处置）＋verify-infra（F122 同款）
            send_email(self._store, tid, body, runner=self._runner)

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
