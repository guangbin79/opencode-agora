# review-loop：开源用户标准指导

agora 的自动合并由两道质量闸把守：**verify 门**（测试由服务真跑）与**评审环**
（代码评审锚）。本文面向想在仓库里接入评审环的开源用户：契约是什么、没接会怎样、
最小实现长什么样、worker 要守什么约定、边界在哪。

## 1. 契约面

- 任务环境（env worktree）内存在 `.review-loop/` 目录 ⇒ 锚门生效。
- 锚文件为 `.review-loop/.last-reviewed`，内容是完整 40 位十六进制 commit sha
  （解析容许首尾空白）。服务在 env 内跑 `git rev-parse HEAD` 与文件内容比对；
  格式的唯一权威实现是 `agorad/access/anchors.py` 的 `read_anchor`（经
  `agorad/main.py` 的 `assemble` 注入 `Gate`，服务无 anchor 入参、自取锚）。
- 锚绿 ＝ head 等于 env 分支当前 HEAD 且 clean=true，含义：**该提交已通过评审，
  无 blocker/high 未决**。
- 服务在两处读取锚（`agorad/core/gate.py`）：

| 时机 | 行为 |
| --- | --- |
| `accept_done`（done 受理） | 仅 `.review-loop/` 在位时检查：缺锚/落后 → 拒 done（`anchor`）；head ≠ done body 的 sha → 拒；未绿 → 拒。均审计 `done_rejected` 并附修复指引 |
| `run()`（合并前重验） | verify 绿后重读锚：缺失/落后/未绿 → 审计 `review_absent_no_merge`，任务驻留 `done` 不合并 |

合并前重验堵住两个窗口：accept→run 之间锚被改动（TOCTOU）、done 之后偷提交。

## 2. 降级路径（没装评审环也能用）

- `.review-loop/` 目录不存在：done 受理**完全跳过锚检查**（目录缺失即无此要求）。
- 门体照常跑 verify；合并前发现锚缺失 → 任务驻留 `done`，**不自动合并**，
  通知派发方人工审阅合并（或 cancel）。
- 一句话：**无评审环也可正常用，只是不自动合并**——自治是双闸（测试＋评审）挣来的。

## 3. 最小参考实现

评审器可插拔：OCR / CodeRabbit / 人工皆可——任何能产出 blocker/high 清单的
评审器都行。agora 只认锚，不关心评审器是谁。

最小参考脚本（对 env 任务 diff 跑评审，绿则写锚）：

```bash
#!/usr/bin/env bash
# review-loop.sh <env_dir> <target_branch> —— 每次提交后运行；未绿退出非 0
set -euo pipefail
cd "$1"
base=$(git merge-base HEAD "${2:?target branch required}")
sha=$(git rev-parse HEAD)
# ↓ 换成你的评审器：OCR / CodeRabbit / 人工清单，输出含 blocker/high 字样的行
findings=$(your_reviewer "$(git diff "$base"..HEAD)")
if printf '%s\n' "$findings" | grep -qiE 'blocker|high'; then
  printf '%s\n' "$findings"; exit 1     # 未绿：修复后重新提交并重跑本脚本
fi
mkdir -p .review-loop
printf '%s\n' "$sha" > .review-loop/.last-reviewed
```

脚本三步即契约全部：取任务 diff → 评审 → 绿则把 HEAD sha 写进锚。

## 4. worker 行为契约

每次提交后：重跑评审环使 `.review-loop/.last-reviewed` 锚追上 HEAD，然后
`post(done, body=完整40位sha, 无附言)`；结论文本另发 `post(kind=note)`。
（转述自 `agorad/core/task.py` `_boot` 下发给 worker 的开工须知。）

## 5. 威胁模型（写实）

锚由 worker 自己写进 env，服务只读不验作者——**这是流程协议，不是安全边界**。
worker 技术上可以伪造 clean=true 跳过评审。真正的质量闸是 **verify 门**：命令由
服务在 env 与目标仓各真跑一次，worker 无法只报结果不跑命令。评审环提升的是评审
覆盖与流程纪律，防的是「没评审就合并」的疏漏，不防恶意 worker——不要把锚当
反作弊机制宣传（README 安全模型的接受面已如实标注此点）。

## 6. 进阶：idle 自触发插件

review-loop 可做成 opencode 插件在会话空闲时自触发（有编辑就自动评审，无需人工
喊跑）。三条设计笔记（均为实践中撞出来的坑）：

1. **评审环自身产物不得计入「编辑」**：锚写入、评审报告落盘若被算作变更，欠债
   自我续期，评审永无止境。
2. **触发前记账落盘必须后置于欠债判定**：先判有无未评审变更，再写记账；否则
   无罪也写盘，且写入可能与目录删除相撞。
3. **两次触发间须有冷却窗口**：否则空闲事件风暴式空转，烧算力不产评审。
