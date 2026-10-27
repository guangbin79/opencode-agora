# opencode-agora（中文）

[English README](README.md)（主文档）

opencode v2 上的多 agent 委托协作层——单机、单人、多 AI agent、多 git 仓。

你只说目标；其余交给 agora：worker 派发进各自任务 worktree，产出经真实运行的 verify 命令把关，绿即 ff-only 自动合并进目标分支。一切记录在只追加的账本里，可对账。

```
人 ── 自然语言 ──▶ 主会话（skills：agora-dispatch / agora-ops）
                          │  MCP（11 工具）
                          ▼
             agorad——Python stdlib 单进程服务
             状态权威 · verify 门与自动合并 · 消息路由 · 探活与恢复 · 审计
                          │
       各任务 worktree（铁律：Repo 同级父目录，保 ../ 相对拓扑）
```

## 解决什么

| 痛点 | 答案 |
|---|---|
| 工作区污染 | 强制隔离：git 仓写入只发生在任务 worktree 内，插件闸机器校验（凭据＋cwd∈环境） |
| 跨仓无编排 | 派发链＋顺序硬闸（B7）：耦合仓按依赖序合并；无关仓完全并行 |
| worker 黑盒 | 链上可见：board／Agent View、last_seen、升级事件流、只追加审计账本 |
| 无实时通道 | 链边消息先落库后注入（在线 ≤5s、离线信箱补拉）；闸体/质量类升级实时拍主会话，探活类事件在 5 分钟健康节拍内送达 |

姿态是**全自主＋两级失败**：自愈失败静默（汇总可见）；救不回来的（blocked、质量 3 连败、dispatcher 死亡、闸体变更）升级拍主会话——闸体/质量类实时、探活类在 5 分钟节拍内——且照样进汇总。

## 安装

依赖：`python3`（仅 stdlib）、`git`、`opencode` v2、`bun`。自动合并另需 `review-loop` 评审环——worker 环境缺 `.review-loop/` 时，verify 绿的任务驻留 done 不合并。

```bash
git clone https://github.com/guangbin79/opencode-agora.git
cd opencode-agora && ./install.sh
```

`install.sh` 幂等：依赖检查→建 `$AGORA_HOME`（默认 `~/plaza`）→注册 MCP→装插件与双 skill→root 牌接管（`.agora/root.token` 0600）→持牌冒烟。**更新＝git pull 后重跑**（安装的是拷贝不是引用）。自举期先 `enforce off`，装好再开闸；本仓自身不豁免（吃狗粮）。

## 日常使用

对主会话正常说话，skill 按意图自动挂载：

- "让 agent 修 repoB 的登录 bug" → `agora-dispatch` 拆解、写窄而自包含的 goal、派发、登记 verify；
- "进展如何" → board 五桶（已合并/在飞/失败/阻塞/已取消；"失败"为保留位——verify 失败在续修期计入在飞）＋链树＋last_seen 查僵；
- "把那条链停了" → `agora-ops` 先展示级联范围、经你确认才 `task_cancel`。

独立仪表盘一条命令：

```bash
python3 board_tui.py    # Agent View：链树/五桶/升级事件流
                        # vi 键位：j/k/g/G 移动、Enter 或 l 明细、h 返回、L tail worker 会话、
                        # d doctor、x 取消（y/n 确认）、/ 过滤、r 刷新、q 退出
```

### 任务生命周期

`todo → dispatched → running → done → merged → closed`，分支 `blocked / cancelled / verify_failed / reopened`。verify 门：worker 回填提案 → 派发方校验（可执行＋覆盖目标）并登记（登记即锁定）→ done 另需 worker 自身评审环绿 → 服务在 worktree 真跑 verify → 绿即 ff-only 自动合并 → 目标仓集成 verify，失败字节级回滚。质量 3 连败 → blocked → 拍人。

### 11 个 MCP 工具

`task_create · task_dispatch · task_update · task_cancel · post · check_mail · board · doctor · wiki_read · wiki_write · enforce`。刻意没有 merge 工具（绿门是唯一合并路径）、没有删除（记录只可 cancel/closed）、没有仓名册（只有启用域休眠）。

## 配置

| 旋钮 | 默认 | 含义 |
|---|---|---|
| `AGORA_HOME` | `~/plaza` | 账本根（state/messages/events/wiki） |
| `.agora/inactive-repos` | 空 | 启用域名册：所列仓中 agora 全休眠（普通工具直通、agora_* 拒）。仓根路径一行一个；插件按 opencode 进程缓存名册——**编辑后需重启 opencode 服务生效**（按会话重载为遗留项）。中央名册，仓不能自我退出 |
| `AGORA_BOARD_INTERVAL` | 5s | board_tui 轮询间隔 |
| `enforce` | on | root 专用总闸，开/关均审计 |
| 链深/并发配额 | 5 / 8 | 派发硬闸 |

## 运维手册（Runbook）

**服务生命周期。** agorad 由插件惰性拉起（setsid＋单实例 flock），无 systemd/cron。端口发现看 `$AGORA_HOME/.agora/service.json`（首启 stdout 有 `AGORA_ROOT_TOKEN:` 行——install 已捕获）。任意时刻 `kill -9` 安全：状态是只追加快照流、断尾容忍、in-flight 链重启后重注入（同凭据、重绑会话）。

**opencode 服务重启。** 会话自持久化，agorad 探测后续跑。注意：`opencode serve` 重启可能轮换服务密码——消息面安全降级为落库补拉；重启 agorad 即重取凭据。平台已知项：opencode v2.1.4 偶发新会话不拉起 MCP 服务（e2e 排障记录在案），重试或新会话即恢复。

**常见操作。** 疑似僵住的 worker → 看 `board` 的 `last_seen`——系统只自动管死不管僵，cancel 或对同任务再 dispatch（换新凭据）。收到拍人 → `board?task=<id>` 看明细，三选一（取消/重派/放任）。服务恢复后插件仍拒 → 拒因带 `resetLift` 恢复指引（或重启 opencode 会话）。

**安全模型（接受面）。** 单机单人：bash 直写绕闸、偷牌、评审锚自写是已声明接受面——审计三件套（events.jsonl＋git 历史＋opencode 会话记录）是事后兜底而非实时防线。角色权限服务端强制（root/dispatcher/worker 矩阵；worker 永不能 cancel 或改 verify）。

## 状态

三计划已交付合并：agorad 核心（12 任务）、接入层（9 任务：MCP 壳/插件 hook·cred·inject·lift/安装/Agent View 仪表盘/T2·T3 真 opencode e2e）、启用域与技能（4 任务）。测试基线：python 182＋bun 41＋install 31 断言＋e2e 十三场景（含崩溃窗口、kill -9 恢复、顺序硬闸）。遗留项（旋转归档、tail 读等）由摩擦驱动。
