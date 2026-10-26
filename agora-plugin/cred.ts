/**
 * cred 凭据管家（设计 §5.2）：会话牌缓存＋落盘读取，供 hook 判定链核验。
 * 牌随任务生死——cancel/重派即废（缓存清理由 inject 侧 T3 对齐）。
 */
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export type AgoraRole = "root" | "dispatcher" | "worker";
export type Cred = { token: string; sid: string; role: AgoraRole; tid?: string; envDir?: string };

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

/** 落盘凭据（0600）；T5 实证 v2.1.4 无插件可见会话目录——inject 路径不落盘（内存缓存＋
 * 服务侧 reinject 重发兜底），本函数留给未来选定落盘点（如 $AGORA_HOME）时复用。 */
export function writeCredential(sessionDir: string, cred: Cred): void {
  writeFileSync(join(sessionDir, CRED_FILE), JSON.stringify(cred) + "\n", { mode: 0o600 });
}

// 附牌形状（T5 实证）：tool.execute.before 改 ev.input 直达 MCP——index.ts 对 agora_* 工具
// 直接写 input._token；本函数保留纯函数形态供非钩子面复用与测试。
export function attachToken(args: Record<string, unknown>, sid: string): Record<string, unknown> {
  const cred = cache.get(sid);
  if (cred) args._token = cred.token;
  return args;
}
