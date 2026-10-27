# opencode-agora ③启用域＋编排技能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 F12 启用域（按仓休眠 agora）＋主会话编排双 skill（agora-dispatch／agora-ops）＋install 接入——补齐接入层的最后一块：主会话的流程智慧载体。

**Architecture:** 启用域＝插件级 denylist（会话仓根 ∈ 中央名册 → 全休眠）；skill＝opencode skill 形态（markdown 随 install 装载），内容源自 e2e 已验证的编排剧本（T7"目标即指令"＋状态驱动收尾）。

**Tech Stack:** 插件 TS on Bun；skill markdown；install bash。

**Spec:** `docs/reqs/opencode-agora.md` F12（2026-10-27 裁定）＋设计稿 §5＋t4-coverage.md

## File Structure

```
agora-plugin/scope.ts          # 启用域：名册读取＋仓根匹配＋休眠判定
agora-plugin/index.ts          # 休眠分支接线（直通 + agora_* 拒）
skills/agora-dispatch/SKILL.md # 编排剧本（重流程）
skills/agora-ops/SKILL.md      # 快操作纪律（取消级联确认等）
install.sh                     # skills 复制＋名册空骨架
```

## Global Constraints

- 名册＝`$AGORA_HOME/.agora/inactive-repos`（仓根绝对路径一行一个，中央名册；初始为空，用户自录）
- 休眠语义两面：普通工具直通（不拦/不读引导/不 lift/不附牌）＋ `agora_*` 拒（"此仓未启用 agora"）
- worktree 会话不受名册影响（git toplevel＝worktree 自身路径，精确匹配）
- 每测试 `Req: F12`（插件）/内容审计锚（skill）；bun/python/install 回归全绿

## Review Focus

1. 名册匹配的边界（尾斜杠/相对路径/symlink 仓根）→ T1
2. 休眠分支绕过检查的完备性（lift/enforce 拉取/附牌/读引导四处全断）→ T1
3. skill 内容与已验证事实的一致性（goal 纪律＝T7 结论；取消确认＝F11 级联语义）→ T2/T3
4. install 对既有部署的升级幂等（skills 新增不改旧件）→ T4

---

### Task 1: 插件启用域（F12）

**Files:**
- Create: `agora-plugin/scope.ts`；Modify: `agora-plugin/index.ts`
- Test: `agora-plugin/scope.test.ts`（bun test）

**Interfaces:**
- Produces: `loadInactiveRepos(): string[]`（读名册，缺失/坏行容错）；`isDormant(repoRoot: string): boolean`（精确路径匹配）；index.ts 休眠分支：`dormant → return {output: undefined}`（普通工具直通，四处检查全短路）；`dormant && tool 匹配 ^agora_ → 拒"此仓未启用 agora"`

- [ ] **Step 1: 红测** — 名册命中：edit 直通（pullEnforce/attach/lift/readGuide 零调用）＋agora_board 拒；名册外：现行行为不变；worktree 路径不匹配主检出条目；尾斜杠/坏行容错。`Req: F12`
- [ ] **Step 2-5:** 循环；commit `feat(plugin): 启用域——中央名册按仓休眠＋agora_* 拒`

### Task 2: agora-dispatch skill

**Files:**
- Create: `skills/agora-dispatch/SKILL.md`

**Interfaces:**
- 内容七节（源自已验证事实）：何时派发（F9 砍·被拦即转派发；无关仓并行/耦合仓甲案 after:）／goal 写作纪律（目标即指令·T7 三轮事故结论）／verify 生命周期（回填→校验→登记或驳回附因）／监控节奏（board 察看·last_seen 查僵·升级流）／失控处置三选一（cancel/重派/放任判据）／终态处置（blocked 重派新凭据·reopened 勿扰·done 后勿动）／禁手清单（不直写·不手动合并·OFF 人权限）
- 验收：内容审计清单逐节对锚（每节尾注来源：需求 Fn/e2e 场景/T7 排障志）；无未验证的发明

- [ ] **Step 1:** 按 t4-coverage＋T7 report 逐节起草；**Step 2:** 审计清单核对（controller 复核）；**Step 3-4:** commit＋回归 `feat(skill): agora-dispatch——编排剧本`

### Task 3: agora-ops skill

**Files:**
- Create: `skills/agora-ops/SKILL.md`

**Interfaces:**
- 内容：取消级联确认纪律（先 board 展链全景→向人确认级联范围→task_cancel——MCP 路径无 y/n 门，纪律由 skill 承载）／enforce 治理（root 权限＋审计提示）／启用域名册查看（inactive-repos 内容＋"休眠仓里 agora 不可用"）／doctor 快查解读（年龄排行/孤儿/~~账本体量~~（无实现锚，T3 审计裁定略去））
- 验收：同 T2 形态（逐节对锚 F11/F1/F12/设计 §5）

- [ ] **Step 1-4:** 起草→审计→commit `feat(skill): agora-ops——取消确认与治理快操作`

### Task 4: install 接入＋核销

**Files:**
- Modify: `install.sh`、`tests/test_install.sh`、`docs/superpowers/plans/t4-coverage.md`

**Interfaces:**
- install：skills 复制到 `~/.config/opencode/skills/`（幂等，二装不改）＋`inactive-repos` 空骨架（0644 owner 可写）；test_install 断言两件在位＋二装幂等；t4-coverage 增 F12 行（e2e 锚＝T1 bun 测＋休眠场景）

- [ ] **Step 1-4:** 红测→实现→全绿→commit `feat(install): skills＋名册骨架接入`

---

## 收尾

T4 绿后：全量回归（python＋bun＋install＋t2/t3 e2e 各一轮）→ 终轮全分支评审 → ff-only 合并 main。
