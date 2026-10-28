/**
 * hook/cred/index 判定链红测——F1 强制隔离执行（插件面）。
 * 判定语义须与 agorad 服务侧一致：非 git 豁免、临时目录豁免、主会话同拦、读引导。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { agoraHome, DISPATCH_HINT, NO_CRED_HINT, READ_HINT, shouldBlock, wrongDoorHint } from "./hook";
import { attachToken, cacheCredential, readCredential, type Cred } from "./cred";
import { resetLift } from "./lift";
import { default as entry, setEnforce } from "./index";

let root: string; // 非豁免夹具根（/tmp 下但不在 /tmp/opencode 内）
let repo: string; // git 仓夹具（.git 目录桩）
let plain: string; // 非 git 目录夹具
let envDir: string; // worker 任务环境夹具
let home: string; // $AGORA_HOME 夹具
let tmpRepo: string; // /tmp/opencode 下带 .git 的目录——临时豁免压过 git 判定
let savedEnv: string | undefined;
let savedOc: string | undefined;
let savedState: string | undefined;
const worker: Cred = { token: "tok-w", sid: "s-worker", role: "worker", envDir: "" };
const rootCred: Cred = { token: "tok-r", sid: "s-root", role: "root" };

beforeAll(() => {
  mkdirSync("/tmp/opencode", { recursive: true });
  root = mkdtempSync(join(tmpdir(), "agora-hook-"));
  repo = join(root, "repo");
  plain = join(root, "plain");
  envDir = join(root, "env-wt");
  home = join(root, "plaza");
  for (const d of [join(repo, ".git"), plain, envDir, join(home, "state", "messages"), join(home, "wiki"), join(home, ".agora")]) {
    mkdirSync(d, { recursive: true });
  }
  writeFileSync(join(repo, "a.md"), "x");
  writeFileSync(join(home, "events.jsonl"), "");
  writeFileSync(join(home, "state", "tasks.jsonl"), "");
  writeFileSync(join(home, "state", "messages", "00000001.md"), "x");
  writeFileSync(join(home, "wiki", "p.md"), "x");
  symlinkSync(join(repo, "a.md"), join(plain, "link.md")); // 符号链接→git 仓内
  tmpRepo = mkdtempSync(join("/tmp/opencode", "agora-hook-tmp-"));
  mkdirSync(join(tmpRepo, ".git"));
  writeFileSync(join(tmpRepo, "b.md"), "x");
  worker.envDir = envDir;
  savedEnv = process.env.AGORA_HOME;
  process.env.AGORA_HOME = home;
  savedOc = process.env.AGORA_OC_URL;
  delete process.env.AGORA_OC_URL; // ensureService 走必传快速拒：判定链测试不真拉 agorad
  savedState = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = join(root, "state-home"); // T5：隔离服务发现源（真机有在跑的 opencode）
  mkdirSync(join(root, "state-home"), { recursive: true });
});

afterAll(() => {
  if (savedEnv === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = savedEnv;
  if (savedOc === undefined) delete process.env.AGORA_OC_URL;
  else process.env.AGORA_OC_URL = savedOc;
  if (savedState === undefined) delete process.env.XDG_STATE_HOME;
  else process.env.XDG_STATE_HOME = savedState;
  rmSync(root, { recursive: true, force: true });
  rmSync(tmpRepo, { recursive: true, force: true });
});

describe("shouldBlock 判定链", () => {
  test("enforce off→放行（git 仓无凭据也不拦）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(repo, "a.md"), plain, null, false)).toBeNull();
  });

  test("非 git 目录→放行", () => {
    // Req: F1
    expect(shouldBlock("edit", join(plain, "x.md"), plain, null, true)).toBeNull();
  });

  test("临时目录 /tmp/opencode→放行（即使带 .git，豁免压过 git 判定）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(tmpRepo, "b.md"), tmpRepo, null, true)).toBeNull();
  });

  test("git 仓无凭据 edit/write→拒＋无凭据专属文案（write 目标可尚未存在）", () => {
    // Req: F26
    expect(shouldBlock("edit", join(repo, "a.md"), plain, null, true)).toBe(NO_CRED_HINT);
    expect(shouldBlock("write", join(repo, "new.md"), plain, null, true)).toBe(NO_CRED_HINT);
  });

  test("realpath 折叠：非 git 目录下符号链接指向仓内文件→拒（折叠后入链）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(plain, "link.md"), plain, null, true)).toBe(NO_CRED_HINT);
  });

  test("持活动凭据 cwd∈env→放行；cwd∉env→拒因 wrong-door（实值退化形态）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(repo, "a.md"), envDir, worker, true)).toBeNull();
    expect(shouldBlock("edit", join(repo, "a.md"), plain, worker, true)).toBe(wrongDoorHint(envDir, plain));
    // 退化形态：envDir 不符 {repo}__wt-* 命名约定→仅列两侧实值，无换算
    expect(wrongDoorHint(envDir, plain)).toContain(plain);
    expect(wrongDoorHint(envDir, plain)).toContain(envDir);
    expect(wrongDoorHint(envDir, plain)).not.toContain("对应副本路径");
  });

  test("wrong-door 换算：命名约定可推导→附副本对应路径；cwd 也不在主仓→退化", () => {
    // Req: F26
    const envWt = join(root, "repo__wt-task-t1__1029"); // 命名约定形：{repo}__wt-*
    const cred: Cred = { token: "tok-c", sid: "s-conv", role: "worker", envDir: envWt };
    const cwdInRepo = join(repo, "sub");
    const hint = shouldBlock("edit", join(repo, "a.md"), cwdInRepo, cred, true);
    expect(hint).toBe(wrongDoorHint(envWt, cwdInRepo));
    expect(hint).toContain(cwdInRepo); // 实值：工作目录
    expect(hint).toContain(envWt); // 实值：任务 env
    expect(hint).toContain(`对应副本路径＝${join(envWt, "sub")}`); // 换算：主仓相对路径映射进副本
    // cwd 不在推导出的主仓根内→换算退化
    expect(wrongDoorHint(envWt, plain)).not.toContain("对应副本路径");
  });

  test("生产实证形态钉死：location=env 而凭据缓存空（BOOT/credential 注册未达）→拒 a 分支", () => {
    // Req: F26——worker 人已在 env 内、凭据没到：仍拒（安全向），但拒因自解释
    expect(shouldBlock("edit", join(repo, "a.md"), envDir, null, true)).toBe(NO_CRED_HINT);
    expect(shouldBlock("edit", join(repo, "a.md"), envDir, { token: "", sid: "s-e", role: "worker", envDir }, true)).toBe(NO_CRED_HINT);
  });

  test("主会话（root 牌）对仓库代码写入同拦——保留 DISPATCH_HINT 原文", () => {
    // Req: F1
    expect(shouldBlock("edit", join(repo, "a.md"), plain, rootCred, true)).toBe(DISPATCH_HINT);
  });

  test("读引导：read/bash 直读 state/tasks.jsonl 与 messages/→拒＋工具面引导", () => {
    // Req: F1
    expect(shouldBlock("read", join(home, "state", "tasks.jsonl"), plain, null, true)).toBe(READ_HINT);
    expect(shouldBlock("read", join(home, "state", "messages", "00000001.md"), plain, null, true)).toBe(READ_HINT);
    expect(shouldBlock("bash", join(home, "state", "tasks.jsonl"), plain, null, true)).toBe(READ_HINT);
  });

  test("读引导例外：events/wiki 裸读开放，主会话 state 读放行", () => {
    // Req: F1
    expect(shouldBlock("read", join(home, "events.jsonl"), plain, null, true)).toBeNull();
    expect(shouldBlock("read", join(home, "wiki", "p.md"), plain, null, true)).toBeNull();
    expect(shouldBlock("read", join(home, "state", "tasks.jsonl"), plain, rootCred, true)).toBeNull();
  });

  test("bash 写 git 仓不拦（接受面：同机直写由 verify 门＋审计兜底）", () => {
    // Req: F1
    expect(shouldBlock("bash", join(repo, "a.md"), plain, null, true)).toBeNull();
  });

  test("enforce off 同压读引导", () => {
    // Req: F1
    expect(shouldBlock("read", join(home, "state", "tasks.jsonl"), plain, null, false)).toBeNull();
  });
});

describe("cred 凭据管家", () => {
  test("readCredential：在位解析／缺失 null／损坏 null", () => {
    // Req: F1
    const dir = join(root, "session-a");
    mkdirSync(dir);
    writeFileSync(join(dir, ".agora-cred.json"), JSON.stringify(worker));
    expect(readCredential(dir)?.token).toBe("tok-w");
    expect(readCredential(join(root, "absent"))).toBeNull();
    writeFileSync(join(dir, ".agora-cred.json"), "{oops");
    expect(readCredential(dir)).toBeNull();
  });

  test("attachToken：缓存命中附 args._token，未命中不动 args", () => {
    // Req: F1
    cacheCredential(worker);
    const args: Record<string, unknown> = { target: "x" };
    expect(attachToken(args, "s-worker")).toEqual({ target: "x", _token: "tok-w" });
    const bare: Record<string, unknown> = { target: "x" };
    expect(attachToken(bare, "s-none")).toEqual({ target: "x" });
  });
});

describe("index 插件入口（T5 实证：{id, setup(ctx)}＋tool/session hook）", () => {
  /** 稳定运行时假 ctx：捕获注册的钩子；session.get 回夹具 cwd（s-boot 例外＝任务 env）。 */
  function wire() {
    const hooks: Record<string, (ev: any) => Promise<void>> = {};
    entry.setup({
      tool: { hook: (name, fn) => { hooks[`tool.${name}`] = fn; } },
      session: {
        hook: (name, fn) => { hooks[`session.${name}`] = fn; },
        get: async ({ sessionID }: { sessionID: string }) => ({
          location: { directory: sessionID.startsWith("s-boot") ? envDir : plain }, // s-boot*＝任务 env 夹具（F24 双前缀用例复用）
        }),
      },
    } as any);
    return hooks;
  }

  test("tool hook execute.before：拦 edit 无凭据抛引导；豁免面静默", async () => {
    // Req: F1
    const hooks = wire();
    setEnforce(true);
    try {
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-x", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/派发/);
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-x", input: { filePath: join(plain, "x.md") } });
    } finally {
      setEnforce(false);
    }
  });

  test("session.prompt hook：credential 消息入缓存，凭 sessionID 供判定链", async () => {
    // Req: F4
    const hooks = wire();
    setEnforce(true);
    try {
      await hooks["session.prompt"]({ sessionID: "s-wired", prompt: { text: "[agora] credential t-w\ntoken: tok-w" } });
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-wired", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/派发/); // 无 envDir：deny 安全向
    } finally {
      setEnforce(false);
    }
  });

  test("BOOT 先行登记 envDir，credential 后至合流→cwd∈env 放行（dispatch 真序）", async () => {
    // Req: F1
    const hooks = wire();
    await hooks["session.prompt"]({
      sessionID: "s-boot",
      prompt: { text: `[agora] gt-t-b: F1 判定链\npage: p.md\nenv: ${envDir}\ncontract: x` },
    });
    await hooks["session.prompt"]({ sessionID: "s-boot", prompt: { text: "[agora] credential t-b\ntoken: tok-b" } });
    setEnforce(true);
    try {
      await hooks["tool.execute.before"]({ tool: "write", sessionID: "s-boot", input: { filePath: join(repo, "a.md") } });
    } finally {
      setEnforce(false);
    }
  });
  test("BOOT task- 新前缀同链登记（F24 双前缀兼容）", async () => {
    // Req: F24
    const hooks = wire();
    await hooks["session.prompt"]({
      sessionID: "s-boot-t",
      prompt: { text: `[agora] task-t-f24: F24 双前缀\npage: p.md\nenv: ${envDir}\ncontract: x` },
    });
    await hooks["session.prompt"]({ sessionID: "s-boot-t", prompt: { text: "[agora] credential t-f24\ntoken: tok-f24" } });
    setEnforce(true);
    try {
      await hooks["tool.execute.before"]({ tool: "write", sessionID: "s-boot-t", input: { filePath: join(repo, "a.md") } });
    } finally {
      setEnforce(false);
    }
  });

  test("enforce 服务持旗拉取：/health on→拒；off→放行；root.token 缺失默认拒（T6 接线）", async () => {
    // Req: F1
    const hooks = wire();
    setEnforce(null); // 清显式旗：前测 finally 的 false 残留会压过服务持旗拉取
    let flag = true;
    const srv = Bun.serve({
      port: 0,
      fetch(req) {
        if (req.headers.get("x-agora-token") !== "tok-root") {
          return new Response(JSON.stringify({ error: "无牌或牌无效" }), { status: 401 });
        }
        return Response.json({ ok: true, enforce_on: flag });
      },
    });
    const port = srv.port;
    writeFileSync(join(home, ".agora", "service.json"), JSON.stringify({ port }));
    writeFileSync(join(home, ".agora", "root.token"), "tok-root\n");
    const savedUrl = process.env.AGORA_OC_URL;
    process.env.AGORA_OC_URL = `http://127.0.0.1:${port}`; // lift 直达探活
    resetLift();
    try {
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-pull", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/派发/); // on（缺省）→拒
      flag = false;
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-pull", input: { filePath: join(repo, "a.md") } }); // off→放行
      rmSync(join(home, ".agora", "root.token"));
      flag = true;
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-pull", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/派发/); // 非授权 401→默认拒
    } finally {
      process.env.AGORA_OC_URL = savedUrl;
      resetLift();
      srv.stop(true);
    }
  });

  test("热路径预过滤＋拉旗计时：非引导类零 HTTP；edit 冷/热拉取 <100ms（T6 评审②）", async () => {
    // Req: F1
    const hooks = wire();
    setEnforce(null); // 生产形态：服务持旗拉取
    let hits = 0;
    const srv = Bun.serve({
      port: 0,
      fetch(req) {
        hits++;
        if (req.headers.get("x-agora-token") !== "tok-root") {
          return new Response(JSON.stringify({ error: "无牌" }), { status: 401 });
        }
        return Response.json({ ok: true, enforce_on: true });
      },
    });
    writeFileSync(join(home, ".agora", "service.json"), JSON.stringify({ port: srv.port }));
    writeFileSync(join(home, ".agora", "root.token"), "tok-root\n");
    const savedUrl = process.env.AGORA_OC_URL;
    process.env.AGORA_OC_URL = `http://127.0.0.1:${srv.port}`; // lift 直达探活
    resetLift();
    try {
      await hooks["tool.execute.before"]({ tool: "grep", sessionID: "s-hot", input: { pattern: "x" } });
      expect(hits).toBe(0); // 预过滤：grep 等非 agora 非引导类零 HTTP 直过
      const before = hits;
      await hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-hot", input: {} });
      expect(hits).toBe(before + 1); // agora_* 冷启动探活（终审修复：首个调用即拉起，设计 §5.4）——探活 1 次不拉旗
      const cold0 = performance.now();
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-hot", input: { filePath: join(plain, "x.md") } });
      const cold = performance.now() - cold0;
      expect(hits).toBeGreaterThan(0); // 引导类工具拉旗（lift 探活＋/health）
      expect(cold).toBeLessThan(100); // 冷路径预算：root.token 读＋HTTP RTT＋Bun.serve 往返
      const warm0 = performance.now();
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-hot", input: { filePath: join(plain, "x.md") } });
      const warm = performance.now() - warm0;
      expect(warm).toBeLessThan(100);
      console.log(`[t6-fix] pullEnforce 冷=${cold.toFixed(1)}ms 热=${warm.toFixed(1)}ms（预算 100ms）`);
    } finally {
      process.env.AGORA_OC_URL = savedUrl;
      resetLift();
      srv.stop(true);
    }
  });

  test("附牌（T5 实证②改参直达）：agora_* 工具附 _token，其余工具不动 input", async () => {
    // Req: F1
    const hooks = wire();
    setEnforce(true);
    try {
      const mcpIn: Record<string, unknown> = { task: "t-1" };
      await hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-wired", input: mcpIn });
      expect(mcpIn._token).toBe("tok-w"); // session.prompt 用例已缓存 s-wired 凭据
      const plainIn: Record<string, unknown> = { filePath: join(plain, "x.md") };
      await hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-wired", input: plainIn });
      expect(plainIn._token).toBeUndefined();
    } finally {
      setEnforce(false);
    }
  });

  test("lift 失败拒因携恢复指引（doctor resetLift）", async () => {
    // Req: F4
    const hooks = wire();
    resetLift();
    setEnforce(true);
    try {
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-x", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/resetLift/);
    } finally {
      setEnforce(false);
      resetLift();
    }
  });

  test("附牌回落：无 worker cred 的主会话调 agora_* → _token＝root.token；worker cred 命中优先；两路皆缺不附", async () => {
    // Req: F1
    const hooks = wire();
    setEnforce(false); // 本例只验附牌面，不涉判定链
    writeFileSync(join(home, ".agora", "root.token"), "tok-rootfile\n");
    try {
      const main: Record<string, unknown> = { task: "t-m" };
      await hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-main", input: main });
      expect(main._token).toBe("tok-rootfile"); // getCredential 未命中→回落 root 牌接管文件
      expect(main._sid).toBe("s-main"); // R25-A：附牌同携调用者真实会话 sid（服务绑主会话）
      cacheCredential({ token: "tok-worker", sid: "s-prio", role: "worker", envDir: "" });
      const w: Record<string, unknown> = { task: "t-w" };
      await hooks["tool.execute.before"]({ tool: "agora_task_create", sessionID: "s-prio", input: w });
      expect(w._token).toBe("tok-worker"); // worker credential 消息优先级不变
      expect(w._sid).toBe("s-prio");
      rmSync(join(home, ".agora", "root.token"));
      const none: Record<string, unknown> = { task: "t-n" };
      await hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-nocred", input: none });
      expect(none._token).toBeUndefined(); // 两路皆缺→不附牌（MCP 壳 -32001 安全向不变）
    } finally {
      rmSync(join(home, ".agora", "root.token"), { force: true });
    }
  });

  test("lift 败态 memo 自愈：agora_* 调用复位 memo，下轮 edit 重探走活服务（免重启恢复）", async () => {
    // Req: F4
    const hooks = wire();
    setEnforce(null); // 服务持旗拉取形态
    const savedUrl = process.env.AGORA_OC_URL;
    delete process.env.AGORA_OC_URL; // 无发现源→ensureService 快速拒＝败态 memo
    resetLift();
    try {
      await expect(
        hooks["tool.execute.before"]({ tool: "edit", sessionID: "s-heal", input: { filePath: join(repo, "a.md") } }),
      ).rejects.toThrow(/resetLift/); // 先证败态：拒因携 memo 指引
      await hooks["tool.execute.before"]({ tool: "agora_board", sessionID: "s-heal", input: {} }); // agora 调用复位 memo
      // 服务复活：stub /health＋service.json/发现源指向它
      const srv = Bun.serve({
        port: 0,
        fetch(req) {
          if (req.headers.get("x-agora-token") !== "tok-root") return new Response("{}", { status: 401 });
          return Response.json({ ok: true, enforce_on: true });
        },
      });
      writeFileSync(join(home, ".agora", "service.json"), JSON.stringify({ port: srv.port }));
      writeFileSync(join(home, ".agora", "root.token"), "tok-root\n");
      process.env.AGORA_OC_URL = `http://127.0.0.1:${srv.port}`;
      const err = await hooks["tool.execute.before"]({
        tool: "edit", sessionID: "s-heal", input: { filePath: join(repo, "a.md") },
      }).then(() => null, (e: Error) => e);
      expect(err).toBeInstanceOf(Error);
      expect((err as Error).message).toContain("派发"); // enforce on 经活服务拉得（重探成功）
      expect((err as Error).message).not.toContain("resetLift"); // 非败态 memo 默认拒——复位已生效
    } finally {
      if (savedUrl === undefined) delete process.env.AGORA_OC_URL;
      else process.env.AGORA_OC_URL = savedUrl;
      resetLift();
    }
  });
});

describe("agoraHome 安全回归：不跟随 ~/.agora-home 指针（gt-32c051 裁定 A）", () => {
  const savedAg = process.env.AGORA_HOME;
  let pHome: string; // 惰性算：describe 注册期 root 尚未由 beforeAll 赋值

  const restore = () => {
    if (savedAg === undefined) delete process.env.AGORA_HOME;
    else process.env.AGORA_HOME = savedAg;
  };

  test("指针指向假目录→仍用缺省 ~/plaza；env 在→env 胜（判定链信任锚仅 env）", () => {
    // Req: gt-32c051（终闸 C 册 blocker 回归：指针=被辖方可写文件，禁作 enforce 锚）
    try {
      pHome = join(root, "pointer-home");
      mkdirSync(pHome, { recursive: true });
      writeFileSync(join(pHome, ".agora-home"), join(root, "fake-home")); // 被辖方一行 bash 可写
      delete process.env.AGORA_HOME;
      expect(agoraHome(pHome)).toBe(join(pHome, "plaza")); // 指针被无视
      process.env.AGORA_HOME = home;
      expect(agoraHome(pHome)).toBe(home); // env 胜过一切
    } finally {
      restore();
    }
  });
});
