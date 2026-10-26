/**
 * inject 事件处理（F4）：credential 消息提取→cred 缓存填充＋落盘（0600）。
 * 消息文本形状与 agorad 侧对齐（task.py/recover.py）："[agora] credential {tid}\ntoken: {token}"。
 * env_dir 来源按最小：沿用缓存既有 envDir（重派换牌不丢环境位），缺省 undefined→hook 走 deny 安全向。
 * opencode inject 事件形状＝假定桩（T5 收紧）。
 */
import { cacheCredential, getCredential, writeCredential, type Cred } from "./cred";

export interface OcInject {
  sessionID: string;
  text: string;
  sessionDir?: string; // shortcut: opencode 会话目录形状 T5 收紧，缺省只缓存不落盘
}

const CRED_MSG = /\[agora\] credential (\S+)\ntoken: (\S+)/;

export function onInject(msg: OcInject): void {
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
  if (msg.sessionDir) {
    try {
      writeCredential(msg.sessionDir, cred);
    } catch {
      // 落盘 best-effort：内存缓存已在，读盘缺失走 readCredential null→deny 安全向
    }
  }
}
