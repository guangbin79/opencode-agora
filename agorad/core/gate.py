"""verify 门与自动合并（F7，设计 §4.5）：done 前置受理→隔离 verify→ff 合并→集成 verify。

受理三拒（R3/R9）：任务非 running／verify 未登记／评审锚缺失·落后·未绿——锚由服务经
注入的 read_anchor 自取（无 anchor 入参），拒收附动作指引。门体（run 后台线程体）：
env 跑 verify→败 verify_failed＋输出尾部（截 2000 字符）实时回 worker＋计连败；
绿→ff-only 合并→non-ff 拒并 reopened（fail_streak 不动）→集成 verify 败→原子回滚
（合并前 sha）＋reopened＋计连败；全绿→fail_streak_reset＋merged（commit 证据落行）
＋环境回收＋尾部置 closed。全程 in-gate 门闩＋pending-cancel 门走完生效（merged 不
逆转历史）。3 连败自动 blocked 与闸体变更经 health.escalate 拍人。core 只 import
stdlib，依赖全构造注入（runner 缺省为 subprocess 真跑）。
"""
import subprocess

from agorad.core.health import KIND_GATEFILES, KIND_QUALITY

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

    def __init__(self, state, gitx, router, oc, read_anchor, health, runner=None):
        self._state, self._gitx, self._router, self._oc = state, gitx, router, oc
        self._read_anchor, self._health = read_anchor, health
        self._runner = runner or _subprocess_runner

    # ---- done 受理（前置三拒，拒收附动作指引） ----

    def accept_done(self, tid, worker_sid, commit):
        t = self._state.get(tid)
        if t["state"] != "running":  # 含二次 done：防重入门
            return (f"rejected:state: 任务 {tid} 当前 {t['state']} 非 running——"
                    "board 查看状态，仅 running 受理 done")
        if t["verify"]["state"] != "registered":
            return (f"rejected:verify-unregistered: 任务 {tid} verify 未登记——"
                    "post 通知派发方 task_update 登记 verify 命令，"
                    "登记后重新 post(done, commit)")
        if not t["verify"]["cmd"].strip():  # 兜底：空命令＝无质量闸，拒入门
            return (f"rejected:verify-empty: 任务 {tid} verify 命令为空——"
                    "task_update 登记非空命令后重新 post(done, commit)")
        a = self._read_anchor(t["env_dir"])  # 锚＝worktree 分支 HEAD 且无 blocker/high
        if not a or a.get("head") != commit or not a.get("clean"):
            return (f"rejected:anchor: 任务 {tid} 评审环未绿或锚落后（.review-loop 锚"
                    "须＝分支 HEAD 且无 blocker/high）——跑 /review-loop 或待 idle "
                    "自动触发，绿后带锚重新 post(done, commit)")
        self._state.transition(tid, "done", commit=commit)
        return "accepted"

    # ---- 门体（后台线程体） ----

    def run(self, tid):
        t = self._state.get(tid)
        env, target, cmd = t["env_dir"], t["project_dir"], t["verify"]["cmd"]
        self._state.set_fields(tid, in_gate=True)
        try:
            if self.gate_files_touched(tid):  # 闸体变更：拍人不拦门
                self._health.escalate(KIND_GATEFILES, tid)
            rc, out = self._runner(env, cmd)
            if rc != 0:
                self._quality_fail(tid, "verify_failed",
                                   f"verify 失败——修复后重新 post(done, commit)"
                                   f"\n输出尾部:\n{out[-_TAIL:]}")
                return
            before = self._gitx.head(target)  # 回滚点＝合并前目标分支 HEAD
            if self._gitx.merge_ff(env, target) != "ok":
                self._state.transition(tid, "reopened")  # 拒并不计连败（R12）
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
            self._gitx.remove_env(env)
            self._post(tid, f"merged @{t.get('commit')}——环境已回收")
        finally:
            self._tail(tid)

    def gate_files_touched(self, tid):
        t = self._state.get(tid)
        return bool(t.get("env_dir")) and self._gitx.touches_gate(t["env_dir"],
                                                                  _GATE_PATTERNS)

    # ---- 内部 ----

    def _quality_fail(self, tid, to, body):
        """质量败（verify 败/集成败）：迁移＋实时回 worker＋计连败（3 连败拍人）。"""
        self._state.transition(tid, to)
        self._post(tid, body)
        self._state.fail_streak_bump(tid)
        if self._state.get(tid)["state"] == "blocked":
            self._health.escalate(KIND_QUALITY, tid)

    def _tail(self, tid):
        """门走完：pending-cancel 生效（merged 不逆转历史）；merged 置 closed；清门闩。"""
        t = self._state.get(tid)
        fields = {"in_gate": False}
        if t.get("pending_cancel"):
            fields["pending_cancel"] = False
        if t.get("pending_cancel") and t["state"] != "merged":
            self._state.transition(tid, "cancelled", **fields)
            if t.get("env_dir"):  # 同 T7 直路径口径：cancelled 即环境回收
                self._gitx.remove_env(t["env_dir"])
        elif t["state"] == "merged":
            self._state.transition(tid, "closed", **fields)
        else:
            self._state.set_fields(tid, **fields)

    def _post(self, tid, body):
        t = self._state.get(tid)
        self._router.post(t["dispatcher_sid"], ("down", tid), "milestone", body)
