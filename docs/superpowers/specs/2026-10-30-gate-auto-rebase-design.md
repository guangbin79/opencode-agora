# F32 门内自动 rebase——同仓并行 non-ff 干净同步自动化

日期：2026-10-30　状态：已批准（用户 2026-10-30「认可」）
需求条目：`docs/reqs/opencode-agora.md` F32（本次新增，条目文本见附录 A）

## 意图与动因

同仓并行任务共享目标分支：先合并者推进分支后，后合并者的试合并必然真 non-ff
（`merge_ff` 祖先判定，F29 语义）。现行处置＝reopened 弹回 worker，手动同步
（rebase 到新基线）后重报 done——整轮 env verify＋集成 verify＋评审重锚全部重走。
同仓并行碰撞在 dogfooding 中高频出现，往返成本真实。

目标分支改动（用户选定方案，2026-10-30 会话）：**门在试合并遇真 non-ff 时，
自动在 integration worktree 内把任务提交 rebase 到目标分支当前 HEAD，集成
verify 验证 rebase 后内容，绿则 ff 推进。** rebase 是让 ff 重新可行的手段，
最终推进仍 ff-only——线性历史铁律不变。

质量口径（用户 2026-10-30 确认）：**干净 rebase＝重测不重审**（集成 verify
跑在 rebase 后内容上；patch 等价故评审无增量信息）；**冲突 reopen＝全量重走**
（worker 解冲突后重 done，锚失效→评审重锚照旧）。

## 目标 / 非目标 / 反目标

**目标**
1. 干净同步（rebase 无冲突）零往返：门内 rebase → 集成 verify → ff 推进，
   worker 无感知，env 分支与 `.review-loop` 锚均不动。
2. 冲突路径行为不变：rebase 冲突 → abort → reopened（不计质量连败，
   F7 口径），worker 解冲突后重走完整 verify 门＋重审。
3. 身份可追溯＋崩溃安全：`rebased_from/to` 成对留痕；崩溃重排幂等不假重开。

**非目标（第一版明确不做）**
- 条件重审（rebase 后 diff ≠ 已审 diff → 强制重审）：先靠审计留痕观察实践。
- 放松 ff-only / 引入 merge commit：禁止。
- 改 accept/评审环口径：评审仍在 done 前、锚仍在 env 分支 HEAD，一字不动。

**反目标（失败形态）**
- rebase 吞掉非预期提交、merged 落账 sha 与验证内容脱节、崩溃后假重开或重复合并。

## 已裁定的设计（builder 不得偏离的部分）

### 流程改造点（gate.run）

试合并 `merge_ff(env, integration)` 返回 "non-ff" 时，不直接走 `_non_ff`，
改为依次：

1. `rebase_onto(env, integration)`（gitx 新方法）：
   - mb = `merge-base(<目标分支或 HEAD>, <env 分支>)`（现有 `_merge_base` 语义）
   - integration（detached at 目标 HEAD）内：`git checkout --detach <env 分支>`，
     然后 `git rebase --onto <目标 HEAD> mb`。
   - **detached rebase，零临时分支**：不建 ref、不碰 env 分支、不碰目标检出；
     per-worktree sequencer 状态使并发任务的 integration rebase 互不干扰。
   - 成功：integration 停在重放后 sha R，工作区即 rebase 后内容。
2. rebase 失败分类（F29 纪律：分类先于动作，禁混类）：
   - 冲突（rebase 停在冲突态）→ `git rebase --abort` → 走现有 `_non_ff`
     reopened 路径（窄竞态守卫 `contains` 照旧先行）。文案说明「同步冲突」。
   - 其他非零（对象损坏等环境病）→ 如实 RuntimeError 上抛，走门内 except。
3. 成功路径接续：集成 verify 跑在 R 上（现执行点不变，验证内容＝推进内容）→
   绿 → `set_fields(commit=R, rebased_from=E)`（**先于最终推进**）→
   审计 `rebased` 事件（from=E, to=R）→ 最终推进：
   `merge --ff-only R`（在目标仓，分支检出态合并，与现状 merge_ff 同款语义；
   ensure_clean 与窄竞态守卫照旧）→ `_succeed`。
4. 集成 verify 败 → reopened（rebase 后内容未过闸，目标分支未动；
   计质量连败，F7 口径）。worker 看到的现场：env 分支仍在 E，重走 done 即可
   （门会再 rebase）。

### 身份与崩溃矩阵

- env 分支永不重写 → 锚（head=E）全程有效。
- 锚重验判定（builder check 裁定①，堵 httpd 重排死锁）：`read_anchor` 的
  head 比对对象为 `t.rebased_from or t.commit`——rebase 落库后崩溃重排的
  done 任务（commit=R、锚=E）重入门时锚检查用 rebased_from=E 判过，
  才能走到再 rebase；无 rebased_from 时行为与现状一字不差。
- `set_fields(commit=R, rebased_from=E)` 必须先于最终推进落库：
  - rebase 中崩：t.commit 仍 E，contains(E)=False → 重走门重新 rebase。安全。
  - 落库后推进前崩：t.commit=R，contains(R)=False → 重走门再 rebase。
    安全（builder check 裁定③：重跑落新 sha R′，不要求 R′==R——旧 R 从未
    推进，新值同样安全；不做 committer date 钉死）。
  - 推进后 transition 前崩：contains(R)=True → `_succeed` 补终态。安全。
- `_succeed` 落账 merged commit=R，audit 事件带 rebased_from=E（builder
  check 裁定②：从 state 读，崩溃补终态路径才有值可取）。
- `rebased_from` 生命周期：rebase 成功落库时置、**accept_done 每次受理清空**
  ——worker 重开后的新 done 携新 commit，陈旧 rebased_from 既会毒化锚判定
  也会伪造留痕（反目标「留痕失真」），受理即清是唯一清点。
- 等价提交边界：rebase merge backend 缺省 drop 补丁等价提交。全部提交等价
  （上游已含同变更）→ R == 目标 HEAD → ff 推进＝幂等推进（merge_ff
  head==src 的既有 ok 语义），任务以零增量 merged 收口，审计可见。

### 验收（可执行判据，Req: F32）

- gitx 层：干净重放成功且 env 分支 sha 不动；冲突 abort 且分类为冲突；
  环境病如实上抛；等价提交全被 drop 时返回目标 HEAD（no-op 形态）。
- gate 层：non-ff → 自动 rebase → 集成 verify → merged @R 且审计含
  rebased_from=E；集成 verify 败 → reopened 计连败、目标分支字节级不动；
  冲突 → reopened 不计连败；崩溃矩阵三态 contains 幂等；integration 目录回收
  后无 rebase 残留状态。
- 全量 `python3 -m pytest tests/ -q` 绿；既有 non-ff 用例按新语义同步
  （拒并 reopened 仅剩冲突与窄竞态两类来源）。

### 文档同步面

- reqs：F32 条目（附录 A 原文落册）。
- README（中英）：生命周期/合并文案一句（non-ff 自动 rebase）。
- `skills/agora-dispatch/SKILL.md`：「non-ff 拒并→reopened」收窄为「同步冲突
  →reopened；干净同步门内自愈」。
- F7 条目不改写（需求只替代不改写）：F32 即替代记录。

## 留给 builder 的部分

- gitx 新方法的签名与放置（与 merge_ff 同层；复用 `_git`/`_merge_base`）。
- 冲突态检测的具体判据（如 rebase-merge 状态目录存在性 or rc＋stderr 形态）。
- 测试夹具细节（构造 non-ff 的两任务序列、伪造 rebase 环境病）。
- e2e（t2/t3）若覆盖 non-ff 场景的同步改动；先 grep 确认是否触及。

---

## 附录 A：reqs F32 条目原文（落册到 `docs/reqs/opencode-agora.md`，编号顺延 F31）

### F32 门内自动 rebase——同仓并行 non-ff 干净同步（2026-10-30，用户裁定）

同仓并行后合并者试合并必真 non-ff（F29 祖先判定），现行 reopened 弹回 worker 手动同步重走全轮 verify。改法（设计文档 `docs/superpowers/specs/2026-10-30-gate-auto-rebase-design.md`）：门内试合并遇真 non-ff 即在 integration worktree（detached）内把任务提交 `rebase --onto <目标 HEAD>` 重放（mb=merge-base）——零临时分支、env 分支与 `.review-loop` 锚不动、最终推进仍 ff-only（线性历史不变）；rebase 后集成 verify 跑在新内容上（验证内容＝推进内容），绿则 set_fields(commit=R, rebased_from=E) 先落库再 ff 推进，审计 rebased 事件成对留痕；崩溃矩阵三态 contains 幂等不假重开。分类纪律沿用 F29：冲突→abort→reopened 不计连败（文案注明同步冲突），环境病如实上抛。等价提交全被 drop→R==目标 HEAD＝幂等推进零增量 merged。质量口径：干净 rebase 重测不重审（patch 等价），冲突 reopen 全量重走。非目标：条件重审、放松 ff-only。
**验收**：干净重放成功且 env 分支 sha 不动；冲突 abort→reopened 不计连败；环境病如实上抛；等价提交 drop 形态 no-op merged；集成 verify 败→reopened 计连败目标分支字节级不动；崩溃三态 contains 幂等；integration 回收无 rebase 残留；全量 pytest 绿（Req: F32）。

---

## 附录 B：builder check 裁定（2026-10-30，本会话）

三问三裁，正文已并入：①锚重验对 rebased_from 判定（否则落库后崩溃重排死锁
于 review_absent_no_merge）；②rebased_from 生命周期＝受理即清＋_succeed 从
state 读（防跨轮伪造留痕）；③放弃 R 确定性（重跑落新 sha，安全性等价）。
builder 确认可推导项：最终推进 --ff-only 语义、等价提交 drop、冲突/环境病
分类、detached rebase 对 env worktree 检出无碍、sequencer 残留随
remove_integration 收敛、未合并 env 分支 -D 级联、空区间 no-op。

## 附录 C：req-gate 快检结论（本应落 `<仓>/.omo/req-gate/`，因 agora 所有权闸改记于此）

结论：PASS（选项②）。需求：F32 门内自动 rebase（用户 2026-10-30 裁定）。
验收标准＝本文「验收（可执行判据，Req: F32）」节全部条目＋全量 pytest 绿。
