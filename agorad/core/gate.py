"""verify 门与自动合并：任务环境验收→集成环境试合并/验收→目标支最终推进。

done 受理仍执行状态、verify、commit 格式与评审锚前置校验。门体先在任务 env 跑
verify，再在目标分支当前 HEAD 的 detached integration worktree 内 ff-only 试合并
并重跑 verify；只有全绿且目标主检出两次干净检查通过，才推进真实目标分支。集成
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


def _subprocess_runner(env_dir, cmd, timeout=300):
    """真 runner：独立进程组跑 verify；超时杀全组，rc=124 按质量败处理。"""
    proc = subprocess.Popen(cmd, shell=True, cwd=env_dir,
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
                 store, runner=None, verify_timeout=300):
        self._state, self._gitx, self._router, self._oc = state, gitx, router, oc
        self._read_anchor, self._health, self._auth = read_anchor, health, auth
        self._store = store
        self._runner = runner or (lambda env, cmd: _subprocess_runner(
            env, cmd, timeout=verify_timeout))

    # ---- done 受理（前置五拒，拒收落审计并附动作指引） ----

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
        self._state.transition(tid, "done", commit=commit)
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
            if not a or a.get("head") != t.get("commit") or not a.get("clean"):
                self._audit(tid, "review_absent_no_merge")
                self._post(tid, "verify 绿但合并时 .review-loop 锚缺失/落后/未绿——"
                                "任务驻留 done 待人工审阅合并（dispatcher 可 "
                                "cancel 或人工 git 合并）")
                return
            # 集成阶段全部落在 detached worktree：试合并/测试失败都不会改目标主检出。
            self._gitx.build_integration(target, integration, branch)
            before = self._gitx.head(integration)
            if self._gitx.merge_ff(env, integration) != "ok":
                self._non_ff(tid, branch, before)
                return
            irc, iout = self._runner(integration, cmd)
            if irc != 0:
                self._quality_fail(tid, "reopened",
                                   f"集成 verify 失败，目标分支未改动——修复后重新"
                                   f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}")
                return
            # 集成验证可能耗时较长，最终推进前复查，关闭“开始干净、结束时变脏”窗口。
            self._gitx.ensure_clean(target)
            if self._gitx.merge_ff(env, target, branch) != "ok":
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
        self._audit(tid, "merged", commit=t.get("commit"))
        self._gitx.remove_env(t["env_dir"])
        self._post(tid, f"merged @{t.get('commit')}——环境已回收")

    def _non_ff(self, tid, branch, before):
        """试合并或最终推进遇真 non-ff：重开不计质量连败，目标主检出保持不动。"""
        t = self._state.get(tid)
        self._state.transition(tid, "reopened")
        self._audit(tid, "reopened")
        self._post(tid, f"non-ff 拒并："
                        f"{branch or self._gitx.symref(t['project_dir'])}@{before}"
                        f" vs branch@{self._gitx.head(t['env_dir'])}——同步后"
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
        elif t["state"] == "closed":
            self._audit(tid, "closed")

    def _audit(self, tid, event, **extra):
        """F8 门段审计：迁移即 events.jsonl 一行（事件名＝迁移后状态名）。"""
        self._store.append_event({"ts": time.time(), "actor": "gate",
                                  "event": event, "task": tid, **extra})

    def _post(self, tid, body):
        t = self._state.get(tid)
        self._router.post(t["dispatcher_sid"], ("down", tid), "milestone", body)
