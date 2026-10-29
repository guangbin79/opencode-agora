/**
 * lift 服务拉起（F4/设计 §4.8）：探测 $AGORA_HOME/.agora/service.json→任意 HTTP 应答（含 401）
 ＝就绪；未运行则 detached 拉起 agorad（脱会话新会话，F27；env 带 AGORA_HOME＋AGORA_OC_URL[＋AGORA_OC_PASSWORD]）
 轮询至 10s 上限。超时拒因两态：没装（main.py 缺）vs 拉不起（起而不就绪／OC 发现落空）。
 * T5 实证接线：AGORA_OC_URL 发现源＝$XDG_STATE_HOME/opencode/service.json（serve --service 写
 {url,password,pid,version}）；API 需 Basic opencode:<password>（oc.py 消费，agorad 只透传）。
 * script 解析序：opts.script → env AGORAD_SCRIPT → $AGORA_HOME/.agora/agorad/main.py（install
 载荷）→ 与插件同仓（dev 布局）——插件独立安装场景不再假定同仓。
 */
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { agoraHome } from "./hook";

export class LiftTimeout extends Error {}

export interface LiftOpts {
  home?: string;
  script?: string;
  stateHome?: string; // T5 发现源根（测试注入隔离）
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

/** opencode 服务发现（T5）：读 $XDG_STATE_HOME/opencode/service.json → {url,password}。 */
export function discoverOc(
  stateHome = process.env.XDG_STATE_HOME || join(homedir(), ".local/state"),
): { url: string; password?: string } | null {
  try {
    const sj = JSON.parse(readFileSync(join(stateHome, "opencode", "service.json"), "utf8")) as {
      url?: unknown;
      password?: unknown;
    };
    return typeof sj.url === "string" && sj.url
      ? { url: sj.url, password: typeof sj.password === "string" ? sj.password : undefined }
      : null;
  } catch {
    return null;
  }
}

function resolveScript(opts: LiftOpts, home: string): string {
  return (
    opts.script ||
    process.env.AGORAD_SCRIPT ||
    join(home, ".agora", "agorad", "main.py") || // install.sh 载荷位（独立安装）
    DEFAULT_SCRIPT // dev 同仓布局兜底
  );
}

export async function ensureService(
  opts: LiftOpts = {},
): Promise<{ port: number; proc?: Killable }> {
  const { home = agoraHome(), timeoutMs = 10_000, intervalMs = 100 } = opts;
  const script = resolveScript(opts, home);
  let port = readPort(home);
  if (port !== null && (await probe(port))) return { port };
  if (!existsSync(script)) throw new LiftTimeout(`agorad 未安装：找不到 ${script}`);
  let ocUrl = process.env.AGORA_OC_URL;
  let ocPassword = process.env.AGORA_OC_PASSWORD;
  if (!ocUrl) {
    const found = discoverOc(opts.stateHome);
    if (!found) {
      throw new LiftTimeout(
        "agorad 拉起失败：AGORA_OC_URL 未设置且服务发现落空（$XDG_STATE_HOME/opencode/service.json 不存在——opencode serve --service 在运行吗）",
      );
    }
    ocUrl = found.url;
    ocPassword ??= found.password; // T5 修正：发现的 password 必须随 env 透传（缺它＝agorad 全 401）
  }
  const proc = Bun.spawn(["python3", script], {
    detached: true, // 脱会话＝原 setsid 包装等价（POSIX 新会话/Windows 新进程组），macOS 无 setsid 亦成立（Req: F27）
    env: {
      ...process.env,
      AGORA_HOME: home,
      AGORA_OC_URL: ocUrl,
      ...(ocPassword ? { AGORA_OC_PASSWORD: ocPassword } : {}),
    },
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

/** agorad 回环面共用寻址＋超时（拉旗 pullEnforce／拉凭据 pullCredential 同源防漂移）。 */
export const AGORAD_TIMEOUT_MS = 2000;
export function agoradUrl(port: number, path: string): string {
  return `http://127.0.0.1:${port}${path}`;
}
