"""agorad 常驻入口（R2/R10）：唯一贯穿三层的装配文件。

flock 单实例→装配 App（root 牌于此授予）→recover.startup 复查重注入→绑端口→
service.json（$AGORA_HOME/.agora/ 下，pid/port/started/version/oc_version）→
health 后台定时线程（默认 300s，AGORA_HEALTH_INTERVAL 可配）→serve_forever。
"""
import hashlib
import math
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # 脚本直启（lift/T4 拉起形态）：补包检索路径
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agorad.access import anchors
from agorad.access.gitx import Gitx
from agorad.access.oc import Oc, discover
from agorad.access.pages import write_stalled_draft, write_task_page
from agorad.access.store import Store
from agorad.core import report
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.gate import Gate
from agorad.core.health import Health, send_email
from agorad.core.msg import Router
from agorad.core.peer import PeerBoard
from agorad.core.recover import Recover
from agorad.core.state import State
from agorad.core.task import TaskService
from agorad.core.verify_hygiene import find_verify_hygiene_issues
from agorad.transport.httpd import make_server

_VERSION = "0.1.0"
_MAIN_SID = "main"
_SERVICE_JSON = ".agora/service.json"  # 设计 §4.8：lift/T4 消费面固定路径


def pointer_home():
    """~/.agora-home 指针回退（gt-32c051：MCP 壳/install 写入的统一发现桥）；缺失/空白＝无。"""
    try:
        return ((Path.home() / ".agora-home").read_text(encoding="utf-8")
                .split("\n", 1)[0].strip() or None)  # 首行：多行指针不内嵌换行进 home
    except (OSError, ValueError):  # ValueError＝坏字节（UnicodeDecodeError 非 OSError 族）
        return None


def resolve_home():
    """AGORA_HOME 优先级 env > ~/.agora-home 指针 > 缺省 ~/plaza（设计 §3）。"""
    return os.environ.get("AGORA_HOME") or pointer_home() or str(Path.home() / "plaza")


def load_env_file(home=None):
    """$AGORA_HOME/.agora/agorad.env 旋钮缺省层：KEY=VALUE 逐行、# 注释、缺文件静默；
    已有 os.environ 值优先（env > env 文件 > 代码默认）。事故根因（ag-fc67bc）：
    daemon 由 supervisor 代管重生，环境干净，手工 export 随重启丢失——此层让旋钮
    （AGORA_VERIFY_TIMEOUT/AGORA_VERIFY_CEILING 等读取点）免疫任意重启。"""
    if home is None:
        home = resolve_home()
    try:
        lines = (Path(home) / ".agora" / "agorad.env").read_text(
            encoding="utf-8").splitlines()
    except (OSError, ValueError):  # 缺文件静默；ValueError＝坏字节（非 OSError 族）
        return {}
    loaded = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue  # 空行/注释/坏行容错跳过
        key, _, value = line.partition("=")
        key = key.strip()
        if not (key.isidentifier() and key.isascii()) or key == "AGORA_HOME":
            # 坏键容错跳过（含 export KEY=… 前缀误写：键含空格不成名）；AGORA_HOME
            # 保留键：env 文件不可重定向自身所在 home（防指针被经其发现的值反超）
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]  # 一对成对引号脱壳（手写防呆：值不需引号）
        if key in loaded:
            continue  # 重复键：首见生效，与 os.environ.setdefault 口径一致
        os.environ.setdefault(key, value)
        loaded[key] = value
    return loaded


def env_config():
    """env 解析：home 经 resolve_home（env > 指针 > 缺省）；
    OC 服务＝AGORA_OC_URL 显式优先，
    缺省读 $XDG_STATE_HOME/opencode/service.json（T5 实证发现源，password 随取）。"""
    home = resolve_home()
    url = os.environ.get("AGORA_OC_URL")
    password = os.environ.get("AGORA_OC_PASSWORD")
    if not url:
        url, found = discover()
        if password is None:
            password = found
    return home, url, password


def verify_timeout_from_env():
    """单次 env/集成 verify 上限（秒）；坏值/非有限值/非正数 fail-fast。"""
    raw = os.environ.get("AGORA_VERIFY_TIMEOUT", "300")
    try:
        timeout = float(raw)
    except ValueError:
        raise ValueError(f"AGORA_VERIFY_TIMEOUT 须为正数秒（收到 {raw!r}）") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"AGORA_VERIFY_TIMEOUT 须为正数秒（收到 {raw!r}）")
    return timeout


def verify_ceiling_from_env():
    """F36 verify 硬顶（秒）；未设＝None（Gate 内缺省 4×软顶）；坏值 fail-fast 同款。"""
    raw = os.environ.get("AGORA_VERIFY_CEILING")
    if raw is None:
        return None
    try:
        ceiling = float(raw)
    except ValueError:
        raise ValueError(f"AGORA_VERIFY_CEILING 须为正数秒（收到 {raw!r}）") from None
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError(f"AGORA_VERIFY_CEILING 须为正数秒（收到 {raw!r}）")
    return ceiling


def quick_limits_from_env():
    """F65 quick 包络旋钮（文件数/行数，正整数）；坏值 fail-fast 同款文案风格。"""
    limits = []
    for name, default in (("AGORA_QUICK_MAX_FILES", 3),
                          ("AGORA_QUICK_MAX_LINES", 30)):
        raw = os.environ.get(name, str(default))
        try:
            v = int(raw)
        except ValueError:
            raise ValueError(f"{name} 须为正整数（收到 {raw!r}）") from None
        if v <= 0:
            raise ValueError(f"{name} 须为正整数（收到 {raw!r}）")
        limits.append(v)
    return tuple(limits)


def instance_identity(home):
    """F96 稳定实例身份：hostname + AGORA_HOME realpath 的 sha256 前 8 位——
    同机同 home 跨重启恒等、异机/异 home 互异；入 Gitx 属主化 env 命名。"""
    digest = hashlib.sha256(os.path.realpath(str(home)).encode("utf-8")
                            ).hexdigest()[:8]
    return f"{socket.gethostname()}-{digest}"


def assemble(home, oc_url=None, oc_password=None, *, oc=None, gitx=None, read_anchor=None,
             runner=None, mail_runner=None, main_sid=_MAIN_SID, store=None,
             verify_timeout=300, verify_ceiling=None, roster_path=None,
             quick_max_files=3, quick_max_lines=30, clean_ignore_env=""):
    """App 唯一装配点：真 oc/gitx 缺省；假件注入即整栈可测（R2）。"""
    store = store if store is not None else Store(Path(home))
    state = State(store)
    auth = Auth(store)
    oc = oc if oc is not None else Oc(oc_url, oc_password)
    gitx = gitx if gitx is not None else Gitx(instance_id=instance_identity(home))
    router = Router(store, state, oc,
                    same_lineage=auth.same_lineage,  # F16 血统链边（装配注入）
                    mail=lambda tid, text: send_email(  # F63 终态邮件必达（外呼缝同 Health）
                        store, tid, text, runner=mail_runner))
    page_writer = lambda home, tid, row: write_task_page(  # F44：快照与事件装配注入
        home, tid, row, tasks=state.snapshot()["tasks"],
        events=store.read_events(tid))  # ag-0f60fd：TaskService/Gate/httpd 三面共用
    task = TaskService(
        state, auth, gitx, oc, store, router, page_writer,
        stale_candidates=report.stale_candidates,  # ag-d1d8c1：僵态同口径
        verify_hygiene=find_verify_hygiene_issues)  # F99：登记面卫生闸
    peer = PeerBoard(store, state)  # F22 同仓协作面（写面权限归 httpd 角色判定）
    recover = Recover(state, oc, gitx, store, auth, router=router)
    app = App(store=store, state=state, auth=auth, router=router, task=task,
              peer=peer, gate=None, health=None, recover=recover, report=report,
              oc=oc, gitx=gitx, main_sid=main_sid, page_writer=page_writer)
    health = Health(state, oc, router, recover, app, store=store,  # R25-A：活绑定
                    stale_candidates=report.stale_candidates,  # 僵态同口径（F5）
                    env_activity=report._env_activity,  # F59 env 活性同口径（禁兄弟 import）
                    draft_writer=write_stalled_draft,  # F59 草稿页写面
                    runner=mail_runner)  # 邮件外呼缝独立于 Gate runner：两契约不同
    gate = Gate(state, gitx, router, oc,
                read_anchor or anchors.read_anchor, health, auth=auth,
                store=store, runner=runner, verify_timeout=verify_timeout,
                verify_ceiling=verify_ceiling,
                on_blocked=task.stop_after_failed,
                roster_path=roster_path or str(Path(home) / ".agora" / "ignore.roster"),
                quick_max_files=quick_max_files,  # F65 quick 包络旋钮透传
                quick_max_lines=quick_max_lines,
                clean_ignore_env=clean_ignore_env,  # ag-f5aa4d 数据层豁免 env 面
                page_writer=page_writer)  # ag-0f60fd：门段事件刷新任务页
    app.gate, app.health = gate, health
    return app


def issue_root(app):
    """无 root 牌则授予 daemon 常量身份 sid="root"（ag-588eb8：占位/客户端 sid
    不进 root 签发——活绑定后牌落会话 sid 属 R25-A 归属设计，污染由 cred_issue
    换牌面自愈）；已有 root 返回 None（rotate_root 语义保留）。"""
    tokens = app.store.read_ephemeral("tokens") or {}
    if any(v[1] == "root" for v in tokens.values()):
        return None
    return app.auth.issue("root", "root")


def migrate_root_poison(app):
    """启动迁移（ag-f80309 B 案）：存量「会话 sid＋root 角色」污染条目一次性
    归位 daemon 身份 sid="root"（auth.pin_roots：旧 sid 入血统留痕，幂等——
    账本干净即零写盘）；worker 在飞牌不动。迁移>0 才落审计一行（actor=agorad）。
    B 案起 bind_main 不再制造污染（每调用 rebind 摘除），本面只清一次存量；
    残留审计（audit_root_poison）排在其后作零残留看门。"""
    n = app.auth.pin_roots()
    if n:
        app.store.append_event({"ts": time.time(), "actor": "agorad",
                                "event": "root_poison_migrated",
                                "note": f"migrated={n}"})
    return n


def audit_root_poison(app):
    """启动维护（ag-588eb8 裁决 A）：扫存量「会话 sid＋root 角色」污染条目，
    仅审计计数不撤销——root 牌常为唯一治理牌，撤销即断 CLI/enforce/doctor；
    账本持久化重启不丢（write_ephemeral），受污染窗口自愈归 cred_issue 换牌面，
    此处只留账（events 一行，零污染零新行）。"""
    tokens = app.store.read_ephemeral("tokens") or {}
    n = sum(1 for v in tokens.values() if v[1] == "root" and v[0] != "root")
    if n:
        app.store.append_event({"ts": time.time(), "actor": "agorad",
                                "event": "root_poison_audit", "note": f"poisoned={n}"})
    return n


def health_loop(app, interval):
    """R10 线程体：按间隔 tick（只探死）＋probe（oc_version 记入 service.json）。"""
    while True:
        time.sleep(interval)
        try:
            app.health.tick()
        except Exception:  # 单轮失败不杀线程：下轮再探（升级链异常同口径）
            pass
        try:
            probe = app.oc.probe()
        except Exception:  # oc 不可达：保留旧 oc_version
            continue
        sj = app.store.read_ephemeral(_SERVICE_JSON) or {}
        sj["oc_version"] = probe.get("oc_version")
        app.store.write_ephemeral(_SERVICE_JSON, sj)


def start_health_thread(app, interval):
    th = threading.Thread(target=health_loop, args=(app, interval), daemon=True,
                          name="health")
    th.start()
    return th


def _log_signal_exit(signum, _frame):
    """信号死因留痕（gt-e38e41）：默认处置无声死，先在 stderr 记因再退。"""
    print(f"agorad exit signal={signal.Signals(signum).name}", file=sys.stderr,
          flush=True)
    raise SystemExit(128 + signum)


def main():
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, _log_signal_exit)
    load_env_file()  # agorad.env 旋钮缺省层先行（env > 文件 > 代码默认，ag-fc67bc）
    home, oc_url, oc_password = env_config()
    if not oc_url:
        print("需要 AGORA_OC_URL（或 $XDG_STATE_HOME/opencode/service.json 服务发现）",
              file=sys.stderr)
        return 2
    try:
        verify_timeout = verify_timeout_from_env()
        verify_ceiling = verify_ceiling_from_env()
        quick_files, quick_lines = quick_limits_from_env()  # F65
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = Store(Path(home))
    if not store.flock_singleton():
        print("agorad 已在运行", file=sys.stderr)
        return 1
    app = assemble(home, oc_url=oc_url, oc_password=oc_password, store=store,
                   verify_timeout=verify_timeout, verify_ceiling=verify_ceiling,
                   quick_max_files=quick_files, quick_max_lines=quick_lines,
                   clean_ignore_env=os.environ.get("AGORA_CLEAN_IGNORE", ""))
    tok = issue_root(app)
    if tok:
        print(f"AGORA_ROOT_TOKEN: {tok}", flush=True)  # R20 固定前缀（T4 捕获解析）
    migrate_root_poison(app)  # ag-f80309：存量污染一次性归位 daemon 身份（幂等）
    audit_root_poison(app)  # ag-588eb8：迁移后残留看门（只记账不撤销）
    app.recover.startup()
    try:
        app.gate.recover_managed()  # Req: F100 managed 交付崩溃收敛（恢复面）
    except Exception:  # 恢复面尽力而为不挡启动：门重排链路（done 残留）兜底
        pass
    try:
        app.health.repair_dispatcher_routes()  # F75：启动即修（不等首个 tick）
    except Exception:  # 修复面 I/O 败不挡服务：health 线程下轮重试（同 probe 口径）
        pass
    try:
        oc_version = app.oc.probe().get("oc_version")
    except Exception:  # oc 未起不挡服务：health 线程补探
        oc_version = None
    server = make_server(app, int(os.environ.get("AGORA_PORT", "0")))
    store.write_ephemeral(_SERVICE_JSON, {
        "pid": os.getpid(), "port": server.server_address[1],
        "started": time.time(), "version": _VERSION, "oc_version": oc_version})
    start_health_thread(app, float(os.environ.get("AGORA_HEALTH_INTERVAL",
                                                  "300")))
    print(f"agorad ready port={server.server_address[1]}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
