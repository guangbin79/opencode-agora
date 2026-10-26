/**
 * lift 服务拉起（F4/设计 §4.8）：探测 $AGORA_HOME/.agora/service.json→任意 HTTP 应答（含 401）
 ＝就绪；未运行则 setsid 拉起 agorad（env 带 AGORA_HOME＋AGORA_OC_URL）轮询至 10s 上限。
 * 超时拒因两态：没装（main.py 缺）vs 拉不起（起而不就绪／OC_URL 未设）。
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { agoraHome } from "./hook";

export class LiftTimeout extends Error {}

export interface LiftOpts {
  home?: string;
  script?: string;
  timeoutMs?: number; // 真值 10s；测试注入短化
  intervalMs?: number;
}

const DEFAULT_SCRIPT = join(import.meta.dir, "..", "agorad", "main.py");
/** proc 只消费 kill：类型收窄免引 Bun 命名空间（测试桩同形）。 */
interface Killable {
  kill(): void;
}

function readPort(home: string): number | null {
  try {
    const sj = JSON.parse(readFileSync(join(home, ".agora", "service.json"), "utf8")) as {
      port?: unknown;
    };
    return typeof sj.port === "number" ? sj.port : null;
  } catch {
    return null;
  }
}

/** 任意 HTTP 应答（含 401）＝就绪；连接拒/超时＝未起。 */
async function probe(port: number): Promise<boolean> {
  try {
    await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(1000) });
    return true;
  } catch {
    return false;
  }
}

export async function ensureService(
  opts: LiftOpts = {},
): Promise<{ port: number; proc?: Killable }> {
  const { home = agoraHome(), script = DEFAULT_SCRIPT, timeoutMs = 10_000, intervalMs = 100 } = opts;
  let port = readPort(home);
  if (port !== null && (await probe(port))) return { port };
  if (!existsSync(script)) throw new LiftTimeout(`agorad 未安装：找不到 ${script}`);
  const ocUrl = process.env.AGORA_OC_URL;
  if (!ocUrl) throw new LiftTimeout("agorad 拉起失败：AGORA_OC_URL 未设置（服务发现 T5 回填）");
  const proc = Bun.spawn(["setsid", "python3", script], {
    env: { ...process.env, AGORA_HOME: home, AGORA_OC_URL: ocUrl },
    stdio: ["ignore", "ignore", "ignore"],
  });
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    port = readPort(home);
    if (port !== null && (await probe(port))) return { port, proc };
    if (Date.now() >= deadline) {
      proc.kill(); // 杀残留：不留给守护孤儿（起而不写/起不来）
      throw new LiftTimeout(
        `agorad 拉起失败：${Math.round(timeoutMs / 1000)}s 内未就绪（脚本 ${script}）`,
      );
    }
    await Bun.sleep(intervalMs);
  }
}

export type LiftResult = { ok: true; port: number } | { ok: false; reason: string };

let memo: Promise<LiftResult> | null = null;

/** 懒调 memo 版（失败同样 memo：每进程至多一跑，防每次工具调用重吃轮询）；resetLift 后重试。 */
export function ensureOnce(): Promise<LiftResult> {
  memo ??= ensureService()
    .then((r): LiftResult => ({ ok: true, port: r.port }))
    .catch((e: unknown): LiftResult => ({ ok: false, reason: e instanceof Error ? e.message : String(e) }));
  return memo;
}

/** 复位失败 memo：用户救活 agorad 后恢复本会话（T4 doctor 接线）。 */
export function resetLift(): void {
  memo = null;
}

export const LIFT_RECOVER = "agorad 不可达已 memo（每进程一次）：救活服务后经 doctor resetLift 恢复，无需重启会话";
