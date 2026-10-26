/**
 * opencode v2 插件入口——plain-object 导出（Plugin.define 包裹形态静默失效，禁用）。
 * 钩子接线真实形状（tool.execute.before 输入/输出、MCP 工具覆盖、改参能力）
 * 留 T5 对账后收紧（探针七项之一）；本文件只落判定链接缝＋本地类型桩。
 */
import { getCredential } from "./cred";
import { shouldBlock } from "./hook";

// opencode API 本地类型桩（真形状 T5 收紧，不引 @opencode 包）
export interface OcBeforeInput {
  tool: string;
  sessionID?: string;
}
export interface OcBeforeOutput {
  args: Record<string, unknown>;
  cwd?: string;
}

let enforceOn = false; // shortcut: enforce 为服务持旗，插件侧拉取接线 T5 落（lift/doctor），装配前默认 off

export function setEnforce(on: boolean): void {
  enforceOn = on;
}

export default {
  "tool.execute.before": (input: OcBeforeInput, output: OcBeforeOutput): void => {
    const args = output.args ?? {};
    const target =
      (typeof args.filePath === "string" && args.filePath) ||
      (typeof args.path === "string" && args.path) ||
      output.cwd ||
      "";
    const cred = input.sessionID ? getCredential(input.sessionID) : undefined;
    const hint = shouldBlock(input.tool, target, output.cwd ?? process.cwd(), cred, enforceOn);
    if (hint) throw new Error(hint);
  },
};
