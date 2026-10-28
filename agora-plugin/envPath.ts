/**
 * worker PATH 补全（F18）：opencode 服务层剥离 PATH（worker 实测只剩
 * ".opencode/bin:/usr/local/bin:/usr/bin:/bin"），bun 等用户级工具失联。
 * 接缝＝shell spawn 前钩子（v2.2.0 实证：shell 工具/终端同经 Shell.create→
 * trigger("shell","create.before",payload)，payload.env 按引用直改直达 spawn env）：
 * 基于既有 PATH 去重前插 $HOME/.bun/bin 与 $HOME/.local/bin，不硬编码全量。
 */
import { homedir } from "node:os";
import { delimiter, isAbsolute, join } from "node:path";

const EXTRA = [".bun/bin", ".local/bin"];

/** shell "create.before" 载荷本地形状（运行时未契约化，偏离由 onShellCreate 安全向兜住）。 */
export type ShellCreatePayload = { env?: Record<string, string | undefined> };

/** 原地补全 env.PATH：只加缺失项（前插保序去重），幂等；无 PATH 视为空；home 空/相对均不动（相对路径前插＝CWD 依赖，安全向）。 */
export function augmentPath(env: Record<string, string | undefined>): void {
  const home = env.HOME || homedir();
  if (!isAbsolute(home)) return;
  const parts = env.PATH ? env.PATH.split(delimiter) : []; // 保留既有空项（POSIX 空项＝CWD），契约只加不减
  const missing = EXTRA.map((d) => join(home, d)).filter((p) => !parts.includes(p));
  env.PATH = [...missing, ...parts].join(delimiter);
}

/** shell "create.before" 钩子入口：payload 缺 env 或补全异常均静默（安全向＝不动 spawn env，绝不阻断 Shell.create）。 */
export function onShellCreate(payload: ShellCreatePayload | undefined): void {
  if (!payload?.env) return;
  try {
    augmentPath(payload.env);
  } catch {
    // 运行时载荷偏离本地类型（如 PATH 非串）也不得炸钩子——钩子横切每次 shell spawn
  }
}
