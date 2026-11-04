# opencode-agora 项目知识索引

> 2026-10-27 首次归档（三天全弧：需求→设计→三计划→实装→狗粮）

## 架构决策

- **甲案跨仓**：耦合仓顺序编排（B7 after:），后仓经 ../ 见前仓已合并形态——无复合环境
- **verify 双路**：预登记（快速路）＋ worker 回填（探索路）并存
- **verify 双顶**（F36）：软顶=AGORA_VERIFY_TIMEOUT 提醒不杀；硬顶=AGORA_VERIFY_CEILING（缺省 4×软顶）杀全组 rc=124；超顶路由派发方（不 transition/不弹 worker/不计连败，任务驻留 done）
- **verify 任务级预算**（F51）：timeout 随 verify 字典（正整数秒可缺省），写权限与 verify_cmd 完全同权（worker 拒）；子任务随字典继承；gate 两级取值——任务 timeout→服务缺省，任务级硬顶固定 4×生效值（服务级 F36 旋钮不动）；软顶/超顶文案附生效预算；gate→runner 传 (timeout, ceiling) kwargs 即注入观察缝
- **驻留 done 取消结算不变量**（F36）：cancel 按 in_gate 分流——门内排队（gate_tail 消费）、驻留（in_gate=False）直结（State.settle_parked_cancel）；否则 pending 悬挂至重启；新增「驻留 done」类出口须同步核查取消结算面
- **三访问层**：agorad 内域核心＋oc（opencode API）＋store（账本）＋gitx（worktree）
- **12+1 工具面**：12 服务端端点（httpd._TOOLS）＋壳层 wait＝MCP 面 13 工具（agora_mcp._TOOLS）
- **会话牌认证**：root/dispatcher/worker 角色矩阵＋附牌链
- **验收授权全局角色判定**（F37，2026-11-01）：worker 保留嵌套建单/派发，但 verify_cmd 写面（task_create 携带＋task_update 登记）一律按 token 全局 role 拒——子任务局部 dispatcher 归属不构成授权；建单 project_dir/target_branch 强制继承现役父任务，verify_pending 自旗拒且豁免旗不随父，verify 字典缺省随父——未获配置 dispatch 硬闸拒
- **门记录豁免作用域约束**（F38，2026-11-01）：basename 级豁免（.omo/.req-gate/.review-loop）须配作用域腿——写目标 ∈ 当前 env（cred.envDir）或当前仓根才豁免，跨 env/跨仓一律拒；「路径含字样即放行」类豁免是投毒面，放宽只认工作区＋精确路径授权

## 关键模式

- **BOOT 角色声明**：worker 出生即知工具面/等待策略/done 纪律——消除试错（狗粮实证零权限试错）
- **enforce 部署缝隙**：源码改动须重跑 install.sh（拷贝非引用）——改 BOOT/代码后忘重跑是最常见部署坑
- **评审环锚绑定**：done 前提＝.review-loop/.last-reviewed == HEAD（代码被审过且绿）
- **摩擦驱动迭代**：狗粮两口实证——第一口 2 次被拒 9 事件；第二口零被拒 7 事件
- **AGORA_HOME 指针与读守卫**（gt-32c051/gt-54f874）：MCP 壳/install 写 ~/.agora-home 单行指针，组件 env>指针>缺省 发现；插件判定链不跟随（被辖方可写文件不作信任锚）；指针读在 discover 出口形状校验收口——半写件抛 TypeError/InvalidURL 不在 (OSError,ValueError,KeyError) 元组内，守卫补 TypeError；控制符牌一律拒读（obs-fold 折行合法，只拒换行不够）
- **TUI 行号与桶行固顶**（gt-c3c74f/gt-8d5761/gt-cb1075）：curses 行算术防 h 过小出负（max(0,·)）；远端 JSON 定深的遍历禁递归用显式栈；键位辅助空滑回退须用按键前行 origin（回退到进辅助时桶行＝光标隐身）；桶行固顶画滚动窗外，clamp_view 加 nb 参钳 [nb, len-visible]，矮屏任务区允许 0
- **install 生命周期与 setsid**（gt-71b6af）：install 只停自拉实例，装后服务由插件 lift 拉起（MCP 壳仅消费）；setsid 脚本上下文原位 exec 不 fork（$! 即 daemon pid）；验脱会话须装后以同启动行自拉安装位载荷——装中观察有竞态窗不可靠
- **内存态心跳禁走落盘字段路径**（gt-a18ef6 事故实证）：health.touch→set_fields(last_seen) 每调用触发全量快照追加，O(调用数) 撑爆 tasks.jsonl（4.4GB 实证）；「崩溃回退接受」的内存态字段（last_seen/mail_read）一律走专用内存方法，set_fields 只收实质变更；重启回退由 F15 env 活性＋busy 免判兜底
- **账本尾读＋有界瘦身**（gt-a02ab1）：load_state 只 seek 末 1MB 解析完整行——启动耗时与账本大小解耦；追加超 500 行即原子重写仅留末完整行（flock 单写者安全，禁每次追加全文件重扫），重写时机避开启动路径
- **运行时管道显式解码**（ag-fa17e4→ag-e96140 实证）：subprocess 管道/文件读一律显式 encoding="utf-8", errors="replace"——text=True 严格解码遇非 UTF-8 崩在 communicate()；git log 坏字节自重编码到不了本侧，git diff 内容行是原样字节流（U+FFFD 断言只落 diff 路径）；有 try 的静态配置读保持严格
- **任务 id 前缀契约词散布面**（gt-99ae7b）：交替正则写 (?:gt|task)- 丢字面 task- 使 verify grep 失配（须 (gt-\S+|task-\S+)）；e2e 有 startswith("gt-") 硬编码单测盖不到——改前缀先 grep 全仓含测试/文档断言面
- **worker 原生写被自家闸拦的通道**（gt-99ae7b）：凭据缓存空时 edit/write 全拦、req-gate 拦首写——原生 write 落 /tmp/opencode 补丁文件＋git apply --unidiff-zero stdin 打零上下文补丁；hunk 行数计数必须准确，少一行 git 静默截尾（EOF 语法错才暴露）
- **合成消息不触发 prompt hook**（task-fb8b01，F25 根因）：v2.2.0 契约明示 synthetic 控制不走 prompt admission hook——inject_silent 对插件 onInject 永不可达；自愈＝对 /api/session/{sid}/prompt 以真实 prompt 重注 BOOT+credential 两消息（配方随 F26 落任务页）；诊断教训：黑盒排障逐项核前提，勿信「凭据清零」惯性叙事
- **mcp_shell 白名单封死执行向量**（task-fb8b01）：ALLOW_COMMANDS 外全拒，并封 git -c、git config、find -exec、awk system() 旁路；worker 跑 verify 合法通道＝一次性 opencode API 会话（POST→move→prompt→GET→DELETE）或交服务侧 merge 门自跑
- **计数声明须分面**（ocr 实证）：同一「工具数」三面异值——MCP tools/list=13（含 wait）、HTTP 端点=12（wait 壳层拦截不转发）、旧注释 12——blanket 替换改错事实；改计数先 grep 各消费面逐面改
- **Python 地板=3.10**（PEP 604 实证）：str | None 运行时注解且无 __future__ import（access/oc.py、access/store.py）——3.8/3.9 导入即 TypeError；README/CI 以 3.10/3.12 为地板，勿回写 ≥3.8
- **Bun.spawn detached 等价 setsid**（F27 实证）：detached:true 子进程 sess≠父（POSIX 新会话/Windows 新进程组），插件拉起不依赖 setsid 包装；install.sh macOS 缺 setsid 走 nohup 兜底（击杀语义不变）
- **维护者自编辑通道**（F27/task-305aff）：enforce ON 下 root 亦被拦（零信任）——维护者直改主检出＝root 拨 enforce off→编辑→on（MCP enforce 工具，审计留痕）；check_mail 附牌回落只回填 MCP 面、不回填 hook 面缓存（两缓存分离）
- **wheel 数据面两坑＋argv 假象**（F31，53fb8ec）：importlib RECORD 是安装态，locate_file 以 site-packages 为基——兜底 Path(sys.prefix)/rel 双态通吃；data-files 目的地键须保源目录名（与 install.sh 布局名实冲突即 cp 落空）；python3 -c 的 argv[0] 是字面 -c，首参自 argv[1] 起
- **O_CREAT mode 仅建时生效**（ag-20898a）：os.open(0o600) 对已存在件不收紧、O_TRUNC 只清内容——崩溃残留 0644 中转件被复用即「重写自愈」静默失效；权限敏感中转件建后必补 os.fchmod(fd, mode)，测试预置遗留终件＋遗留中转件两路断言
- **rebase 冲突态检测须双 backend 目录**（F32/ag-4b300d）：sequencer 目录随 backend 分叉——merge 停 rebase-merge、apply 停 rebase-apply，单查其一即把真冲突误判环境病；定式＝rev-parse --git-path 两目录逐一探测，任一在位→abort→冲突，皆缺才按环境病上抛（分类先于动作）
- **递归引用图遍历必带 visited＋显式栈**（F38 W-2）：after 环无校验→stop_after_failed 下钻 RecursionError（落假 reclaim_failed 审计）；定式＝起点自身入 seen、已访问不下钻、dispatch 沿 after 链查自引用/成环拒派报因含路径；visited 只消环不消链深，递归版长链仍引爆
- **网络子进程超时杀按进程组**（F-1）：git push 类派生 ssh 孙进程——start_new_session＋TimeoutExpired 时 os.killpg(SIGKILL)＋宽限收尸（逃逸后代持管道弃读防挂死）；只杀直接子＝孤儿累积（gate runner 同款对齐）

## 已知接受面

- bash 直写绕闸（审计兜底）
- 偷牌（同机同用户信任模型）
- 评审锚自写（服务只读不证真）
- root 回落权限面（B1：缺 cred 会话暂获 root 工具面）
