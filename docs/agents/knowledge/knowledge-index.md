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
- **TUI 行号与深树两坑**（gt-c3c74f 评审实证）：curses 行切片/行号算术须防 h 过小出负值（如 `max(0, ·)`，draw_board 实例）；树深由远端 JSON 决定的遍历禁递归，用显式栈（build_rows DFS）

## 已知接受面

- bash 直写绕闸（审计兜底）
- 偷牌（同机同用户信任模型）
- 评审锚自写（服务只读不证真）
- root 回落权限面（B1：缺 cred 会话暂获 root 工具面）
