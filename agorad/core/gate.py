"""verify 门与自动合并（F7，设计 §4.5）：done 前置受理→隔离 verify→ff 合并→集成 verify。

受理五拒（R3/R9/F13，均经 _deny_done 落 done_rejected 审计并附动作指引）：state
非 running／verify 未登记／verify 命令空／body-format done body 非纯 40 位 hex 完整
sha（先行，防参数错伪装成锚问题）／anchor——锚拒收仅评审环在位时适用（env 有
.review-loop/ 才查锚；锚由服务经注入的 read_anchor 自取，无 anchor 入参），拆无锚·
≠分支 HEAD·未绿三支各配指引；评审环缺失→放行 done，verify 绿驻留 done 人工审合并
（门体既有分支接管）。门体（run 后台线程体）：
env 跑 verify→败 verify_failed＋输出尾部（截 2000 字符）实时回 worker＋计连败；
绿→合并前锚重验：锚缺失/落后/未绿驻留 done（review_absent_no_merge 审计＋
回 worker——合并充分条件＝.review-loop/ 锚绿于报告 commit，堵 accept→run
窗口 TOCTOU 与 done 后偷提交）→ff-only 合并→non-ff 拒并 reopened（fail_streak 不动）→集成 verify 败→原子回滚
（合并前 sha）＋reopened＋计连败；全绿→fail_streak_reset＋merged（commit 证据落行）
＋环境回收＋尾部置 closed。每处迁移落 events.jsonl 一行（F8 门段审计，事件名＝
迁移后状态名，merged 带 commit）。全程 in-gate 门闩（accept 即置位）＋
pending-cancel 门走完生效（结算经 State.gate_tail 原子完成，merged 不逆转历史）。
3 连败自动 blocked 与闸体变更经 health.escalate 拍人。core 只 import
stdlib，依赖全构造注入（runner 缺省为 subprocess 真跑）。
"""
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


def _subprocess_runner(env_dir, cmd):
    """真 runner：env_dir 内 shell 跑 verify.cmd，返回 (rc, 合并输出)。"""
    r = subprocess.run(cmd, shell=True, cwd=env_dir, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class Gate:
    """accept_done 单写者串行受理；run 为后台线程体（httpd 包 Thread 调用）。"""

    def __init__(self, state, gitx, router, oc, read_anchor, health, auth,
                 store, runner=None):
        self._state, self._gitx, self._router, self._oc = state, gitx, router, oc
        self._read_anchor, self._health, self._auth = read_anchor, health, auth
        self._store = store
        self._runner = runner or _subprocess_runner

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
        self._state.set_fields(tid, in_gate=True)
        origin = None
        try:
            if branch:
                origin = self._gitx.symref(target)  # 合入前分支位；门毕还原（try 内防静默卡死）
            if self.gate_files_touched(tid):  # 闸体变更：拍人不拦门
                self._health.escalate(KIND_GATEFILES, tid)
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
            before = self._gitx.head(target, branch)  # 回滚点＝合并前目标分支 HEAD
            if self._gitx.merge_ff(env, target, branch) != "ok":
                self._state.transition(tid, "reopened")  # 拒并不计连败（R12）
                self._audit(tid, "reopened")
                self._post(tid, "non-ff 拒并：目标分支已推进——同步主干后重新"
                                " post(done, commit)")
                return
            irc, iout = self._runner(target, cmd)  # 集成 verify 在目标仓
            if irc != 0:
                self._gitx.rollback(target, before)  # 原子回滚（并发推进守卫基点）
                self._quality_fail(tid, "reopened",
                                   f"集成 verify 失败，目标分支已回滚——修复后重新"
                                   f" post(done, commit)\n输出尾部:\n{iout[-_TAIL:]}")
                return
            self._state.fail_streak_reset(tid)  # 全绿清连败（R12）
            self._state.transition(tid, "merged", commit=t.get("commit"))
            self._audit(tid, "merged", commit=t.get("commit"))
            self._gitx.remove_env(env)
            self._post(tid, f"merged @{t.get('commit')}——环境已回收")
        except Exception as e:  # 门内崩（checkout 拒/分支失）：转 reopened 通知，防静默卡死
            if self._state.get(tid)["state"] == "done":
                self._state.transition(tid, "reopened")
                self._audit(tid, "reopened")
                self._post(tid, f"门内异常：{e}——处理目标分支后重新 post(done, commit)")
            # 终态后异常（如环境回收败）不逆转门结果
        finally:
            if origin and not self._gitx.checkout(target, origin):
                self._audit(tid, "restore_failed")  # 还原败留痕：主仓停外部支，缺省任务将漂移
            self._tail(tid)

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
                self._gitx.remove_env(t["env_dir"])
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
