# opencode-agora 需求

- 日期：2026-10-26　状态：已批准（用户 2026-10-26）
- 前身：`docs/reqs/agora-v2.md`（v1 概念基底稿）已废弃删除——本稿为 greenfield 重写
- 约定：实现与测试携带 `Req: F<n>` 锚；验收标准均为可执行判据

## 1. 问题定义

一个开发者 + 多个 AI agent，单机并行开发多个 git 项目。四个痛点：

1. agent 直接改主检出——工作区污染、改动无隔离
2. 跨项目任务没有编排——谁在干什么、干到哪了，说不清
3. 派出去的 agent 是黑盒——失联、卡死、跑飞，靠人肉发现
4. 人↔agent、agent↔agent 没有实时沟通通道

**opencode-agora = opencode v2 上的多 agent 委托协作层**：强制隔离、全程留痕、双向实时、自愈。

## 2. 用户与角色

单机、单人类用户。

| 角色 | 定义 |
|---|---|
| 人 | 唯一决策者，经 opencode 主会话驱动一切 |
| dispatcher | 主会话（或其派生会话）作为执行臂，可派发任务 |
| worker | 被派发的会话，只在专属隔离环境中干活 |

**自主度（用户 2026-10-26 裁定）：全自主委托**——人只说目标、过程不介入、回头只看结果。推论：派发无确认门；**verify 绿即自动合并**（不等人点头）。**失败分两层（2026-10-26 顶层确认）**：自愈层（链自己救回来的）静默、仅在汇总呈现；失控层（blocked 及一切升级性事件）**当时实时拍人**，且照样进汇总——通知是失控子集的实时副本，汇总是全集。

**人中止权（2026-10-26 顶层确认）**：人可随时中止任一在飞派发链（级联其下级）；中止属决策级动作，不构成过程介入。

已知的风险转移（用户知情接受）：无人把关后，**verify 命令是唯一质量闸**；兜底 = F7 原子回滚 + F8 完整审计。

## 3. 核心场景（验收以此为纲）

| 场景 | 一句话 |
|---|---|
| S1 单仓委托 | "修 X" → 建任务+隔离环境+派发 → 干活 → verify → 自动合并 |
| S2 跨仓委托 | 任务跨两仓 → 按依赖顺序各自派发（前仓合并后后仓再跑），链上可见 |
| S3 过程沟通 | worker 发现歧义 → 实时问 → 实时答 → 继续，不挂起 |
| S4 失联自愈 | worker 卡死 / 任一服务重启 → 链上自动发现、恢复或上报 |
| S5 强制兜底 | 任何会话绕过直改仓库 → 拦下并引导正道 |
| S6 回看结果 | 人回来一句"进展如何" → 已合并/在飞/失败/阻塞/已取消 一目了然 |

## 4. 功能需求

### F1 强制隔离执行（S5）
git 仓的代码写入只发生在委托的专属隔离环境（**git worktree，形态/命名/清理以全局 AGENTS.md worktree 铁律为唯一真源**——语义约束：Repo 同级父目录保证 `../` 相对拓扑等价、线性历史、用毕级联清理）内；会话须持有效凭据（活动任务 + 环境归属，cwd ∈ 该环境），否则在写入工具执行前拒绝并引导派发（拦 edit/write；bash 不拦——同机直写为已知接受面，兜底＝verify 门＋审计＋纪律层）。豁免面：非 git 目录、临时目录、enforce(off)（服务持旗，仅 root 牌可开、开/关写审计）。账本面（$AGORA_HOME）：写一律经服务；读分三档——events.jsonl/wiki 裸读开放，tasks.jsonl 与 messages/ 经工具面（越权读禁），主会话例外全开。**API 调用方认证**：每会话一牌（worker 随 BOOT、dispatcher 随派发登记、主会话 root 牌——root 是角色非单例，多主会话各持牌），服务对牌办事。
**验收**：无凭据写 git 仓必拒且含引导；持凭据 cwd∈环境放行；豁免面三例放行；opencode-agora 自身仓不豁免（自食狗粮）；非主会话开 OFF 不生效（非 root 牌调 enforce 必拒）；OFF 开/关均有审计行；越权读 messages/tasks.jsonl 经工具面拦截。

### F2 任务登记（S6）
每个委托有身份（id）、状态、归属链记录。任务为**平表**（无 deps/parent——编排顺序由 dispatcher 的派发与消息节奏表达）。verify（真实可执行的验收判据）由 **worker 进环境勘察后回填**，经派发方校验（可执行性＋需求覆盖）确认后登记；登记即锁定——**worker 不得修改自己任务的 verify**，修改权属派发方，修改写审计。登记字段（语义清单，schema 归设计）：id · 目标 · 仓 · 环境 · verify（含登记状态）· 状态 · 归属（dispatcher/worker 会话）· 链深 · 连败 · 当前凭据 · 时间戳；**交互原文进消息库、历史事件进审计，任务页只存当前态＋锚**。
**验收**：任务创建后 id/状态/归属可查；状态迁移由服务权威记录；worker 回填→派发方确认登记、驳回起重各一例；登记后 worker 改 verify 必拒且审计留行。

### F3 派发与链
dispatcher 经服务派发 worker（opencode API create+inject）；支持嵌套派发，**链深上限**（默认 5）与并发配额（默认值 builder 定）硬约束。会话不复用——一任务一会话一环境一凭据（生死绑定）；跨仓任务按依赖顺序编排：前仓 merged 后 dispatcher 才派后仓（甲案；平表无 deps，顺序由派发节奏表达）。
**验收**：正常派发链建立并登记；超深度/超配额派发必拒且报因；跨仓两任务经同一 dispatcher 派发后，board 按该 dispatcher 一次查出两任务及各自状态（S2 链上可见）；跨仓顺序派发 e2e：前仓 merged 后后仓才 dispatched，后仓经相对路径见到前仓主检出新形态。

### F4 双向实时消息（S3）
dispatcher↔worker 结构化消息，方向不限：接收方会话存活即实时注入（≤5s）；不存活则落服务端消息库，恢复后补拉（拉取工具）。里程碑事件同时迁移状态+审计；过程性消息不进状态。消息拓扑＝链边：只对上级/直下级寻址，无广播、无兄弟直连，兄弟协作经共同上级中转。无 ack 协议（inject 即送达、回 result 即回执）。失控通知（升级性事件）经同一通道实时注入主会话，注入即视为"拍到"——主会话是人的唯一入口；升级通知正文由服务权威合成，任务/worker 供给文本（输出尾部等）以不可信数据形态携带；会话外通知通道不进 v2（见 §6）。
**验收**：在线投递 ≤5s（stub runtime 与真 opencode 各一轮）；离线消息恢复后可拉到；done/blocked 均产生状态迁移+审计行；制造一例 blocked→主会话 5s 内出现通知；接收方繁忙时连续 10 条注入按序全到（stub 与真各一轮）。

### F5 自上而下健康检查（S4）
服务内置定时器只做**探活**（会话死了没，默认 5min 可配）：对每个有在飞下级的 dispatcher 只探其**直接下一跳**；无全局巡检。下级会话死亡→按 F6 恢复（重注入续跑）；恢复失败→沿链上报并实时通知主会话；dispatcher 自身死亡→沿链上报并实时通知主会话（人）。**"僵"（会话活着但无进展）不自动处置**：worker 每次调用服务即刷新 last_seen，board/doctor 展示最后活动时间，由人或上级查询判断、人工 cancel/重派。**升级性事件（失控层通知判据）初始集**：dispatcher 死亡；同一任务连续 3 次质量失败（verify_failed/集成 verify 败）；恢复重注入失败；闸体变更（任务 diff 触及构建/测试配置）。blocked 仅两条出路：cancel 终局（F11 适用于 blocked 链）或对同任务再 dispatch——重派签新凭据、旧凭据即废。
**验收**：会话死亡注入一例→走恢复或上报路径；board/doctor 可查 worker 最后活动时间；每类升级事件注入一例→主会话实时收到且汇总含对应项；全库无 cron/全局扫描/stalled 自动处置代码路径。

> **决策记录**：僵态可见性增强（2026-10-28 用户裁定）：doctor/TUI 对在飞且 >20min 无活动任务标注"僵态候选"（纯显示，非处置）；agora-dispatch 剧本加查僵纪律（超 20min 无下级进展→主动 doctor）。均不违反反需求（无定时器、无自动处置）。

### F6 崩溃恢复（S4）
权威状态持久化（append 全量快照行＋逐行 fsync，迁移先落快照后写消息）、启动重载末行快照（断尾回退；消息按 msg-id 幂等去重，丢失由补拉/重发兜底）。agorad（服务）或 opencode 服务任一重启：in-flight 链自动恢复——worker 死则按任务页+环境现场重注入续跑（同一凭据）；dispatcher 死则通知其父或根。worker 侧工具调用失败（服务暂不可用）本地排队重放。
**验收**：`kill -9` 服务→重启→状态一致+in-flight 续跑；opencode 重启→服务探测并走同一恢复路径；dispatcher 单会话死亡（服务与 opencode 均正常）→父/根收到通知。

### F7 验证门与自动合并（S1）
完成以**真实运行的 verify 命令**背书（受理前置：verify 已登记＋环境评审环绿——`.review-loop` 锚＝worktree 分支 HEAD 且无 blocker/high 残留，缺一 done 拒收并附动作指引；服务在隔离环境内执行）；绿即**自动合并**（ff-only），合并后在目标仓跑集成 verify，失败原子回滚并置 reopened。verify_failed（附输出尾部）与 reopened（环境保留）均实时通知 worker 续修，再次 done 重走完整 verify 门；同一任务连续 3 次质量失败（verify_failed 或集成 verify 败的 reopened；非 ff 拒并不计）→置 blocked 并实时通知（进失控层）。任务 diff 触及 verify 依赖面（构建/测试配置）即触发升级事件拍人（闸体变更检测）。
**验收**：verify 败→不合并、状态 verify_failed、worker 收到输出尾部；集成 verify 败→目标分支字节级回到合并前+reopened；非 ff→拒并 reopened；reopened 后 worker 收到通知且环境在位；3 连败→blocked+实时通知；合并仅由服务自动门执行（无 merge 工具面）；verify 未登记或评审环未绿 done 必拒（附动作指引：/review-loop 或待 idle 自动触发，绿后带锚重报受理）；闸体文件被任务 diff 触及→升级通知。

### F8 审计留痕
谁/何时/干了什么/证据，三份日志可对账：审计 JSONL（只追加只写）+ 项目 git 历史 + opencode 会话记录。
**验收**：任一任务可在审计日志 grep 出完整事件链且与 git 提交对应。

### F10 结果报告（S6）
人回看时一个入口给出汇总：已合并/在飞/失败/阻塞/已取消 计数与明细（verify_failed/reopened 归在飞）、每件的结论与证据指针；支持按 dispatcher 展开链级视图（承接 S2 链上可见）。
**验收**：S1 全链跑完后，报告含"已合并+结论+commit"；人为制造一例失败后报告含失败项与原因；再造一例取消后五桶计数正确；跨仓两任务可按 dispatcher 关联展开。

### F11 中止派发链（承接 §2 人中止权）
人可中止任一在飞或 blocked 派发链；dispatcher 可中止自己派出的子链（权力随链递减）。中止级联下级：在飞 worker 实时收到终止通知，链上任务置 cancelled，环境即回收；已 merged 任务不回滚（中止不逆转历史）。任务置 cancelled 即凭据作废、配额即释放（配额按活动任务计数）；verify/合并门临界段内的 cancel 排队、门走完生效；空链（无在飞任务）cancel 幂等成功＋审计；不做会话强杀（凭据作废即无害）。
**验收**：中止一条两级在飞链→全部任务 cancelled、两级 worker 均收到终止、审计含 cancel 事件；中止含已 merged 任务的链→merged 保持不变；worker 调 cancel 必拒、dispatcher 中止非自己派出的链必拒（均审计留行）；cancelled 后旧凭据写入必拒；门临界段内 cancel 于门走完生效；空链 cancel 幂等＋审计留行。

### F12 启用域（2026-10-27 用户裁定）
指定仓为根目录的 opencode 会话中 agora 整体休眠：普通工具（edit/write/read/bash）直通——不拦、不读引导、不拉起服务、不附牌；`agora_*` 工具拒绝（"此仓未启用 agora"）。名册＝`$AGORA_HOME/.agora/inactive-repos`（仓根绝对路径一行一个；**中央名册**，防仓内自声明标记解除 worker 武装）；名册外一切照常；委托 worktree 会话（git toplevel＝worktree 自身路径）不受名册影响。与 enforce off 正交（插件侧按仓日常形态 vs 服务侧全局紧急阀）。
**验收**：休眠仓会话直写放行＋agora_* 拒；名册外仓全链照常；worktree 会话闸不变；名册按精确仓根路径匹配。

> **决策记录**：保护名册（拒 task_create 目标仓）**不做**（用户 2026-10-27"启用域就够了"）；名册初始为空、装好后用户自录（2026-10-27）。

> **决策记录**：F9 逃生门（人的快速小改通道）**先不做**（用户 2026-10-26"逃生门先不要"；被拦即转派发。后需摩擦实证再议）。

### F13 参数契约硬化（2026-10-28）
post 调用参数错误频发（gt-e103ae 三连拒：verify 未登记时序、短 sha、body 混附言均被报成 anchor 问题，指引错向）。防纠双面：①壳层 schema 充实——post.kind 枚举 note/question/answer/done/verify/verify_reject（与派发 skill 词汇对齐）、body 的 done＝纯 40 位完整 sha 契约、任务 id 形态、worker 不可用工具（task_update/task_cancel/board/doctor）与 enforce（仅 root）标注于描述；②accept_done 新增 body-format 先行拒（非 40 位 hex 报格式并附收到内容），anchor 拆无锚／≠分支 HEAD／未绿各配正确指引；③httpd 客户端 kind 白名单校验（服务内部 process/milestone/cancel 直调 Router 不经此路）；④400 与 done 拒收落审计（bad_request／done_rejected 事件）——参数错误从零记录变可计量；⑤403 拒绝文案附 worker 可用面与 task_update 退路（post(kind=question) 请派发方）。
**验收**：短 sha／文本 body 的 done 得 rejected:body-format 且文案含收到内容与 40 位契约；sha≠HEAD 文案含双方 sha；未知 kind 得 400 且列举词汇；坏参调用后 events 含 bad_request；done 拒收后含 done_rejected；worker 403 文案含可用工具；全量测试绿。

## 5. 非功能需求

- 单机、零第三方依赖（服务与 MCP：Python 3 stdlib；插件：TS on Bun）
- opencode 服务 / agorad 任意重启不丢链；两进程互为看护互不纠缠
- 工具钩子拦截开销 <100ms
- 权威状态文件人类可读（JSON）
- 护栏硬边界：链深、并发配额、（builder 定）单委托预算

## 6. 非目标与反需求

非目标：多机同步、多用户、Web 管理界面、通用消息中间件、opencode 之外运行时、人动手逃生门、会话外通知通道（v2 注入主会话即视为拍到，待摩擦实证再议）。

反需求（出现即失败）：状态靠事件重放推导；cron/全局看门狗巡检；可绕过的强制；ack 类传输层协议；删除任务记录（只许 cancel/closed）；stalled 自动处置（"僵"由人或上级判断）。

## 7. 退役（无迁移）

v2 全新开发，无数据迁移：任务状态从零开始。`~/plaza` wiki 知识层保留沿用；v1 的 `log/`、`board/` 原地归档不再增长。v1 的 22 个 bash 脚本、notify 桥、HTTP 只读窗口（由服务 `/view` 承接其职责）、4321 行旧 smoke 套件全部退役。
