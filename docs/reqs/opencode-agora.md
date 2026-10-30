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

> **决策记录**：僵态自动提醒（2026-10-28 用户裁定）：health tick 顺带边沿判僵（同 doctor 口径 20min），新进入候选集→失控通道注入主会话＋可选邮件（$AGORA_HOME/.agora/email.json：to＋command，正文走 stdin；未配置退回仅主会话提醒）。F5 定时器语义扩「探活＋僵态提醒（只提醒不处置）」，反需求「stalled 自动处置」仍禁。

> **决策记录**：僵态免判口径完善（2026-10-28 用户裁定，正午 7 邮件 4-5 误报复盘）：①last_seen 只由 worker 服务调用刷新，dispatcher 查看动作不续命；②report 僵态候选对 in_gate=True 免判（门内 verify/合并静默属正常），agorad 启动清残留门闩（门线程不跨进程存活）；③health tick 复用探死 GET 读 oc 会话 time.updated/idle，updated>idle 免判＝doctor 口径减 busy 免决（F15 env mtime 保留作 doctor 纯账本兜底）；④done 滞留（锚缺失驻留待人工合并，in_gate 已落）照拍＝催合并。盲区声明：门线程挂死（verify 永不返回）随 in_gate 永久免检，属门故障类，报警另立。均只提醒不处置，反需求不变。

> **决策记录**：stalled 拍人降噪（2026-10-28 用户裁定）：僵态提醒不再会话注入（注入唤醒模型回合→桌面端「响应就绪」弹出扰人）——改走 opencode synthetic 免通知通道（`POST /session/{sid}/synthetic` resume:false，只落主会话转录、不调度执行、不弹窗）；通道不可达/异常留库补拉（F4 离线语义兜底）；审计行与邮件例外不变；其余失控类（quality/gatefiles/reinject-fail/dispatcher-dead）注入语义不变。只提醒不处置，反需求不变。

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
**验收**：中止一条两级在飞链→全部任务 cancelled、两级 worker 均收到终止、审计含 cancel 事件；中止含已 merged 任务的链→merged 保持不变；worker 中止父任务／他人任务必拒，dispatcher 中止非自己派出的链必拒（均审计留行）；cancelled 后旧凭据写入必拒；门临界段内 cancel 于门走完生效；空链 cancel 幂等＋审计留行。

### F12 启用域（2026-10-27 用户裁定）
指定仓为根目录的 opencode 会话中 agora 整体休眠：普通工具（edit/write/read/bash）直通——不拦、不读引导、不拉起服务、不附牌；`agora_*` 工具拒绝（"此仓未启用 agora"）。名册＝`$AGORA_HOME/.agora/inactive-repos`（仓根绝对路径一行一个；**中央名册**，防仓内自声明标记解除 worker 武装）；名册外一切照常；委托 worktree 会话（git toplevel＝worktree 自身路径）不受名册影响。与 enforce off 正交（插件侧按仓日常形态 vs 服务侧全局紧急阀）。
**验收**：休眠仓会话直写放行＋agora_* 拒；名册外仓全链照常；worktree 会话闸不变；名册按精确仓根路径匹配。

> **决策记录**：保护名册（拒 task_create 目标仓）**不做**（用户 2026-10-27"启用域就够了"）；名册初始为空、装好后用户自录（2026-10-27）。

> **决策记录**：F9 逃生门（人的快速小改通道）**先不做**（用户 2026-10-26"逃生门先不要"；被拦即转派发。后需摩擦实证再议）。

### F13 参数契约硬化（2026-10-28）
post 调用参数错误频发（gt-e103ae 三连拒：verify 未登记时序、短 sha、body 混附言均被报成 anchor 问题，指引错向）。防纠双面：①壳层 schema 充实——post.kind 枚举 note/question/answer/done/verify/verify_reject（与派发 skill 词汇对齐）、body 的 done＝纯 40 位完整 sha 契约、任务 id 形态、worker 不可用工具（task_update/task_cancel/board/doctor）与 enforce（仅 root）标注于描述；②accept_done 新增 body-format 先行拒（非 40 位 hex 报格式并附收到内容），anchor 拆无锚／≠分支 HEAD／未绿各配正确指引；③httpd 客户端 kind 白名单校验（服务内部 process/milestone/cancel 直调 Router 不经此路）；④400 与 done 拒收落审计（bad_request／done_rejected 事件）——参数错误从零记录变可计量；⑤403 拒绝文案附 worker 可用面与 task_update 退路（post(kind=question) 请派发方）。
**验收**：短 sha／文本 body 的 done 得 rejected:body-format 且文案含收到内容与 40 位契约；sha≠HEAD 文案含双方 sha；未知 kind 得 400 且列举词汇；坏参调用后 events 含 bad_request；done 拒收后含 done_rejected；worker 403 文案含可用工具；全量测试绿。

### F14 agora wait 阻塞谓词（2026-10-28）
agora 此前只有 MCP 工具面，bash 无可判定只读条件——bg-wait 类看门狗被迫逆向内部文件（gt-9ea9 七轮误诊、poll.sh/derive-board.sh 格式假设、gt-7db149 派发会话手搓看门狗连出两 bug）。补 CLI 谓词整类逆向消失：仓根新增可执行脚本 `agora`（Python3 stdlib），子命令 `wait <task-id> [--timeout 秒] [--once]` 阻塞等待任务终态（merged/closed/blocked/cancelled，对齐 docker wait / kubectl wait 约定）。退出码 0＝终态到达（stdout `<state> @<sha 前 12 位>`，无 commit 只打状态）；1＝--timeout 到期仍在飞（缺省 1800）或 --once 下当前在飞（stdout `in-flight: <state>`）；2＝用法错/任务未知 404/root 牌缺失/重试至超时仍不可达（stderr）。内部轮询 15s；瞬时连接拒由重试吸收。取数照抄 board_tui.py 形态（AGORA_HOME/.agora/{root.token,service.json} → POST /board），零直读状态文件、零格式逆向；状态分类与 HTTP 取数分函数留测试缝。配套：MCP wait 工具 description 消歧（本地休眠≠任务条件等待，终态等待用 CLI）；install.sh 拷贝到 ~/.local/bin/agora。明确排除：--expect 谓词参数、stall/心跳分支（归看门狗层）。
**验收**：终态四类各 exit 0 且 stdout 含状态（merged 附 sha）；--once 在飞 exit 1；--timeout 到期 exit 1；404/连接拒耗尽/用法错 exit 2；MCP schema 含 CLI 消歧行；全量测试绿。

**换模跟随补（gt-fd5bb0）**：服务杀旧拉新（端口重分配、service.json/root.token 改写）曾使 wait 对冻结旧端点重试到超时误报 exit 2。wait 轮询每轮重读 {service.json, root.token}——端口/牌换代即跟随新端点续等；指针半写/缺失（含空牌半写、非对象 JSON、非数端口、撕裂牌换行）瞬间保持旧端点，discover 出口校验端口与牌形状不使坏指针外溢 fetch；跟随形态认牌失败 401/403（撕裂对/换代窗口）按瞬态重试，超时/单查消息头报「认牌失败」与不可达区分，不跟随（直调 wait 缝）仍终局 exit 2。**验收**：service.json 指死端口 A、CLI 进程起后才改指活服务 B → wait exit 0 拿终态（拆除 reread=discover 接线该用例即红）；空牌不采纳；非跟随认牌失败与 --once 认牌失败 exit 2 不变；既有用例全绿。

### F15 env 活性进观测与僵态判据（2026-10-28）
生产实证两次（gt-ccd034 两轮误拍）：僵态基线只看 last_seen（agora 工具面调用），worker 原生干活期（TDD/编码/rebase/评审 LLM 重活）全程不碰工具面→健康 worker 被误报 stalled。把 env 工作区文件活性并入判据与观测面：report._env_activity（os.walk 目录树最新 mtime，缺失/异常 0.0，纯 stdlib）；stale_candidates 基线 max(last_seen or created, env 活性)——文件持续在写＝活着，签名不变（health 构造注入零改动）；report.detail 增 env_active_seconds_ago（env 回收/缺失省略该键）；doctor_view 的 age_rank/last_seen_rank 同口径融合（doctor 输出字段名不变，值改为融合基线）。
**验收**：env 文件持续写入（mtime 新）不进僵态候选、env 静默超 20min 进候选；detail 含 env_active_seconds_ago、env 缺失省略；doctor 排行按融合基线排序；全量测试绿。

> **决策记录**：残余限制已知不修（另行立项）：worker 处于长 LLM 推理/评审段（既无工具调用也无文件写入）时仍可能过早进候选——观测面下一步诚实化＝会话活性探针（oc.alive 类）。

### F16 sid 血统——dispatcher 换号链边连续（2026-10-28）
主会话（dispatcher）重启后真实 sid 滚动，root 牌经 rebind 跟随新 sid，但旧任务行 dispatcher_sid 仍记旧值——派发方 post 旧任务报「非链边」失声（F14 修订被迫 cancel＋重建）。设计（用户选⑤「sid 血统」）：①令牌记录身份沿革——rebind 把旧 sid 追加进该牌血统集随牌持久化（旧格式缺字段＝空集），same_lineage 提供判定；②链边校验接受血统——dispatcher 边判定扩为「sid 相等或 same_lineage」，血统判定经装配注入 Router 单点持有（worker 边不加，recover 已覆写 worker_sid），消息 from 仍记真实当前 sid。已知边角：doctor rotate_root 换新牌血统从零起、旧任务链边仍断——轮换属手动管理动作，文档化豁免不修。
**验收**：rebind 累积血统且重启后在；换号后 post 旧任务达 worker（down 边）、归因记新 sid；无血缘仍拒；worker 边不放行；常规链边零变化；全量测试绿。

### F17 hook 无凭据拒因指路 check_mail（2026-10-28；文案形态被 F26 替代）
**dropped（gt-958281 撤销）**：credential 直投失败是小概率事件（信箱账本 47 派发 0 落信箱）且 check_mail 需持牌构成死路（无牌 worker 在 MCP 壳即拒，recover 不覆盖活而无牌）；兜底=派发方查僵→task_cancel＋重建任务重派（新会话新签牌）。
生产实证（本任务派发链）：worker 会话 credential 直投失败时 hook 恒拒且拒因只给「去派发」——引导错向，worker 无法自愈。拆 shouldBlock 无凭据拒因：cred?.token 为假（本会话无凭据）→DISPATCH_HINT 追加 NO_CRED_HINT「若你是被派发的 worker：凭据可能直投失败已落信箱，先调 agora_check_mail 领牌再重试」；有凭据但 cwd∉envDir 的路径维持原 DISPATCH_HINT 不变（持牌主会话同拦／持牌服务不可达默认拒文案不变；无凭据子情形走 NO_CRED_HINT 支）。附带修复 hook.test.ts BOOT 夹具腐化：夹具 BOOT 文本旧格式「[agora] BOOT task t-b」不匹配 inject.ts BOOT_MSG（/\[agora\] (gt-\S+): /）致 envDir 未登记、合流后恒 deny——夹具对齐现行 gt- 前缀格式，断言不变，inject.ts 不动。
**验收**：cred=null 拒串含 "agora_check_mail"；有 token 而 envDir 不含 cwd 拒串严格等于原 DISPATCH_HINT；BOOT 先行＋credential 合流放行用例恢复绿；bun test agora-plugin/ 全绿。

### F18 插件侧 worker PATH 注入（2026-10-28，gt-26c027）
生产实证（gt-71b6af 踩坑）：agorad 拉起全量透传（lift.ts Bun.spawn），但 worker 会话（opencode serve 服务生成）PATH 被剥成 ".opencode/bin:/usr/local/bin:/usr/bin:/bin"——剥离在 opencode 服务层，worker 跑 bun 套件须手动补 ~/.bun/bin。用户裁决：不做 worker 自救，插件/装配侧一次做对。接缝勘察定夺（v2.2.0 二进制实证）：shell 工具与终端同经 Shell.create→trigger("shell","create.before",payload)，payload.env 按引用直改直达 spawn env——插件 setup 可选挂载 ctx.shell.hook("create.before")（旧运行时无 shell 命名空间静默跳过），spawn 前基于既有 PATH 去重前插 $HOME/.bun/bin 与 $HOME/.local/bin（不硬编码全量；幂等；payload 缺 env 安全向不动）。
**验收**：注入路径插件测试锁死（补全/去重/幂等/接线原地改 env/缺 env 与旧运行时安全向）；`grep -rqF ".bun/bin" agora-plugin/` 过；bun test agora-plugin/ 全绿；install.sh 整目录复制天然携带新模块零改动。

## 5. 非功能需求

- 单机、零第三方依赖（服务与 MCP：Python 3 stdlib；插件：TS on Bun）
- opencode 服务 / agorad 任意重启不丢链；两进程互为看护互不纠缠
- 工具钩子拦截开销 <100ms
- 权威状态文件人类可读（JSON）
- 护栏硬边界：链深、并发配额、（builder 定）单委托预算

## 6. 非目标与反需求

非目标：多机同步、多用户、Web 管理界面、通用消息中间件、opencode 之外运行时、人动手逃生门、会话外通知通道（v2 注入主会话即视为拍到，待摩擦实证再议）。

反需求（出现即失败）：状态靠事件重放推导；cron/全局看门狗巡检；可绕过的强制；ack 类传输层协议；删除任务记录（只许 cancel/closed）；stalled 自动处置（"僵"由人或上级判断）。

> **决策记录**：会话外通知通道开僵态邮件例外（2026-10-28 用户裁定，opt-in 配置，未配置零行为变化）。

## 7. 退役（无迁移）

v2 全新开发，无数据迁移：任务状态从零开始。`~/plaza` wiki 知识层保留沿用；v1 的 `log/`、`board/` 原地归档不再增长。v1 的 22 个 bash 脚本、notify 桥、HTTP 只读窗口（由服务 `/view` 承接其职责）、4321 行旧 smoke 套件全部退役。

### F19 worker 牌换号自愈——活动任务漂移即改绑（2026-10-28）
会话续接会更换调用 sid，dispatcher 侧已有 bind_main 自愈（R25-A），worker 牌无此待遇：续接 worker（token 仍有效）调 done 因牌面 sid 与任务行漂移死锁（实证 gt-f23e94：活已干完 done 死锁，靠 cancel＋cherry-pick 收割 gt-dba437）。设计（对齐派发方同款）：worker 牌调用携 X-Agora-Sid 且与注册 sid 漂移、注册 sid 是某活动任务（dispatched/running/done/verify_failed/reopened/blocked）的 worker_sid → auth.rebind（旧 sid 入血统，token 本体不变）＋任务行 worker_sid 同步覆写（与 recover.reinject 同口径）。守卫：终态（merged/closed/cancelled）与无主 sid 不触发；dispatcher bind_main 与 reinject 行为不变；改绑后 done 走既有五拒，验收一克不减。
**验收**：续接 worker 调 done 受理＋行覆写＋血统入册；无牌/他牌拒且零改绑；终态不触发改绑；reinject 与 dispatcher 改绑既有测试保持绿；全量测试绿。

### F20 账本尾读＋有界瘦身（2026-10-28，gt-a02ab1）
4.4GB 写放大事故（F5 决策记录）善后加固，F6 不改写：①启动 load_state 只 seek 末 TAIL_BYTES(1MB) 解析完整行取最后完整快照（末行胜出语义一字不变），文件 ≤阈值或尾窗无完整行回退全文件扫——启动耗时与账本大小解耦；②追加路径有界保持：行数超 COMPACT_LINES(500) 即原子重写（tmp+fsync+replace）仅留刚追加的末完整行——flock 单写者安全、时机在迁移路径避开启动；此为「账本只追加」的有界例外，快照语义（§4.8）不变。
**验收**：大文件尾读命中末行；尾窗无完整行回退；尾读断尾行回退上一完整行；超阈重写单行且无 .tmp 残留、load 往返一致；`python3 -m unittest tests.test_access_store tests.test_core_state` 绿。

### F21 dispatcher 侧换号改绑——post 非链边死号自愈（2026-10-28，gt-31ecee）
生产实证（同日两次）：MCP 壳被 harness 重启后领新 sid，对在飞旧任务 post 报「非链边」拒——任务登记 dispatcher_sid 是旧号，F16 same_lineage 只认血统（重开壳是无血统新生，永不被认），F19 改绑只覆盖 worker 侧，派发方对在飞任务失声（紧急修正只能走 cancel 重派重炮路径）。修法（方案 a，dispatcher 改绑＋死号守卫）：post 路由命中「非链边」拒绝前先判愈合条件——发送者持 root 级（主会话）牌、sid ≠ 任务登记 dispatcher_sid、且登记会话已不存在（oc 会话存在性检查，F19/recover 同款）三者全中 → auth.rebind（旧号入血统，token 本体不变）＋活动任务行 dispatcher_sid 同步覆写＋本笔 post 按链边放行；审计 event=dispatcher_rebind（含新旧 sid 与任务）。守卫（不满足保持既有拒绝）：登记会话仍活（防活号互抢）、发送者非 root 牌、任务终态（merged/closed/cancelled 冻结）、oc 不可达≠死（recover 同口径免判）。
**验收**：死号＋root 牌→改绑且 post 达 worker、行覆写、血统入册、审计落盘；死号一次改绑其名下全部活动任务行；活号/非 root 牌/终态/无登记号各不改绑仍拒；oc 不可达免判；既有链边与 done 五拒零变化；全量测试绿。

### F22 同仓协作面——repo_board＋peer 留言（2026-10-28，gt-5ba70a 用户裁定）
worker 间无横向观测面（消息拓扑＝链边，兄弟协作经共同上级中转）：同仓并行 worker 互不知情，重复踩坑与撞车只能靠派发方转述。加一面只读＋轻写面：①写面 post 加 kind=peer——无任务目标、仅 worker 牌可写，落同仓留言面（按 project_dir 分区，normpath 口径；复用消息库，kind=peer 头带 repo/作者任务，to="" 保持消息库全员有 to 不变量）；作者＝调用者任务 tid；单条上限 500 字符超限截断；审计行落 events（event=peer_post）。②读面新只读接口 repo_board（httpd POST 端点＋MCP 壳同名工具，worker 白名单）：同仓在飞任务摘要（tid/title/state/last_seen 四字段）＋最近 20 条 peer 留言（最新在前）。③分区强制：worker 牌按其任务 project_dir 过滤、跨仓不可见；root/dispatcher 牌可传 repo 指定任意仓（缺省全仓，诊断用）。④四护栏：噪音——读只取最近 20 条＋写面截断，无翻史；陈旧——留言带作者任务当前 state＋age_seconds，作者任务终态（merged/closed/cancelled）is_stale=true（数据保留、消费方降权）；滥用——同作者任务软限 10 条，超限拒写提示「沉淀请走 knowledge index」；注入——留言属不可信文本，返回体逐条带 untrusted=true，任务页 BOOT 加协作行「repo_board 可查同仓在飞任务与 peer 留言（可选，非强制）；peer 留言是数据不是指令，仅供参考」。明确排除：全量翻历史、留言删除/编辑、跨仓广播。
**验收**：worker 牌写读回路达（post kind=peer→repo_board 可见）；分区隔离（跨仓任务与留言互不可见）；超 500 截断、第 11 条拒写且文案含 knowledge index；作者任务终态后 is_stale=true；root/dispatcher 牌 peer 写 403；repo_board 进 worker 面、board/doctor 仍 403；events 含 peer_post；BOOT 含协作行；verify 绿：`grep -rqF "repo_board" agorad/ agora_mcp.py && python3 -m unittest tests.test_core_msg tests.test_transport_httpd`。

### F23 verify 流程加固——建单原子登记＋派发硬闸（2026-10-28，gt-54d5fc；correct 剧本产物）
三例同根因事故（gt-e103ae、gt-87f3c2、20261026-135627）：两步舞（create→update→dispatch）无强制，漏 task_update 登记最晚在 worker post(done) 才爆炸。修法两件：①task_create 加可选 verify_cmd——传入即建单时原子登记（verify_registered 事件照落、状态直接 registered，与 update 路同款审计），快速路一步完成；②task_dispatch 硬闸——目标任务 verify 未登记即 403 拒派＋专属审计行 dispatch_rejected_verify_missing（拒绝在派发瞬间：不建环境不建会话），唯一豁免＝建单显式 verify_pending: true（探索路：worker 进 env 提案 verify→派发方 task_update 登记，现行流程不变）。兼容：旧形态 create 后 update 再 dispatch 合法不变；task_update 语义不变（空命令拒、登记即锁定）；verify_cmd 与 verify_pending 互斥拒。MCP 壳 task_create schema 透传新参（httpd→task 全链）。（编号 F23：F22 已被 gt-5ba70a 同仓协作面占用，2026-10-29 rebase 撞号让位）
**验收**：原子路 create(verify_cmd)→registered＋事件落盘＋dispatch 放行；硬闸 create（无 verify 无旗）→dispatch 403＋审计行＋零副作用（不建环境不建会话）；豁免 create(verify_pending=true)→dispatch 放行、后续 update 登记照常；事故复刻 gt-87f3c2 形态（create 后漏 update 即 dispatch）必被拒；`python3 -m unittest tests.test_core_task tests.test_transport_httpd` 绿；全量绿。

### F24 任务 id 前缀 task-＋历史 gt- 终身双兼容（2026-10-29，gt-99ae7b 用户裁定）
新任务 id 生成前缀从 gt- 换 task-（望文生义；6 hex 长度不变）。前缀是契约词汇散布多个校验/解析面，存量 75+ 任务终身 gt-*，所有输入/解析面双形态同收：①生成 task.py _new_tid；②after 链输入面（存在性校验前缀无关，测试锚定旧 id 全链）；③MCP 壳 schema 描述与 agora CLI help 标注双形态；④高危点插件 inject BOOT 正则双前缀——漏改＝worker 凭据登记断链；⑤e2e 断言面（t3_chain.sh S1 startswith 双前缀，ocr 评审补册）。
**验收**：新 tid 命中 ^task-[0-9a-f]{6}$；旧 gt- id 作 after 建单＋派发全链通；插件 BOOT 两前缀用例绿；`python3 -m unittest tests.test_core_task tests.test_transport_httpd` 绿。
### F25 worker 凭据周期重注入自愈（2026-10-29，task-4d9fff）
worker 会话插件凭据缓存仅驻内存（inject.ts onInject 收 BOOT/credential 才缓存 envDir/token），会话进程或插件宿主重载即清零——hook 判 cwd 非任务 env 全拦，worker 僵死（gt-fd5bb0 僵死取消重派；gt-99ae7b 靠 mcp_shell git apply 通道自救）。F21 只修 dispatcher 侧换号，worker 侧缺口两次实锤。修法（health tick 周期性幂等重注入，走静默通道）：对每个在飞（dispatched/running/done，终态不注入）且有 worker_sid 的任务，每 N 秒（缺省 600，AGORA_CRED_REINJECT_INTERVAL 可配）重发 dispatch 同形凭据（"[agora] credential {tid}\ntoken: {tok}"）；通道用 oc.inject_silent（synthetic resume:false，落转录不调度模型回合＝零上下文污染，插件 onInject 照常命中重缓存）；牌经 auth.token_for 只读取（不改绑不轮换）；通道失败（契约 False 与抛异常两形态）走留库兜底口径不抛（check_mail 补拉）；防噪：不逐轮落审计事件、oc 不可达轮次整体免判（同僵态口径）、崩溃回退多注一次（幂等无害）。
**验收**：首轮到期即注且 dispatch 同形＋免通知通道；周期内不重发、到期再触发；重复到达牌与任务行不变；终态/无 worker 不注入；通道失败两形态 tick 不抛且留库可补拉；零审计行；`grep -rqF "reinject_cred" tests/ && python3 -m unittest tests.test_core_recover tests.test_core_health` 绿。

**修正记录（2026-10-29，task-02b208）：推送通道证伪，推改拉。** 实证修正（task-fb8b01 worker＋task-4d9fff 复盘）：inject_silent（synthetic）在 opencode v2.2.0 明示不触发插件 session.prompt 钩子——本条 600s 周期重注入推不进插件缓存，等效失效；且存在「出生即空」竞态（会话创建期注入的消息转录可见、location=env 正确，但插件加载与消息注入竞速致钩子未捕获），已致多名 worker 恒拒。处置：①移除 health._cred_reinject 周期推送（_CRED_STATES/_CRED_INTERVAL/AGORA_CRED_REINJECT_INTERVAL 一并退役；stalled 的 synthetic 免通知通道不受累），拉取式覆盖其全部目标；②agorad 新端点 POST /cred_for_sid {sid}——sid 是某在飞（非冻结）任务 worker_sid 且牌活 → 200 {tid, env_dir, token}，否则 404；无牌调用放行（插件拉取时无凭据是常态；server 仅绑 127.0.0.1＝回环单用户 plaza 信任模型，与既有 localhost 面同口径）；每请求落一行审计（event=cred_pull，防噪归插件节流）；③agora-plugin hook write 工具判定遇缓存未命中→节流拉取（每 sid 每 30s 至多一拉——按 sid 而非进程级，防同宿主多 worker 跨会话互堵；短超时 2s）→命中填缓存放行，未命中/失败走现有默认拒文案。
**验收（修正后）**：/cred_for_sid 命中与 404（未知 sid/牌废）、审计行；hook 未命中→拉取→放行全链（mock 端点）＋节流＋形状缺/服务异常安全向 null；`grep -rqF "cred_for_sid" agorad/ agora-plugin/ && cd agora-plugin && bun test` 绿；`python3 -m unittest tests.test_transport_httpd tests.test_core_health` 绿。

**修正记录（2026-10-30，ag-8fb642）：拉取触发判定补齐逆半条缓存。** 代码级根因（ag-442743 探针＋本机 serve v2.2.2 实证）：「出生即空」竞态定位到 opencode 平台层——PluginHooks.Service 是 per-location 注册表（core plugin/hooks.ts `makeLocationNode`），SessionPrompt.prepare 只等全局 Plugin.awaitActivation，不等本 location 激活（supervisor 激活另带 100ms debounce）——新建 env worktree 的冷 location 上 dispatch BOOT prompt 的 session.prompt 触发为静默 no-op，credential prompt 后至（激活已完成）正常入缓存→缓存＝{token,envDir缺}：worker agora MCP 照常可用（插件附 worker 牌，非 lift 带外供牌；agora_mcp.py 无 root 回退、缺 _token 直接拒），写面却裸 NO_CRED_HINT 且拉取自愈被 `!cred?.token` 短路（零 cred_pull 审计行——审计即判据）。处置：拉取未命中判定放宽为**缺 token 或缺 envDir**（index.ts 一处），服务侧零改动（/cred_for_sid 语义已正确）；镜像 F32（envDir 在 token 缺）合成竞态全覆盖。
**验收（本修正）**：逆半条缓存（token 有 envDir 缺）写判定触发拉取→同调用放行；404 路由仍 DISPATCH_HINT；`cd agora-plugin && bun test` 绿＋全量 pytest 绿。

### F27 macOS 兼容——setsid 去硬依赖＋lift detached 拉起＋平台口径如实（2026-10-29，用户裁定"做"）
推广评审 W1 实证：install.sh 硬依赖检查含 setsid 而 macOS 原生无（brew util-linux keg-only 不入 PATH），首跑即"缺依赖"拒装。三处收敛：①install.sh 依赖检查去 setsid（python3/git/opencode/bun 四件为硬面）；②拉起行条件分支——setsid 在位保原形态（gt-71b6af 原位 exec 语义、$! 即 daemon pid、源级锁不变），缺位走 nohup 兜底（`"${AD_ENV[@]}" nohup python3 … &`，$! 同为 python pid，cleanup/第 8 步击杀语义两形态一致；SIGHUP 由 main.py handler 留痕退出）；③插件 lift.ts Bun.spawn 去 `["setsid", …]` 包装改 `detached: true`（等价 POSIX 新会话、Windows 新进程组；本机 bun 实证子 sess≠父 sess）。README 平台口径如实分层：Linux 已验证；macOS 代码就绪未真机验证（不写"支持"）；Windows 指引 WSL。明确排除：Windows 原生支持（flock/curses/bash 四套抽象层，另立项）、macOS"已支持"声明（待真机 install→dispatch→verify→merge 全链实证后放开）。
**验收**：新断言两件先红后绿（依赖检查不含 setsid／nohup 兜底分支源级锁）＋既有 setsid 主分支源级锁保持（本就绿）；`bash tests/test_install.sh` 退出 0；`cd agora-plugin && bun test` 绿（含 lift detached 源级契约）；`python3 -m unittest discover -s tests -t .` 全量绿；README×2 平台行含"未真机验证"与 WSL 字样。
**终闸补记（三分册）**：C 册 detached=完整 setsid(2) 语义实证、env/stdio/0600 两形态一致（零 finding）；A 册 3 warning 修毕（lift.ts 头注释/lift.test.ts 用例名/本验收行计数）；B 册 LOW 接受为已知限制——nohup 分支安装窗内 SIGHUP 可致冒烟假失败（daemon 留痕退出 handler 为刻意设计，Python signal.signal 覆盖继承的 SIG_IGN，trap 包装无效；重跑即恢复、无数据丢失）；B 册越册线索已修——cleanup sed 改 -i.bak 形态（GNU/BSD 通吃，保 gt-e38e41 取牌即抹契约）。test_install.sh 夹具（setsid 直拉/stat -c）仍为 Linux 开发机面，macOS 真机验证阶段统一处理。

### F26 hook 拒因分流＋wrong-door 副本换算＋BOOT 副本行（2026-10-29，task-fb8b01）
四病一文案致误诊传染（gt-99ae7b 与 task-4d9fff 的 worker 把拒误诊为凭据缓存清零，绕道 shell 作业并错误归因；task-fb8b01 worker 自身再实证——BOOT/credential 注册未达插件缓存恒拒 DISPATCH_HINT，无法自诊）。shouldBlock 拒面按因分流，判定语义与豁免面一字不变只改信息量：①cred 缺 token/envDir→NO_CRED_HINT「无凭据缓存——可经真实 prompt 通道重注 BOOT+credential 自愈（配方见任务页），或 post(kind=question) 求派发方」（F25 静默通道已被实证证伪：v2.2.0 synthetic 不触发 prompt hook，文案不给死路指引）；②有凭据 cwd∉env→wrongDoorHint 列两侧实值＋按 worktree 命名约定（envDir basename 形如 {repo}__wt-*，主仓根＝envDir 父目录/{repo}）尽力推导副本对应路径（{envDir}/{相对路径}），推导不出退化仅列实值；③主会话 role=root→维持 DISPATCH_HINT 原文；④服务不可达默认拒维持 index.ts lift 败态拼 LIFT_RECOVER。附 BOOT 契约预防行（task.py _boot）：「goal 中的主仓路径仅指代——你的可写区＝env 副本（同构 worktree），编辑一律用副本内对应路径」。
**验收**：四分支文案断言（a 文案；b 实值＋换算路径＋两形态退化；c 原文；d 既有 LIFT_RECOVER 用例保持）且既有豁免面/root 例外/读引导用例全绿；`grep -qF "在任务 env" agora-plugin/hook.ts && cd agora-plugin && bun test` 双绿。

### F28 cancel 忙碌闸＋agora status 三态判读（2026-10-29，task-7f4cfe）
判例链三例误杀：worker 离线评审环（心跳天然盲区）进行中派发方抢先 task_cancel，env 回收连尾巴提交一起带走——判死依据「心跳冷＋树像完成」不充分，oc 会话回合进行中（busy）才是「深潜 vs 真僵」分界。修法两件：①task_cancel 忙碌闸——取消前按 health 僵态豁免同款信号探测目标任务 worker 会话（session 存在且 time.updated>time.idle＝回合进行中）：busy→拒绝，返回 {cancelled:0, reason:"worker_busy"}＋专属审计行 cancel_rejected_busy＋hint 指路 force；force:true→照常取消（级联/凭据作废/env 回收不减）＋cancelled 审计行注 forced；非 busy（闲置/会话死/无 worker/冻结态）→现行路径不变；oc 探测失败 fail-open 放行（防 oc 故障卡死取消能力）＋审计注 probe_failed。httpd force 布尔校验（非布尔 400，enforce/verify_pending 同口径）＋MCP 壳 schema 透传。②agora CLI status 子命令——`agora status <tid>`（--json）单查 {task, state, last_seen_ago, class}：fresh(心跳近≤1200s)/busy_round(在飞＋心跳冷＋不在 doctor stale_candidates)/stale_candidate(在 doctor 候选单)；终态或无 worker→class 空；数据全取既有 /board 单任务视图＋/doctor（零服务端新端点），分类客户端推导（客户端锚 1200 与 report._STALE_AFTER 同口径）。挂起项（done 代填，原②）：完而未报实证≥1 或 sid 死锁复发≥2 再立项。
**验收**：单测 a-e（busy 拒/busy＋force 审计/闲置照常/探测失败放行＋probe_failed/status 三态各一例含 --json）；`grep -rqF "worker_busy" agorad/ && grep -rqF "def test_status" tests/ && python3 -m unittest tests.test_core_task tests.test_transport_httpd` 绿；全量绿。

### F27 拉取闭合加固：attach 分流＋readish 拉取＋拒因枚举＋等价性结论（2026-10-29，task-f151ab）
拉取式闭合度审计出两缺口一优化：①attach 面破口——index.ts 给 agora_* MCP 调用附牌时冷缓存径直回落 root 牌，worker 首调可能以 root 身份行动（task_cancel/enforce 级）。修法＝attach 前先拉取分流：/cred_for_sid 200 → 附拉取的 worker 牌；404（非在飞 worker＝主会话/未知会话）→ 维持 root 附牌（主会话合法路径不动）；不可达/节流/lift 败态 → 不附牌（调用方收 401/-32001 明确报错），绝不落 root。②readish 面接拉取（裁定 A，派发方 2026-10-29）：READ_HINT 本意是「worker 走带审计的工具面」而非「限制非 worker」——root 牌从不入插件缓存致「主会话不受此限」成死条文；拉取 404（not_worker）按主会话例外放行 state 读（单用户模型下非在飞 worker 与主会同属可信主类，放行面与 enforce 之前裸读时代等价，零新增暴露）；拉到 worker 凭据或不可达/节流 → 维持 READ_HINT（fail-closed）。③pullCredential 拒因枚举 ok/not_worker/unreachable/throttled，NO_CRED_HINT 按拒因附分诊文案（区分等节流/服务异常/真非 worker），判定语义零漂移只增信息量。④安全等价性结论（防未来审计误判）：/cred_for_sid 返回 token 的信任边界与直读 $AGORA_HOME/tokens 凭据文件等价（单用户回环模型，仅 127.0.0.1 可达），本端点未新增攻击面——不是新机制，勿过度加固。⑤节流窗回放判据（round1 high 修复）：节流窗内二次拉取零网络并回放上次判定而非裸 throttled——防已知 not_worker 被降级断主会话 root 附牌/误吃 READ_HINT，陈旧度 ≤30s 与节流窗同界。
**验收**：三态测试锚——冷缓存 worker attach 走拉取不落 root；404 会话 root 附牌保持＋404 会话读 state 放行；网络失败无附牌＋不可达读 state 仍 READ_HINT；worker 拉取后读 state 仍 READ_HINT；`grep -rqF "not_worker" agora-plugin/ && cd agora-plugin && bun test` 绿。

### F27 写面 not_worker 路由：root 死条文收殓（2026-10-29，task-305aff）
F26 分流遗留残留 finding：write 面 `cred?.role==="root"` 分支生产不可达（root 牌从不入插件缓存），主会话直改仓落进无凭据分支吃 NO_CRED_HINT 死路文案（「静待重注入」对主会话永不到来）。修法镜像 F27 readish 裁定 A 路由形态（判定语义除路由外零漂移）：write 面缓存未命中按拉取判据路由——404（not_worker）→DISPATCH_HINT（主会话正确指引＝派任务勿直改）；200→填缓存走既有 worker env 判定；不可达/节流→维持 NO_CRED_HINT fail-closed（未知身份不给主会话指引）。index.ts 拒因分诊随路由收敛：not_worker 不再产 NO_CRED_HINT 附文（真非 worker 分支退役）；F25 接线用例 404 锚钉紧为 /任务面派发/。
**验收**：main_write 三态纯函数锚（not_worker→DISPATCH_HINT；worker 拉取命中既有判定不回归；不可达/节流/缺省 fail-closed）＋既有豁免面/root 防御位/readish 用例全绿；`grep -qF "main_write" agora-plugin/hook.test.ts && cd agora-plugin && bun test` 绿。

### F24 worktree 环境名去重 task 字面量（2026-10-29，task-5c520c）
F24 换前缀后 build_env 拼名 `__wt-task-{tid}__` 与 tid 自带 `task-` 前缀双写（`wt-task-task-xxx`）。拼名收窄为 `__wt-{tid}__`（docstring 同步）；remove_env 兜底解析 `partition("__wt-task-")` 放宽为 `partition("__wt-")`——存量旧名亦含该子串，天然双兼容，存量 worktree 不迁移不重命名。测试面同步：单测正则 `__wt-task-gt-x1__`→`__wt-gt-x1__`；e2e 残留探测 glob `*__wt-task-*`→`*__wt-*`（t2_enforce.sh、t3_chain.sh×2，宽匹配对残留检测语义不变）。agora-plugin/hook.ts 只认 `__wt-` 前缀不动；docs 历史 wt-task 记录不改。
**验收**：`python3 -m pytest tests/ -q` 全绿；build_env 产出名形如 `<REPO>__wt-<tid>__<MMDDHHMM><rand4>`；remove_env 兜底对存量 `__wt-task-*` 名仍能定位主仓。

### F29 任务 id 生成前缀 ag-——task 字面量退役（2026-10-29，task-de9871 用户裁定）
接替 F24 的 task- 生成条款：生成前缀 task- 换 ag-（出处标记＋防 task 名词过载——任务/task 页/task_id 语境重叠；语义由 title 字段承担，6 hex 长度不变）。存量 gt-/task- id 终身兼容不迁移（wiki 页/账本/在飞任务不动），输入面三形态收：①生成 task.py _new_tid `"ag-"+secrets.token_hex(3)`，注释谱系更新；②高危点插件 inject BOOT 正则三前缀 `(gt-\S+|task-\S+|ag-\S+)`（字面交替写法，勿合并去字面——gt-99ae7b 前科）；③描述面如实三形态（agora_mcp.py schema×3、agora CLI help×2）；④e2e t3_chain S1 startswith 三前缀。README 生成态 task- 示例：grep 确认无，零改动。reqs 既有条目不改写，本条即替代记录。
**验收**：新 tid 命中 `^ag-[0-9a-f]{6}$`；存量 task-/gt- id 作 after 建单派发全链通；插件 BOOT 三前缀用例绿（task-/gt- 既有用例保持）；`python3 -m pytest tests/ -q` 全绿；`cd agora-plugin && bun test` 绿。

### F30 嵌套派发完整闭环＋after 失败／主动取消分义（2026-10-29，用户裁定）
嵌套派发不能止于 worker 持有 `task_dispatch`：worker 同时是其所建子任务的 dispatcher，必须能完成建单、验收登记、取消、结果接收与恢复续管。①权限面补齐 `task_create／task_update／task_cancel`，TaskService 继续以 `dispatcher_sid==caller` 作归属终审——worker 只能管理己建子任务，父任务、他人任务及 worker 自己的 verify 仍不可改，board／doctor／enforce 不开放。②服务以 `result` 直接向子任务登记的 dispatcher 回传 `closed／blocked／cancelled`，复用“先落库、后 inject、失败留库”语义；父 worker 换号／重注入后，未结案子任务（含已建未派 `todo`）同步改绑，`check_mail` 沿牌血统补拉发往旧 sid 的离线结果。③父任务 `done` 新增子任务闸：全部嵌套子任务须为 `merged／closed` 或 dispatcher 有意 `cancelled`；`blocked`、在飞及 `dependency_failed:*` 自动取消均拒收。dispatcher 对依赖失败自动取消的子任务再显式 `task_cancel` 一次，可确认有意跳过（原因改写为 `dispatcher_confirmed:dependency_failed:*`，状态不变、审计留行），避免父任务永久无法收尾。④after 与嵌套父子关系分义：主动取消 after 前驱＝有意跳过，后继可派；前驱三连败进入 `blocked`＝终态失败，服务递归取消整条 after 后继链并记录 `dependency_failed:<失败前驱>`；门内后继的取消原因随 pending-cancel 原子传到门尾。父任务取消仍按原语义级联取消嵌套子任务。
**验收**：真 HTTP 跑通 root 派父→父 worker 建／派子→子在飞时父 done 拒→子 closed→父收 result→父 done→closed；worker 可 update/cancel 己建子任务且不能管理他任务；普通主动取消子任务放行父 done，依赖失败取消拒收、显式确认后放行；blocked 前驱停止多层 after 链且门尾保留原因；closed／blocked／cancelled 三类 result 回传；父 worker 重注入／换号后活动与 todo 子任务归属保留，旧 sid 离线结果可补拉；全量 Python、插件与安装测试绿。

### F31 PyPI 分发——pipx 一条链完整安装（2026-10-30，用户裁定"打进去"）
Python 运行件与插件/skills/install.sh 全部进 wheel：`pipx install opencode-agora` 后跑 `agora-install` 即完成与仓库 install.sh 等价的完整安装，全程无需 git clone。①pyproject（setuptools 后端）：包 `agorad*`＋顶层模块 `board_tui`/`agora_mcp`；版本单源 `agorad.main._VERSION`（AST 字面读取）；data-files＝plugin 非测试件＋skills 两剧本＋install.sh＋agora CLI（显式清单，新文件须同步——防 glob 排除语义漂移）；console scripts `agorad`/`agora-board`/`agora-mcp`/`agora-install` 四入口。②`agora-install`（agorad.installer:main）：定位安装记录内 share/opencode-agora，注入 `AGORA_PKG_ROOT`（plugin/skills/install.sh/agora 拷贝源）＋`AGORA_AGORAD_SRC`（site-packages 的 agorad 包＝lift 载荷拷贝源）后原样转跑 install.sh——repo 分支逐字不动，既有源级锁与测试夹具不破。③install.sh pkg 分支（仅 `AGORA_PKG_ROOT` 在位时生效）：MCP 注册 `command=["agora-mcp"]`（PATH 入口）、lift 走 PATH `agorad`、board_tui 载荷与符号链接跳过（console script 替代）；repo 模式（env 缺省）行为零变。lift.ts 与载荷位契约（`$AGORA_HOME/.agora/agorad/main.py`）不变。
**验收**：tests/test_packaging.py——pyproject 契约（名字/py 版本/四入口/数据面/版本单源）、四 entry callable 可导入、installer 定位纯函数命中与落空两例（Req: F31）；tests/test_install.sh 新增 pkg 场景——fake AGORA_PKG_ROOT＋真 agorad 源＋PATH stub `agorad`，断言插件/skills 从包内拷、载荷在位、opencode.json command=["agora-mcp"]、PATH stub 确被调用、冒烟毕退出 0；test_install.sh 既有场景全绿（repo 模式回归）；`python3 -m pytest tests/ -q` 全绿。

### F32 门环境消毒＋凭据半条缓存拉取放宽（2026-10-30，用户裁定"都修"）
两处实证缺口（F31 任务 ag-fed303 撞实）：①agorad 服务 env（AGORA_PORT/AGORA_OC_URL/AGORA_OC_PASSWORD）被 verify 子进程链整条继承，install.sh 类自测拉起的 daemon 硬绑服务在用端口即崩（门 54.8s 即败根因，f9cf8fb 已测试侧消毒——治标）；②F25 修正的 cred_for_sid 拉取自愈触发条件 !cred 被"半条缓存"（BOOT env 位已缓存、token 缺失——会话创建期注入与插件加载竞态的产物）短路，worker 写面恒拒。修法：①gate._subprocess_runner 构造 verify env 时剥除上述三枚服务自身态变量（AGORA_HOME 明示不剥——无事故面且夹具自设各自值；剥除落显式副本不动 os.environ）；②插件凭据"未命中"判定从 !cred 放宽为缺 token 即未命中，既有 write 拉取点即自愈——半条缓存者首次写尝试即补全放行，竞态无害化（竞态属 opencode 平台时序不可消除，push 修复路线已由 F25 修正记录证伪）。F25 既有语义全保：节流（每 sid 30s）、loopback 无牌放行、失败留库兜底、404 主会话路由。
**验收**：插件 bun 测试——半条缓存形态（env 位有 token 空）写判定触发拉取并同调用放行、全缺形态回归不破、404 主会话路由不变；agorad 单测——runner env 三变量被剥、其余变量照常继承、os.environ 本体不被污染、既有 gate 测试全绿；`python3 -m pytest tests/ -q`＋`bash tests/test_install.sh`＋`(cd agora-plugin && bun test)` 全绿。

### F32 门内自动 rebase——同仓并行 non-ff 干净同步（2026-10-30，用户裁定）

同仓并行后合并者试合并必真 non-ff（F29 祖先判定），现行 reopened 弹回 worker 手动同步重走全轮 verify。改法（设计文档 `docs/superpowers/specs/2026-10-30-gate-auto-rebase-design.md`）：门内试合并遇真 non-ff 即在 integration worktree（detached）内把任务提交 `rebase --onto <目标 HEAD>` 重放（mb=merge-base）——零临时分支、env 分支与 `.review-loop` 锚不动、最终推进仍 ff-only（线性历史不变）；rebase 后集成 verify 跑在新内容上（验证内容＝推进内容），绿则 set_fields(commit=R, rebased_from=E) 先落库再 ff 推进，审计 rebased 事件成对留痕；崩溃矩阵三态 contains 幂等不假重开。分类纪律沿用 F29：冲突→abort→reopened 不计连败（文案注明同步冲突），环境病如实上抛。等价提交全被 drop→R==目标 HEAD＝幂等推进零增量 merged。质量口径：干净 rebase 重测不重审（patch 等价），冲突 reopen 全量重走。非目标：条件重审、放松 ff-only。
**验收**：干净重放成功且 env 分支 sha 不动；冲突 abort→reopened 不计连败；环境病如实上抛；等价提交 drop 形态 no-op merged；集成 verify 败→reopened 计连败目标分支字节级不动；崩溃三态 contains 幂等；integration 回收无 rebase 残留；全量 pytest 绿（Req: F32）。

### F33 agora 闸豁免面收编 req-gate 门记录目录（2026-10-30，ag-deef8f）
req-gate-enforcer（dotfiles 插件，伴生任务改记 `.req-gate/`）门过时写 `<repo>/.req-gate/<sessionID>.md`；agora 所有权闸拦 edit/write 致两钩子死锁——门记录写本身被拦（本仓 10 条存量 `.omo/req-gate/` 记录早于 enforce 即实证）。修法：插件写面豁免收编门记录目录，basename 级路径任一段命中即豁免，旧 `.omo` 与新 `.req-gate` 同收；其余判定语义与既有豁免面零漂移。配套 `.gitignore` 增 `.req-gate/`（`.omo/` 旁并存）。
**验收**：F33 用例——write/edit 落 `<repo>/.omo/**` 与 `<repo>/.req-gate/**` 放行、控制组非豁免 dotdir 仍拒；`python3 -m pytest tests/ -q`＋`cd agora-plugin && bun test` 全绿（Req: F33）。

### F32 令牌/易失件落盘权限——write_ephemeral 建 tmp 即 0600（2026-10-30，安全评审收尾，裁定记录 ag-20898a 消息 649）
write_ephemeral（agorad/access/store.py）建 tmp 即 0600：os.open mode 0o600＋os.fchmod 兜底——O_CREAT mode 仅建时生效，复用旧版崩溃遗留 0644 .tmp 时显式收紧，自愈才闭环；tokens 含 root 全量牌，不得按 umask 0644 落盘。实现 a72eab2 已随 F6 落地，本条收编台账、测试锚自 F6 归位 F32。
**验收**：tests/test_access_store.py 的 test_write_ephemeral_owner_only_perms——umask 0o022 下预置遗留 0644 终件＋遗留 0644 中转件，重写后 st_mode&0o077==0（两路自愈）；`python3 -m pytest tests/ -q` 全绿。

### F34 req 编号服务端发放——并行撞号根治（2026-10-30，ag-76ad62）
req 编号历来派发方各自手扫台账领取，同仓并行即撞号（本仓实证 F32 两案并存；史有 F24×2/F27×3）。修法：task_create 新可选参 req_id，传入即在任务行登记发放；发放面服务端查重——已发集＝任务行 req_id 字段（随状态账本持久），种子＝扫 project_dir docs/reqs/opencode-agora.md 现存最大 F<n>（F32 双条目场景取 max；每次发放重扫，台账只增等价首用种子且免持久化种子态），重复发放拒绝（403 报因含已发任务指针）、格式限 F<数字>、缺文档种子记 0。无自动默认发放：是否领号由派发方决定（大量任务无需 req 号），服务只保证「发出去的号不重」。约束力口径如实：服务端分配＝权威号源，worker 从任务页 frontmatter 取号（req_id 行）；不阻止绕过（先例约束，非闸）。MCP 壳 schema 透传（httpd→task 全链，F23 先例形态）。不做：历史编号 renumber、文档迁移、wiki 计数器页。
**验收**：F34 用例（Req: F34 锚）——发放登记、无 req_id 行缺省 None、重复拒绝含已发指针、种子扫描双条目取 max、缺文档种子 0、坏格式拒、httpd 透传 403/400、任务页 req_id 行、MCP schema properties；`python3 -m pytest tests/ -q` 全绿。
