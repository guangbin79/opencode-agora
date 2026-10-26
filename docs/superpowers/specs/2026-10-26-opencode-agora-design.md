# opencode-agora 设计——opencode v2 插件 + 常驻服务

- 日期：2026-10-26　状态：待用户批准（需求见 `docs/reqs/opencode-agora.md`）
- 取代：v1.x「git 事件账本 + bash 机械层 + cron/poll 自愈」架构（v1.40 为旧架构终版）

## 1. 意图与动机

v1 为多机同步与并发写付出了巨大复杂度（时序推导三连 bug、810 行 poll.sh、22 脚本双语言）。实际使用是单机，全自主委托要求实时双向通信与自上而下健康检查——v1 架构均做不好。v2 地基换 **opencode v2 插件体系 + 共享服务运行时**，其上只加一个 Python 3 stdlib 单进程服务。预期删掉约 2/3 代码。

## 2. 已定决策

用户定：
- D1 地基 = opencode v2 插件 + 共享服务运行时；其上 Python3 stdlib 单进程服务（agorad）
- D2 权威状态归服务；审计降级为只写 JSONL（账本推导退役）
- D3 接受常驻服务（「无守护进程」废弃）；多机机制全线退役
- D4 全自主委托：派发无确认门、verify 绿自动合并、F9 逃生门砍（见需求 §2/§4）

builder 裁决（待用户否决）：
- B1 MCP 工具面 13 件：`task_create/dispatch/post/check_mail/board/history/wiki_read/wiki_write/attach/merge/cancel/who/doctor`；死 `sync`、`redispatch`（恢复自动化接管，手动重派=对同任务再 dispatch）；`merge` 升格为工具（服务执行）
- B2 既有资产处置（无迁移）：`~/plaza` wiki 保留；`log/`+`board/` 原地归档；任务状态从零
- B3 强制豁免面沿用现 enforcer 语义：非 git、`/tmp/opencode/`、显式 OFF；opencode-agora 自身仓不豁免
- B4 客户端↔服务：`127.0.0.1` HTTP JSON（MCP 用 urllib、插件用 fetch）；`/view` 提供只读结果报告页（承接 agora_http.py 职责）
- B5 服务生命周期：客户端惰性拉起（setsid+单实例锁+就绪探测），不依赖 systemd/cron
- B6 隔离环境沿用 worktree 铁律：`../<REPO>__wt-<task>__<MMDDHHMM>` 主仓同级；建/清/并归服务

## 3. 架构总览

```
opencode v2 共享服务（agent 运行时；会话持久化、可随时重启）
 ├── 会话：主会话(人) · dispatcher · worker…
 │     └── agora 插件(TS)：强制钩子 + 接收注入 + 惰性拉起服务
 ├── agora_mcp.py（MCP 薄壳：13 工具 → 服务 API 代理，无自身逻辑）
 └── agorad（Python3 stdlib 单进程，新写）
       状态权威 · 派发执行 · 消息路由 · 自上而下健康检查
       · verify 门与自动合并 · 恢复协调 · 审计 JSONL · /view
数据：$AGORA_HOME（默认 ~/plaza）
 ├── state/tasks.json        权威状态（原子写）
 ├── state/messages/<id>.md  消息库（每条一文件）
 ├── events.jsonl            审计（只追加，只写不解析）
 ├── wiki/…                  知识层（git markdown，沿用 v1 格式）
 └── .agora/service.json     端口发现
```

依赖方向：插件/MCP → agorad → opencode API。agorad 挂：在跑 worker 代码在环境里不丢，工具调用排队重放；opencode 挂：会话自持久化，agorad 探测并协调恢复。

## 4. agorad 规格

### 4.1 状态模型（F2）
tasks.json：`{tasks: {<id>: {title, project_dir, env_dir, verify, state, dispatcher_sid?, worker_sid?, credential, chain_depth, created, updated}}}`。
状态机（内存直接迁移，无推导）：`todo → dispatched → running → done → merged → closed`；分支 `blocked / cancelled / verify_failed / reopened`；reopened→通知 worker 续修、再 done 重走 verify 门，verify_failed/reopened 计同任务连败、连续 3 次→blocked。board 与结果报告 = 状态视图函数。单写者使配额/TOCTOU 退化为内存计数。

### 4.2 派发（F3）
task_create：建任务页（wiki/tasks/<id>.md，服务生成 frontmatter）→ 建环境（B6）→ 签发凭据。verify 缺失拒建；verify 修改权属派发方，worker 修改拒绝并审计。
dispatch：opencode API `create` 会话 → 注入 BOOT（任务页路径+环境路径+凭据+行为合同）→ 登记 dispatcher_sid/worker_sid → 审计 dispatched。保险丝：can_spawn（名册）+ 链深（chain_depth<5）+ 并发配额（内存计数）。
cancel：人（任意在飞链）或 dispatcher（自己派出的子链）中止——级联停止在飞 worker（实时终止通知）、任务置 cancelled、环境入 GC 宽限；已 merged 不回滚。

### 4.3 消息路由（F4）
worker→dispatcher：`agora_post`（正文进消息库）→ 服务探对方活 → opencode API `inject` 实时投递（≤5s）；不活落库，对方恢复/拉取时补交。反向同构。里程碑同时迁移状态+审计；过程消息只进消息库。无 ack。

### 4.4 健康检查（F5）
内置定时器（默认 5min）：对每个有在飞下级的 dispatcher 探直接下一跳（opencode API status）。warn(20min)→inject 提醒；crit(40min)→先重注入恢复下级，连续 3 次→blocked+实时通知 dispatcher。dispatcher 死→沿链上报，根链通知主会话。升级性事件（verify/reopened 3 连败、重注入失败）同路径实时通知主会话。无任何全局巡检。

### 4.5 verify 门与自动合并（F7）
done：worker post(done, commit) → 服务在 env_dir 真跑 verify → 败：verify_failed+输出尾部实时回 worker。绿：自动 ff-only 合并 → 目标仓集成 verify → 败：`reset --hard` 原子回滚（并发推进守卫）+reopened+实时通知 worker 续修（环境保留，再 done 重走本门；连败 3 次→blocked+通知主会话）；绿：merged、通知 worker、环境入 GC 宽限。

### 4.6 恢复协调（F6）
启动：重载 tasks.json → 对每条 in-flight 链探活 → worker 死：按任务页+环境现场重 create+inject（同一凭据）；dispatcher 死：通知父/根。常驻期周期探 opencode health，捕捉其中途重启走同一恢复路径。worker 侧调用失败本地排队、服务回来重放。

### 4.7 审计与结果报告（F8/F10）
events.jsonl：`{ts, actor, event, task?, commit?, note?}` 只追加。`/view` 页 + `board` 工具给出已合并/在飞/失败/阻塞/已取消汇总与明细（reopened 归在飞），按 dispatcher 可展开链级视图。

## 5. opencode 插件规格（TS，plain-object 导出——Plugin.define 包裹形态静默失效，禁用）

1. 强制钩子（F1）：`tool.execute.before` 拦 edit/write/bash——目标为本机 git 仓路径且无有效凭据（cwd ∈ 该任务 env_dir）→ 拒并引导 dispatch；豁免面 = B3。主会话对仓库代码写入同拦。
2. 接收注入：inject 即投递，插件将消息以系统提示形态接入会话。
3. 服务拉起（B5）：任何 agora 调用前探 service.json+health；未运行 setsid 拉起 agorad 等就绪。

## 6. 部署与既有资产处置

B2 处置；`install.sh` 重写：依赖检查（python3/git/opencode）→ 建 state 目录 → 注册 MCP+插件 → 冒烟自检。退役清单：v1 全部 22 个 bash 脚本、notify-opencode.sh、agora_http.py、LINE_RE 契约、4321 行旧 smoke（以新测试取代）、cron 条目（已停）。

## 7. 测试与验收（对应需求场景）

- 服务单测（stdlib unittest）：状态机、原子写/重载、路由在线/离线、verify 门、恢复路径
- 插件强制 e2e：无凭据拒/有凭据放行/豁免三例/自身仓不豁免
- 派发链 e2e（stub runtime + 真 opencode 各一轮）：S1 全链（含自动合并）；杀 agorad / 杀 opencode service 各一次验证 F6；stall 注入验证 F5
- 验收合同：F1-F8/F10 逐条可执行验收（需求文档 §4）

## 8. 留给 builder 的自由度

消息文件 schema、service.json 字段、端口策略（默认随机+发现文件）、审计字段微调、插件钩子事件名（以 V2 总线实测为准）、单测组织、agorad 分模块、护栏默认值（配额/预算）。

## 9. 明确不做（防复辟）

事件重放推导、多机同步、union merge 依赖、cron 自愈、全局看门狗、bash 机械层、注入式单行协议、ack 协议、人动手逃生门、会话外通知通道（v2 注入主会话即拍到）。
