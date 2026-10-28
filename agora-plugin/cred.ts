/**
 * cred 凭据管家（设计 §5.2）：会话牌缓存＋落盘读取，供 hook 判定链核验。
 * 牌随任务生死——cancel/重派即废（缓存清理由 inject 侧 T3 对齐）。
 * F25 修正推改拉：推送通道（inject_silent）证伪后，缓存未命中经 pullCredential
 * 主动向 agorad /cred_for_sid 拉取自愈（节流，失败安全向 null＝默认拒）。
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

// 拉取节流：每 sid 每窗至多一拉（未命中也计窗，保护 agorad）；按 sid 而非进程级
// ——同宿主多 worker 会话共享插件进程，进程级窗口会让别家首拉被误堵一窗
const PULL_INTERVAL_MS = 30_000;
const pullSeen = new Map<string, number>(); // sid->上次拉取时刻

/** 测试注入口：清节流窗。 */
export function resetCredPull(): void {
  pullSeen.clear();
}

/** 缓存未命中拉取自愈（F25 修正）：命中即填缓存并返回；404/失败/节流内返回 null
 * ＝走现有默认拒文案。短超时 2s：不拖垮工具调用判定链。 */
export async function pullCredential(sid: string, port: number): Promise<Cred | null> {
  const now = Date.now();
  if (now - (pullSeen.get(sid) ?? 0) < PULL_INTERVAL_MS) return null;
  for (const [k, ts] of pullSeen) { // 出账：只留窗内活跃 sid，防常驻进程无界增长
    if (now - ts >= PULL_INTERVAL_MS) pullSeen.delete(k);
  }
  pullSeen.set(sid, now);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/cred_for_sid`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid }),
      signal: AbortSignal.timeout(2000),
    });
    if (!r.ok) return null; // 404＝非在飞 worker：常态未命中
    const d = (await r.json()) as { tid?: string; env_dir?: string; token?: string };
    if (!d.token || !d.env_dir) return null; // 形状缺＝安全向拒绝
    const cred: Cred = { token: d.token, sid, role: "worker", tid: d.tid, envDir: d.env_dir };
    cacheCredential(cred);
    return cred;
  } catch {
    return null; // 服务不可达/超时：默认拒（与判定链服务不可达同口径）
  }
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
