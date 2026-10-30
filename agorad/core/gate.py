"""verify 门与自动合并：任务环境验收→集成环境试合并/验收→目标支最终推进。

done 受理仍执行状态、verify、commit 格式与评审锚前置校验。门体先在任务 env 跑
verify，再在目标分支当前 HEAD 的 detached integration worktree 内 ff-only 试合并
并重跑 verify；试合并遇真 non-ff 先门内 detached rebase 干净自愈（F32：重放后
集成 verify 跑在新内容上，绿则 sha 级 ff 推进），同步冲突才弹回；只有全绿且目标
主检出两次干净检查通过，才推进真实目标分支。集成
失败不再依赖 reset --hard 回滚，用户未提交工作与目标分支均不受测试副作用影响。

默认 runner 以独立进程组执行命令，超时杀全组并返回 124，避免单个挂死验证永久
堵住串行门。崩溃重排先探测目标支是否已含任务 commit：已含说明上轮最终推进已
完成，只补 merged/closed 终态，不重验后假 reopened。gate_env 与 target_branch 的
原始分支位随任务快照持久化，集成环境和分支位均可跨重启收敛。

全程 in-gate 门闩、pending-cancel 门尾原子结算、三连败 blocked 与事件审计保持
不变。core 只 import stdlib，依赖全构造注入。
"""
import os
import signal
import subprocess
import time
from pathlib import Path

# 升级 kind 与 health 的类别词汇对齐（core 禁 import 兄弟模块，字面量本地定义）
KIND_QUALITY = "quality"
KIND_GATEFILES = "gatefiles"

_TAIL = 2000
# 闸体＝verify 依赖面（构建/测试配置）：任务 diff 触及即升级拍人
_GATE_PATTERNS = ("Makefile", "*.mk", "pyproject.toml", "setup.py", "setup.cfg",
                  "requirements*.txt", "package.json", "package-lock.json",
                  "go.mod", "Cargo.toml", "tox.ini", "noxfile.py",
                  ".github/workflows/*")


# F32 门环境消毒：服务自身态三变量不随 env 泄入 verify 子进程链——test_install.sh
# 类自测拉起的 daemon 经 main.py os.environ.get("AGORA_PORT","0") 硬绑服务在用端口即崩。
# AGORA_HOME 明示不剥：无事故面，夹具本就自设各自值。
_GATE_ENV_STRIP = ("AGORA_PORT", "AGORA_OC_URL", "AGORA_OC_PASSWORD")


def _subprocess_runner(env_dir, cmd, timeout=300):
    """真 runner：独立进程组跑 verify；超时杀全组，rc=124 按质量败处理。

    F32 门环境消毒：剥除 _GATE_ENV_STRIP 三枚服务自身态变量后以显式副本传子进程
    （不改 os.environ 本体）。
    """
    child_env = {k: v for k, v in os.environ.items() if k not in _GATE_ENV_STRIP}
    proc = subprocess.Popen(cmd, shell=True, cwd=env_dir, env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 超时与正常退出竞速：进程组已消失即无需补杀
        out, err = proc.communicate()
        return 124, ((out or "") + (err or "") +
                     f"\nverify 超时（>{timeout:g}s）——已终止验证进程组；"
                     "可用 AGORA_VERIFY_TIMEOUT 调整上限")
    return proc.returncode, (out or "") + (err or "")


class Gate:
    """accept_done 单写者串行受理；run 为后台线程体（httpd 包 Thread 调用）。"""

    def __init__(self, state, gitx, router, oc, read_anchor, health, auth,
                 store, runner=None, verify_timeout=300, on_blocked=None):
        self._state, self._gitx, self._router, self._oc = state, gitx, router, oc
        self._read_anchor, self._health, self._auth = read_anchor, health, auth
        self._store, self._on_blocked = store, on_blocked
        self._runner = runner or (lambda env, cmd: _subprocess_runner(
            env, cmd, timeout=verify_timeout))

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
        c = commit.lower()
        if len(c) != 40 or any(ch not in "0123456789abcdef" for ch in c):
            return self._deny_done(  # F13：body-format 先行，参数错不再伪装成锚问题
                tid, "body-format",
                f"done body 须为纯 40 位十六进制完整 commit sha（收到 {commit[:20]!r}）"
                "——无前缀无附言；结论文本另发 post(kind=note)")
        commit = c  # 归一一次：锚比较与落库均用小写，大写 sha 不再伪装成锚问题（OCR）
        children = [(cid, x) for cid, x in self._state.snapshot()["tasks"].items()
                    if x.get("dispatcher_sid") == worker_sid]
        open_children = [cid for cid, x in children
                         if x["state"] not in ("merged", "closed", "cancelled")
                         or str(x.get("cancel_reason", "")).startswith("dependency_failed")]
        if open_children:
            return self._deny_done(
                tid, "children",
                f"嵌套子任务未结案：{', '.join(sorted(open_children))}——"
                "等待 result 回传，或由你取消子任务；成功/有意跳过后再 post(done)")
        # 锚检查仅评审环在位时适用：缺失→放行 done，由门体 review_absent 分支驻留
        if (Path(t["env_dir"]) / ".review-loop").is_dir():
            a = self._read_anchor(t["env_dir"])  # 锚＝worktree 分支 HEAD 且无 blocker/high
            if a is None:
                return self._deny_done(
                    tid, "anchor",
                    f"任务 {tid} 评审环无锚或锚落后（.review-loop/.last-reviewed 缺失"
                    "或不等于分支 HEAD）——跑 /review-loop 至锚绿，重新 post(done, commit)")
            if a.get("head") != commit:
                return self._deny_done(
                    tid, "anchor",
                    f"任务 {tid} {commit} ≠ 分支 HEAD {a.get('head')}——"
                    "以 env 内完整 HEAD sha 重新 post(done, commit)")
            if not a.get("clean"):
                return self._deny_done(
                    tid, "anchor",
                    f"任务 {tid} 评审环未绿——修复 blocker/high 后"
                    "重新 post(done, commit)")
        self._state.transition(tid, "done", commit=commit,
                               rebased_from=None)  # F32：受理即清陈旧留痕（唯一清点）
        self._audit(tid, "done", commit=commit)
        return "accepted"

    def _deny_done(self, tid, tag, msg):
        """F13：done 拒收统一出口——先审计 done_rejected（note=tag）再带因拒回。"""
        self._audit(tid, "done_rejected", note=tag)
        return f"rejected:{tag}: {msg}"

    # ---- 门体（后台线程体） ----

    def run(self, tid):
        t = self._state.get(tid)
        branch = t.get("target_branch")  # 合入目标支（外部工作流）；缺省＝仓默认支
        env, target, cmd = t["env_dir"], t["project_dir"], t["verify"]["cmd"]
        integration = t.get("gate_env") or f"{env}__gate"
        origin = t.get("gate_origin_branch")  # 崩溃重排沿用首轮分支位，不采信漂移后的现场
        self._state.set_fields(tid, in_gate=True, gate_env=integration)
        try:
            # kill 落在“目标已推进、merged 未落库”窗口：任务提交已在目标支即只补终态，
            # 绝不重跑集成 verify 后把已合法合并的任务假 reopened，更不需要 reset --hard。
            if self._gitx.contains(target, t["commit"], branch):
                self._succeed(tid)
                return
            self._gitx.ensure_clean(target)  # 主检出写前硬闸：用户未提交/未跟踪工作一律不碰
            if branch:
                origin = origin or self._gitx.symref(target)
                self._state.set_fields(tid, gate_origin_branch=origin)
            if self.gate_files_touched(tid):  # 闸体变更：拍人不拦门
                self._health.escalate(KIND_GATEFILES, tid)
            warn = self._gitx.diff_check(env, branch)  # 冲突标记机械检查，先于用户 verify
            if warn:  # gt-83185d R3：非空走 verify_failed 口径（截 2000 与 verify 败一致）
                self._quality_fail(tid, "verify_failed",
                                   "diff 检查发现冲突标记/空白错误——清理后重新"
                                   f" post(done, commit)\n输出尾部:\n{warn[-_TAIL:]}")
                return
            rc, out = self._runner(env, cmd)
            if rc != 0:
                self._quality_fail(tid, "verify_failed",
                                   f"verify 失败——修复后重新 post(done, commit)"
                                   f"\n输出尾部:\n{out[-_TAIL:]}")
                return
            a = self._read_anchor(env)  # 合并前锚重验：堵 accept→run 窗口 TOCTOU
            # 锚比对对象＝rebased_from or commit（F32 裁定①）：崩溃重排的 done 任务
            # （commit=R、锚=E）重入门用留痕 E 判过，才能走到再 rebase，不死锁。
            if not a or a.get("head") != (t.get("rebased_from") or t.get("commit")) \
                    or not a.get("clean"):
                self._audit(tid, "review_absent_no_merge")
                self._post(tid, "verify 绿但合并时 .review-loop 锚缺失/落后/未绿——"
                                "任务驻留 done 待人工审阅合并（dispatcher 可 "
                                "cancel 或人工 git 合并）")
                return
            # 集成阶段全部落在 detached worktree：试合并/测试失败都不会改目标主检出。
            self._gitx.build_integration(target, integration, branch)
            before = self._gitx.head(integration)
            r = None  # R＝F32 重放后任务提交（None＝ff 试合并直通）
            if self._gitx.merge_ff(env, integration) != "ok":
                # F32 真非 ff：门内 detached rebase 干净自愈（env 分支与锚不动），
                # 同步冲突才弹回（不计连败）；环境病如实上抛走门内 except。
                if self._gitx.rebase_onto(env, integration) == "conflict":
                    self._non_ff(tid, branch, before, conflict=True)
                    return
                # E＝锚对齐的 env 提交：崩溃重排时留痕值优先于落库残值 R
                e = t.get("rebased_from") or t.get("commit")
                r = self._gitx.head(integration)
                self._state.set_fields(tid, commit=r, rebased_from=e)  # 先落库再推进（崩溃幂等）
                self._audit(tid, "rebased", **{"from": e, "to": r})
            irc, iout = self._runner(integration, cmd)
            if irc != 0:
                where = "rebase 后集成 verify 失败" if r else "集成 verify 失败"
                self._quality_fail(tid, "reopened",
                                   f"{where}，目标分支未改动——修复后重新"
                                   f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}")
                return
            # 集成验证可能耗时较长，最终推进前复查，关闭“开始干净、结束时变脏”窗口。
            self._gitx.ensure_clean(target)
            if r is not None:  # 重放推进：sha 级 ff（env 分支仍在 E，不可再作合并源）
                if self._gitx.ff_sha(target, r, branch) != "ok":
                    self._non_ff(tid, branch, self._gitx.head(target, branch))
                    return
            elif self._gitx.merge_ff(env, target, branch) != "ok":
                self._non_ff(tid, branch, self._gitx.head(target, branch))
                return
            self._succeed(tid)
        except Exception as e:  # 门内崩（checkout 拒/分支失/脏工作区）：转 reopened 通知
            if self._state.get(tid)["state"] == "done":
                self._state.transition(tid, "reopened")
                self._audit(tid, "reopened")
                self._post(tid, f"门内异常：{e}——处理目标分支后重新 post(done, commit)")
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

    def _succeed(self, tid):
        """唯一成功出口：目标支已含任务 commit，补终态、审计与任务环境回收。"""
        t = self._state.get(tid)
        self._state.fail_streak_reset(tid)  # 全绿清连败（R12）
        self._state.transition(tid, "merged", commit=t.get("commit"))
        # F32：rebased_from 从 state 读（裁定②）——崩溃补终态路径才有值可取
        extra = {"rebased_from": t["rebased_from"]} if t.get("rebased_from") else {}
        self._audit(tid, "merged", commit=t.get("commit"), **extra)
        self._gitx.remove_env(t["env_dir"])
        self._post(tid, f"merged @{t.get('commit')}——环境已回收")

    def _non_ff(self, tid, branch, before, conflict=False):
        """F32 同步冲突或最终推进窄竞态遇真 non-ff：重开不计质量连败，目标主检出保持不动。"""
        t = self._state.get(tid)
        # 窄竞态守卫：试合并/推进窗口内 commit 已被人工推进目标支，则补终态成功，绝不重开
        if self._gitx.contains(t["project_dir"], t.get("commit"), branch):
            self._succeed(tid)
            return
        self._state.transition(tid, "reopened")
        self._audit(tid, "reopened")
        why, act = (("同步冲突", "解冲突后") if conflict else ("non-ff 拒并", "同步后"))
        self._post(tid, f"{why}："
                        f"{branch or self._gitx.symref(t['project_dir'])}@{before}"
                        f" vs branch@{self._gitx.head(t['env_dir'])}——{act}"
                        "重新 post(done, commit)")

    def gate_files_touched(self, tid):
        t = self._state.get(tid)
        return bool(t.get("env_dir")) and self._gitx.touches_gate(
            t["env_dir"], _GATE_PATTERNS, t.get("target_branch"))

    # ---- 内部 ----

    def _quality_fail(self, tid, to, body):
        """质量败（verify 败/集成败）：迁移＋审计＋实时回 worker＋计连败（3 连败拍人）。"""
        self._state.transition(tid, to)
        self._audit(tid, to)
        self._post(tid, body)
        self._state.fail_streak_bump(tid)
        if self._state.get(tid)["state"] == "blocked":
            self._audit(tid, "blocked")
            self._health.escalate(KIND_QUALITY, tid)
            self._router.notify_dispatcher(
                tid, "result", f"任务 {tid} 已 blocked；请重派或取消")
            if self._on_blocked is not None:
                self._on_blocked(tid)

    def _tail(self, tid):
        """门走完：pending-cancel 经 State.gate_tail 原子结算（merged 不逆转历史）；副作用随终态。"""
        t = self._state.gate_tail(tid)
        if t["state"] == "cancelled":
            self._audit(tid, "cancelled")
            if t.get("env_dir"):  # 同 T7 直路径口径：cancelled 即环境回收
                try:
                    self._gitx.remove_env(t["env_dir"])
                except Exception as e:  # 回收败留痕后继续：终止通知＋凭据作废不得被拦断
                    self._audit(tid, "reclaim_failed", note=str(e)[:200])
            if t.get("worker_sid"):  # 同 T7 直路径：终止通知＋凭据作废
                self._router.post(t["dispatcher_sid"], ("down", tid), "cancel",
                                  f"task {tid} cancelled at gate tail")
                self._auth.revoke_session(t["worker_sid"])
            self._router.notify_dispatcher(tid, "result", f"任务 {tid} 已取消")
        elif t["state"] == "closed":
            self._audit(tid, "closed")
            self._router.notify_dispatcher(
                tid, "result",
                f"任务 {tid} 已 closed；commit={t.get('commit') or '-'}")

    def _audit(self, tid, event, **extra):
        """F8 门段审计：迁移即 events.jsonl 一行（事件名＝迁移后状态名）。"""
        self._store.append_event({"ts": time.time(), "actor": "gate",
                                  "event": event, "task": tid, **extra})

    def _post(self, tid, body):
        t = self._state.get(tid)
        self._router.post(t["dispatcher_sid"], ("down", tid), "milestone", body)
