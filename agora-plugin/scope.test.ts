/**
 * scope 启用域红测——F12 中央名册按仓休眠（R26 缓存／R29 短路）。
 * 名册＝$AGORA_HOME/.agora/inactive-repos，仓根绝对路径一行一个；
 * 命中→普通工具直通（lift/拉旗/附牌/判定链全短路，零 HTTP）＋agora_* 直接拒；
 * worktree 会话 toplevel＝worktree 自身路径→不受主检出名册条目影响。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { isDormant, loadInactiveRepos, resetScope } from "./scope";
import { resetLift } from "./lift";
import { default as entry, setEnforce } from "./index";

let root: string;
let repo: string; // 主检出仓（.git 目录桩）——名册条目
let wt: string; // 同仓 worktree（.git 文件桩）——toplevel＝自身路径
let home: string;
let roster: string;
let savedEnv: string | undefined;
let savedUrl: string | undefined;

beforeAll(() => {
  mkdirSync("/tmp/opencode", { recursive: true });
  root = mkdtempSync(join(tmpdir(), "agora-scope-"));
  repo = join(root, "repo");
  wt = join(root, "wt-repo");
  home = join(root, "plaza");
  mkdirSync(join(repo, ".git"), { recursive: true });
  mkdirSync(wt, { recursive: true });
  writeFileSync(join(wt, ".git"), "gitdir: elsewhere\n"); // worktree 形态：.git 是文件
  mkdirSync(join(home, ".agora"), { recursive: true });
  writeFileSync(join(repo, "a.md"), "x");
  writeFileSync(join(wt, "b.md"), "x");
  roster = join(home, ".agora", "inactive-repos");
  savedEnv = process.env.AGORA_HOME;
  process.env.AGORA_HOME = home;
  savedUrl = process.env.AGORA_OC_URL;
});

afterAll(() => {
  if (savedEnv === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = savedEnv;
  if (savedUrl === undefined) delete process.env.AGORA_OC_URL;
  else process.env.AGORA_OC_URL = savedUrl;
  setEnforce(false);
  resetScope();
  resetLift();
  rmSync(root, { recursive: true, force: true });
});

describe("loadInactiveRepos 名册解析", () => {
  test("缺失文件→空名册（全员活跃，安全向＝现行行为）", () => {
    // Req: F12
    rmSync(roster, { force: true });
    expect(loadInactiveRepos()).toEqual([]);
  });

  test("尾斜杠容错＋空白行/相对路径坏行跳过", () => {
    // Req: F12
    writeFileSync(roster, "  /x/r1/  \n\nrelative/path\n/x/r2///\n\n");
    expect(loadInactiveRepos()).toEqual(["/x/r1", "/x/r2"]);
  });

  test("空名册文件＝全员活跃", () => {
    // Req: F12
    writeFileSync(roster, "\n \n");
    expect(loadInactiveRepos()).toEqual([]);
  });
});

describe("isDormant 精确匹配（R26 缓存）", () => {
  test("命中／前缀不扩／worktree 路径不匹配主检出条目／查询尾斜杠容错", () => {
    // Req: F12
    writeFileSync(roster, repo + "\n");
    resetScope(); // 模拟新会话启动加载
    expect(isDormant(repo)).toBe(true);
    expect(isDormant(repo + "-sibling")).toBe(false); // 精确匹配：无前缀扩match
    expect(isDormant(wt)).toBe(false); // worktree toplevel≠主检出路径→活跃
    expect(isDormant(repo + "/")).toBe(true); // 查询侧尾斜杠同样容错
  });

  test("名册热载：运行中改名册即时生效（去缓存后行为）", () => {
    // Req: F12
    writeFileSync(roster, repo + "\n");
    resetScope();
    expect(isDormant(repo)).toBe(true);
    writeFileSync(roster, ""); // 运行中清空名册
    expect(isDormant(repo)).toBe(false); // 即时生效（去缓存）
  });
});

describe("index 休眠分支（R29：短路在 lift/拉旗/附牌/判定链之前）", () => {
  /** 假 ctx：捕获钩子；session.get 按 sid 回目录。 */
  function wire(dirs: Record<string, string>) {
    const hooks: Record<string, (ev: any) => Promise<void>> = {};
    entry.setup({
      tool: { hook: (name, fn) => { hooks[`tool.${name}`] = fn; } },
      session: {
        hook: (name, fn) => { hooks[`session.${name}`] = fn; },
        get: async ({ sessionID }: { sessionID: string }) => ({
          location: { directory: dirs[sessionID] },
        }),
      },
    } as any);
    return hooks;
  }

  /** 生产接线：服务探活＋/health 全记入 hits（非休眠路径必付 ≥2 次）。 */
  function startServer() {
    let hits = 0;
    const srv = Bun.serve({
      port: 0,
      fetch() {
        hits++;
        return new Response("{}", { status: 401 }); // 401→默认拒安全向，计数照记
      },
    });
    writeFileSync(join(home, ".agora", "service.json"), JSON.stringify({ port: srv.port }));
    writeFileSync(join(home, ".agora", "root.token"), "tok-root\n");
    process.env.AGORA_OC_URL = `http://127.0.0.1:${srv.port}`;
    return { srv, hits: () => hits };
  }

  function stopServer(srv: ReturnType<Bun.serve>) {
    process.env.AGORA_OC_URL = savedUrl;
    rmSync(join(home, ".agora", "root.token"), { force: true });
    rmSync(join(home, ".agora", "service.json"), { force: true });
    resetLift();
    srv.stop(true);
  }

  test("名册命中：edit 直通（零 HTTP）＋agora_board 拒（文案＋零 HTTP＋零附牌）", async () => {
    // Req: F12
    writeFileSync(roster, repo + "\n");
    resetScope();
    resetLift();
    setEnforce(null); // 生产形态：非休眠的 edit 必付拉旗 HTTP——休眠短路即零
    const { srv, hits } = startServer();
    try {
      const hooks = wire({ "s-dorm": repo });
      const inp: Record<string, unknown> = { filePath: join(repo, "a.md") };
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-dorm", input: inp }); // 直通不抛
      expect(hits()).toBe(0); // pullEnforce/lift 全短路
      const board: Record<string, unknown> = { task: "t" };
      await expect(
        hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-dorm", input: board }),
      ).rejects.toThrow(/此仓未启用 agora/);
      expect(hits()).toBe(0); // agora_* 直接拒：不 ensureOnce
      expect(board._token).toBeUndefined(); // 附牌短路（root.token 在位也不附）
    } finally {
      setEnforce(false);
      stopServer(srv);
    }
  });

  test("名册外现行行为回归：判定链拒照旧＋付 HTTP（计数器有效性对照）", async () => {
    // Req: F12
    writeFileSync(roster, wt + "\n"); // 名册只列 wt；repo 会话在名册外
    resetScope();
    resetLift();
    setEnforce(null);
    const { srv, hits } = startServer();
    try {
      const hooks = wire({ "s-out": repo });
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-out", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/派发/); // 判定链照跑：无凭据 edit 仓→派发引导
      expect(hits()).toBeGreaterThan(0); // 名册外照付拉旗开销
    } finally {
      setEnforce(false);
      stopServer(srv);
    }
  });

  test("worktree 会话不受主检出名册条目影响：照常活跃走判定链", async () => {
    // Req: F12
    writeFileSync(roster, repo + "\n"); // 名册列主检出；worktree toplevel 不同
    resetScope();
    resetLift();
    setEnforce(null);
    const { srv, hits } = startServer();
    try {
      const hooks = wire({ "s-wt": wt });
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-wt", input: { filePath: join(wt, "b.md") } }),
      ).rejects.toThrow(/派发/); // 未短路：worktree 会话照常被强制判定
      expect(hits()).toBeGreaterThan(0);
    } finally {
      setEnforce(false);
      stopServer(srv);
    }
  });
});
