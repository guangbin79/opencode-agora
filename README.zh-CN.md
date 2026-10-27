# opencode-agora（中文）

[English README](README.md)（主文档）

opencode v2 上的多 agent 委托协作层——你只说目标，agent 交付经过验证的代码，自动合并。

```
你："修一下 repoB 的登录 bug"
  └─ agora 派发 worker 到隔离的 git worktree
     worker 修复 → 测试通过 → 代码评审 → 自动合并
     你看板面：✅ 已合并
```

## 解决什么

| 痛点 | 方案 |
|------|------|
| 工作区污染 | 每任务独立 worktree，主检出不动 |
| 跨仓无编排 | 依赖顺序派发（`after:`）；无关仓并行 |
| worker 黑盒 | 实时看板、失控告警、只追加审计账本 |
| 无问责 | verify 门（真跑测试）＋评审门＋完整审计链 |

## 快速开始

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

依赖：`python3`（仅 stdlib）、`git`、`opencode` v2、`bun`。

装好后正常对话即可：

- *"让 agent 修 repoY 的 X"* → 建任务、派 worker
- *"进展如何"* → 五桶看板＋链树
- *"停掉那个任务"* → 级联取消（带确认）

独立仪表盘：`agora-board`

## 工作原理

```
人 ──▶ 主会话（skill: agora-dispatch / agora-ops）
          │  MCP（12 工具）
          ▼
     agorad（Python stdlib 服务）
     状态权威 · verify 门 · 自动合并 · 消息路由 · 崩溃恢复 · 审计
          │
     各任务 worktree（git 隔离）
```

任务生命周期：`创建 → 派发 → worker 干活 → verify → 自动合并(ff-only) → 关闭`

**verify 门**：质量底线。worker 提交验收命令 → 派发方校验登记 → 服务真跑 → 绿即合并，败则 worker 收到输出重试。三连败 = blocked + 失控告警。

**评审门**：装了 review-loop 则 done 另需代码评审在当前提交通过。没装 → verify 通过但**不自动合并**——你手动审。

## 核心能力

- **全自主**：verify 绿＋评审绿 = ff-only 自动合并，无需人确认
- **强制隔离**：插件级闸拦截无凭据的 git 仓写入
- **跨仓编排**：`after:` 依赖链；无关仓并行派发
- **崩溃恢复**：任意时刻 kill -9，重启后 worker 重注入续跑
- **实时升级通知**：质量三连败、dispatcher 死亡、闸体变更——秒级拍主会话
- **会话牌**：root / dispatcher / worker 角色矩阵，越权必拒
- **启用域**：指定仓中 agora 完全休眠
- **目标分支**：`target_branch="feature/x"` 合并进指定分支——兼容 PR 工作流

## 配置

| 选项 | 默认 | 说明 |
|------|------|------|
| `AGORA_HOME` | `~/plaza` | 账本根目录 |
| `.agora/inactive-repos` | 空 | 休眠仓名册（一行一个路径） |
| `AGORA_BOARD_INTERVAL` | 5 | 仪表盘轮询间隔（秒） |
| `enforce` | on | 强制闸总开关（仅 root，开/关审计） |
| 链深 / 并发配额 | 5 / 8 | 派发硬上限 |

## 依赖

- **opencode v2** — 会话创建、消息注入、插件钩子
- **git** — worktree 隔离与 ff-only 合并
- **python3**（≥3.8）— 服务与 MCP 壳（仅 stdlib，零 pip 依赖）
- **bun** — 插件运行时
- **review-loop**（可选）— 代码评审门。没装则 verify 绿但不自动合并

## 安全模型

**强制**（机器检查）：worktree 外写入必拒 / verify 真跑 / 角色矩阵 / 全审计

**接受面**（单机信任模型）：bash 直写绕闸 / 同用户偷牌 / 评审锚自写

## License

MIT
