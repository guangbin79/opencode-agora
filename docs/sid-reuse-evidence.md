# opencode session id 跨对话复用疑点——证据报告（ag-ff7dae）

> 采集时间 2026-11-10 10:04–10:40 CST；环境 opencode v2.2.6；全部勘察只读（SQLite `mode=ro` 打开，未触碰 `~/.local/share/opencode/` 写面、未重启服务）；agora 账本只读（tasks.jsonl/events.jsonl 经文件裸读）。

## 背景与问题

2026-11-09 观察到：任务 ag-4628f9 与 ag-272b49 的 `dispatcher_sid` 均为 `ses_e4a4d868bffehrkdlwyjq4m8eO`，与「另一对话」的 req-gate 记录同 sid，但任务疑似并非该对话所派。假设：opencode serve 重启后多对话共用 session id。本报告用会话存储＋agora 账本＋命令面实现三源对照裁决。

## 一、session id 分配与持久化机制（存储实证）

存储位置：`~/.local/share/opencode/opencode.db`（SQLite，WAL 模式，1.5GB）；会话表 `session_v2`，`id text PRIMARY KEY`，含 `project_id/directory/title/time_created/time_updated` 等列。

- **分配**：id 形如 `ses_` + 24 字符。经验形态：相近时刻创建的会话共享前缀簇（如本任务批三个 worker 会话 `ses_e3ccd019…/ses_e3ccd02d…/ses_e3ccd045…` 创建于 10:04:21–10:05:05），但**整体非时间有序**（11-07 会话前缀 `e4a4d8…` > 11-10 会话前缀 `e3ccd0…`），中段 3 字符（ffe/ffd/2ff…）与尾段为随机段——顺序批次前缀＋随机段，非纯随机、非时间戳直译。
- **唯一性**：全库 `SELECT COUNT(*), COUNT(DISTINCT id) FROM session_v2` → **5472 / 5472**，主键约束下零重复。
- **持久化**：会话行跨 serve 重启存活（2026-11-07 19:08:55 创建的会话存活至今且 `time_updated` 持续刷新）；重启不清存储、不改 id 格式。opencode 日志（`log/opencode.log`）仅含进程 spawn 行，无重启边界标记，不影响本结论（存储层证据已闭合）。

## 二、跨对话 sid 复用：存储层否证

目标 sid `ses_e4a4d868bffehrkdlwyjq4m8eO` 在存储中**恰一行**：

```
id=ses_e4a4d868bffehrkdlwyjq4m8eO  directory=/home/guangbin/silicon-memory
title=semantic-memory 还有用吗     created=2026-11-07 19:08:55 CST
updated=2026-11-10 10:04（勘察时仍活跃）
```

同目录同期存在多个**各自独立 id** 的对话（16:49「参考opencode编写README」、16:56 worker、17:37「经验记忆写入 dotfiles 改为提交」等）——opencode 对「同目录多对话」各发各的 id，不存在复用。**「多对话共用一个 session id」在 opencode 存储层不成立**：该 sid 背后是一个 11-07 起长活、跨仓派发的主力主窗会话（agora 账本可见它 11-07→11-10 持续建任务，这是主会话行为，非异常共享）。

「同 sid 出现在不同对话记录里」的全部实例可归因于**消费层伪影**，与 opencode 无关：

### 伪影 A：agora `dispatcher_sid` 记账（主现象，已修复）

任务账本（plaza/state/tasks.jsonl 快照行）时间线：

| 任务（创建 CST） | project_dir | root_sid（真实建单窗） | dispatcher_sid（落账值） |
|---|---|---|---|
| ag-0d9b8c（11-07 19:24） | dotfiles | ses_e4a4d868… | ses_e4a4d868…（主窗自建，自洽） |
| ag-4628f9（11-09 16:53） | HXNativeApp | **ses_e41342d5…** | ses_e4a4d868… |
| ag-272b49（11-09 19:45） | coding-gates | **ses_e469fdab…** | ses_e4a4d868… |
| ag-145899（11-09 22:07） | Work | **ses_e3f6425a…** | ses_e4a4d868… |
| ag-588eb8（11-09 22:21） | opencode-agora | **ses_e3f54d798…** | ses_e4a4d868… |
| ag-ff7dae 等（11-10，f80309 后） | 各仓 | ses_e4a4d868… | root（daemon 常量） |

关键反证就在表内：**root_sid 各不相同**（≥5 个真实窗口并存）而 dispatcher_sid 钉死同一值。机制（ag-588eb8 证据页 `wiki/drafts/root-poison-evidence.md` 已结案，此处仅引用）：R25-A bind_main 时代，root 牌账本 sid 被活绑定到「最近活跃主会话」（当时即 ses_e4a4d868），而 CLI 建任务落账取 root 牌账本 sid（task.py:269）——**任何窗口**的 CLI 建单都记成同一个 sid。当日审计 main_sid_bound 34 行在 ≥8 个会话 sid 间翻转即此机制的直接痕迹。已由 ag-f80309 B 案根除（commit 3e8d6ba：bind_main 摘除 rebind、建单归属改记对齐调用 sid）；修复后新任务行 dispatcher=root＋root_sid=真实窗，不再漂移。**这不是 opencode 层复用，是 agora 记账伪影。**

### 伪影 B：req-gate 命令的 sid 探测启发式（在位，未修）

用户对照面「另一对话的 req-gate 记录同 sid」的来源在命令实现里明文可见（coding-gates/req-gate/command.md L38）：

> 会话 ID 获取方式同插件契约——**实测本机会话存储中本项目目录最新会话**，取不到则写 manual-…；记录写入 `<TMPDIR>/opencode/req-gate/<当前会话ID>.md`

即 req-gate 记录的 sid **不是会话自证**，而是「查存储取本项目目录最新会话」的启发式。多窗同仓并发、查询口径漂移、窗口时差，都会把他人（往往是长活主窗）的 sid 盖到记录文件名上——这正是「跨对话同 sid」观感的直接制造机。佐证：

- **harness 自证源存在且准确**：opencode 原生向 spawn 进程注入 `OPENCODE_SESSION_ID`＝本会话 id（本 worker 会话 2026-11-10 实测 `env` 输出＝`ses_e3ccd0191ffem6q0PyOXjhn8uc`，与本任务 worker_sid 一致；该变量不在 agora 插件注入面，系 opencode 原生行为）。命令面根本无需探测存储。
- 记录抽样核对：现行 `/tmp/opencode/req-gate/`（tmp 会滚动清理，11-09 记录已不在）；历史仓内 `.omo/req-gate/`、`.req-gate/` 抽 4 份 HXNativeApp 记录，文件名 sid ↔ session_v2 目录 4/4 对上（记录本体寻获的样本无误标）。未寻获 ses_e4a4d868 命名的记录本体（tmp 已清），但启发式缺陷是文本明证，现象可完整复现路径。

## 三、结论与建议

**结论**

1. opencode v2.2.6 会话存储层**无 sid 跨对话复用**：id 主键唯一（5472/5472）、目标 sid 全库单行、同目录多会话异 id、存储跨重启持久。原假设「serve 重启后多对话共用 session id」**被否证**。
2. 「同 sid 出现在不同对话」＝两个**消费层伪影**：agora `dispatcher_sid` 记账伪影（R25-A bind_main 时代产物，ag-f80309 已修）＋ req-gate 命令的「存储探测最新会话」启发式（在位）。sid 是被**错记/错探**，不是被**复用**。
3. 本证据**不支持**向 opencode 上游报 bug；真实缺口在消费侧对「会话身份自证」的缺失使用。

**建议**

1. req-gate（及一切命令/脚本面）改用 `$OPENCODE_SESSION_ID` 作为会话 id 来源（harness 原生、本 worker 实测准确）；存储探测仅作取不到 env 时的回退，且回退产物应在文件名或正文标注「derived（非自证）」以防伪影再次入账。
2. 消费侧防御通则：凡按 sid 绑定身份的机制（agora 牌/`cred_for_sid`、req-gate-enforcer 放行凭证等），只信自证头或服务端注入，拒绝「查询存储猜身份」类启发式——agora 侧 ag-32a5e2/ag-f80309 的教训同源：**身份必须自证或由可信面附证，不能从共享存储反推**。
3. 上游动作（可选、非 bug）：opencode 已有 env 自证面但未见文档化——向上游提文档/稳定化承诺（`OPENCODE_SESSION_ID` 对 spawn 进程的注入语义）比报复用 bug 更贴合证据。

## 证据附录（复现命令）

```bash
# 会话存储（只读）
sqlite3 "file:$HOME/.local/share/opencode/opencode.db?mode=ro" \
  "SELECT COUNT(*),COUNT(DISTINCT id) FROM session_v2;"                       # 5472|5472
sqlite3 "…?mode=ro" "SELECT id,directory,title,datetime(time_created/1000,'unixepoch','localtime') \
  FROM session_v2 WHERE id='ses_e4a4d868bffehrkdlwyjq4m8eO';"                 # 单行
sqlite3 "…?mode=ro" "SELECT id,directory,datetime(time_created/1000,'unixepoch','localtime') \
  FROM session_v2 WHERE directory='/home/guangbin/silicon-memory' \
  AND time_created>1793900000000 ORDER BY time_created;"                      # 同目录多会话异 id
# agora 账本（只读）
jq -r '.tasks|to_entries[]|select(.value.dispatcher_sid=="ses_e4a4d868bffehrkdlwyjq4m8eO")\
 |[.key,.value.project_dir,.value.root_sid,.value.dispatcher_sid]|@tsv' \
  /home/guangbin/plaza/state/tasks.jsonl
# 自证源
env | grep OPENCODE_SESSION_ID   # spawn 进程内＝本会话 id
# req-gate 启发式原文
sed -n '38p' /home/guangbin/coding-gates/req-gate/command.md
```

边界与未覆盖项：req-gate 的 ses_e4a4d868 记录本体未寻获（tmp 滚动清理）；`OPENCODE_SESSION_ID` 只验证了 worker 会话形态（主窗/TUI 形态未验证，同机制推定）；opencode id 生成器内部实现未逆向（二进制分发），id 形态结论均为存储行为实证。
