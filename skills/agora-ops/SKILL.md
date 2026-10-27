---
name: agora-ops
description: agora 收摊与治理快操作。用户说"取消任务/停掉/关闸/开了 enforce/关掉 enforce/哪些仓休眠/服务健康/查一下 doctor"等，或需中止派发链、拨隔离闸、查启用域名册、解读 doctor 输出时激活。
---

# agora-ops——收摊与治理快操作

本 skill 管收摊操作：中止链、治理 enforce、启用域名册、doctor 体检。干活流程（派发/goal/verify/监控/失控三选一）见 agora-dispatch——触发词以动词分界：派发与问进展属 dispatch，停掉与治理属本 skill。工具面即 MCP 11 件中与本 skill 相关的四件：`task_cancel`／`board`／`doctor`／`enforce`。

## 1. 取消级联确认纪律

MCP 路径的 `task_cancel` 无确认门（无 board-tui 式 y/n），确认纪律由本 skill 承载。取消必走四步：

1. **展链**：`board` 按目标任务的 dispatcher 展开链级视图，看清其全部下级。
2. **复述**：向人复述级联范围——"取消 gt-x 将连带其 N 个下级任务"。
3. **确认**：得人点头后才动手。
4. **执行**：`task_cancel`。返回值即级联取消数，向人回报。

**裁定：级联数 0（叶子任务）免确认直执行**——无人受连带，确认门空转。判断依据即第 1 步 board 链视图中该任务无下级。

服务侧语义（已定，勿臆测勿替代）：级联递归中止全部下级，在飞 worker 实时收终止通知，任务置 cancelled，环境即回收；merged/closed/cancelled 不动（已 merged 不回滚）；done 在 verify/合并门临界段内→cancel 排队（pending_cancel），门走完生效；空链 cancel 幂等成功＋审计；凭据即作废、配额即释放；不做会话强杀。权限：人可中止任一在飞或 blocked 链；dispatcher 只能中止自己派出的子链（越权必拒＋审计）；worker 调 cancel 必拒。

> 来源：F11／设计§4.1（task_cancel 语义）／agorad/core/task.py:106-131（级联递归＋queue_cancel＋noop 审计）；级联数 0 免确认＝本 skill 裁定（T3）

## 2. enforce 治理

`enforce(on|off)` 是服务侧全局紧急阀，不是日常开关：

- **权限**：仅主会话 root 牌可拨；worker/dispatcher 调用必拒，拒绝写 auth_denied 审计。开与关均写审计行——每次拨动有账。
- **旗标语义**：服务持旗、只内存持有，服务重启默认 on（off 不跨重启，无须回拨）。
- **off 效果**：git 仓写拦截整体豁免，其余照常——agora_* 工具面、审计、名册、verify 门均不受影响。
- **服务不可达**：旗标核验默认按 on 处理（安全向拒），off 关不掉这个兜底。
- **与 F12 启用域正交**：off 是全局临时放开（紧急排障用，用完即拨回——重启也自动回 on）；休眠名册是按仓常驻关闸。两者互不替代。

> 来源：F1（豁免面 enforce(off)／仅 root 牌／开/关写审计）／设计 B1/B3/§4.9（root 牌、重启默认 on＝auth.py:29）／F12（正交裁定）／agorad/core/auth.py:94-100（非 root 拒＋auth_denied）

## 3. 启用域名册

- **查看**：读 `$AGORA_HOME/.agora/inactive-repos`——中央名册，仓根绝对路径一行一个。向人展示时按行报仓。
- **语义**：名册内仓＝agora 整体休眠——普通工具（edit/write/read/bash）直通：不拦、不读引导、不拉起服务、不附牌；`agora_*` 工具一律拒"此仓未启用 agora"。目标仓在册＝不能对它派发，转活跃仓或报人手改。
- **边界**：精确仓根路径匹配（无前缀/模糊，尾斜杠容错）；worktree 会话不受名册影响（git toplevel＝worktree 自身路径，对不上名册行）；名册缺失/不可读＝空名册（全员活跃）。
- **修改**：直接编辑文件（加/删行），无工具面——名册不热载，**新会话生效**；改完告知人"已生效于之后新开的会话"。
- **常态**：名册初始为空、用户自录；保护名册（拒 task_create 目标仓）已裁定不做，勿越权加校验。

> 来源：F12／决策记录（名册初始为空／保护名册不做）／agora-plugin/scope.ts:1-37（中央名册/精确匹配/缺失＝空/不热载）／index.ts:85-87（休眠分支短路）

## 4. doctor 快查解读

`doctor` 无参直调，返回账本侧事实三组，逐组解读：

- **`age_rank`**：任务年龄排行（创建至今）——前列者占额嫌疑。
- **`last_seen_rank`**：最后活动排行——worker 每次调用服务即刷新 last_seen，活着但排名垫底（久未动）＝**僵态候选**。"僵"不自动处置（无巡检、无 stalled 定时器），由人判断后转 dispatch 失控三选一（cancel／再 dispatch／放任）。
- **`env`**：每任务 env_dir 在盘性（`on_disk`）——false＝登记环境已不在盘（merged/cancelled 回收后属正常）。
- **孤儿目录**：盘上无任务对应的环境目录永不自动删；当前 doctor 只报在册环境的在盘性，盘上孤儿扫描属②期——on_disk 与状态对不上、或人目视发现无主目录时，才值得人 `ls` 复核一圈。

何时值得人看一眼：age 前列且状态非在飞/blocked、last_seen 长期停滞、on_disk 与任务状态矛盾。三者之外勿主动巡检——不上 cron、不做全局扫描。

> 来源：F5（僵不自动处置、last_seen 刷新）／设计§4.7（doctor 含任务年龄/最后活动排行）＋B6（孤儿永不自动删）／agorad/core/report.py:58-76（age_rank/last_seen_rank/env on_disk；孤儿扫描属②期＝:59）
