/**
 * hook 强制判定链（F1，设计 §5.1）：拦截判定纯函数，opencode 接线形状 T5 已实证收紧
 * （tool.execute.before 覆盖 MCP 工具＋改参直达；cwd＝会话 location，异步取）。
 * 语义与 agorad 服务侧一致：豁免面 B3（非 git、/tmp/opencode、enforce off）、
 * F33 门记录目录（.omo/.req-gate，插件侧新增——门记录写本身即被闸死锁面）、
 * 读引导（state 账本经工具面，events/wiki 开放，主会话例外）、主会话对仓写入同拦。
 */
import { existsSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import type { Cred, PullReason } from "./cred";

export const DISPATCH_HINT =
  "被拦：本机 git 仓直改仅限委托的隔离环境（cwd∈任务 env）。请经 agora 任务面派发（task_create/task_dispatch）持凭据作业；直接改仓仅限豁免面。";
export const NO_CRED_HINT =
  "被拦：无凭据缓存——可经真实 prompt 通道重注 BOOT+credential 自愈（配方见任务页），或 post(kind=question) 求派发方。";
export const READ_HINT =
  "被拦：tasks.jsonl 与 messages/ 属账本面，请走工具面（board/check_mail）；events.jsonl 与 wiki 可直接读。主会话不受此限。";

const TMP_ROOT = "/tmp/opencode"; // B3 设计定值
const GATE_RECORD_DIRS = new Set([".omo", ".req-gate"]); // F33：req-gate 门记录目录（新旧同收）
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

/** F33 豁免面：req-gate 门记录目录（basename 级，路径任一段命中即豁免）。 */
function inGateRecordDir(p: string): boolean {
  return resolve(p).split(sep).some((seg) => GATE_RECORD_DIRS.has(seg));
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

/** read/bash 目标落 state 账本子树（READ_HINT 面才消费凭据）——F27 readish 拉取 gate，
 * 防普通 read 每次白付拉取 HTTP。 */
export function readishGated(tool: string, targetPath: string): boolean {
  return READISH_TOOLS.has(tool) && under(join(agoraHome(), "state"), fold(targetPath));
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
 * pull＝本次拉取拒因（F27，缺省 null）：404（not_worker）＝服务侧无在飞 worker 绑定本会话，
 * 单用户模型下即主会话（root 牌从不入插件缓存，404 是主会话的唯一可判据——裁定 A）。
 * readish 面：404→按「主会话例外」放行；worker 凭据/不可达/节流→READ_HINT（fail-closed）。
 * write 面（F27 收殓残留 finding，裁定 A 镜像）：404→DISPATCH_HINT（主会话直改的正确指引
 * ＝派任务，替代生产不可达的 root 缓存死条文路由）；200→已填缓存按 worker env 判定；
 * 不可达/节流→NO_CRED_HINT（fail-closed，未知身份不给主会话指引）。
 */
export function shouldBlock(
  tool: string,
  targetPath: string,
  cwd: string,
  cred: Cred | undefined | null,
  enforceOn: boolean,
  pull: PullReason | null = null,
): string | null {
  if (!enforceOn) return null;
  const target = fold(targetPath);
  if (READISH_TOOLS.has(tool)) {
    if (!under(join(agoraHome(), "state"), target)) return null; // events/wiki/其余裸读开放
    if (cred?.role === "root") return null; // 主会话例外全开
    if (pull === "not_worker") return null; // F27 裁定 A：非在飞 worker＝可信主类，放行
    return READ_HINT;
  }
  if (!WRITE_TOOLS.has(tool)) return null;
  if (under(TMP_ROOT, target) || !inGitRepo(target) || inGateRecordDir(target)) return null; // 豁免面 B3＋F33 门记录
  if (cred?.token && cred.envDir && under(cred.envDir, cwd)) return null;
  // 拒因分流：缓存命中 root 牌＝防御位（生产不入缓存）维持 DISPATCH_HINT；无凭据按拉取
  // 判据路由（F27 收殓残留 finding，裁定 A 镜像）——404（not_worker）＝主会话直改→
  // DISPATCH_HINT（「静待重注入」对主会话是死路，正确指引是派任务）；200 命中已填缓存
  // 走上行 worker env 判定；不可达/节流＝未知身份维持 NO_CRED_HINT fail-closed。
  // 服务不可达默认拒由 index 在 lift 败态时拼 LIFT_RECOVER 附着，不在此分支。
  if (cred?.role === "root") return DISPATCH_HINT;
  if (!cred?.token || !cred.envDir) {
    if (pull === "not_worker") return DISPATCH_HINT; // F27 裁定 A 镜像：救活写面死条文路由
    return NO_CRED_HINT;
  }
  return wrongDoorHint(cred.envDir, cwd);
}
