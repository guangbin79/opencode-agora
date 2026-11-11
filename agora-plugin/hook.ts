/**
 * hook 强制判定链（F1，设计 §5.1）：拦截判定纯函数，opencode 接线形状 T5 已实证收紧
 * （tool.execute.before 覆盖 MCP 工具＋改参直达；cwd＝会话 location，异步取）。
 * 语义与 agorad 服务侧一致：豁免面 B3（非 git、/tmp/opencode、enforce off）、
 * F33 门记录目录（.omo/.req-gate，插件侧新增——门记录写本身即被闸死锁面）、
 * 读引导（state 账本经工具面，events/wiki 开放，主会话例外）、主会话对仓写入同拦。
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import type { Cred, PullReason } from "./cred";

export const DISPATCH_HINT =
  "被拦：本机 git 仓直改仅限委托的隔离环境（cwd∈任务 env）。请经 agora 任务面派发（task_create/task_dispatch）持凭据作业；直接改仓仅限豁免面。";
export const NO_CRED_HINT =
  "被拦：无凭据缓存——可经真实 prompt 通道重注 BOOT+credential 自愈（配方见任务页），或 post(kind=question) 求派发方。";
export const READ_HINT =
  "被拦：tasks.jsonl 与 messages/ 属账本面，请走工具面（board/check_mail）；events.jsonl 与 wiki 可直接读。主会话不受此限。";
export const GATE_SCOPE_HINT =
  "被拦：门记录目录（.omo/.req-gate/.review-loop/.superpowers）写入仅限当前任务 env 或当前仓根——跨 env/跨仓目标不豁免（F38 W-1）。";
export const QUESTION_HINT =
  "被拦：worker 会话禁用原生 question——需裁决/问人请改走 post(kind=question) 发派发方（唤醒通道），发完即结束回合，勿挂回合等人。";

const TMP_ROOT = "/tmp/opencode"; // B3 设计定值
const GATE_RECORD_DIRS = new Set([".omo", ".req-gate", ".review-loop", ".superpowers"]); // F33：门记录目录（新旧同收；.review-loop=评审环证据 1224fbd；.superpowers=SDD 账本/workspace 面，2026-11-02 实测误伤面）
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
export function fold(p: string): string {
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

/** git 仓根：沿目标向上找 .git（目录或文件——worktree/submodule 形态）；不属任何仓＝null。 */
function gitRoot(p: string): string | null {
  let cur = resolve(p);
  for (;;) {
    if (existsSync(join(cur, ".git"))) return cur;
    const parent = dirname(cur);
    if (parent === cur) return null;
    cur = parent;
  }
}

/** F38 W-1 位置约束：门记录命中仅在写目标 ∈ 当前 env（cred.envDir）或当前仓根
 * （cwd 所在 git 根）时豁免；跨 env/跨仓命中一律不豁免（防一次 write 投毒他仓治理面）。 */
function gateDirInScope(target: string, cwd: string, envDir: string | undefined): boolean {
  if (envDir && under(envDir, target)) return true;
  const root = gitRoot(cwd);
  return root !== null && under(root, target);
}

/** git 仓判定：沿目标向上找 .git（目录或文件——worktree/submodule 形态）。 */
function inGitRepo(p: string): boolean {
  return gitRoot(p) !== null;
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

/** wrong-door 拒因＋副本换算：写入要求 cwd 与 target 同在任务 env——target 越界
 * 优先点名 target；否则按 envDir basename 形如 {repo}__wt-* 推导 cwd 对应副本路径。 */
export function wrongDoorHint(envDir: string, cwd: string, targetPath?: string): string {
  if (targetPath && under(envDir, cwd) && !under(envDir, targetPath)) {
    return `被拦：写入目标 ${targetPath} 在任务 env ${envDir} 外——请在副本内编辑。`;
  }
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
  if (under(TMP_ROOT, target) || !inGitRepo(target)) return null; // 豁免面 B3
  if (inGateRecordDir(target)) {
    // F33 门记录豁免＋F38 W-1 位置约束：作用域内放行（豁免先行，先于 env 归属判定）；
    // 跨 env/跨仓命中一律拒——先于 cwd∈env 放行，不落穿。
    if (gateDirInScope(target, cwd, cred?.envDir)) return null;
    return GATE_SCOPE_HINT;
  }
  if (cred?.token && cred.envDir && under(cred.envDir, cwd)
      && under(cred.envDir, target)) return null;
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
  return wrongDoorHint(cred.envDir, cwd, target);
}

/** 原生 question 三分支之 a（ag-4f03df，ag-f51f40 事故）：worker 凭据（含半条缓存——
 * BOOT 已登记即 worker 身份）→拒并引导 post(kind=question)；dispatcher/root/无凭据→放行
 * （b 分支邮件联动与 c 分支零行为由 index 接线分流）。 */
export function questionGate(cred: Cred | undefined | null): string | null {
  return cred?.role === "worker" ? QUESTION_HINT : null;
}

/** 邮件外呼缝：默认真 subprocess（30s 超时同 health.send_email 口径），测试注入 fake runner。 */
export type MailRunner = (cmd: string[], inputText: string) => Promise<void>;
let mailRunner: MailRunner | null = null;
export function setMailRunner(r: MailRunner | null): void {
  mailRunner = r;
}

const MAIL_TIMEOUT_MS = 30_000;
const MAIL_SUMMARY_MAX = 400; // 问题摘要拍平截断上限（与超时同处声明，单点对齐策略）

const defaultRunner: MailRunner = (cmd, text) =>
  new Promise((resolve, reject) => {
    const p = spawn(cmd[0], cmd.slice(1), { stdio: ["pipe", "ignore", "ignore"] });
    const timer = setTimeout(() => p.kill("SIGKILL"), MAIL_TIMEOUT_MS);
    p.on("error", (e) => { clearTimeout(timer); reject(e); });
    p.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`mail command exit ${code}`));
    });
    p.stdin.on("error", () => {}); // EPIPE 等：成败口径已由 close 收口
    p.stdin.end(text);
  });

/** 分支 b 主会话邮件联动（ag-4f03df）：读与 agorad 同一份 $AGORA_HOME/.agora/email.json
 * （{to, command}），问题摘要（sid/tid＋问题文本 JSON 拍平截断——F49 同款行界压平，恰一行
 * 正文防伪造邮件结构）经 stdin 交 command。缺配置静默跳过、外呼失败吞异常（尽力而为，
 * 不阻塞弹框）；本函数永不 reject。 */
export async function mailQuestion(sid: string, tid: string | undefined, input: unknown): Promise<void> {
  try {
    const cfg = JSON.parse(
      readFileSync(join(agoraHome(), ".agora", "email.json"), "utf8"),
    ) as { to?: string; command?: string };
    if (!cfg.to || !cfg.command) return;
    // shortcut: command 按空白切分（shlex 引号形态不入支持面——opt-in 自配场景）
    const flat = (JSON.stringify(input ?? {}) ?? "")
      .replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029]/g, " ")
      .slice(0, MAIL_SUMMARY_MAX);
    await (mailRunner ?? defaultRunner)(
      cfg.command.trim().split(/\s+/),
      `[opencode] question 待裁决（主会话弹框）\nsid: ${sid}\ntid: ${tid ?? "-"}\n${flat}`,
    );
  } catch {
    // 未配置/外呼失败＝尽力而为：绝不影响弹框
  }
}
