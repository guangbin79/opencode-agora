/**
 * opencode v2.1.4 插件入口——T5 探针实证接线（/tmp/opencode/oc-probe.json）：
 * 稳定运行时＝plain-object `{id, setup(ctx)}`（async-Hooks 形态静默失效，禁用）；
 * 钩子＝ctx.session.hook("prompt", ev)（ev {sessionID, prompt:{text}, delivery}）＋
 * ctx.tool.hook("execute.before", ev)（ev {tool, sessionID, input}——MCP 工具同名覆盖，
 * tool 名＝"<server>_<tool>"；改 ev.input 直达 MCP 服务端，已实测）。
 * 会话 cwd＝ctx.session.get({sessionID}).location.directory（异步取，失败回落 process.cwd()）。
 * 语义与 agorad 服务侧一致：豁免面 B3、读引导、主会话例外、服务不可达默认拒。
 */
import { getCredential } from "./cred";
import { onInject } from "./inject";
import { ensureOnce, LIFT_RECOVER, type LiftResult } from "./lift";
import { shouldBlock } from "./hook";

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
}

let enforceOn = false; // shortcut: enforce 为服务持旗，插件侧拉取接线 T6 落（lift/doctor），装配前默认 off

export function setEnforce(on: boolean): void {
  enforceOn = on;
}

export default {
  id: "agora",
  setup(ctx: OcCtx): void {
    ctx.session.hook("prompt", async (event) => {
      onInject({ sessionID: event.sessionID, text: event.prompt?.text ?? "" });
    });
    ctx.tool.hook("execute.before", async (event) => {
      // 服务不可达默认拒：lift 失败静默，判定链按无凭据走引导；拒因携 resetLift 恢复指引
      let lift: LiftResult | null = null;
      if (enforceOn) lift = await ensureOnce();
      const input = event.input ?? {};
      const target =
        (typeof input.filePath === "string" && input.filePath) ||
        (typeof input.path === "string" && input.path) ||
        "";
      let cwd = process.cwd();
      try {
        cwd = (await ctx.session.get({ sessionID: event.sessionID }))?.location?.directory || cwd;
      } catch {
        // 会话读取失败按服务 cwd 兜底：envDir 含括判定走 deny 安全向
      }
      const cred = getCredential(event.sessionID);
      // 附牌主案（T5 实证②：改 ev.input 直达 MCP）：仅 agora MCP 工具面附 _token
      if (cred?.token && /^agora_/.test(event.tool)) input._token = cred.token;
      const hint = shouldBlock(event.tool, target, cwd, cred, enforceOn);
      if (hint) {
        throw new Error(lift && !lift.ok ? `${hint}（${LIFT_RECOVER}；拒因：${lift.reason}）` : hint);
      }
    });
  },
};
