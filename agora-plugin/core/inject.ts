/**
 * inject 事件处理（F4）：session.prompt 钩子（T5 实证形状：event {sessionID, messageID,
 * prompt:{text}, delivery}）→BOOT 环境位登记＋credential 消息提取→cred 缓存填充。
 * 消息文本形状与 agorad 侧对齐（task.py dispatch 序）：BOOT 先行（"env: {env}" 登记环境位）、
 * credential 后至，envDir 取缓存既有值合流；亦收单消息双标记形态（issue-agent 单 prompt 发牌）。
 * credential 两形态（F43）：指针形态 "[agora] credential {tid}\nfile: {牌文件}"——hook 进程内
 * fs 读牌文件填 token（模型上下文不碰文件内容），读失败 token 置空走半条缓存拉取自愈；
 * 旧文本形态 "token: {token}" 保留解析（存量消息库 check_mail 兼容）。
 * env_dir 来源按最小：沿用缓存既有 envDir（重派换牌不丢环境位），缺省 undefined→hook 走 deny 安全向。
 * 牌只驻内存缓存（T5 实证：v2.1.4 会话无插件可见落盘目录）；进程重启丢牌由服务侧 recover
 * reinject 重发兜底。
 */
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { cacheCredential, getCredential, type Cred } from "./cred";
import { agoraHome } from "./hook";

export interface OcInject {
  sessionID: string;
  text: string;
}

const CRED_MSG = /\[agora\] credential (\S+)\ntoken: (\S+)/; // 旧文本形态（存量兼容）
const CRED_PTR = /\[agora\] credential (\S+)\nfile: (.+)$/m; // F43 指针形态（路径取整行，容空格）
const MAIL_ENV = /^\[agora\] task (\S+) (\S+) from (\S+)$/m; // 服务投递信封（msg.py _deliver）
const BOOT_MSG = /\[agora\] (gt-\S+|task-\S+|ag-\S+): /;
const BOOT_ENV = /^env: (\S+)$/m;

/** F43 指针 pin：只认 $AGORA_HOME/credentials/<tid>.token 本尊——路径投毒（伪造 file:
 * 读任意文件入缓存）与笔误一律拒读（空 token 走拉取自愈）。 */
function legitPointer(path: string, tid: string): boolean {
  return resolve(path.trim()) === resolve(join(agoraHome(), "credentials", `${tid}.token`));
}

/** F43：读牌文件取 token；缺失/损坏一律 ""（半条缓存，cred_for_sid 拉取自愈兜底），不抛。 */
function readTokenFile(path: string): string {
  try {
    const tok = (JSON.parse(readFileSync(path, "utf8")) as { token?: string }).token;
    return typeof tok === "string" ? tok : "";
  } catch {
    return "";
  }
}

/** F46 H-8 收口：token 解析具名化——指针形态独占且 tid/token 同标记：指针 tid 与
 * boot tid 并存不一致→视同非法（""，抑制 prev 回落走拉取自愈）；legit→trim 后读牌
 * （尾空格/CRLF 残留不静默降级）；无指针回落旧文本形态（无 credential 时 undefined
 * →调用点 prev 回落继承既有，BOOT-only 注入不 wipe 缓存）。 */
function credToken(m: RegExpExecArray | null, p: RegExpExecArray | null,
                   bootTid: string | undefined): string | undefined {
  if (p) {
    if (bootTid !== undefined && bootTid !== p[1]) return "";
    return legitPointer(p[2], p[1]) ? readTokenFile(p[2].trim()) : "";
  }
  return m?.[2];
}

export function onInject(msg: OcInject): void {
  // 信封门：全部标记（BOOT/env/credential）只在裸注入（无信封，dispatch/reinject 直投＋
  // 会话创建 boot）或 credential 信封（直投失败落库兜底的 inject 送达）内可信——其余信封
  // 正文＝post body 原文，持牌 worker 可任意伪造标记劫持收件会话凭据缓存（含 envDir
  // 覆写破写闸 containment／空牌覆写钉死主会话），跳过解析。
  const mail = MAIL_ENV.exec(msg.text);
  const credText = mail && mail[2] !== "credential" ? "" : msg.text;
  const boot = credText ? BOOT_MSG.exec(credText) : null;
  const m = credText ? CRED_MSG.exec(credText) : null;
  const p = credText ? CRED_PTR.exec(credText) : null;
  if (!boot && !m && !p) return;
  // BOOT 不再 early-return：boot 与 cred 各自解析后合并为一次 cacheCredential——
  // env 有则取之否则继承既有（重派换牌不丢环境位），cred 有 token 则取之。
  const env = credText ? BOOT_ENV.exec(credText) : null;
  const prev = getCredential(msg.sessionID);
  const cred: Cred = {
    token: credToken(m, p, boot?.[1]) ?? prev?.token ?? "",
    sid: msg.sessionID,
    role: "worker",
    tid: boot?.[1] ?? m?.[1] ?? p?.[1] ?? prev?.tid,
    envDir: env?.[1] ?? prev?.envDir,
  };
  cacheCredential(cred);
}
