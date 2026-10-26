# opencode-agora ②接入层实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通人↔agorad 的最后三件：MCP 壳（11 工具）、opencode v2 插件（hook/cred/inject/lift）、install.sh；并以真 opencode 对账 API 形状、跑通 T2 强制 e2e 与 T3 派发链 e2e。

**Architecture:** ①已并入 main 的 agorad（HTTP 127.0.0.1＋X-Agora-Token 鉴权）为服务端；MCP 壳＝stdio JSON-RPC 薄代理；插件＝TS 宿主进程内四段；e2e 以 tmux 驱动真 opencode（headless 注入不可达——v1 实证教训，家规 e2e 先例 review-loop-e2e.sh）。

**Tech Stack:** MCP 壳 Python 3 stdlib（零第三方）；插件 TypeScript on Bun；e2e bash＋tmux＋python3。

**Spec:** `docs/reqs/opencode-agora.md`（已批准）＋ `docs/superpowers/specs/2026-10-26-opencode-agora-design.md` §5/§6/§7-T2T3（已批准）＋ `docs/superpowers/plans/t4-coverage.md`（②核销清单）

## File Structure

```
agora_mcp.py            # MCP 壳：stdio JSON-RPC 循环＋11 工具→agorad HTTP 映射
agora-plugin/
├── index.ts            # 插件入口（plain-object 导出，禁 Plugin.define 包裹）
├── hook.ts             # 拦截判定链（edit/write＋读引导＋默认拒）
├── cred.ts             # 凭据读取/缓存
├── inject.ts           # 消息接入会话
└── lift.ts             # service.json 探测＋setsid 拉起＋就绪等待
install.sh              # 三部件装配＋冒烟
tests/
├── test_mcp_shell.py   # 壳对 stub agorad（复用①夹具形态）
└── e2e/                # T2/T3 脚本（bash＋tmux）
    ├── t2_enforce.sh
    └── t3_chain.sh
```

## Global Constraints

- MCP 壳：Python 3 stdlib ONLY（零第三方依赖，需求 §5）
- 插件：TS on Bun，plain-object 导出（Plugin.define 包裹形态静默失效，禁用——设计 §5）
- 铁律继承：插件拦 edit/write（bash 不拦＝已知接受面）；服务不可达默认拒（安全方向）；/view=board、/health=doctor 权限矩阵
- e2e：tmux 驱动真 opencode（headless `opencode run` 不处理注入——v1 实证）；测试自清理（会话/worktree/服务进程，残留在 CI 不可接受）
- 每测试带 `Req: F<n>` 锚；真 opencode API 形状与 stub 契约的任何偏离＝对账 finding，修 oc.py/stub 双侧
- install.sh 幂等（重复执行无副作用）

## Review Focus（五类最可能咬人的输入——各钉进所属任务）

1. MCP stdio 帧格式（LSP 头 vs 换行分隔 JSON-RPC；initialize 握手版本）——客户端连不上一切免谈 → Task 1
2. 插件在 agorad 未拉起时的首跑体验（lift 就绪等待超时→默认拒误伤合法写入）→ Task 3
3. 真 opencode API 形状偏离 stub 契约（inject 排队语义/alive 404/create 返回形状/sid 熵）→ Task 5
4. e2e 残留（opencode 会话、worktree、agorad 进程泄漏——测试跑完环境必须还原）→ Task 6/7
5. install.sh 幂等与半装态（装到一半重跑；opencode 配置已有 mcp 段时的合并不覆盖）→ Task 4

---

### Task 1: agora_mcp.py——stdio MCP 壳

**Files:**
- Create: `agora_mcp.py`
- Test: `tests/test_mcp_shell.py`

**Interfaces:**
- Consumes: agorad HTTP（①已并入：`POST /tool/<name>` JSON body＋`X-Agora-Token` 头；`GET /health`）
- Produces: stdio JSON-RPC server（newline-delimited JSON；methods：`initialize`→{protocolVersion,capabilities:{tools},serverInfo}；`tools/list`→11 工具名＋inputSchema；`tools/call`{name,arguments}→HTTP 代理＋结果/错误映射）；token 来源 env `AGORA_TOKEN`（worker 经 BOOT 注入）；AGORAD_URL 缺省 `http://127.0.0.1:<service.json port>`

- [ ] **Step 1: 写失败测试** — 对 stub agorad（复用① StubOpencode 形态或直接 mock HTTP）：initialize 握手；tools/list 恰 11 件五族；tools/call task_create→POST 透传＋token 头；agorad 401/403→JSON-RPC 错误码映射；未知工具→错误。`Req: F2/F3`
- [ ] **Step 2-5:** 循环；commit `feat(mcp): stdio 壳——11 工具映射＋鉴权透传＋错误映射`

### Task 2: 插件 hook＋cred（TS）

**Files:**
- Create: `agora-plugin/index.ts`、`hook.ts`、`cred.ts`
- Test: `agora-plugin/hook.test.ts`（bun test）

**Interfaces:**
- Produces: `shouldBlock(tool: string, targetPath: string, cwd: string, cred: Credential|null, enforceOn: boolean): {block:boolean, reason?:string}`——判定链：enforce off→放行；非 git 仓/临时目录（realpath 折叠）→放行；持活动凭据且 cwd∈env→放行；否则拒＋派发引导文案。`readCredential(sessionDir): Credential|null`
- 真实 opencode 钩子接线（tool.execute.before 事件形状）本任务以类型桩＋对账清单形式留 Task 5 实测后收紧

- [ ] **Step 1: 写失败测试（bun test）** — 判定链全分支：无凭据写 git 仓拒（含引导文案）；持凭据 cwd∈env 放行；非 git/临时目录/enforce off 放行；realpath 符号链接折叠；主会话同拦（凭据面同规则）。`Req: F1`
- [ ] **Step 2-5:** 循环；commit `feat(plugin): hook/cred——判定链＋realpath 折叠＋引导文案`

### Task 3: 插件 inject＋lift（TS）

**Files:**
- Create: `agora-plugin/inject.ts`、`lift.ts`
- Test: `agora-plugin/lift.test.ts`

**Interfaces:**
- Produces: `ensureService(): Promise<{url:string}>`——读 `$AGORA_HOME/.agora/service.json`→health 探测→未运行 setsid 拉起 `python3 agorad/main.py`→轮询就绪（上限 10s）→失败抛 LiftTimeout；`onInject(msg)`——系统提示形态接入会话（opencode 事件形状对账留 Task 5）
- **首跑体验（Review Focus 2）**：lift 超时→hook 走"服务不可达默认拒"但**拒因文案附 lift 失败线索**（区分"没装"与"拉不起"）

- [ ] **Step 1: 写失败测试** — ensureService 三路径：已运行直返；未运行拉起→就绪（真起一个 stub agorad 进程形态）；拉起失败→LiftTimeout；10s 上限。`Req: F4/B5`
- [ ] **Step 2-5:** 循环；commit `feat(plugin): inject/lift——惰性拉起＋就绪等待＋超时分类`

### Task 4: install.sh

**Files:**
- Create: `install.sh`
- Test: `tests/test_install.sh`（bash 临时 HOME 夹具）

**Interfaces:**
- Produces: 幂等安装：依赖检查（python3/git/opencode/bun）→`$AGORA_HOME` 骨架（state/messages/wiki/.agora）→MCP 注册（opencode 配置 mcp 段**合并不覆盖**）→插件复制到 `~/.config/opencode/plugins/`→冒烟（起 agorad→doctor→停）
- 幂等断言：连跑两遍 diff 为空；半装态（删一半重跑）收敛

- [ ] **Step 1: 写失败测试（bash）** — 临时 HOME：首装全件在位；二装幂等（配置 mcp 段已有其他 server 不被覆盖）；缺依赖退出码＋提示。`Req: §6`
- [ ] **Step 2-5:** 循环；commit `feat(install): 三部件装配＋幂等＋冒烟`

### Task 5: 真 opencode API 对账（oc.py/stub 双侧修）

**Files:**
- Modify: `agorad/access/oc.py`、`tests/stub_opencode.py`（按实测形状）
- Test: 对账脚本 `tests/e2e/oc_probe.ts`（真 opencode serve 起后跑）＋①套件回归

**Interfaces:**
- Produces: 对账清单落账（README 或 ledger）：create/inject/alive/probe 四形状实测记录；stub 修正为真形状；偏离项→oc.py 适配；**inject 忙时排队语义**（连续 10 条实测）、**alive 404 vs 200/{alive:false}**、**sid 形态与熵**
- 真服务起法：`opencode serve`（本机）；对账只读（create 临时会话后清理）

- [ ] **Step 1: 探针脚本**（起真 opencode→四形状实测→JSON 落盘）
- [ ] **Step 2: 对照 stub 契约**——逐项记录一致/偏离；偏离→双侧修＋①套件绿
- [ ] **Step 3: Commit** `feat(oc): 真形状对账——stub/oc.py 双侧适配＋对账记录`

### Task 6: T2 插件强制 e2e（真 opencode）

**Files:**
- Create: `tests/e2e/t2_enforce.sh`（tmux 驱动）
- Test: 即脚本自身（退出码判定）

**Interfaces:**
- 场景五例（tmux 开真 opencode 会话，插件已装）：无凭据拒（会话内 edit 主检出→引导文案出现）；持凭据放行（建任务环境）；豁免三例；**自身仓不豁免**（对 opencode-agora 仓写入拒）；服务停机默认拒（kill agorad→写入拒＋lift 线索）
- 自清理：tmux 会话杀净、测试任务/worktree 经 cancel+doctor 清

- [ ] **Step 1: 脚本＋红跑**；**Step 2: 绿**；**Step 3: 残留断言**（worktree list 空、pgrep agorad 净）；**Step 4: Commit** `test(e2e): T2 强制五例——tmux 驱动真 opencode`
- `Req: F1`

### Task 7: T3 派发链 e2e（真 opencode 半边）

**Files:**
- Create: `tests/e2e/t3_chain.sh`
- Test: 即脚本自身

**Interfaces:**
- 场景：S1 全链（真建任务→dispatch→worker 真会话干活→回填 verify→done→自动合并→closed）；S2 跨仓顺序（两 tmp 仓，前仓 merged 后后仓见新主检出）；杀 agorad 重启链续（F6）；会话死亡重注入（牌重绑后链边可达）；done 崩溃窗口（迁移中 kill -9→重启不双死等）；接收方繁忙连续 10 条按序（对账 Review Focus 3 的 e2e 面）；门内 cancel 排队生效；非 root 牌 enforce 403
- 自清理同 T2；全程 tmux 驱动，脚本退出码即判定

- [ ] **Step 1: 脚本＋红跑**；**Step 2: 绿**；**Step 3: 残留断言**；**Step 4: Commit** `test(e2e): T3 派发链八场景——真 opencode 全链`
- `Req: F3/F4/F6/F7/F11`

---

## T4 核销

T3 绿后：更新 `docs/superpowers/plans/t4-coverage.md`——全部标②行落 e2e 用例锚，红项清零（F1 插件面/F4 真 opencode 半边/F5 e2e 等）。
