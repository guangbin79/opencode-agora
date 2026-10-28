# opencode-agora 项目知识索引

> 2026-10-27 首次归档（三天全弧：需求→设计→三计划→实装→狗粮）

## 架构决策

- **甲案跨仓**：耦合仓顺序编排（B7 after:），后仓经 ../ 见前仓已合并形态——无复合环境
- **verify 双路**：预登记（快速路）＋ worker 回填（探索路）并存
- **三访问层**：agorad 内域核心＋oc（opencode API）＋store（账本）＋gitx（worktree）
- **11+1 工具面**：11 服务端＋1 壳层 wait
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

## 已知接受面

- bash 直写绕闸（审计兜底）
- 偷牌（同机同用户信任模型）
- 评审锚自写（服务只读不证真）
- root 回落权限面（B1：缺 cred 会话暂获 root 工具面）
