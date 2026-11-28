"""入向 JSON 口（B4）：15 工具端点＋/cred_for_sid（凭据拉取＋每窗口 dispatcher
牌签发面）＋/gate_deny（shim 拒绝审计上报，Req: F176）＋/view＋/health；
单线程串行＋后台门队列。

门队列消费经 gate.run_guarded（P0-1 看门狗，ag-9c4cbb）：门级硬顶到点杀本门
子进程组＋blocked(gate_overrun) 终局，单个挂死门不再堵死后续 done 任务；门体
崩溃留 gate_crashed 审计行不杀消费线程。

transport 只 import agorad.core（R2）——access 件经 App 属性触达（store.home
下 wiki 裸读写，无 import）。鉴权两段（R18）：X-Agora-Token→identify（无牌
401）→may 粗判（越权 403＋审计行）；归属细判与保险丝归 TaskService。done
特殊路径＝gate 六拒同步应答（拒收落审计），受理绿→落库→1-worker 串行队列后台跑门体。
"""
import hashlib
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agorad.core import report
from agorad.core.auth import EV_AUTH_DENIED
from agorad.core.gate import dirty_face_warning  # ag-9a680b 派发前脏仓预警
from agorad.core.health import _ACTIVE, KIND_STALLED
from agorad.core.msg import NotChainEdge, parse_noop  # Req: F174 前缀判定
from agorad.core.peer import Rejected as PeerRejected
from agorad.core.state import EV_CANCELLED, EV_RUNNING, InvalidTransition
from agorad.core.task import Rejected
from agorad.core.verify_hygiene import find_verify_hygiene_issues

_TOOLS = frozenset({"task_create", "task_dispatch", "task_update", "task_cancel",
                    "task_deliver", "task_wait", "post", "check_mail", "board",
                    "doctor", "repo_board", "wiki_read", "wiki_write", "enforce",
                    "reverify"})  # Req: F101 task_deliver；ag-d68faf reverify；Req: F124 task_wait
_POST_KINDS = ("note", "question", "answer", "done", "verify", "verify_reject",
               "peer")
_PULL_STATES = frozenset({"dispatched", "running", "done", "verify_failed",
                          "reopened", "blocked"})  # 拉取面＝在飞非冻结（F25 修正；牌活性二道守卫）
_DENY_HINT = {"worker": "worker 面：post/check_mail/repo_board/wiki_read/wiki_write/"
                        "task_create/task_dispatch/sleep；task_cancel 仅限己建子任务；"
                        "task_update/验收命令写面全局 worker 角色永拒"
                        "（建单缺省继承父任务配置）——需登记 verify→post(kind=verify) 提案"}


# F89 公共字符串面：坏类型在 HTTP 边界 400（缺必填仍走 KeyError→400）
_STRING_FIELDS = {
    "task_create": ("goal", "project_dir", "after", "target_branch",
                    "verify_cmd", "req_id", "model", "title", "deliverable",
                    "client_key"),  # Req: F95
    "task_dispatch": ("task",),
    "task_update": ("task", "verify_cmd"),
    "task_cancel": ("task",),
    "task_deliver": ("task", "outcome", "commit", "base", "reason"),  # Req: F101
    "reverify": ("task",),
    "task_wait": ("task",),  # Req: F124（wait_on 可 null＝清除，不入公共字符串面）
    "post": ("task", "kind", "body"),
    "board": ("task", "view"),
    "repo_board": ("repo", "tag"),  # Req: F173 tag 过滤参数（值域归 board 域）
    "wiki_read": ("path",),
    "wiki_write": ("path", "text", "mode", "expected_hash"),  # Req: F180
    "task_nudge": ("task", "body"),
    "task_alarm": ("task",),
}

# Req: F176 /gate_deny 字段校验面：类型/长度上限/控制符压平（F49 同款口径——
# DEL/C1/Unicode 行界按 Python splitlines 行界显式 \uXXXX 转义，恰一行不可伪造；
# 含孤立代理段 D800–DFFF：JS/Python JSON 往返均物化该类，不压平则 utf-8 落账
# UnicodeEncodeError 丢行＝选择性湮灭审计——C 册 round1 实锤）
_GATE_DENY_CAPS = (("sid", 128), ("tool", 64), ("target", 512), ("reason", 256))
_GATE_DENY_CTRL = str.maketrans({c: "\\u%04x" % c
                                 for c in (*range(0x20), *range(0x7f, 0xa0),
                                           0x2028, 0x2029,
                                           *range(0xd800, 0xe000))})
_GATE_DENY_WINDOW_S = 30.0  # 服务端节流窗（内存态——gt-a18ef6：追加禁 O(调用数) 无界）


class _WikiConflict(Exception):
    """Req: F180 wiki_write CAS 拒写（409 语义）：拒因携带实况哈希。"""


def _string_field(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 须为字符串")
    return value


def _validate_strings(name, body):
    for field in _STRING_FIELDS.get(name, ()):
        if field in body:
            _string_field(body[field], field)


class _Handler(BaseHTTPRequestHandler):
    app = None    # make_server 经子类属性注入
    gate_q = None

    def log_message(self, *a):  # 测试/常驻输出 pristine
        pass

    # ---- HTTP 谓词 ----

    def do_GET(self):
        name = self.path.strip("/").split("?")[0]
        if name not in ("view", "health"):
            return self._json(404, {"error": "not found"})
        ident = self.app.auth.identify(self.headers.get("X-Agora-Token", ""))
        if ident is None:
            return self._json(401, {"error": "无牌或牌无效"})
        sid, role = ident
        sid = self._bind_caller(sid, role)
        tool = "board" if name == "view" else "doctor"  # 查询面同级（may 矩阵）
        if not self.app.auth.may(role, tool, None, None):
            return self._deny(sid, role, tool, None)
        if name == "view":
            self._html(200, report.view_html(self.app.state.snapshot()))
        else:
            self._json(200, {"ok": True,
                             "enforce_on": self.app.auth.enforce_on()})

    def do_POST(self):
        sid = None
        try:
            name = self.path.strip("/").split("?")[0]
            if name not in _TOOLS and name not in ("cred_for_sid", "gate_deny",
                                                   "task_nudge", "task_alarm"):
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 须为 JSON 对象")
            if name == "cred_for_sid":  # 插件拉取面先于鉴权：无牌是常态（缓存未命中才拉）
                return self._cred_for_sid(body)
            if name == "gate_deny":  # Req: F176 shim 拒绝审计上报面（信息面无鉴权）
                return self._gate_deny(body)
            token = self.headers.get("X-Agora-Token", "")
            ident = self.app.auth.identify(token)
            if ident is None:
                return self._json(401, {"error": "无牌或牌无效"})
            sid, role = ident
            sid = self._bind_caller(sid, role)  # 改绑后行取新 sid：开工/归属按牌面一致判定
            _validate_strings(name, body)  # task 非串先于行查找；其余坏类型不达域服务
            tid = body.get("task")
            row = None
            if tid is not None:
                try:
                    row = self.app.state.get(tid)
                except KeyError:
                    return self._json(404, {"error": f"task {tid} 不存在"})
            if name == "task_nudge":  # board 内部面：细授权不走通用 may 矩阵
                return self._json(200, self._task_nudge(sid, role, body, row))
            if name == "task_alarm":  # F77 同款内部面：root-only 手动拍僵
                return self._json(200, self._task_alarm(sid, role, body, row))
            if not self.app.auth.may(role, name, tid, row):
                return self._deny(sid, role, name, tid)
            self.app.health.touch(sid)  # last_seen：每次工具调用即刷新
            if (row and row["state"] in ("dispatched", "verify_failed", "reopened")
                    and row.get("worker_sid") == sid):
                # worker 调用即开工（含 verify_failed/reopened 续修边）：→running＋审计
                self.app.state.transition(tid, EV_RUNNING)
                self.app.store.append_event({"ts": time.time(), "actor": sid,
                                             "event": EV_RUNNING, "task": tid})
                if self.app.page_writer is not None:  # ag-0f60fd：任务页随开工刷新
                    try:  # 页面写面尽力而为（F54 口径）：败不断工具调用
                        self.app.page_writer(self.app.store.home, tid,
                                             self.app.state.get(tid))
                    except Exception:
                        pass
            return self._json(200, self._tool(name, sid, role, token, body))
        except Rejected as e:      # 归属细判/保险丝拒（报因在消息）
            return self._json(403, {"error": str(e)})
        except PeerRejected as e:  # F22 peer 域拒（权限/软限/无作者任务）
            return self._json(403, {"error": str(e)})
        except NotChainEdge as e:
            return self._bad(sid, name, str(e))
        except InvalidTransition as e:
            return self._json(409, {"error": str(e)})
        except _WikiConflict as e:  # Req: F180 CAS 拒写＝409 语义错误码
            return self._json(409, {"error": str(e)})
        except FileNotFoundError as e:
            return self._json(404, {"error": str(e)})
        except KeyError as e:      # 缺必填字段
            return self._bad(sid, name, f"缺字段: {e}")
        except (ValueError, OSError) as e:
            return self._bad(sid, name, str(e))
        except Exception as e:     # 兜底：带因 500，不让连接静默断
            return self._json(500, {"error": str(e)})

    # ---- 工具面 ----

    def _tool(self, name, sid, role, token, body):
        app = self.app
        if name == "task_create":
            vp = body.get("verify_pending", False)
            vc = body.get("verify_cmd")
            rq = body.get("req_id")
            to = body.get("timeout")  # F51：任务级 verify 预算（正整数秒）
            md = body.get("model")  # F55：任务级 worker 模型
            qk = body.get("quick", False)  # F65：quick 微任务旗
            be = body.get("verify_base_exempt")  # Req: F107 base-red 豁免旗（缺 None＝不设）
            dl = body.get("deliverable")  # ag-af96bb②：review-only 交付契约（值白名单归 task 域）
            ck = body.get("client_key")  # Req: F95 幂等建单键（域校验归 task 域）
            if not isinstance(vp, bool):  # "false" 字符串得 True——豁免旗语义反转拒收
                raise ValueError("verify_pending 须为布尔")
            if not isinstance(qk, bool):  # 同豁免旗口径
                raise ValueError("quick 须为布尔")
            if be is not None and not isinstance(be, bool):  # Req: F107 同口径
                raise ValueError("verify_base_exempt 须为布尔")
            if to is not None and (isinstance(to, bool)
                                    or not isinstance(to, int)):  # 同 verify_cmd 口径
                raise ValueError("timeout 须为正整数秒")
            pd = (body.get("project_dir") if role == "worker"  # F37：worker 缺省继承
                  else body["project_dir"])  # 受信角色仍必填（缺即 400）
            return {"task": app.task.create(body["goal"], pd,
                                            body.get("after"), token,
                                            title=body.get("title"),
                                            target_branch=body.get("target_branch"),
                                            verify_cmd=vc, verify_pending=vp,
                                            req_id=rq, timeout=to, model=md,
                                            quick=qk, deliverable=dl,
                                            client_key=ck,  # Req: F95
                                            verify_base_exempt=be,  # Req: F107
                                            actor_sid=sid)}  # ag-f80309：归属记真实调用 sid
        if name == "task_dispatch":
            # 公共响应不带 worker 牌：凭据只经文件指针注入/拉取面（F43/F85）
            # ag-9a680b 派发前只读脏仓预警：spawn 前 porcelain 非空即随响应
            # 附警行——warn-only 绝不拦派发（终局拦截归交付门 local_target_dirty）
            warn = ""
            try:
                records = app.gitx.status_records(
                    app.state.get(body["task"])["project_dir"])
                if records:
                    warn = dirty_face_warning(records)
            except Exception:
                pass  # 探针病零影响派发（warn-only 契约）
            app.task.dispatch(body["task"], token, actor_sid=sid)
            r = {"task": body["task"]}
            if warn:
                r["warning"] = warn
            return r
        if name == "task_update":
            vc = body["verify_cmd"]
            to = body.get("timeout")  # F51：预算同权可改
            if to is not None and (isinstance(to, bool)
                                   or not isinstance(to, int)):  # 同 create 口径
                raise ValueError("timeout 须为正整数秒")
            app.task.update_verify(body["task"], token, vc, timeout=to,
                                   actor_sid=sid)  # ag-f80309：登记人同口径
            return {"ok": True}
        if name == "task_cancel":
            force = body.get("force", False)
            if not isinstance(force, bool):  # "true" 字符串得 True——强杀语义反转拒收
                raise ValueError("force 须为布尔")
            return app.task.cancel(body["task"], token, force=force,
                                   actor_sid=sid)  # F28 忙碌闸应答直透
        if name == "task_deliver":  # Req: F101 外部交付确认（external_branch）
            r = app.task.deliver(body["task"], token, body["outcome"],
                                 body["commit"], base=body.get("base"),
                                 reason=body.get("reason", ""), actor_sid=sid)
            if r.get("requeued"):
                self.gate_q.put(body["task"])  # F110 起 non_ff 不再 requeue（预判 diverged 终局）；reverify 仍走此面
            return r
        if name == "reverify":  # ag-d68faf：done 驻留重排（verify-dead 恢复边）
            r = app.task.reverify(body["task"], token, actor_sid=sid)
            if r.get("requeued"):
                self.gate_q.put(body["task"])  # 与 task_deliver non_ff 同款投门队列
            return r
        if name == "task_wait":  # Req: F124 受控等待登记/清除
            wo = body.get("wait_on")
            if wo is None:
                wo = ""  # null/缺省＝清除等待（人工恢复）
            if not isinstance(wo, str):
                raise ValueError("wait_on 须为字符串或 null")
            return app.task.wait(body["task"], token, wo, actor_sid=sid)
        if name == "post":
            return self._post(sid, role, body)
        if name == "check_mail":
            return {"messages": app.router.check_mail(sid)}
        if name == "board":
            snap = app.state.snapshot()
            if "task" in body:
                return report.detail(snap, app.store, body["task"],
                                     publication=getattr(  # P1b 出版投影（假件无该面＝缺席零变化）
                                         app.gitx, "publication", None))
            if body.get("view") == "full":  # F48 单调用全量（紧凑 {} 形态一字不动）
                return report.full_view(snap, app.store)
            return {"buckets": report.buckets(snap),
                    "chain": report.chain_view(snap, sid),
                    "forest": report.forest_view(snap)}
        if name == "doctor":
            try:  # R2 禁 import access：OcUnavailable/假件缺 models 面不可判型——兜 Exception 同降级
                models = app.oc.models()
            except Exception:
                models = None
            return report.doctor_view(app.state.snapshot(), app.store.home,
                                      models=models,
                                      verify_profiles=app.verify_profiles,  # Req: F126 画像摘要（{}＝count 0 亦注入，缺文件≠没读到）
                                      verify_hygiene=find_verify_hygiene_issues)  # F99 审计接线（core 禁兄弟 import，transport 注入）
        if name == "repo_board":  # F22：worker 按己任务仓分区；root/dispatcher 可传 repo 诊断
            snap = app.state.snapshot()
            repo = (app.peer.partition(sid) if role == "worker"
                    else body.get("repo"))
            # Req: F173：tag 过滤透传（非法值 board 域 ValueError→400）
            return app.peer.board(snap, repo, body.get("tag"))
        if name == "wiki_read":
            text = self._wiki(body["path"]).read_text(
                encoding="utf-8", errors="replace")  # 页面可含坏字节：降级 U+FFFD 不 500
            # Req: F180 hash＝页文本 sha256（与 CAS 校验同口径）
            return {"text": text,
                    "hash": hashlib.sha256(
                        text.encode("utf-8")).hexdigest()}
        if name == "wiki_write":
            rel = Path(body["path"])
            if rel.parts and rel.parts[0] == "tasks":  # 任务页服务独占刷新
                raise Rejected("wiki/tasks/ 前缀拒写（服务独占）")
            mode = body.get("mode", "overwrite")  # Req: F180 缺省＝旧行为
            if mode not in ("append", "overwrite"):
                raise ValueError("mode 须为 append/overwrite")
            p = self._wiki(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            eh = body.get("expected_hash")
            if mode == "overwrite" and eh is None:
                p.write_text(body["text"], encoding="utf-8")
            else:
                # Req: F180 append＋CAS 读-拼-写：本栈 HTTP 面单线程串行（§4.8）
                # ＝请求间互斥；残余面：health 线程后台写 wiki/drafts/（证据页
                # 自愈）可与 append 竞——已知限制，升级＝跨 access/pages 共享 wiki 锁
                cur = (p.read_text(encoding="utf-8", errors="replace")
                       if p.exists() else "")  # 缺页＝空文本（CAS 可建页）
                if eh is not None:
                    actual = hashlib.sha256(
                        cur.encode("utf-8")).hexdigest()
                    if eh != actual:
                        raise _WikiConflict(
                            f"expected_hash 不匹配: 当前 {actual}")
                if mode == "append":
                    # shortcut: 坏字节页 append 以 U+FFFD 重写原文，需字节级追加时再升级
                    if body["text"]:  # 空文本＝零追加：不补 \n 不重写（hash 不轮换）
                        if cur and not cur.endswith("\n"):
                            cur += "\n"  # 原文无尾换行先补 \n
                        p.write_text(cur + body["text"], encoding="utf-8")
                else:
                    p.write_text(body["text"], encoding="utf-8")
            return {"ok": True}
        if name == "enforce":  # may() 已滤非 root；开/关审计在 Auth
            on = body["on"]
            if not isinstance(on, bool):  # "false" 字符串得 True——语义反转拒收
                raise ValueError("on 须为布尔")
            app.auth.set_enforce(on, role)
            return {"enforce_on": app.auth.enforce_on()}
        raise KeyError(name)

    def _bind_caller(self, sid, role):
        """ag-f80309 B 案 root 钉死＋gt-d55b9e worker 换号自愈：MCP 壳透传
        X-Agora-Sid（调用者真实会话 sid）——root 牌只更新 main_sid 活绑定并返回
        caller 作本笔归属身份（牌面零改绑）；worker 换号照旧改绑；无头直连
        （T4 冒烟/看板 tui）不动绑定。返回对齐后 sid——后续归属判定以新 sid 走。"""
        caller = self.headers.get("X-Agora-Sid", "")
        if not caller:
            return sid
        if role == "root":
            return self.app.bind_main(sid, caller)  # 对齐后 sid：改绑当笔即生效（F21 前置）
        elif role == "worker":
            return self.app.bind_worker(sid, caller)
        return sid

    def _deny(self, sid, role, tool, tid):
        """越权 403：POST/GET 共用（审计行同形）；hint 非空附可用面退路（F13）。"""
        hint = _DENY_HINT.get(role, "")
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid, "event": EV_AUTH_DENIED,
             "tool": tool, "task": tid, "note": f"role={role}"})
        msg = f"{tool} 拒绝（role={role}）"
        if hint:
            msg += f"——{hint}"
        return self._json(403, {"error": msg})

    def _bad(self, sid, tool, msg):
        """F13 参数错 400：落 bad_request 审计行（可计量）后带因返回。"""
        if sid is not None:  # 鉴权前解析错属协议噪声，不混入参数错计量
            try:  # best-effort：审计写失败不影响 400 应答
                self.app.store.append_event(
                    {"ts": time.time(), "actor": sid,
                     "event": "bad_request", "tool": tool or "?",
                     "note": msg[:200]})
            except Exception:
                pass
        return self._json(400, {"error": msg})

    def _post(self, sid, role, body):
        app, kind = self.app, body["kind"]
        if kind == "peer":  # F22 写面：无任务目标，作者＝调用者任务；仅 worker 牌
            if role != "worker":
                raise PeerRejected("peer 留言仅 worker 牌可写")
            # Req: F173：回执形 {"id", "collision"}（撞车提醒不拒写）
            return {"message": app.peer.post(sid, body["body"])}
        tid = body["task"]
        if kind not in _POST_KINDS:  # F13 客户端 kind 白名单；内部直调 Router 不经此路
            raise ValueError(f"kind 须为 {'/'.join(_POST_KINDS)} 之一（收到 {kind!r}）")
        t = app.state.get(tid)
        if kind == "done":  # F7 入口：六拒同步应答，受理绿才落库并入门
            if t.get("worker_sid") != sid:
                raise NotChainEdge(f"{sid}->{tid}: done 仅任务 worker")
            r = app.gate.accept_done(tid, sid, body["body"].strip())
            if r != "accepted":
                # ag-d03e0e done 拒收可见化：拒回＝同步报错（与「done 仅任务
                # worker」同档 400——MCP 调用失败＋拒因原文），不再 200 result
                # 成功形状（worker 零感知的「受理成功后异步拒」形态废除）；
                # 拒因落消息面由 gate._deny_done 持久化。
                raise ValueError(r)
            app.router.post(sid, ("up", tid), "done", str(body["body"]))
            self.gate_q.put(tid)  # 门体后台串行
            return {"result": "accepted"}
        if not app.router.is_dispatcher(sid, t):
            app.bind_dispatcher(role, sid, t)  # F21：root 持牌＋登记号死→改绑后放行
            t = app.state.get(tid)
        edge = ("down", tid) if self.app.router.is_dispatcher(sid, t) else ("up", tid)
        if kind == "question" and edge[0] == "up":  # Req: F174 worker 上行申报面
            archived = self._noop_archive(sid, tid, t, body["body"])
            if archived is not None:
                return archived
        if kind == "verify" and edge[0] == "up":  # Req: F175 提案机械预检
            rejected = self._verify_precheck(sid, tid, t, body["body"])
            if rejected is not None:
                return rejected
        return {"message": app.router.post(sid, edge, kind, body["body"])}

    def _verify_precheck(self, sid, tid, t, body):
        """Req: F175 提案机械预检（F99 闸前移，两代同构——v3 落 serverv3.
        _message_post）：worker 上行 verify 提案在落库前过 F99 单一规则谓词
        （App 装配注入，未注入＝闸关照常规投递）——blocking 报因即①服务合成
        verify_reject 下行投 worker（from=agorad 服务标识非 dispatcher sid，
        同 done 拒因 notify_worker 形：只落库不注入——worker 同回合已收同步
        回执，B 册 R2 免 head-of-line）②审计 verify_precheck_reject③提案
        不投递 dispatcher④响应附 precheck_rejected 回执（加键不破形状）。
        GLOBAL_STATE 仅告警不拒（F99 登记闸同口径）；谓词干净零变化。"""
        if t.get("worker_sid") != sid:
            # 非本任务 worker（跨链 sid）：照常规投递，链边闸照旧 NotChainEdge
            # 400 零落库零注入（_noop_archive 同款守卫——预检先于 Router._resolve
            # 终审，无此守卫则跨链脏提案可触发服务写入）；顺带消灭未派发行
            # project_dir=None 直入谓词的 realpath(None) TypeError 面
            return None
        fn = self.app.verify_hygiene
        if fn is None:
            return None
        reasons = [f for f in fn(str(body), t["project_dir"])
                   if f["blocking"]]
        if not reasons:
            return None
        note = "; ".join(f"{f['code']}: {f['fragment']}"
                         for f in reasons)[:200]
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid,
             "event": "verify_precheck_reject", "task": tid,
             "note": note})
        text = ("verify 提案机械预检未过（F99 卫生闸前移）——提案未送达派发方：\n"
                + "\n".join(f"· {f['code']}: {f['fragment']} —— {f['hint']}"
                            for f in reasons)
                + "\n修正后重报")
        mid = None
        try:  # 留痕面尽力而为（F54）：消息面败不吞同步回执
            mid = self.app.router.notify_worker(tid, "verify_reject", text)
        except Exception:
            pass
        return {"message": mid, "precheck_rejected": True, "reasons": reasons}

    def _noop_archive(self, sid, tid, t, body):
        """Req: F174 no-op 申报自动归档：worker 上行 question 首行 no-op: 前缀＋
        零提交（gate 既有 done 零提交锚判定）→ 申报原文落库不注入（留案供翻案，
        check_mail 可补拉）＋程序化 cancel（reason=noop_confirmed:附首行摘要；
        级联/凭据作废/回收/通知走 cancel 既有全链——result 通知实时达派发方）。
        服务内部动作不经角色闸；不满足/判据不可立→None 照常规 question 投递。"""
        if t.get("worker_sid") != sid or not t.get("dispatcher_sid"):
            return None
        hit, first = parse_noop(body)
        if not hit or not self.app.gate.zero_commit(tid):
            return None
        # TOCTOU 收窄（评审 B 册）：zero_commit 跑多个 git 子进程期间，health
        # tick reinject 可改绑 worker_sid、并发写者可把行推进终态——破坏性
        # 动作前按新鲜行复验绑定与状态；失效即弃归档照常规投递（链边/状态
        # 闸自然裁决），防僵尸申报误杀重派接替者
        fresh = self.app.state.get(tid)
        if (fresh.get("worker_sid") != sid
                or fresh["state"] in ("merged", "closed", "cancelled")):
            return None
        mid = self.app.router.record(sid, tid, "question", body)
        # stop_after_failed 同款内部程序化 seam（public cancel 无 reason 形参
        # 且带角色闸/忙碌闸——自动归档是零提交服务动作，两闸语义均不适用）；
        # EV_CANCELLED 审计行显式补齐（v3 公共 cancel 自带，两代同构；n=0
        # ＝done-in-gate 排队，note 沿 public cancel cancelled={n} 同口径如实）
        n = self.app.task._cancel_one(tid, sid,
                                      f"noop_confirmed:{first[:120]}")
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid, "event": EV_CANCELLED,
             "task": tid, "note": f"noop_confirmed cancelled={n}"})
        return {"message": mid, "auto_cancelled": True,
                "reason": "noop_confirmed"}

    def _task_nudge(self, sid, role, body, row):
        """F73 board 手动提示面：root 任意任务；任务派发方限己任务链。

        缺省/空白＝F70 同源自检；自定义加服务来源行（文本不落审计正文）。
        无 worker/注入失败返回 200 结构化原因；实际尝试才落 worker_nudge 审计。"""
        tid = body["task"]
        if role != "root" and not self.app.router.is_dispatcher(sid, row):
            raise Rejected(f"{sid}->{tid}: prompt 仅 root/任务派发方")
        if not row.get("worker_sid"):
            return {"nudged": False, "reason": "no_worker"}
        text = body.get("body")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("prompt body 须为字符串")
        text = text.strip()
        if len(text) > 2000:
            raise ValueError("prompt body 长度须≤2000 字符")
        mode = "custom" if text else "default"
        ok = self.app.health.nudge_worker(tid, text=text or None)
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid, "event": "worker_nudge",
             "task": tid,
             "note": f"mode={mode} chars={len(text)} "
                     f"delivered={str(ok).lower()}"})
        if ok:
            return {"nudged": True}
        return {"nudged": False, "reason": "inject_failed"}

    def _task_alarm(self, sid, role, body, row):
        """F77 手动立即拍僵：root-only 直调 escalate＋nudge 两原语。

        绕过 20min 阈值与退避计数、不触碰 _stale_seen/_stale_esc（自动判僵
        边沿照常可触发）；无 worker 零投递零审计（同 task_nudge 口径）；
        非活动态（终态/todo）拒——评审环 B 册：终态行 worker_sid 永留，
        无闸会对已取消任务注入「继续」自检、唤醒已废牌 worker。"""
        tid = body["task"]
        if role != "root":
            raise Rejected(f"{sid}->{tid}: alarm 仅 root")
        if not row.get("worker_sid"):
            return {"alarmed": False, "reason": "no_worker"}
        if row["state"] not in _ACTIVE:
            return {"alarmed": False, "reason": "not_active"}
        self.app.health.escalate(KIND_STALLED, tid)
        ok = self.app.health.nudge_worker(tid)
        self.app.store.append_event(
            {"ts": time.time(), "actor": sid, "event": "task_alarm",
             "task": tid,
             "note": f"manual=1 delivered={str(ok).lower()}"})
        return ({"alarmed": True} if ok
                else {"alarmed": False, "reason": "inject_failed"})

    def _cred_for_sid(self, body):
        """F25 修正推改拉面：插件 hook 缓存未命中按会话 sid 拉凭据。

        sid 是某在飞（非冻结）任务 worker_sid 且牌活 → {tid, env_dir, token}，
        否则 404；无牌调用放行（server 仅绑 127.0.0.1＝回环单用户 plaza 信任
        模型，与既有 localhost 面同口径）；每请求一行审计，防噪归插件侧节流。
        ag-af96bb①：携有效 root 牌改走签发/拉取面（每窗口独立派发牌）。
        """
        root = self.app.auth.identify(self.headers.get("X-Agora-Token", ""))
        if root is not None and root[1] == "root":  # ag-af96bb① 先于 worker 拉取面
            return self._cred_issue(root[0], str(body.get("sid") or ""))
        asker = str(body.get("sid") or "")
        hit = next(((tid, t) for tid, t in self.app.state.snapshot()["tasks"].items()
                    if t.get("worker_sid") == asker and t["state"] in _PULL_STATES
                    and t.get("env_dir")), None)
        tok = self.app.auth.token_for(asker) if hit else None  # 牌废（取消/重派）即 404
        self.app.store.append_event({"ts": time.time(), "actor": asker or "?",
                                     "event": "cred_pull",
                                     "task": hit[0] if tok else None})
        if not tok:
            return self._json(404, {"error": "no cred for sid"})
        # F27 等价性注释：此处返回 token 的信任边界与直读 $AGORA_HOME/tokens 凭据文件
        # 等价（单用户回环模型，本端点仅 127.0.0.1 可达）——未新增攻击面，勿过度加固。
        return self._json(200, {"tid": hit[0], "env_dir": hit[1]["env_dir"],
                                "token": tok})

    def _cred_issue(self, root_sid, asker):
        """每窗口独立派发牌（ag-af96bb①，服务端单侧先行——插件 lift 适配属后续任务）：
        先按 sid 全账本 role-aware 扫描（auth.live_token）——已有非 root 活牌→幂等
        旧牌回（worker 在飞行随带 tid/env_dir，不重签防铸牌膨胀，ag-588eb8 裁决 A）；
        仅持 root 牌（R25-A 活绑定落会话 sid 的污染）或无牌→ auth.issue(sid,
        "dispatcher") 签发＋cred_issue 审计（污染换牌附 root-poison reissue 标记），
        污染 root 条目保留不撤（root 常为唯一治理牌，撤销即断 CLI/enforce 面）。
        授权＝root 牌（may() 全量同口径），信任边界同 F27 等价性注释（回环单用户，
        签发不扩面）。"""
        if not asker:
            return self._json(404, {"error": "no cred for sid"})
        found = self.app.auth.live_token(asker)
        if found is not None and (found[1] != "root" or asker == "root"):
            # 幂等：非 root 活牌照旧回；daemon 身份 sid="root" 的 root 牌同口径
            tok = found[0]
            hit = next(((tid, t) for tid, t in
                        self.app.state.snapshot()["tasks"].items()
                        if t.get("worker_sid") == asker and t["state"] in _PULL_STATES
                        and t.get("env_dir")), None)
            self.app.store.append_event({"ts": time.time(), "actor": root_sid,
                                         "event": "cred_pull",
                                         "task": hit[0] if hit else None})
            ident = self.app.auth.identify(tok)  # 门线程 revoke 竞窗（snapshot/审计窗）防 None
            if ident is None:  # 牌已废：与 worker 拉取面同 404 出口
                return self._json(404, {"error": "no cred for sid"})
            return self._json(200, {"tid": hit[0] if hit else None,
                                    "env_dir": hit[1]["env_dir"] if hit else None,
                                    "token": tok, "role": ident[1]})
        poisoned = found is not None  # 走到签发且原持 root＝会话 sid 污染换牌
        tok = self.app.auth.issue(asker, "dispatcher")
        self.app.store.append_event({"ts": time.time(), "actor": root_sid,
                                     "event": "cred_issue",
                                     "note": f"dispatcher:{asker}"
                                             + (" root-poison reissue" if poisoned else "")})
        return self._json(200, {"token": tok, "role": "dispatcher"})

    def _gate_deny(self, body):
        """Req: F176 shim 拒绝审计上报面：插件（及后续 pi/dsh shim）判定链产出
        拒时 fire-and-forget POST——信息面无鉴权（回环单用户口径同 /cred_for_sid）。
        字段校验（类型/长度上限/控制符压平）→ 一行 event=gate_deny 审计（标注
        shim 上报源）；服务端节流防线：同 (sid,tool,reason) 30s 窗内后续丢弃——
        防失控循环撑爆审计账本（gt-a18ef6 教训：节流走内存态，禁每事件全量落盘）。"""
        flat = {}
        for k, cap in _GATE_DENY_CAPS:
            v = body.get(k, "")
            if not isinstance(v, str):
                return self._json(400, {"error": f"{k} 须为字符串"})
            flat[k] = v.translate(_GATE_DENY_CTRL)[:cap]
        ts = body.get("ts", 0)
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return self._json(400, {"error": "ts 须为数字"})
        key = (flat["sid"], flat["tool"], flat["reason"])
        # 内存态节流挂 app（单线程串行免锁；测试夹具逐 app 隔离防窗串扰）
        seen = getattr(self.app, "_gate_deny_seen", None)
        if seen is None:
            seen = self.app._gate_deny_seen = {}
        now = time.time()
        if key in seen and now - seen[key] < _GATE_DENY_WINDOW_S:
            return self._json(200, {"ok": True, "throttled": True})
        seen[key] = now
        for k in [k for k, t in seen.items()  # 出账：只留窗内活跃键（内存态有界）
                  if now - t >= _GATE_DENY_WINDOW_S]:
            del seen[k]
        self.app.store.append_event({
            "ts": now, "actor": flat["sid"] or "?", "event": "gate_deny",
            "task": None,
            "note": (f"shim tool={flat['tool']} target={flat['target']} "
                     f"reason={flat['reason']} ts={ts}")})
        return self._json(200, {"ok": True})

    def _wiki(self, rel):
        root = (Path(self.app.store.home) / "wiki").resolve()
        p = (root / rel).resolve()
        p.relative_to(root)  # 越出 wiki 根即 ValueError → 400
        return p

    # ---- 应答 ----

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(app, port: int = 0, gate_deadline=None):
    """单线程 HTTPServer（串行口径 §4.8）＋后台门队列（1 worker 串行）。

    装配即重排 done 残留（kill -9 门中崩溃）：重启后 verify 幂等重跑，
    pending-cancel 与门闩照常由门体结算（F-4）。
    gate_deadline（P0-1，ag-9c4cbb）：门级看门狗硬顶注入缝（None＝缺省
    max(4×生效 verify 预算, 600s)）——测试秒级跑。
    """
    q = queue.Queue()
    app.gate_q = q  # Req: F130 health 宽限兜底重排投门面（晚绑同 Handler.gate_q）
    for tid, t in app.state.snapshot()["tasks"].items():
        if t["state"] == "done":
            q.put(tid)

    # F112 门按仓分池：同仓任务经 per-repo 车道串行（仓级交付锁本就同仓
    # 互斥），跨仓任务并行进门——verify 长任务不再堵死全部门流量。车道
    # 懒建懒活（无仓无线程）；repo 数实践中有限，不设上限。
    lanes = {}
    lanes_guard = threading.Lock()

    def lane_loop(rq, repo):
        while True:
            tid = rq.get()
            try:
                app.gate.run_guarded(tid, deadline=gate_deadline)
            except Exception as e:  # 门体崩溃留痕（P0-1：无痕 except pass 已废）
                try:
                    app.store.append_event({"ts": time.time(), "actor": "gate",
                                            "event": "gate_crashed", "task": tid,
                                            "note": str(e)[:200]})
                except Exception:
                    pass
                print(f"agorad gate crashed on {tid}: {e}", file=sys.stderr,
                      flush=True)
            finally:
                rq.task_done()

    def consume():
        while True:
            tid = q.get()
            try:
                repo = app.state.get(tid)["project_dir"]
            except KeyError:  # 任务已除籍/毒丸：门无对象可跑——审计留痕同旧径
                try:
                    app.store.append_event({"ts": time.time(), "actor": "gate",
                                            "event": "gate_crashed", "task": tid,
                                            "note": "task not found"})
                except Exception:
                    pass
                q.task_done()
                continue
            with lanes_guard:
                rq = lanes.get(repo)
                if rq is None:
                    rq = queue.Queue()
                    lanes[repo] = rq
                    threading.Thread(target=lane_loop, args=(rq, repo),
                                     daemon=True,
                                     name=f"gate-{repo}").start()
            rq.put(tid)
            q.task_done()

    threading.Thread(target=consume, daemon=True, name="gate").start()

    class Handler(_Handler):
        pass
    Handler.app = app
    Handler.gate_q = q
    return HTTPServer(("127.0.0.1", port), Handler)
