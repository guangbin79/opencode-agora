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
- D5 环境与生命周期（2026-10-26 十二问批次）：worktree 铁律形态；跨仓＝甲案顺序编排（无复合环境）；F5 只管死不管"僵"（无 stalled 自动处置）；worktree 回收＝merged/cancelled 即删、blocked 保留至处置、孤儿永不自动删；操作面＝MCP 工具唯一（无 slash 命令/恢复命令/删除）；会话不复用；消息拓扑＝链边；done 受理前置＝verify 登记＋评审环绿

builder 裁决（待用户否决）：
- B1 MCP 工具面 11 件五族：任务生命周期 `task_create/task_dispatch/task_update/task_cancel`；消息 `post/check_mail`；查询 `board/doctor`；知识层 `wiki_read/wiki_write`（拒写 `wiki/tasks/` 前缀）；强制 `enforce(on|off)`（服务持旗、仅主会话可开、开/关写审计）。死 `sync`/`redispatch`（恢复自动化接管，重派=对同任务再 task_dispatch）；删 `attach`（无需求锚）/`merge`（F7 自动门覆盖，留着只是攻击面）/`who`/`history`（并入 board）
- B2 既有资产处置（无迁移）：`~/plaza` wiki 保留；`log/`+`board/` 原地归档；任务状态从零
- B3 强制豁免面沿用现 enforcer 语义：非 git、`/tmp/opencode/`、显式 OFF（仅人经主会话可开、开/关写审计）；opencode-agora 自身仓不豁免
- B4 客户端↔服务：`127.0.0.1` HTTP JSON（MCP 用 urllib、插件用 fetch）；`/view` 提供只读结果报告页（承接 agora_http.py 职责）
- B5 服务生命周期：客户端惰性拉起（setsid+单实例锁+就绪探测），不依赖 systemd/cron
- B6 隔离环境＝worktree 铁律形态：`../<REPO>__wt-task-<id>__<MMDDHHMM>` 主仓同级（每任务仓一个）；跨仓按依赖序拆单仓任务（甲案）；建/清/并归服务——回收＝merged/cancelled 即删（`worktree remove --force`＋`branch -d` 级联），blocked 保留，孤儿目录永不自动删（doctor 报告）；建环境含评审钩装配（hooks/插件），done 前置查 `.review-loop` 锚

## 3. 架构总览

```
opencode v2 共享服务（agent 运行时；会话持久化、可随时重启）
 ├── 会话：主会话(人) · dispatcher · worker…
 │     └── agora 插件(TS)：强制钩子 + 接收注入 + 惰性拉起服务
 ├── agora_mcp.py（MCP 薄壳：11 工具 → 服务 API 代理，无自身逻辑）
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

**实现三层**：①插件层（TS，宿主进程内：拦截/注入接收/惰性拉起——闸与注入必须住在宿主内）②agorad 服务层（全部权威逻辑）③MCP 壳层（11 工具薄映射，零逻辑）。
**agorad 内部＝域核心＋三访问层**：域核心（`state/task/msg/gate/health/recover/audit/report`，纯逻辑零 I/O；模块间不互调，经 `state` 串行；假 oc/store/gitx 注入即可纯内存单测）＋北向 `oc`（opencode API 唯一封装）＋南向 `store`（账本唯一磁盘接触点：atomic_write/append_only/load/flock 四原语，stdlib 文件 JSON；升级触发器＝千级任务/复杂查询/第二写者进程→sqlite3）＋`gitx`（worktree 建/清、ff-only 合并、回滚——共享资源，失败域独立于 store）＋`httpd`（入向 JSON 口，域核心的皮）。

## 4. agorad 规格

### 4.1 状态模型（F2）
tasks.json：`{tasks: {<id>: {title, project_dir, env_dir, verify, state, dispatcher_sid?, worker_sid?, credential, chain_depth, fail_streak, created, updated}}}`。
状态机（内存直接迁移，无推导）：`todo → dispatched → running → done → merged → closed`；分支 `blocked / cancelled / verify_failed / reopened`；verify_failed（附输出尾部）/reopened→通知 worker 续修、再 done 重走 verify 门，两者计同任务连败、连续 3 次→blocked。fail_streak：verify_failed/集成 verify 败 reopened 时 +1、verify 绿或对同任务再 dispatch 清零、≥3→blocked（持久化于 tasks.json，kill -9 不丢）。blocked 仅经 cancel（→cancelled）或对同任务再 dispatch（→dispatched，签新凭据、旧凭据即废）离开。merged→closed：环境回收完成时服务置位。board 与结果报告 = 状态视图函数。单写者使配额/TOCTOU 退化为内存计数。

### 4.2 派发（F3）
task_create：建任务页（wiki/tasks/<id>.md，服务生成 frontmatter）→ 建环境（B6）→ 签发凭据。verify 创建时可空：worker 勘察后回填（agora_post），派发方校验（可执行＋需求覆盖）后经 task_update 登记；登记即锁定——修改权属派发方，worker 修改拒绝并审计；verify 未登记 done 拒收。
task_dispatch：opencode API `create` 会话 → 注入 BOOT（任务页路径+环境路径+凭据+行为合同）→ 登记 dispatcher_sid/worker_sid → 审计 dispatched。保险丝：can_spawn（名册）+ 链深（chain_depth<5）+ 并发配额（内存计数）。会话不复用——一任务一会话一环境一凭据。
task_cancel：人（任意在飞或 blocked 链）或 dispatcher（自己派出的子链）中止——级联停止在飞 worker（实时终止通知）、任务置 cancelled、环境即回收；已 merged 不回滚。cancelled 即凭据作废＋配额即释放；verify/合并门临界段内 cancel 排队、门走完生效；空链 cancel 幂等＋审计；不做会话强杀。

### 4.3 消息路由（F4）
worker→dispatcher：`agora_post`（正文进消息库）→ 服务探对方活 → opencode API `inject` 实时投递（≤5s）；不活落库，对方恢复/拉取时补交。反向同构。里程碑同时迁移状态+审计；过程消息只进消息库。寻址仅链边（上级/直下级），无广播、无兄弟直连；兄弟协作经共同上级中转。无 ack。

### 4.4 健康检查（F5）
内置定时器（默认 5min）只做探活：对每个有在飞下级的 dispatcher 探直接下一跳会话存活（opencode API status）。下级死→按 4.6 恢复；恢复失败/dispatcher 死→沿链上报并实时通知主会话。"僵"（活着但无进展）不自动处置：worker 每次调用服务刷新 last_seen，board/doctor 展示最后活动时间，人或上级查询判断、人工 cancel/重派。升级性事件（dispatcher 死亡、质量失败 3 连败、重注入失败、闸体变更）同路径实时通知主会话；升级通知正文由服务权威合成，worker 供给文本（输出尾部等）以不可信数据形态携带。无任何全局巡检与 stalled 定时处置。

### 4.5 verify 门与自动合并（F7）
done：worker post(done, commit) → 服务在 env_dir 真跑 verify → 败：verify_failed+输出尾部实时回 worker。绿：自动 ff-only 合并 → 目标仓集成 verify → 败：`reset --hard` 原子回滚（并发推进守卫）+reopened+实时通知 worker 续修（环境保留，再 done 重走本门；连败 3 次→blocked+通知主会话）；绿：merged、通知 worker、环境即回收。任务 diff 触及构建/测试配置（闸体）→升级事件拍人。

### 4.6 恢复协调（F6）
启动：重载 tasks.json → 对每条 in-flight 链探活 → worker 死：先复查任务状态（cancelled/merged/closed/blocked 不重注入，blocked 等人处置），活动在跑则按任务页+环境现场重 create+inject（同一凭据）；dispatcher 死：通知父/根。常驻期周期探 opencode health，捕捉其中途重启走同一恢复路径。worker 侧调用失败本地排队、服务回来重放。

### 4.7 审计与结果报告（F8/F10）
events.jsonl：`{ts, actor, event, task?, commit?, note?}` 只追加。`/view` 页 + `board` 工具给出已合并/在飞/失败/阻塞/已取消汇总与明细（verify_failed/reopened 归在飞），按 dispatcher 可展开链级视图。

### 4.8 数据面格式与并发
- 布局 `$AGORA_HOME`：`.agora/service.json`（{pid,port,started,version}；127.0.0.1 单机单人无 token）＋`state/tasks.json`＋`state/mail_read.json`（{sid: last_read_ts}）＋`state/messages/<msg-id>.md`（不可变；frontmatter：id/ts/task/kind=process|milestone/from/to）＋`events.jsonl`＋`wiki/`（git 仓，知识层）。
- tasks.json：`{version:1, tasks:{<id>:{title,goal,project_dir,env_dir,verify:{cmd,state:pending|registered,registered_by,registered_at},state,dispatcher_sid?,worker_sid?,credential,chain_depth,fail_streak,last_seen,created,updated}}}`——version 为 schema 演进唯一伏笔；last_seen＝隐式心跳；全量原子写（数据小，不搞增量）。
- 双源规则：wiki 任务页＝tasks.json 渲染视图（服务独占刷新），权威只在 tasks.json。
- 并发三防线：flock 单实例（进程间无并发）；httpd 单线程串行＋长操作（verify/合并）转后台线程、状态迁移全过 `state.transition()` 一把锁（进程内）；原子写使文件读者永见完整快照（运行期无人读文件——视图出自内存，文件仅启动重载读一次）。
- 账本不 git 化：events.jsonl 只追加自身即历史，git 化＝高频写放大＋崩溃推理变难（v1 账本推导已退役）；wiki/ 保持 git（人写低频内容）。
- `$AGORA_HOME` 默认 ~/plaza 保留（语义连贯、改名零收益）；改名通道＝环境变量。

## 5. opencode 插件规格（TS，plain-object 导出——Plugin.define 包裹形态静默失效，禁用）

1. 强制钩子（F1）：`tool.execute.before` 拦 edit/write/bash——目标为本机 git 仓路径且无有效凭据（cwd ∈ 该任务 env_dir）→ 拒并引导 dispatch；豁免面 = B3。主会话对仓库代码写入同拦。凭据/enforce 旗标核验服务不可达时默认拒（安全方向）。
2. 接收注入：inject 即投递，插件将消息以系统提示形态接入会话。
3. 服务拉起（B5）：任何 agora 调用前探 service.json+health；未运行 setsid 拉起 agorad 等就绪。

## 6. 部署与既有资产处置

B2 处置；`install.sh` 重写：依赖检查（python3/git/opencode）→ 建 state 目录 → 注册 MCP+插件 → 冒烟自检。退役清单：v1 全部 22 个 bash 脚本、notify-opencode.sh、agora_http.py、LINE_RE 契约、4321 行旧 smoke（以新测试取代）、cron 条目（已停）。

## 7. 测试与验收（对应需求场景）

- 服务单测（stdlib unittest）：状态机、原子写/重载、路由在线/离线、verify 门、恢复路径
- 插件强制 e2e：无凭据拒/有凭据放行/豁免三例/自身仓不豁免
- 派发链 e2e（stub runtime + 真 opencode 各一轮）：S1 全链（含自动合并）；S2 跨仓顺序派发（前仓 merged 后后仓见新主检出）；杀 agorad / 杀 opencode service 各一次验证 F6；会话死亡注入验证 F5；done 前置（评审锚缺失/落后拒收、绿后带锚受理）；接收方繁忙连续 10 条注入按序全到
- 验收合同：F1-F8/F10/F11 逐条可执行验收（需求文档 §4）

## 8. 留给 builder 的自由度

端口策略（默认随机+发现文件）、审计字段微调、插件钩子事件名（以 V2 总线实测为准）、单测组织、护栏默认值（配额/预算）。

## 9. 明确不做（防复辟）

事件重放推导、多机同步、union merge 依赖、cron 自愈、全局看门狗、bash 机械层、注入式单行协议、ack 协议、人动手逃生门、会话外通知通道（v2 注入主会话即拍到）、复合环境/跨仓目录（跨仓＝顺序派发）、显式心跳与 stalled 自动处置、slash 命令面（主会话自然语言驱动）、任务记录删除、会话复用、广播与兄弟直连（拓扑＝链边）、merge/attach 工具（自动门唯一合并路径）、OFF 环境变量形态（enforce 服务持旗唯一）。
