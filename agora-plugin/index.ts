/**
 * opencode v2.1.4 插件入口——T5 探针实证接线（/tmp/opencode/oc-probe.json）：
 * 稳定运行时＝plain-object `{id, setup(ctx)}`（async-Hooks 形态静默失效，禁用）；
 * 钩子＝ctx.session.hook("prompt", ev)（ev {sessionID, prompt:{text}, delivery}）＋
 * ctx.tool.hook("execute.before", ev)（ev {tool, sessionID, input}——MCP 工具同名覆盖，
 * tool 名＝"<server>_<tool>"；改 ev.input 直达 MCP 服务端，已实测）。
 * 会话 cwd＝ctx.session.get({sessionID}).location.directory（异步取，失败回落 process.cwd()）。
 * 语义与 agorad 服务侧一致：豁免面 B3、读引导、主会话例外、服务不可达默认拒。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { getCredential } from "./cred";
import { onShellCreate, type ShellCreatePayload } from "./envPath";
import { onInject } from "./inject";
import { ensureOnce, resetLift, LIFT_RECOVER, type LiftResult } from "./lift";
import { isDormant } from "./scope";
import { agoraHome, enforceRelevant, shouldBlock } from "./hook";

// 稳定运行时 ctx 本地面类型（只注本插件消费面，不引 @opencode 包）
interface OcToolEvent {
  tool: string;
  sessionID: string;
  input?: Record<string, unknown>;
}
interface OcPromptEvent {
  sessionID: string;
  prompt?: { text?: string };
}
export interface OcCtx {
  tool: { hook(name: "execute.before", fn: (ev: OcToolEvent) => Promise<void>): void };
  session: {
    hook(name: "prompt", fn: (ev: OcPromptEvent) => Promise<void>): void;
    get(args: { sessionID: string }): Promise<{ location?: { directory?: string } } | undefined>;
  };
  /** v2.2.0 起 shell 工具/终端 spawn 前钩子；旧运行时缺省（可选挂载）。 */
  shell?: {
    hook(name: "create.before", fn: (ev: ShellCreatePayload) => void | Promise<void>): void;
  };
}

let enforceOverride: boolean | null = null; // 测试/特殊接线显式旗；生产置 null 走服务持旗拉取

export function setEnforce(on: boolean | null): void {
  enforceOverride = on; // null＝清除显式旗，恢复服务持旗拉取
}

/** root 牌接管文件（F1 唯一落盘点，0600）；缺失/损坏返回 null（调用方按无牌走安全向）。 */
function readRootToken(): string | null {
  try {
    return readFileSync(join(agoraHome(), ".agora", "root.token"), "utf8").trim() || null;
  } catch {
    return null;
  }
}

/** 服务持旗拉取（设计 §5.1 核验面，lift/doctor 接线）：lift 定端口→doctor 查询面读
 * /health.enforce_on（root 牌接管文件授权）；服务不可达/异常一律 true＝默认拒（安全向）。 */
async function pullEnforce(): Promise<boolean> {
  const lift = await ensureOnce();
  if (!lift.ok) return true;
  try {
    const tok = readRootToken();
    if (tok === null) return true;
    const r = await fetch(`http://127.0.0.1:${lift.port}/health`, {
      headers: { "X-Agora-Token": tok },
      signal: AbortSignal.timeout(2000),
    });
    if (!r.ok) return true;
    return ((await r.json()) as { enforce_on?: boolean }).enforce_on !== false;
  } catch {
    return true;
  }
}

export default {
  id: "agora",
  setup(ctx: OcCtx): void {
    ctx.session.hook("prompt", async (event) => {
      onInject({ sessionID: event.sessionID, text: event.prompt?.text ?? "" });
    });
    // F18 worker PATH 补全：shell spawn 前 payload.env 按引用直改（补 ~/.bun/bin、~/.local/bin）；
    // 旧运行时无 shell 命名空间→可选挂载静默跳过。
    ctx.shell?.hook("create.before", (event) => onShellCreate(event));
    ctx.tool.hook("execute.before", async (event) => {
      // R29：休眠分支短路在 lift/pullEnforce/attach/readGuide 之前——休眠仓零 HTTP 零文件读（除名册缓存）。
      const isAgora = /^agora_/.test(event.tool);
      let cwd = process.cwd();
      try {
        cwd = (await ctx.session.get({ sessionID: event.sessionID }))?.location?.directory || cwd;
      } catch {
        // 会话读取失败按服务 cwd 兜底：envDir 含括判定走 deny 安全向
      }
      if (isDormant(cwd)) {
        // F12 启用域：名册内休眠仓——普通工具直通；agora_* 直接拒（不 ensureOnce，零 HTTP）。
        if (isAgora) throw new Error("此仓未启用 agora");
        return;
      }
      // 服务不可达默认拒：lift 失败静默，判定链按无凭据走引导；拒因携 resetLift 恢复指引。
      // 旗按需拉取（T6 评审）：预过滤先行——非引导类工具（MCP/grep 等）零开销直过。
      const on = enforceOverride ?? (enforceRelevant(event.tool) ? await pullEnforce() : false);
      let lift: LiftResult | null = null;
      if (on) lift = await ensureOnce();
      if (isAgora) {
        // 冷启动（设计 §5.4）：首个 agora_* 调用即拉起 agorad；败态即复位 memo——
        // 下轮 edit 重探自愈（免重启恢复，兑现 lift.ts LIFT_RECOVER 承诺）。
        const l = lift ?? (await ensureOnce());
        if (!l.ok) resetLift();
      }
      const input = event.input ?? {};
      const target =
        (typeof input.filePath === "string" && input.filePath) ||
        (typeof input.path === "string" && input.path) ||
        "";
      const cred = getCredential(event.sessionID);
      // 附牌主案（T5 实证②：改 ev.input 直达 MCP）：仅 agora MCP 工具面附 _token——
      // worker credential 消息优先，未命中回落 root 牌接管文件（主会话经 MCP 调任务面）。
      // _sid 携调用者真实会话 id（R25-A：服务登记主会话绑定，拍人注入真会话）。
      if (isAgora) {
        const token = cred?.token ?? readRootToken();
        if (token) {
          input._token = token;
          input._sid = event.sessionID;
        }
      }
      const hint = shouldBlock(event.tool, target, cwd, cred, on);
      if (hint) {
        throw new Error(lift && !lift.ok ? `${hint}（${LIFT_RECOVER}；拒因：${lift.reason}）` : hint);
      }
    });
  },
};
