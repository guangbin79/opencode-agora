# opencode-agora ②接入层实施计划（A 册裁决修订版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通人↔agorad 的最后三件：MCP 壳（11 工具）、opencode v2 插件（hook/cred/inject/lift）、install.sh；对齐①与设计的消费面偏差；以真 opencode 对账 API 形状、跑通 T2/T3 e2e。

**Architecture:** agorad（①已并入 main，HTTP＋X-Agora-Token）为服务端；MCP 壳＝stdio JSON-RPC 薄代理（路由 `POST /<tool_name>` 根级）；插件＝TS 四段（token 主案＝插件附牌）；e2e tmux 驱动真 opencode。

**Tech Stack:** MCP 壳 Python 3 stdlib；插件 TS on Bun；e2e bash＋tmux。

**Spec:** 需求＋设计（已批准）＋ t4-coverage.md（②核销清单）

## File Structure

```
agora_mcp.py            # MCP 壳：stdio JSON-RPC＋11 工具→agorad HTTP 映射
agora-plugin/
├── index.ts            # 插件入口（plain-object 导出，禁 Plugin.define）
├── hook.ts             # 拦截判定链（edit/write＋读引导＋默认拒）
├── cred.ts             # 凭据读取/缓存（inject 事件提取 token）
├── inject.ts           # 消息接入会话（credential 消息→cred 缓存）
└── lift.ts             # .agora/service.json 探测＋setsid 拉起＋就绪等待
install.sh              # 三部件装配＋root 牌接管＋冒烟
tests/
├── test_mcp_shell.py
└── e2e/
    ├── oc_probe.ts     # T5 对账探针（真 opencode）
    ├── t2_enforce.sh
    └── t3_chain.sh
```

## Global Constraints

- MCP 壳：Python 3 stdlib ONLY；插件：TS on Bun，plain-object 导出
- **token 主案＝插件附牌**：inject.ts 收到 credential 消息→cred.ts 按 sid 缓存→hook 对 MCP 工具调用改写 arguments 附 `_token`→壳剥除并填 X-Agora-Token；**T5 探针实证三前提**（tool.execute.before 覆盖 MCP 工具／可改参／MCP spawn 拓扑），失败即升级用户裁替代案
- service.json＝`$AGORA_HOME/.agora/service.json`（对齐设计 §4.8）；就绪判据＝任意 HTTP 应答（/health 401 也算就绪）
- 铁律继承：拦 edit/write（bash 接受面）；服务不可达默认拒；权限矩阵 /view=board、/health=doctor
- e2e tmux 驱动＋自清理；每测试带 `Req: F<n>` 锚；真 API 偏离＝双侧修（oc.py/stub）
- install.sh 幂等；e2e 产物统一 `/tmp/opencode`（不入仓）

## Review Focus

1. MCP stdio 帧格式＋initialize 握手 → T1
2. lift 首跑（未装 vs 拉不起的拒因区分）→ T3
3. 真 opencode API 形状偏离（含附牌三前提）→ T5
4. e2e 残留自清理 → T6/T7
5. install 幂等与半装态 → T4

---

### Task 0: 对齐①与设计的消费面

**Files:**
- Modify: `agorad/main.py`、`agorad/access/store.py`（或 main 写路径处）、`tests/`（对应断言）
- Test: ①套件回归

**Interfaces:**
- Produces: service.json 落 `$AGORA_HOME/.agora/service.json`（设计 §4.8 原文）；main.py env 缺省：AGORA_HOME 缺省 `~/plaza`、AGORA_OC_URL 缺省 `http://127.0.0.1:port`（从 opencode 服务发现——T5 定来源，本任务先允许 env 显式指定＋留 `AGORA_OC_URL` 必填缺省占位）；root 牌 stdout 行格式固定位（T4 捕获解析用）

- [ ] **Step 1:** 红测——service.json 在 `.agora/` 下；AGORA_HOME 缺省生效
- [ ] **Step 2-4:** 实现→绿→①全量回归 132+绿
- [ ] **Step 5:** Commit `fix(agorad): service.json 落 .agora/＋env 缺省——对齐设计 §4.8`

### Task 1: agora_mcp.py——stdio MCP 壳

**Files:**
- Create: `agora_mcp.py`
- Test: `tests/test_mcp_shell.py`（对 make_server 起的真 agorad）

**Interfaces:**
- Consumes: agorad `POST /<tool_name>`（根级路由，httpd.py 实际形状）；`{"error": str}` 统一错误体
- Produces: stdio newline-delimited JSON-RPC：`initialize`/`tools/list`（11 件五族＋inputSchema）/`tools/call`——arguments 内 `_token` 剥除→X-Agora-Token；错误映射全档：400/401/403/404/409/500→JSON-RPC error（code 负数区，message 原文）；未知工具/缺 token→壳内直错

- [ ] **Step 1: 红测** — 对真 make_server：initialize 握手；tools/list 恰 11 件；task_create 透传（_token→头）；401/403/400/404/409 五档映射；未知工具错。`Req: F2/F3`
- [ ] **Step 2-5:** 循环；commit `feat(mcp): stdio 壳——根级路由映射/附牌剥除/五档错误映射`

### Task 2: 插件 hook＋cred（TS）

**Files:**
- Create: `agora-plugin/index.ts`、`hook.ts`、`cred.ts`
- Test: `agora-plugin/hook.test.ts`（bun test）

**Interfaces:**
- Produces: `shouldBlock(tool, targetPath, cwd, cred, enforceOn)`——判定链：enforce off→放行；非 git/临时目录（realpath 折叠）→放行；**读引导**（read/bash 目标为 $AGORA_HOME/state|messages→拒＋引导走工具面，events/wiki/主会话例外）；持活动凭据 cwd∈env→放行；否则拒＋派发引导文案。`readCredential(sessionDir)`；`attachToken(args, sid)`（附牌主案——形状 T5 收紧，本任务按假定形状＋桩）
- opencode 钩子接线形状留 T5 对账后收紧（T5 探针七项之一）

- [ ] **Step 1: 红测（bun test）** — 判定链全分支含读引导；realpath 折叠；主会话同拦。`Req: F1`
- [ ] **Step 2-5:** 循环；commit `feat(plugin): hook/cred——判定链＋读引导＋附牌`

### Task 3: 插件 inject＋lift（TS）

**Files:**
- Create: `agora-plugin/inject.ts`、`lift.ts`
- Test: `agora-plugin/lift.test.ts`

**Interfaces:**
- Produces: `ensureService()`——读 `$AGORA_HOME/.agora/service.json`→任意 HTTP 应答＝就绪→未运行 setsid 拉起 `python3 agorad/main.py`（env 带 AGORA_HOME＋AGORA_OC_URL）→轮询上限 10s→LiftTimeout；`onInject(msg)`——系统提示接入＋credential 消息提取→cred 缓存（事件形状 T5 收紧）
- 首跑体验：lift 超时拒因区分"没装 vs 拉不起"

- [ ] **Step 1: 红测** — 三路径＋10s 上限＋超时分类文案。`Req: F4`
- [ ] **Step 2-5:** 循环；commit `feat(plugin): inject/lift——.agora 探测/拉起传 env/就绪任意应答/凭据提取`

### Task 4: install.sh＋root 牌接管

**Files:**
- Create: `install.sh`
- Test: `tests/test_install.sh`（bash 临时 HOME）

**Interfaces:**
- Produces: 幂等安装：依赖检查→`$AGORA_HOME` 骨架（state/messages/wiki/.agora）→MCP 注册（mcp 段合并不覆盖）→插件复制→**root 牌接管：拉起 agorad→捕获 stdout root token 行→写 `.agora/root.token`（0600）**→冒烟（持 root 牌调 doctor）→停

- [ ] **Step 1: 红测** — 首装全件；二装幂等（他 server 不被覆盖）；缺依赖退出码；root.token 在位且 0600。`Req: F1`
- [ ] **Step 2-5:** 循环；commit `feat(install): 三部件装配＋root 牌接管＋幂等＋持牌冒烟`

### Task 5: 真 opencode API 对账（七项探针）

**Files:**
- Create: `tests/e2e/oc_probe.ts`
- Modify: `agorad/access/oc.py`、`tests/stub_opencode.py`、`agora-plugin/*`（接线形状收紧）
- Test: 探针 JSON 落 `/tmp/opencode/oc-probe.json`＋①套件回归＋插件测试回归

**Interfaces:**
- 探针七项：create/inject/alive/probe 四形状；**tool.execute.before 对 MCP 工具的覆盖与改参能力**；**MCP spawn 拓扑**（每 opencode 一进程 vs 每会话）；**inject 事件在插件侧形状**＋opencode 服务端口发现（AGORA_OC_URL 来源）
- 产出：对账记录（ledger/README）；偏离→oc.py/stub 双侧修；附牌三前提判定——任一失败→BLOCKED 上报用户裁替代案

- [ ] **Step 1: 探针脚本＋实测落盘**；**Step 2: 对照契约逐项裁决**；**Step 3: 双侧修＋全绿＋接线收紧**；**Step 4: Commit** `feat(oc): 七项对账——真形状双侧适配＋附牌前提判定`

### Task 6: T2 插件强制 e2e（真 opencode，tmux）

**Files:**
- Create: `tests/e2e/t2_enforce.sh`

**Interfaces:**
- 场景七例：无凭据拒（引导文案）；持凭据放行；豁免三例；自身仓不豁免；服务停机默认拒（lift 线索）；**账本读引导**（state/messages 直读→引导）；**bash 不拦**（接受面断言——bash 直写主检出不被拦，记录在案）
- 自清理：tmux 会话杀净、任务/worktree 清、pgrep agorad 净

- [ ] **Step 1-4:** 红跑→绿→残留断言→commit `test(e2e): T2 强制七例`。`Req: F1`

### Task 7: T3 派发链 e2e（真 opencode，tmux）

**Files:**
- Create: `tests/e2e/t3_chain.sh`

**Interfaces:**
- 场景十项：S1 全链（回填→登记→done→自动合并→closed）；**S2 跨仓顺序**（前仓 merged 后后仓见新主检出）；**S3 过程沟通**（问答一轮）；杀 agorad 重启续（F6）；**杀 opencode 服务重启**（探测恢复路径）；会话死亡重注入（牌重绑链边可达）；done 崩溃窗口；**在线 ≤5s 计时断言**；连续 10 条按序；门内 cancel 排队；非 root enforce 403；**verify 驳回起重一例**
- 残留断言＋**反需求语义扫**（grep 无 cron/全局扫描/stalled 处置——t4-coverage F5 弱锚收口）

- [ ] **Step 1-4:** 红跑→绿→残留断言→commit `test(e2e): T3 十二场景全链`。`Req: F3/F4/F6/F7/F11`

---

## T4 核销

T3 绿后更新 t4-coverage.md：全部标②行落 e2e 锚（F1 插件面/读引导/F2 驳回起重/F4 ≤5s 真半边/F5 语义扫/杀 opencode），红项清零。
