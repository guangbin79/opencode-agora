"""agorad 常驻入口（R2/R10）：唯一贯穿三层的装配文件。

flock 单实例→装配 App（root 牌于此授予）→recover.startup 复查重注入→绑端口→
service.json（$AGORA_HOME/.agora/ 下，pid/port/started/version/oc_version）→
health 后台定时线程（默认 300s，AGORA_HEALTH_INTERVAL 可配）→serve_forever。
"""
import os
import signal
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # 脚本直启（lift/T4 拉起形态）：补包检索路径
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agorad.access import anchors
from agorad.access.gitx import Gitx
from agorad.access.oc import Oc, discover
from agorad.access.pages import write_task_page
from agorad.access.store import Store
from agorad.core import report
from agorad.core.app import App
from agorad.core.auth import Auth
from agorad.core.gate import Gate
from agorad.core.health import Health
from agorad.core.msg import Router
from agorad.core.peer import PeerBoard
from agorad.core.recover import Recover
from agorad.core.state import State
from agorad.core.task import TaskService
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


def env_config():
    """env 解析：AGORA_HOME 优先级 env > ~/.agora-home 指针 > 缺省 ~/plaza（设计 §3）；
    OC 服务＝AGORA_OC_URL 显式优先，
    缺省读 $XDG_STATE_HOME/opencode/service.json（T5 实证发现源，password 随取）。"""
    home = os.environ.get("AGORA_HOME") or pointer_home() or str(Path.home() / "plaza")
    url = os.environ.get("AGORA_OC_URL")
    password = os.environ.get("AGORA_OC_PASSWORD")
    if not url:
        url, found = discover()
        if password is None:
            password = found
    return home, url, password


def assemble(home, oc_url=None, oc_password=None, *, oc=None, gitx=None, read_anchor=None,
             runner=None, mail_runner=None, main_sid=_MAIN_SID, store=None):
    """App 唯一装配点：真 oc/gitx 缺省；假件注入即整栈可测（R2）。"""
    store = store if store is not None else Store(Path(home))
    state = State(store)
    auth = Auth(store)
    oc = oc if oc is not None else Oc(oc_url, oc_password)
    gitx = gitx if gitx is not None else Gitx()
    router = Router(store, state, oc,
                    same_lineage=auth.same_lineage)  # F16 血统链边（装配注入）
    task = TaskService(state, auth, gitx, oc, store, router, write_task_page)
    peer = PeerBoard(store, state)  # F22 同仓协作面（写面权限归 httpd 角色判定）
    recover = Recover(state, oc, gitx, store, auth, router=router)
    app = App(store=store, state=state, auth=auth, router=router, task=task,
              peer=peer, gate=None, health=None, recover=recover, report=report,
              oc=oc, gitx=gitx, main_sid=main_sid)
    health = Health(state, oc, router, recover, app, store=store,  # R25-A：活绑定
                    stale_candidates=report.stale_candidates,  # 僵态同口径（F5）
                    runner=mail_runner,  # 邮件外呼缝独立于 Gate runner：两契约不同
                    cred_interval=float(os.environ.get(  # F25 凭据重注入周期（秒）
                        "AGORA_CRED_REINJECT_INTERVAL", "600")))
    gate = Gate(state, gitx, router, oc,
                read_anchor or anchors.read_anchor, health, auth=auth,
                store=store, runner=runner)
    app.gate, app.health = gate, health
    return app


def issue_root(app):
    """无 root 牌则授予（sid=main）；已有 root 返回 None（rotate_root 语义保留）。"""
    tokens = app.store.read_ephemeral("tokens") or {}
    if any(v[1] == "root" for v in tokens.values()):
        return None
    return app.auth.issue(app.main_sid, "root")


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
    home, oc_url, oc_password = env_config()
    if not oc_url:
        print("需要 AGORA_OC_URL（或 $XDG_STATE_HOME/opencode/service.json 服务发现）",
              file=sys.stderr)
        return 2
    store = Store(Path(home))
    if not store.flock_singleton():
        print("agorad 已在运行", file=sys.stderr)
        return 1
    app = assemble(home, oc_url=oc_url, oc_password=oc_password, store=store)
    tok = issue_root(app)
    if tok:
        print(f"AGORA_ROOT_TOKEN: {tok}", flush=True)  # R20 固定前缀（T4 捕获解析）
    app.recover.startup()
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
