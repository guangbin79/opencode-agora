# opencode-agora

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](https://www.python.org)
[![made for opencode v2](https://img.shields.io/badge/made%20for-opencode%20v2-6e7bff.svg)](https://opencode.ai)

[English](README.md) | [简体中文](README.zh-CN.md)

[opencode](https://opencode.ai) v2 的多 agent 委托协作层。你用自然语言说目标；每个任务在自己的 git worktree 里跑；**测试由服务亲自跑**；只有验证过的代码才会被合并。

```text
你：  "修一下 repoB 的登录 bug，跑 pytest -q，绿了就合并"
agora：  └─ 在隔离的 git worktree 里派一个 worker
           worker 修复 → 服务亲自跑 pytest → 评审绿 → ff-only 合并
你：  "进展如何？"   →   看板：✅ 已合并
```

一句话说清差异：**这里 agent 不能给自己的作业打分。** verify 命令在服务侧执行而非 worker 侧——绿＝测试真的在合并提交上跑过。

## 快速开始

前置：[opencode](https://opencode.ai) v2（`curl -fsSL https://opencode.ai/v2/install | bash`）、[bun](https://bun.sh)（`curl -fsSL https://bun.sh/install | bash`）、`git`、`python3` ≥ 3.10（仅标准库，零 pip 依赖）。

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

装完即用。安装脚本幂等（可重复跑）；注册 MCP、复制插件与 skills，root 凭据落在 `$AGORA_HOME/.agora/root.token`（0600）。

之后正常和 opencode 会话对话即可：

| 你说 | 发生什么 |
|---|---|
| *"让 agent 修 repoY 的 X，用 `pytest -q` 验证"* | 建任务，worker 派发进独立 worktree |
| *"进展如何？"* | 实时看板：五桶、依赖链、失控告警流 |
| *"停掉那个任务"* | 级联取消（带确认） |
| *"repoA 升级依赖，然后把同样的修复应用到 repoB 和 repoC"* | `after:` 依赖链——B、C 等 A；无关仓并行 |

独立仪表盘：`agora-board`（vi 键位 TUI）。更新：`git pull && ./install.sh`。

## 工作原理

```text
人 ──▶ 主会话（skills: agora-dispatch / agora-ops）
           │  MCP（13 工具）
           ▼
      agorad — localhost 上的 Python stdlib 服务
      状态权威 · verify 门 · 自动合并 · 消息路由 · 崩溃恢复 · 审计
           │
      各任务 git worktree（强制隔离）
```

任务生命周期：`创建 → 派发 → worker 干活 → verify（服务真跑）→ 自动合并（ff-only）→ 关闭`

## 两道质量闸

**verify 门**——不可协商的那道。验收命令在建单时登记（`task_create(verify_cmd="pytest -q")`，或 `verify_pending: true` 延后登记）；**服务在 worktree 里真跑**。绿＝可合并；红＝worker 收到输出重试；三连败＝blocked＋失控告警。worker 无法跳过、伪造或自报结果。

**评审门**——开放标准，评审器自带。契约只有一个锚文件：

- 任务 worktree 内存在 `.review-loop/` 目录时，`done` 受理检查锚，**合并前重验**（堵住"评审过又偷提交"的窗口）。
- 锚绿＋verify 绿 = 自动 ff-only 合并。
- 没有 `.review-loop/` 目录？一切照常——任务完成驻留 `done`，**你手动合并**。自治是两道闸挣来的，不是默认送的。

任何评审器都行——OpenCodeReview、CodeRabbit、人工清单——agora 只认锚。最小评审器约 10 行：

```bash
#!/usr/bin/env bash
# review-loop.sh <worktree> <target_branch> —— 每次提交后运行；未绿退出非 0
set -euo pipefail
cd "$1"
base=$(git merge-base HEAD "${2:?}"); sha=$(git rev-parse HEAD)
findings=$(your_reviewer "$(git diff "$base"..HEAD)")   # OCR / CodeRabbit / 人工清单
printf '%s\n' "$findings" | grep -qiE 'blocker|high' && { printf '%s\n' "$findings"; exit 1; }
mkdir -p .review-loop && printf '%s\n' "$sha" > .review-loop/.last-reviewed
```

完整规范——契约细节、worker 协议、威胁模型、idle 自触发设计笔记：[docs/review-loop.md](docs/review-loop.md)。

## 核心能力

- **验证过的自治**——服务真跑测试＋评审锚绿才自动合并，且合并前重验锚
- **强制隔离**——插件级闸拦截无任务凭据的 git 仓写入；worker 碰不到你的主检出
- **跨仓编排**——`after:` 依赖链；无关仓并行；链深（5）与并发（8）硬上限
- **角色分权**——root / dispatcher / worker 会话牌；worker 永远不能取消任务、改 verify 命令、看板
- **崩溃恢复**——任意时刻 `kill -9`；只追加状态＋worker 重启后凭原凭据重注入续跑
- **实时问责**——失控实时拍主会话；每次状态变更落只追加 JSONL 审计；健康巡检兜底 dispatcher 死亡
- **兼容 PR 工作流**——`task_create(target_branch="feature/x")` 合并进特性分支而非 main
- **轻量**——Python stdlib 服务，零 pip 依赖，只绑 localhost，无遥测

## 对比

| | 手动多会话 | Swarm 框架 | opencode-agora |
|---|---|---|---|
| 每任务隔离 | 自己管 worktree | 视实现 | 强制（插件闸＋每任务 worktree） |
| "测试过了"的含义 | agent 自己说 | agent 自己说 | 服务亲自跑过命令 |
| 跨仓依赖 | 手动排序 | 有 | `after:` 链，无关仓并行 |
| 合并 | 手动 | 视实现 | 双闸绿才自动 |
| 审计链 | 无 | 部分 | 只追加 JSONL，每次状态变更 |
| 安装足迹 | — | 重运行时 | python3 stdlib＋git＋bun |

## 配置

| 选项 | 缺省 | 说明 |
|------|------|------|
| `AGORA_HOME` | `~/plaza` | 账本根目录（状态、消息、审计、wiki） |
| `$AGORA_HOME/.agora/inactive-repos` | 空 | agora 完全休眠的仓（一行一个路径） |
| `AGORA_BOARD_INTERVAL` | `5` | 仪表盘轮询间隔（秒） |
| `AGORA_HEALTH_INTERVAL` | `300` | dispatcher 健康巡检（秒） |
| `enforce` | on | 强制闸总开关（仅 root，开关留审计） |
| `$AGORA_HOME/.agora/email.json` | 关 | 僵态任务邮件提醒（`{"to","command"}`） |

## 安全模型

如实披露——哪些是机器检查，哪些是信任：

**强制**（无法绕过）：
- git 仓写入仅限任务 worktree 内（插件闸＋会话牌）
- verify 必须真跑且通过，由服务执行
- 角色矩阵：worker 不能取消、改 verify、拨 enforce
- 每次状态变更落只追加审计账本

**接受面**（单机信任模型）：
- worker 用裸 `bash` 可绕写闸（审计兜底）
- 同用户下偷牌
- 评审锚是流程协议不是反作弊：worker 写、服务读。真正的质量底线是 verify 门；评审加的是纪律，不是安全

## FAQ

**怎么更新？** `git pull && ./install.sh`——插件与服务载荷是拷贝不是引用，拉新后必须重跑安装脚本。

**怎么卸载？**
```bash
rm -rf ~/.config/opencode/plugins/agora ~/.config/opencode/skills/agora-dispatch \
       ~/.config/opencode/skills/agora-ops ~/.local/bin/agora ~/.local/bin/agora-board \
       ~/plaza ~/.agora-home
# 再把 ~/.config/opencode/opencode.json 里 mcp 段的 "agora" 条目删掉
```

（`~/plaza` 是缺省 `AGORA_HOME`——若你配置了自定义路径，请删那个目录而非 `~/plaza`。）

**没有 review-loop 能用吗？** 能——verify 照跑照拦；任务完成驻留 `done`，手动合并。实现[开放标准](docs/review-loop.md)即解锁自动合并。

**我的代码会被传到哪？** 哪都不去。agorad 只绑 localhost，worker 是本地 opencode 会话，无遥测、无云端、无需给 agora 任何 API key。opencode 配什么模型，agora 就编排什么模型。

**平台支持？** Linux（已验证）。macOS：代码路径就绪（无 `setsid` 时安装脚本回落 nohup），尚未真机验证。Windows：请在 WSL 内使用。

## License

MIT
