/**
 * inject 事件处理（F4）：session.prompt 钩子（T5 实证形状：event {sessionID, messageID,
 * prompt:{text}, delivery}）→BOOT 环境位登记＋credential 消息提取→cred 缓存填充。
 * 消息文本形状与 agorad 侧对齐（task.py dispatch 序）：BOOT 先行（"env: {env}" 登记环境位）、
 * credential 后至（"[agora] credential {tid}\ntoken: {token}"，envDir 取缓存既有值合流）。
 * env_dir 来源按最小：沿用缓存既有 envDir（重派换牌不丢环境位），缺省 undefined→hook 走 deny 安全向。
 * 牌只驻内存缓存（T5 实证：v2.1.4 会话无插件可见落盘目录）；进程重启丢牌由服务侧 recover
 * reinject 重发兜底。
 */
import { cacheCredential, getCredential, type Cred } from "./cred";

export interface OcInject {
  sessionID: string;
  text: string;
}

const CRED_MSG = /\[agora\] credential (\S+)\ntoken: (\S+)/;
const BOOT_MSG = /\[agora\] (gt-\S+|task-\S+): /;
const BOOT_ENV = /^env: (\S+)$/m;

export function onInject(msg: OcInject): void {
  const boot = BOOT_MSG.exec(msg.text);
  if (boot) {
    // BOOT 先行（dispatch 序：create 携 boot prompt→credential inject）：登记任务环境位，
    // credential 随后合流补 token——缺此步 worker 永远无 envDir＝hook 恒 deny 安全向。
    const env = BOOT_ENV.exec(msg.text);
    if (env) {
      cacheCredential({
        token: getCredential(msg.sessionID)?.token ?? "",
        sid: msg.sessionID,
        role: "worker",
        tid: boot[1],
        envDir: env[1],
      });
    }
    return;
  }
  const m = CRED_MSG.exec(msg.text);
  if (!m) return;
  const [, tid, token] = m;
  const cred: Cred = {
    token,
    sid: msg.sessionID,
    role: "worker",
    tid,
    envDir: getCredential(msg.sessionID)?.envDir,
  };
  cacheCredential(cred);
}
