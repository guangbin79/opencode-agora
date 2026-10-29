# opencode-agora 项目知识索引

> 2026-10-27 首次归档（三天全弧：需求→设计→三计划→实装→狗粮）

## 架构决策

- **甲案跨仓**：耦合仓顺序编排（B7 after:），后仓经 ../ 见前仓已合并形态——无复合环境
- **verify 双路**：预登记（快速路）＋ worker 回填（探索路）并存
- **三访问层**：agorad 内域核心＋oc（opencode API）＋store（账本）＋gitx（worktree）
- **12+1 工具面**：12 服务端端点（httpd._TOOLS）＋壳层 wait＝MCP 面 13 工具（agora_mcp._TOOLS）
- **会话牌认证**：root/dispatcher/worker 角色矩阵＋附牌链

## 关键模式

- **BOOT 角色声明**：worker 出生即知工具面/等待策略/done 纪律——消除试错（狗粮实证零权限试错）
- **enforce 部署缝隙**：源码改动须重跑 install.sh（拷贝非引用）——改 BOOT/代码后忘重跑是最常见部署坑
- **评审环锚绑定**：done 前提＝.review-loop/.last-reviewed == HEAD（代码被审过且绿）
- **摩擦驱动迭代**：狗粮两口实证——第一口 2 次被拒 9 事件；第二口零被拒 7 事件
- **AGORA_HOME 指针**：MCP 壳/install 写 ~/.agora-home 单行指针；组件 env>指针>缺省 发现；插件判定链不跟随（被辖方可写文件不作信任锚，gt-32c051 裁定 A）
- **TUI 行号与深树三坑**（gt-c3c74f/gt-8d5761 评审实证）：curses 行切片/行号算术须防 h 过小出负值（如 `max(0, ·)`，draw_board 实例）；树深由远端 JSON 决定的遍历禁递归，用显式栈（build_rows DFS）；键位处理器先改 sel 再调滑动辅助时，空滑回退必须用按键前行 origin——回退到进辅助时的桶行＝光标隐身（_skip_bucket 实例）
- **状态桶行固顶**（gt-cb1075）：sel 最小＝首任务行索引，clamp 追 sel 会把桶行（行流前缀）永久滚出——固顶＝桶行画在滚动窗外（draw_board 固定画 y=1..nb），clamp_view 加 nb 参把 top 钳在 [nb, len-visible]；矮屏任务区允许 0（`max(0, ·)` 又一实例：硬挤 1 行 h≤6 越界 curses.error 退出、更高则挤入提示行不可见）
- **install 生命周期与 setsid 观察法**（gt-71b6af）：install 只停自拉实例（第 8 步），装后服务不在场（由插件 lift 拉起，MCP 壳仅消费）；setsid 在脚本上下文原位 exec 不 fork（`$!` 即 daemon pid，kill/wait 仍达）；验脱会话须装后以同启动行自拉安装位载荷——装中观察有 ready→smoke→kill 竞态窗不可靠
- **指针读守卫对齐下游 raise 面**（gt-54f874 评审实证）：指针文件半写形状里，非对象 JSON 在下标处抛 TypeError、非数端口经 f-string 进 urlopen 抛 http.client.InvalidURL（HTTPException 族，非 OSError）——`except (OSError,ValueError,KeyError)` 元组兜不住，穿透轮询循环经 main 兜底 exit 2 杀活等待。收口单点＝discover 出口形状校验（`int(port)`＋拒含 `\r`/`\n` 的牌），守卫元组补 TypeError；合法 header 值规则（`\n(?![ \t])|\r(?![ \t\n])`）比直觉宽（obs-fold 折行合法）——控制符牌须一律拒读而非只拒 `\n`
- **内存态心跳禁走落盘字段路径**（gt-a18ef6 写放大事故实证）：httpd 每工具调用经 health.touch→set_fields(last_seen) 触发全量快照追加——O(调用数) 行×全任务快照把 tasks.jsonl 撑到 4.4GB/48791 行（相邻快照 293 对中 updated 变 291、last_seen 变 238，样本每秒 3 次），load_state 逐行解析挂死。定式：模块头声明「崩溃回退接受」的内存态字段（last_seen/mail_read）一律走专用内存方法（touch_seen/touch_mail 口径），set_fields 只收实质变更；心跳内存化的重启回退由 F15 env 活性＋busy 免判兜底，僵态只提醒不处置
- **账本尾读＋有界瘦身**（gt-a02ab1 事故善后）：load_state 只 seek 末 1MB 解析完整行（尾窗无完整行回退全扫，末行胜出不变）——启动耗时与账本大小解耦；追加路径行数超 500（计数器内存懒播种＋自增，flock 单写者安全，禁每次追加全文件重扫——ocr 实证 O(size)/append 复耦）即原子重写仅留末完整行——「只追加」的有界例外，重写时机避开启动路径
- **任务 id 前缀契约词散布面**（gt-99ae7b）：前缀变更漏面两处实证——①交替正则写 `(?:gt|task)-` 丢字面 `task-` 子串，registered verify 的 grep -qE 直接失配（写成 `(gt-\S+|task-\S+)` 保两字面）；②e2e 断言 startswith("gt-") 硬编码在 t3_chain.sh（ocr 捕获，单测覆盖不到）。改前缀先 grep 全仓字面前缀含测试/文档断言面
- **worker 会话原生写被自家闸拦的写通道**（gt-99ae7b）：harness 会话插件凭据缓存空（inject 未达）时 edit/write 全拦、req-gate 拦首写——mcp_shell 白名单（git/cat/ls）成唯一写通道：原生 write 落 /tmp/opencode 补丁文件＋`git apply --unidiff-zero -` stdin 打零上下文补丁；铁律：hunk 行数计数必须准确，计数少一行时 git 静默截尾行（EOF 语法错才暴露）
- **合成消息不触发 prompt hook——F25 静默通道失效根因**（task-fb8b01）：v2.2.0 官方契约明示 synthetic/shell/compaction/move 控制均不走 prompt admission hook——inject_silent 静默重注入对插件 onInject 永不可达（牌不入缓存→worker 恒拒；彼时四病一文案无法自诊）；自愈＝对 opencode `/api/session/{sid}/prompt` 以真实 prompt 依次重注 BOOT 形＋credential 形两消息（onInject 入缓存，下轮 edit 过闸；配方随 F26 落任务页与 NO_CRED_HINT 指路）；诊断教训：黑盒排障逐项核前提（location 经 /api 实证、安装副本 git hash-object 比对、serve 启动时 vs 插件 mtime），且勿信「凭据清零」惯性叙事——拒因不可辨正是 F26 前的系统病
- **mcp_shell 白名单封死一切执行向量**（task-fb8b01）：ALLOW_COMMANDS（ls/cat/head/tail/grep/find/wc/sort/uniq/cut/awk/git/du/df/ps/rg/agentctl/semantic-memory）外 bun/python/bash/sh/env 全拒，策略还深防执行旁路——`git -c`（command-scoped config）、`git config` 子命令、`find -exec`（`;` 报 shell operator、`+` 显式拒）、`awk` external access（system()）逐一封死；worker 跑 verify 的合法通道＝一次性 opencode API 会话（POST /session→move→prompt「跑 X 原样贴输出」→GET message 读转录→DELETE session），或交服务侧 merge 门自跑（gate.py subprocess shell=True cwd=env_dir）
- **计数声明须分面**（推广重写 ocr 实证）：同一"工具数"三面异值——MCP tools/list=13（含 wait）、HTTP 端点=12（wait 壳层本地拦截 `_tools_call` 不转发）、旧注释 12——blanket 全局替换把 httpd 事实改错（13≠端点数）；改计数类声明先 grep 各消费面逐面改，注释随面定值
- **Python 地板=3.10**（PEP 604 实证）：`str | None` 运行时注解且无 `from __future__ import annotations`（access/oc.py、access/store.py）——3.8/3.9 导入即 TypeError；README/CI matrix 以 3.10/3.12 为地板，勿回写 ≥3.8

## 已知接受面

- bash 直写绕闸（审计兜底）
- 偷牌（同机同用户信任模型）
- 评审锚自写（服务只读不证真）
- root 回落权限面（B1：缺 cred 会话暂获 root 工具面）
