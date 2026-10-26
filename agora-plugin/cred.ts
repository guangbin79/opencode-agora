/**
 * cred 凭据管家（设计 §5.2）：会话牌缓存＋落盘读取，供 hook 判定链核验。
 * 牌随任务生死——cancel/重派即废（缓存清理由 inject 侧 T3 对齐）。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

export type AgoraRole = "root" | "dispatcher" | "worker";
export type Cred = { token: string; sid: string; role: AgoraRole; envDir?: string };

const cache = new Map<string, Cred>(); // sid → cred

export function cacheCredential(cred: Cred): void {
  cache.set(cred.sid, cred);
}

export function getCredential(sid: string): Cred | undefined {
  return cache.get(sid);
}

const CRED_FILE = ".agora-cred.json";

/** 读会话目录内的落盘凭据；缺失/损坏一律 null（调用方按无凭据拒，安全方向）。 */
export function readCredential(sessionDir: string): Cred | null {
  try {
    const cred = JSON.parse(readFileSync(join(sessionDir, CRED_FILE), "utf8")) as Cred;
    return cred.token ? cred : null;
  } catch {
    return null;
  }
}

// shortcut: 附牌形状假定 args._token=<token>，opencode 钩子改参能力 T5 探针实证后收紧
export function attachToken(args: Record<string, unknown>, sid: string): Record<string, unknown> {
  const cred = cache.get(sid);
  if (cred) args._token = cred.token;
  return args;
}
