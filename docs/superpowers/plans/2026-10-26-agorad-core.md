# opencode-agora ①agorad 核心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 agorad 服务层——域核心八模块＋三访问层（oc/store/gitx）＋transport 入口，oc 以 stub 注入，T0/T1 全绿。

**Architecture:** 域核心纯逻辑零 I/O（访问层对象经构造注入，core 不 import 任何 agorad 包——假件即纯内存单测）；store 唯一磁盘接触点（账本只追加）；单线程 httpd＋长操作后台线程＋两级锁。物理布局＝分层规划本身（access/core/transport 三包＋main 装配），Task 12 守卫 import 方向。规格见设计稿 §3/§4。

**Tech Stack:** Python 3 stdlib only（unittest、http.server、json、fcntl、subprocess=git）。零第三方依赖。

**Spec:** `docs/reqs/opencode-agora.md`（已批准）＋ `docs/superpowers/specs/2026-10-26-opencode-agora-design.md`（已批准）

## File Structure（物理布局＝分层）

```
agorad/
├── main.py              # 装配＋lift 入口（唯一同时 import access/core/transport 的文件）
├── access/              # 三访问层（只 import stdlib）
│   ├── store.py         #   账本：append 四原语＋flock
│   ├── oc.py            #   opencode API 客户端
│   └── gitx.py          #   worktree/合并/回滚/闸体检测
├── core/                # 域核心（纯逻辑；只 import stdlib；访问层对象构造注入）
│   ├── state.py         #   状态机/快照/fail_streak/审计事件名
│   ├── auth.py          #   会话牌与角色判定矩阵
│   ├── msg.py           #   消息路由（链边/落库先行/幂等/游标）
│   ├── task.py          #   生命周期（保险丝/级联中止/verify 登记）
│   ├── gate.py          #   verify 门（done 前置/自动合并/回滚/门闩）
│   ├── health.py        #   探活（只管死）/升级事件合成
│   ├── recover.py       #   启动重载/状态复查重注入
│   └── report.py        #   五桶/链视图/doctor//view
└── transport/
    └── httpd.py         # 入口皮（只 import core）
tests/
├── stub_opencode.py     # 本地 HTTP stub（create/inject/status/health 可脚本化）
├── test_architecture.py # 分层守卫（import 方向断言）
├── test_access_{store,oc,gitx}.py
├── test_core_{state,auth,msg,task,gate,health,recover,report}.py
└── test_transport_httpd.py
```

## Global Constraints

- 零第三方依赖：Python 3 stdlib（需求 §5）
- 账本文本只追加、永不改写；迁移写序＝先 append 快照行后写消息文件；msg-id 幂等去重（设计 §4.8）
- tasks.jsonl 末行完整快照＝权威；断尾回退上一行；不触"禁事件重放推导"（设计 §4.8）
- worktree 形态/命名/清理唯一真源＝全局 AGENTS.md 铁律，代码不复制条文、只留语义断言（同级父目录、线性历史、级联清理）（B6）
- 状态集：todo/dispatched/running/done/merged/closed/blocked/cancelled/verify_failed/reopened；迁移矩阵见 state 模块（设计 §4.1）
- import 方向铁律：access→stdlib；core→stdlib（访问层经构造注入）；transport/httpd→core；main→全部
- 每测试带 `Req: F<n>` 锚；线性历史 rebase＋ff-only
- enforce 旗标重启默认 on；升级事件四类调用点＝质量连败/闸体（gate）、重注入失败（recover）、dispatcher 死亡（health）

## Review Focus（规格未点名、最可能咬人的五类输入——各钉进所属任务）

1. tasks.jsonl 末行被 kill -9 撕裂（半行 JSON）→ load 回退上一完整行而非拒启/崩溃 → Task 1
2. 非 ff 合并请求（目标分支已推进）→ 拒并置 reopened，绝无 merge commit → Task 8
3. 同一 done 消息崩溃后重发 → 按 msg-id 幂等去重，verify 门只跑一次 → Task 6
4. 伪造/越权牌调用（worker 牌调 task_cancel、非 root 调 enforce）→ 403 且审计留行 → Task 3
5. 恢复期对 cancelled/blocked 任务的死会话重注入 → 复查状态后拒绝重注入 → Task 9

---

### Task 1: access/store 访问层

**Files:**
- Create: `agorad/access/__init__.py`、`agorad/access/store.py`、`agorad/__init__.py`
- Test: `tests/test_access_store.py`

**Interfaces:**
- Produces: `class Store(home: Path)`；`append_state(snap: dict) -> None`；`load_state() -> dict | None`（末行完整快照，断尾回退）；`append_event(ev: dict) -> None`；`write_message(fm: dict, body: str) -> int`（返回 msg-id，全局单调）；`scan_max_msg_id() -> int`；`write_ephemeral(name: str, data: dict)`；`flock_singleton() -> bool`

- [ ] **Step 1: 写失败测试**

```python
def test_torn_last_line_falls_back(self):  # Req: F6
    st = Store(self.tmp)
    st.append_state({"v": 1}); self.tmp.joinpath("state/tasks.jsonl").open("a").write('{"torn')
    self.assertEqual(Store(self.tmp).load_state(), {"v": 1})
def test_msg_id_global_monotonic(self):  # Req: F4
    a = self.st.write_message({"task": "t1"}, "x"); b = self.st.write_message({"task": "t2"}, "y")
    self.assertEqual((b - a), 1)
```

另含：append 后文件历史行字节不变（只增断言）、flock 第二实例 False、events.jsonl 只追加。

- [ ] **Step 2: 跑测确认失败** — `python3 -m unittest tests.test_access_store -v` → FAIL: No module named agorad
- [ ] **Step 3: 实现 store.py** — 全量快照行＝`json.dumps(snap, ensure_ascii=False)+"\n"`＋flush/fsync；断尾＝逐行 try json.loads 取最后完整行；msg 计数器内存＋`scan_max_msg_id` 扫文件名恢复；flock 用 `fcntl.flock(LOCK_NB)`。
- [ ] **Step 4: 跑测通过** — 同命令 → OK
- [ ] **Step 5: Commit** — `git add agorad tests/test_access_store.py && git commit -m "feat(agorad): access/store——append-only 账本四原语＋flock"`

### Task 2: core/state 状态机与审计事件

**Files:**
- Create: `agorad/core/__init__.py`、`agorad/core/state.py`
- Test: `tests/test_core_state.py`（Store 用 tmp 夹具真件；core 只注入其对象）

**Interfaces:**
- Consumes: `Store.append_state/load_state`
- Produces: `class State(store)`；`transition(tid: str, to: str, **fields) -> None`（非法抛 `InvalidTransition`，附合法后继）；`snapshot() -> dict`（含 mail_read）；`touch_mail(sid: str)`；`LEGAL: dict[str, set[str]]`；`fail_streak_bump(tid)/fail_streak_reset(tid)`（质量败 +1、绿/重派清零、≥3 自动 blocked）；审计事件名常量（dispatched/merged/cancelled/…）

- [ ] **Step 1: 写失败测试**

```python
def test_illegal_transition_rejected(self):  # Req: F2
    with self.assertRaises(InvalidTransition): self.st.transition("t1", "merged")  # from todo
def test_fail_streak_blocks_at_3_and_resets_on_redispatch(self):  # Req: F7
    for _ in range(3): self.st.fail_streak_bump("t1")
    self.assertEqual(self.st.get("t1")["state"], "blocked")
```

矩阵用例：LEGAL 全合法边逐条过＋blocked 双出口（cancelled/dispatched）、verify_failed→done 续修边。

- [ ] **Step 2-5:** 同 Task 1 循环；commit `feat(agorad): core/state——迁移矩阵/fail_streak/审计事件`

### Task 3: core/auth 会话牌与 enforce 旗标

**Files:**
- Modify: `agorad/access/store.py`（补 `read_ephemeral(name) -> dict | None`，附一条 store 测试）
- Create: `agorad/core/auth.py`
- Test: `tests/test_core_auth.py`、`tests/test_access_store.py`（追加 read_ephemeral 用例）

**Interfaces:**
- Consumes: `Store.write_ephemeral/read_ephemeral`（牌持久化）
- Produces: `class Auth(store)`；`issue(sid, role) -> str`（role∈root/dispatcher/worker；token=secrets.token_hex；持久化经 write_ephemeral("tokens")）；`identify(token) -> tuple[str,str] | None`（kill -9 后新实例从盘恢复仍有效）；`revoke(token) -> bool`；`may(role, tool, tid, state) -> bool`（矩阵：root→enforce/任意；dispatcher→task_update(自己任务)/task_cancel(子链)；worker→post/check_mail/wiki_*/task_dispatch(嵌套)；worker 调 task_cancel/task_update/enforce 恒 False）；`rotate_root() -> str`；`set_enforce(on: bool, role: str) -> bool`＋`enforce_on() -> bool`（仅 root 生效＋写 events 审计；重启默认 on）

- [ ] **Step 1: 写失败测试** — `may` 矩阵全叉乘（3 角色 × 11 工具，按设计 §4.9）；token 重启恢复；revoke 后 identify None；非 root set_enforce 返回 False 且 events 有审计行。`Req: F1/F6`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/auth——角色判定矩阵/牌持久化/enforce 旗标`

### Task 4: access/oc 访问层（含 stub）

**Files:**
- Create: `agorad/access/oc.py`；`tests/stub_opencode.py`（本地 HTTP stub）
- Test: `tests/test_access_oc.py`

**Interfaces:**
- Produces: `class Oc(base_url: str)`；`create(cwd: str, boot: str) -> str`；`inject(sid: str, text: str) -> bool`；`alive(sid: str) -> bool`；`probe() -> dict`（health＋oc_version）；异常 `OcUnavailable`

- [ ] **Step 1: 写失败测试** — 对 stub：create 返回 sid、inject 死会话 False、probe 返回 oc_version；stub 停机→`OcUnavailable`。`Req: F3/F4`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): access/oc＋stub opencode`

### Task 5: access/gitx 访问层

**Files:**
- Create: `agorad/access/gitx.py`
- Test: `tests/test_access_gitx.py`（真实 git，tmp 夹具）

**Interfaces:**
- Produces: `class Gitx`；`build_env(project_dir: str, tid: str) -> str`（worktree 名＝`<REPO>__wt-task-<tid>__<MMDDHHMM>`＋4 位随机后缀，签出 repo 同级父目录，断言同层级）；`remove_env(env_dir) -> None`（remove --force＋branch -d 级联）；`merge_ff(env_dir, target) -> str`（"ok"|"non-ff"）；`rollback(target, before_sha) -> None`；`touches_gate(env_dir, patterns) -> bool`

- [ ] **Step 1: 写失败测试**

```python
def test_env_sibling_of_repo_and_ff_only(self):  # Req: F1
    env = self.gx.build_env(self.repo, "gt-x1")
    self.assertEqual(Path(env).parent, self.repo.parent)
    self.assertEqual(self.gx.merge_ff(env, "main"), "non-ff")  # 主干已推进时
```

另：remove_env 后 worktree list 干净；rollback 后目标分支字节级回退；merge 成功路径（"ok"）真 git 断言。

- [ ] **Step 2-5:** 循环；commit `feat(agorad): access/gitx——worktree 建清/ff-only/回滚/闸体检测`

### Task 6: core/msg 消息路由

**Files:**
- Create: `agorad/core/msg.py`
- Test: `tests/test_core_msg.py`（假 store/oc 注入）

**Interfaces:**
- Consumes: `Store.write_message`、`State.snapshot/touch_mail`、`Oc.inject`
- Produces: `class Router(store, state, oc)`；`post(from_sid, to_edge: tuple[str,str], kind: str, body: str) -> int`（链边寻址：("up", tid)/("down", tid)；越边抛 `NotChainEdge`）；`dedup_key(msg_id) -> bool`（重发返回 True 不再投递）；`check_mail(sid) -> list[dict]`（游标推进）

- [ ] **Step 1: 写失败测试** — 在线→inject 调一次＋落库；离线→仅落库；同 msg-id 重发→inject 不再调；越链边 post→NotChainEdge。`Req: F4`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/msg——链边/落库先行/幂等补拉`

### Task 7: core/task 生命周期

**Files:**
- Create: `agorad/core/task.py`、`agorad/access/pages.py`
- Test: `tests/test_core_task.py`、`tests/test_access_pages.py`（薄断言 2-3 例）

**Interfaces:**
- Consumes: State/Auth/Gitx/Store/Oc/**Router**/**pages**
- Produces: `pages.write_task_page(home, tid, snap) -> Path`（wiki/tasks/<id>.md 渲染视图，access 层）；`class TaskService(state, auth, gitx, oc, store, router, pages)`；`create(goal, project_dir, after: str | None, actor) -> str`（调 pages 写任务页）；`dispatch(tid, dispatcher_token) -> str`（保险丝：链深<5、配额（活动任务计数）、can_spawn 名册、B7 after 前驱终态校验——违者拒且报因；**重派先 revoke 旧牌**）；`cancel(tid, actor_token)`（级联下级、**经 router.post 发终止通知**、凭据作废、配额即释放、环境即回收、merged 不动、空链幂等＋审计行）；`update_verify(tid, actor_token, cmd)`（登记即锁定，registered_by/registered_at 落行；非派发方/worker 必拒＋审计）

- [ ] **Step 1: 写失败测试** — 保险丝四拒绝各一例＋B7：after 未终态 dispatch 拒；cancel 两级级联断言＋**两级 worker 各收一条终止消息**；worker token update_verify 拒且 events 有拒绝行；**happy path：worker 回填经 post→dispatcher update_verify→verify.state=registered＋registered_by/registered_at**；**create 后 snapshot 可查 id/state/归属**；**重派后旧 token identify None**；**空链 cancel 幂等＋events 有行**；**create 产出任务页文件**。`Req: F2/F3/F11`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/task——保险丝/级联中止/verify 登记/任务页`

### Task 8: core/gate verify 门与自动合并

**Files:**
- Create: `agorad/core/gate.py`
- Test: `tests/test_core_gate.py`（假 gitx/oc 注入）

**Interfaces:**
- Consumes: State/Gitx/Router/Oc（anchor 读取经构造注入 `read_anchor(env_dir) -> dict | None`，默认实现归 access 层）
- Produces: `class Gate(state, gitx, router, oc, read_anchor, health)`；`accept_done(tid, worker_sid, commit) -> str`（"accepted"/"rejected:verify-unregistered"/"rejected:anchor"——服务经 read_anchor 自取环境 `.review-loop` 锚＝worktree 分支 HEAD 且无 blocker/high；拒收附动作指引文案）；`run(tid) -> None`（后台线程体：env 跑 verify→败 verify_failed＋输出尾部回 worker；绿→merge_ff→non-ff→reopened 且 fail_streak 不变；集成 verify 败→rollback＋reopened；绿→`transition(merged, commit=sha)`（证据指针落快照行）＋环境回收＋尾部置 closed；全程 in-gate 门闩＋pending-cancel 检查；质量 3 连败→health.escalate；闸体变更→health.escalate；verify 绿→fail_streak_reset）；`gate_files_touched(tid) -> bool`

- [ ] **Step 1: 写失败测试** — 三拒收分支；verify 败/绿全路径；非 ff→reopened 且无 merge commit 且 streak 不变；集成败→目标分支 sha 回退断言；merged 行含 commit 字段；终态 closed；3 连败触发 escalate 注入主会话一例；门内 cancel→pending 生效、merged 任务不动。`Req: F7`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/gate——done 前置/verify 门/自动合并/回滚/升级调用点`

### Task 9: core/health 与 core/recover

**Files:**
- Create: `agorad/core/health.py`、`agorad/core/recover.py`
- Test: `tests/test_core_health.py`、`tests/test_core_recover.py`

**Interfaces:**
- Produces: `class Health(state, oc, router, recover)`——`tick() -> None`（探直接下一跳：死→recover.reinject，失败→escalate；`touch(sid)` 刷 last_seen；常驻期由 main 定时线程驱动，兼探 oc health）；`escalate(kind, tid) -> None`（正文服务权威合成，worker 文本不可信段携带；四类：dispatcher 死亡/质量连败/重注入失败/闸体变更）；`class Recover(state, oc, gitx)`——`startup() -> None`（load→in-flight 复查→死会话按状态重注入：终态/blocked 不注入）；`reinject(tid) -> bool`（失败→escalate）

- [ ] **Step 1: 写失败测试** — tick 对死下级触发上报注入；escalate 正文含服务合成标记且 worker 文本转义；四类升级事件各注入一例达主会话；startup 对 cancelled 任务的死 worker 不重注入、running 任务重注入（同凭据——牌持久化后重启仍有效）。`Req: F5/F6`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/health·recover——只管死/升级合成/状态复查重注入`

### Task 10: core/report 视图

**Files:**
- Create: `agorad/core/report.py`
- Test: `tests/test_core_report.py`

**Interfaces:**
- Consumes: `State.snapshot`
- Produces: `buckets(snap) -> dict`（五桶：已合并/在飞(含 verify_failed·reopened)/失败/阻塞/已取消）；`chain_view(snap, dispatcher_sid) -> list`；`doctor_view(snap, store_home) -> dict`（孤儿目录、任务年龄排行）；`view_html(snap) -> str`

- [ ] **Step 1: 写失败测试** — 五桶分桶断言（reopened/verify_failed 归在飞）；链级按 dispatcher 展开。`Req: F10`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): core/report——五桶/链视图/doctor`

### Task 11: transport/httpd 与 main 装配

**Files:**
- Create: `agorad/transport/__init__.py`、`agorad/transport/httpd.py`、`agorad/main.py`
- Test: `tests/test_transport_httpd.py`（真服务＋stub oc/gitx）

**Interfaces:**
- Consumes: 全部 core 模块＋access 件
- Produces: `class App`（core/app.py：持 store/state/auth/router/task/gate/health/recover/report——由 main 装配）；`make_server(app, port: int = 0) -> HTTPServer`（单线程；路由＝11 工具端点＋/view＋/health；鉴权从 `X-Agora-Token` 过 Auth；gate.run 转后台线程池 1 worker）；`main()`＝装配 App（**root 牌于此授予**）＋flock＋write_ephemeral(service.json 含 oc_version)＋**health 后台定时线程（默认 300s 可配）**＋serve_forever

- [ ] **Step 1: 写失败测试** — task_create→dispatch→post(done)（假 gate）全链 HTTP 冒烟；无牌 401；worker 牌调 enforce 403＋审计行；**root 牌调 enforce on/off 生效＋审计行**；连续 10 条注入按序全到（stub 记录 inject 序）；**health 定时线程按间隔驱动 tick（短间隔夹具）**。`Req: F2-F4/F5`
- [ ] **Step 2-5:** 循环；commit `feat(agorad): transport/httpd＋main——单线程/鉴权/后台门/健康定时/冒烟全链`

### Task 12: 分层守卫（architecture test）

**Files:**
- Create: `tests/test_architecture.py`

**Interfaces:**
- Consumes: 无（AST 扫描 agorad/ 全部 import）

- [ ] **Step 1: 写失败测试**

```python
def test_import_direction(self):  # Req: F2
    assert_pkg_imports("agorad/access", allowed_local=[])          # 仅 stdlib
    assert_pkg_imports("agorad/core", allowed_local=[])            # 仅 stdlib（访问层经注入）
    assert_pkg_imports("agorad/transport", allowed_local=["agorad.core"])
    assert_only("agorad/main.py", may_import=["agorad.access", "agorad.core", "agorad.transport"])
```

- [ ] **Step 2: 跑测** — 预期 OK（守卫既有结构；若前序任务违规则红，据此修 import）
- [ ] **Step 3: Commit** — `test: 分层守卫——import 方向＝架构铁律`

---

## 计划②（预告，另出）：MCP 壳 11 工具映射、插件四段（hook/cred/inject/lift）、install.sh 三部件宿主装配、T2 强制 e2e、T3 派发链 e2e（真 opencode）。T4 验收对应表（需求 §4 每验收行 ↔ 用例）随①收尾落盘 `docs/superpowers/plans/t4-coverage.md`，②首任务核销。
