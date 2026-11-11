/**
 * cred 凭据管家（设计 §5.2）：会话牌缓存＋落盘读取，供 hook 判定链核验。
 * 牌随任务生死——cancel/重派即废（缓存清理由 inject 侧 T3 对齐）。
 * F25 修正推改拉：推送通道（inject_silent）证伪后，缓存未命中经 pullCredential
 * 主动向 agorad /cred_for_sid 拉取自愈（节流，失败安全向 null＝默认拒）。
 * ag-32a5e2 lift 换牌：主会话拉取 404 即以 root 牌头换本会话 dispatcher 牌入缓存
 * （root 牌本体自此退出附牌面——多窗口审计归各自 sid；幂等面窗内回同牌自愈）。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { agoraHome } from "./hook";
import { AGORAD_TIMEOUT_MS, agoradUrl } from "./lift";

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
const lastVerdict = new Map<string, PullReason>(); // sid->窗内回放判据（评审 high 修复）
const inFlight = new Map<string, Promise<PullResult>>(); // sid->在途拉取（ag-6099ba 单飞）

/** 测试注入口：清节流窗。 */
export function resetCredPull(): void {
  pullSeen.clear();
  lastVerdict.clear();
  inFlight.clear();
}

/** 拉取拒因四分支（F27）：ok＝命中；not_worker＝404 服务侧无在飞任务绑本会话（主会话常态，
 * cred 可为 lift 换牌产物＝本会话 dispatcher 牌）；unreachable＝连接拒/超时/5xx/形状缺（异常
 * 安全向）；throttled＝节流窗内零网络。 */
export type PullReason = "ok" | "not_worker" | "unreachable" | "throttled";
export type PullResult = { reason: PullReason; cred: Cred | null };

/** root 牌接管文件（F1 唯一落盘点，0600）；缺失/损坏返回 null（调用方按无牌走安全向）。 */
export function readRootToken(): string | null {
  try {
    return readFileSync(join(agoraHome(), ".agora", "root.token"), "utf8").trim() || null;
  } catch {
    return null;
  }
}

/** lift 换牌（ag-32a5e2，服务端签发面 ag-af96bb①）：root 牌头＋本会话 sid 经
 * /cred_for_sid 签发面换会话牌——新 sid 签发 dispatcher 牌（cred_issue 审计），
 * 已持牌幂等回同牌；坏/缺 root 头回落 worker 拉取面。仅在 404 判定后调用；
 * 非 200/形状缺/异常一律 null（既有 not_worker 安全向不动）。 */
async function exchangeCredential(sid: string, port: number): Promise<Cred | null> {
  const root = readRootToken();
  if (!root) return null;
  try {
    const r = await fetch(agoradUrl(port, "/cred_for_sid"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Agora-Token": root },
      body: JSON.stringify({ sid }),
      signal: AbortSignal.timeout(AGORAD_TIMEOUT_MS),
    });
    if (!r.ok) return null;
    const d = (await r.json()) as { token?: string; role?: string; tid?: string; env_dir?: string };
    if (!d.token || (d.role !== "dispatcher" && d.role !== "worker")) return null;
    // tid/env_dir 归一 undefined：签发面已持牌幂等支回显 JSON null（httpd._cred_issue），
    // Cred 类型契约是可选 string 非 null——直存会让缓存条目带 null 值（下游 truthy 守卫
    // 今皆成立，但 === undefined 比对/序列化会踩）。
    const cred: Cred = {
      token: d.token, sid, role: d.role,
      tid: d.tid ?? undefined, envDir: d.env_dir ?? undefined,
    };
    cacheCredential(cred);
    return cred;
  } catch {
    return null;
  }
}

/** 缓存未命中拉取自愈（F25 修正推改拉）：命中即填缓存并返回；其余按拒因枚举返回
 * cred=null＝走现有默认拒文案（例外：not_worker 可携 lift 换牌产物，见 PullReason）。
 * 单跳短超时 2s；404 路径拉取＋换牌两跳串行（换牌仅 404 判定后发），判定链最坏 ~2×2s。 */
export async function pullCredential(sid: string, port: number): Promise<PullResult> {
  // 单飞（ag-6099ba 根修，先于节流窗判）：并发同 sid 调用共享在途拉取——
  // 否则第二调落节流空窗（lastVerdict 尚空）回放 throttled→零牌即时拒，
  // Promise.all 首拒还会取消在途首拉的兄弟调用（2026-11-05 派发方 answer 丢投实证）
  const flight = inFlight.get(sid);
  if (flight) return flight;
  const now = Date.now();
  if (now - (pullSeen.get(sid) ?? 0) < PULL_INTERVAL_MS) {
    // 窗内零网络但回放上次判据——裸 throttled 会把已知 not_worker 降级：主会话
    // （dispatcher 换牌缓存）窗内第二次调用将误吃 READ_HINT/断写面路由
    // （≤30s 陈旧度与节流设计同界）。
    return { reason: lastVerdict.get(sid) ?? "throttled", cred: null };
  }
  for (const [k, ts] of pullSeen) { // 出账：只留窗内活跃 sid，防常驻进程无界增长
    if (now - ts >= PULL_INTERVAL_MS) {
      pullSeen.delete(k);
      lastVerdict.delete(k);
    }
  }
  pullSeen.set(sid, now);
  const p = (async (): Promise<PullResult> => {
    try {
      const r = await fetch(agoradUrl(port, "/cred_for_sid"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid }),
        signal: AbortSignal.timeout(AGORAD_TIMEOUT_MS),
      });
      if (r.status === 404) {
        lastVerdict.set(sid, "not_worker"); // 非在飞 worker：常态未命中（窗内回放防降级）
        // lift 换牌（ag-32a5e2）：主会话判定即以 root 头换本会话牌入缓存——后续 agorad
        // 调用附会话牌（审计/邮件路由归各自 sid），root 牌本体退出附牌面。
        const issued = await exchangeCredential(sid, port);
        return { reason: "not_worker", cred: issued };
      }
      if (!r.ok) {
        lastVerdict.set(sid, "unreachable");
        return { reason: "unreachable", cred: null };
      }
      const d = (await r.json()) as { tid?: string; env_dir?: string; token?: string };
      // 形状缺＝异常安全向（归 unreachable：调用方不得回落 root 附牌）
      if (!d.token || !d.env_dir) {
        lastVerdict.set(sid, "unreachable");
        return { reason: "unreachable", cred: null };
      }
      const cred: Cred = { token: d.token, sid, role: "worker", tid: d.tid, envDir: d.env_dir };
      cacheCredential(cred);
      lastVerdict.delete(sid); // ok 判据随缓存生效（后续走缓存命中，不再进本函数）
      return { reason: "ok", cred };
    } catch {
      lastVerdict.set(sid, "unreachable"); // 服务不可达/超时：默认拒（与判定链服务不可达同口径）
      return { reason: "unreachable", cred: null };
    }
  })();
  inFlight.set(sid, p);
  try {
    return await p;
  } finally {
    inFlight.delete(sid);
  }
}

// 附牌生产唯一入口在 index.ts：tool.execute.before 直接写 ev.input._token（T5 实证）。
