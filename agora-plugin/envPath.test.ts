/**
 * envPath PATH 补全红测——F18（gt-26c027）：worker PATH 被 opencode 服务层剥离，
 * shell spawn 前补 $HOME/.bun/bin 与 $HOME/.local/bin（前插保序去重），接线锁死。
 */
import { describe, expect, test } from "bun:test";
import { homedir } from "node:os";
import { join } from "node:path";

import { augmentPath, onShellCreate } from "./envPath";
import { default as entry } from "./index";

const STRIPPED = ".opencode/bin:/usr/local/bin:/usr/bin:/bin"; // worker 实测剥离态

describe("augmentPath PATH 补全", () => {
  test("剥离 PATH 前插 ~/.bun/bin 与 ~/.local/bin，原项保序保留", () => {
    // Req: F18
    const env: Record<string, string | undefined> = { HOME: "/h", PATH: STRIPPED };
    augmentPath(env);
    expect(env.PATH).toBe(`${join("/h", ".bun/bin")}:${join("/h", ".local/bin")}:${STRIPPED}`);
  });

  test("幂等（重复钩子不叠加）；无 PATH 视为空；HOME 缺省 homedir()", () => {
    // Req: F18
    const once: Record<string, string | undefined> = { HOME: "/h", PATH: "/usr/bin" };
    augmentPath(once);
    const twice = { ...once };
    augmentPath(twice);
    expect(twice.PATH).toBe(once.PATH);
    const empty: Record<string, string | undefined> = { HOME: "/h" };
    augmentPath(empty);
    expect(empty.PATH).toBe(`${join("/h", ".bun/bin")}:${join("/h", ".local/bin")}`);
    const noHome: Record<string, string | undefined> = { PATH: "/usr/bin" };
    augmentPath(noHome);
    expect(noHome.PATH).toContain(join(homedir(), ".bun/bin"));
  });

  test("home 相对路径不动（防 CWD 依赖前插）；既有空 PATH 项保留（契约只加不减）", () => {
    // Req: F18
    const rel: Record<string, string | undefined> = { HOME: "rel/home", PATH: "/usr/bin" };
    augmentPath(rel);
    expect(rel.PATH).toBe("/usr/bin");
    const gaps: Record<string, string | undefined> = { HOME: "/h", PATH: "/usr/bin:" };
    augmentPath(gaps);
    expect(gaps.PATH).toBe(`${join("/h", ".bun/bin")}:${join("/h", ".local/bin")}:/usr/bin:`);
  });
});

describe("shell.create.before 接线（v2.2.0 实证：payload.env 按引用直改）", () => {
  test("setup 挂钩：spawn env 原地补全；缺 env 静默；旧运行时无 shell 命名空间不炸", () => {
    // Req: F18
    const hooks: Record<string, (ev: any) => unknown> = {};
    entry.setup({
      tool: { hook: () => {} },
      session: { hook: () => {}, get: async () => undefined },
      shell: { hook: (name, fn) => { hooks[`shell.${name}`] = fn; } },
    } as any);
    const spawnEnv: Record<string, string | undefined> = { HOME: "/h", PATH: STRIPPED };
    hooks["shell.create.before"]({ env: spawnEnv });
    expect(spawnEnv.PATH).toContain(join("/h", ".bun/bin"));
    expect(() => hooks["shell.create.before"]({})).not.toThrow(); // 缺 env＝安全向
    expect(() =>
      entry.setup({ tool: { hook: () => {} }, session: { hook: () => {}, get: async () => undefined } } as any),
    ).not.toThrow(); // 旧运行时无 shell 命名空间
  });

  test("onShellCreate 直调：undefined payload 静默；运行时载荷偏离本地类型不炸且 env 不动", () => {
    // Req: F18
    expect(() => onShellCreate(undefined)).not.toThrow();
    const skewed = { env: { PATH: 42 as unknown as string } }; // 运行时类型偏离（PATH 非串）
    expect(() => onShellCreate(skewed as any)).not.toThrow();
    expect(skewed.env.PATH).toBe(42); // 安全向：异常即放弃补全
  });
});
