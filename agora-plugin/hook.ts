/**
 * hook 强制判定链（F1，设计 §5.1）：拦截判定纯函数，opencode 接线形状 T5 已实证收紧
 * （tool.execute.before 覆盖 MCP 工具＋改参直达；cwd＝会话 location，异步取）。
 * 语义与 agorad 服务侧一致：豁免面 B3（非 git、/tmp/opencode、enforce off）、
 * 读引导（state 账本经工具面，events/wiki 开放，主会话例外）、主会话对仓写入同拦。
 */
import { existsSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import type { Cred } from "./cred";

export const DISPATCH_HINT =
  "被拦：本机 git 仓直改仅限委托的隔离环境（cwd∈任务 env）。请经 agora 任务面派发（task_create/task_dispatch）持凭据作业；直接改仓仅限豁免面。";
export const NO_CRED_HINT =
  "被拦：无凭据缓存——可经真实 prompt 通道重注 BOOT+credential 自愈（配方见任务页），或 post(kind=question) 求派发方。";
export const READ_HINT =
  "被拦：tasks.jsonl 与 messages/ 属账本面，请走工具面（board/check_mail）；events.jsonl 与 wiki 可直接读。主会话不受此限。";

const TMP_ROOT = "/tmp/opencode"; // B3 设计定值
const WRITE_TOOLS = new Set(["edit", "write"]);
const READISH_TOOLS = new Set(["read", "bash"]);

export function agoraHome(homeDir = homedir()): string {
  // 判定链信任锚仅 env（gt-32c051 裁定 A）：~/.agora-home 指针是被辖方可写文件，
  // 若跟随则 enforce 旗/读边界可被一行 bash 挪根（终闸 C 册 blocker 实证）——禁加指针回退。
  // homeDir 参数仅供测试注入口（Bun 缓存 homedir）。
  if (process.env.AGORA_HOME) return process.env.AGORA_HOME;
  return join(homeDir, "plaza");
}

/** realpath 折叠：末端不存在时折叠到最深存在祖先再拼回（write 目标常尚未创建）。 */
function fold(p: string): string {
  let cur = resolve(p);
  const tail: string[] = [];
  for (;;) {
    try {
      return join(realpathSync.native(cur), ...tail);
    } catch {
      const parent = dirname(cur);
      if (parent === cur) return resolve(p);
      tail.unshift(basename(cur));
      cur = parent;
    }
  }
}

function under(root: string, p: string): boolean {
  const r = fold(root);
  return p === r || p.startsWith(r + sep);
}

/** git 仓判定：沿目标向上找 .git（目录或文件——worktree/submodule 形态）。 */
function inGitRepo(p: string): boolean {
  let cur = resolve(p);
  for (;;) {
    if (existsSync(join(cur, ".git"))) return true;
    const parent = dirname(cur);
    if (parent === cur) return false;
    cur = parent;
  }
}

/** 该工具是否落在判定链可能产出 hint 的类（write 引导面＋read/bash 账本读引导面）——
 * enforce 旗按需拉取的预过滤（T6 评审：豁免类工具不付拉旗开销）。 */
export function enforceRelevant(tool: string): boolean {
  return WRITE_TOOLS.has(tool) || READISH_TOOLS.has(tool);
}

/** write 判定类（F25 修正：仅 write 工具缓存未命中触发 cred_for_sid 拉取）。 */
export function isWriteTool(tool: string): boolean {
  return WRITE_TOOLS.has(tool);
}

/** wrong-door 拒因＋副本换算（尽力而为）：envDir basename 形如 {repo}__wt-* →
 * 主仓根＝envDir 父目录/{repo}；cwd 落主仓内则附副本对应路径，推导不出退化为仅列两侧实值。 */
export function wrongDoorHint(envDir: string, cwd: string): string {
  const m = basename(envDir).match(/^(.+)__wt-/);
  const mainRoot = m ? join(dirname(envDir), m[1]) : null;
  const conv =
    mainRoot && under(mainRoot, cwd)
      ? `；若编辑目标在主仓，对应副本路径＝${join(envDir, relative(mainRoot, cwd))}`
      : "";
  return `被拦：工作目录 ${cwd} 在任务 env ${envDir} 外${conv}——请在副本内编辑。`;
}

/**
 * 判定链（F1）：返回引导文案＝拒，null＝放行。
 * 凭据"活动"的本地代理＝持牌＋envDir＋cwd∈envDir；活动任务终审在服务侧（T5 接线对齐）。
 */
export function shouldBlock(
  tool: string,
  targetPath: string,
  cwd: string,
  cred: Cred | undefined | null,
  enforceOn: boolean,
): string | null {
  if (!enforceOn) return null;
  const target = fold(targetPath);
  if (READISH_TOOLS.has(tool)) {
    if (!under(join(agoraHome(), "state"), target)) return null; // events/wiki/其余裸读开放
    if (cred?.role === "root") return null; // 主会话例外全开
    return READ_HINT;
  }
  if (!WRITE_TOOLS.has(tool)) return null;
  if (under(TMP_ROOT, target) || !inGitRepo(target)) return null; // 豁免面 B3
  if (cred?.token && cred.envDir && under(cred.envDir, cwd)) return null;
  // 拒因分流（语义不变只改信息量）：主会话／无凭据／wrong-door 各吐专属文案；
  // 服务不可达默认拒由 index 在 lift 败态时拼 LIFT_RECOVER 附着，不在此分支。
  if (cred?.role === "root") return DISPATCH_HINT;
  if (!cred?.token || !cred.envDir) return NO_CRED_HINT;
  return wrongDoorHint(cred.envDir, cwd);
}
