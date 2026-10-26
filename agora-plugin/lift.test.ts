/**
 * lift/inject 红测——F4 服务拉起三路径＋超时分类＋credential 提取＋落盘 0600。
 * ensureService：已运行直返／未运行拉起 stub→就绪／拉起失败 LiftTimeout（没装 vs 拉不起两态）。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { cacheCredential, getCredential } from "./cred";
import { onInject } from "./inject";
import { ensureOnce, ensureService, LiftTimeout, resetLift } from "./lift";

const STUB = join(import.meta.dir, "tests", "stub_agorad.py");
const PLACEHOLDER_OC = "http://127.0.0.1:59999"; // 占位：桩不消费，只为过必传门
let root: string;
let home: string;
let savedHome: string | undefined;
let savedOc: string | undefined;
let savedState: string | undefined;
const sjPath = () => join(home, ".agora", "service.json");

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "agora-lift-"));
  home = join(root, "plaza");
  mkdirSync(join(home, ".agora"), { recursive: true });
  savedHome = process.env.AGORA_HOME;
  savedOc = process.env.AGORA_OC_URL;
  process.env.AGORA_HOME = home;
  process.env.AGORA_OC_URL = PLACEHOLDER_OC;
  savedState = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = join(root, "state-home"); // T5：隔离服务发现源（真机有在跑的 opencode）
  mkdirSync(join(root, "state-home"), { recursive: true });
});

afterAll(() => {
  if (savedHome === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = savedHome;
  if (savedOc === undefined) delete process.env.AGORA_OC_URL;
  else process.env.AGORA_OC_URL = savedOc;
  if (savedState === undefined) delete process.env.XDG_STATE_HOME;
  else process.env.XDG_STATE_HOME = savedState;
  rmSync(root, { recursive: true, force: true });
});

describe("ensureService 拉起三路径", () => {
  test("已运行：service.json＋任意应答（401 亦就绪）→直返 port 不拉起", async () => {
    // Req: F4
    const srv = Bun.serve({ port: 0, fetch: () => new Response(null, { status: 401 }) });
    writeFileSync(sjPath(), JSON.stringify({ pid: 1, port: srv.port }));
    try {
      // script 指向不存在路径：若误走拉起即抛"未安装"，本测断言直返
      const r = await ensureService({ script: join(root, "no-such.py"), timeoutMs: 1000 });
      expect(r.port).toBe(srv.port);
      expect(r.proc).toBeUndefined();
    } finally {
      srv.stop(true);
    }
  });

  test("未运行：setsid 拉起 stub→写 service.json→轮询就绪，凭 env 传 AGORA_HOME", async () => {
    // Req: F4
    rmSync(sjPath(), { force: true });
    const r = await ensureService({ script: STUB, timeoutMs: 5000, intervalMs: 50 });
    try {
      expect(r.port).toBeGreaterThan(0);
      const res = await fetch(`http://127.0.0.1:${r.port}/`);
      expect(res.status).toBe(401);
      expect(JSON.parse(readFileSync(sjPath(), "utf8")).port).toBe(r.port);
    } finally {
      r.proc?.kill();
    }
  });

  test("拉起失败：起而不写→LiftTimeout『拉起失败』，短上限即刻拒＋杀残留", async () => {
    // Req: F4
    rmSync(sjPath(), { force: true });
    const hung = join(root, "hung.py");
    writeFileSync(hung, "import time\ntime.sleep(60)\n");
    const t0 = Date.now();
    try {
      await expect(
        ensureService({ script: hung, timeoutMs: 300, intervalMs: 50 }),
      ).rejects.toThrow(/拉起失败/);
    } finally {
      rmSync(hung, { force: true });
    }
    expect(Date.now() - t0).toBeLessThan(4000); // 注入短上限生效（真值 10s）
  });

  test("没装分类：script 不存在→『未安装』", async () => {
    // Req: F4
    rmSync(sjPath(), { force: true });
    await expect(
      ensureService({ script: join(root, "absent.py"), timeoutMs: 300 }),
    ).rejects.toBeInstanceOf(LiftTimeout);
    await expect(
      ensureService({ script: join(root, "absent.py"), timeoutMs: 300 }),
    ).rejects.toThrow(/未安装/);
  });

  test("AGORA_OC_URL 未设→必传门『拉起失败』分类文案", async () => {
    // Req: F4
    rmSync(sjPath(), { force: true });
    delete process.env.AGORA_OC_URL;
    try {
      await expect(
        ensureService({ script: STUB, timeoutMs: 300 }),
      ).rejects.toThrow(/AGORA_OC_URL/);
    } finally {
      process.env.AGORA_OC_URL = PLACEHOLDER_OC;
    }
  });
  test("T5 修正：discovery 命中→spawn env 带 url＋password（非仅 url）", async () => {
    // Req: F4
    rmSync(sjPath(), { force: true });
    const stateHome = join(root, "state-oc");
    mkdirSync(join(stateHome, "opencode"), { recursive: true });
    writeFileSync(join(stateHome, "opencode", "service.json"),
      JSON.stringify({ url: "http://127.0.0.1:49374", password: "pw-discovered" }));
    delete process.env.AGORA_OC_URL;
    delete process.env.AGORA_OC_PASSWORD;
    const envDump = join(root, "env-dump.txt");
    writeFileSync(join(root, "dump_env.py"),
      `import os\nopen(${JSON.stringify(envDump)}, "w").write(os.environ.get("AGORA_OC_URL", "") + " " + os.environ.get("AGORA_OC_PASSWORD", ""))\n`);
    try {
      await expect(
        ensureService({ script: join(root, "dump_env.py"), stateHome, timeoutMs: 1500, intervalMs: 50 }),
      ).rejects.toBeInstanceOf(LiftTimeout); // dump 后即退：等 port 落空＝超时分类路径
      const [url, password] = readFileSync(envDump, "utf8").split(" ");
      expect(url).toBe("http://127.0.0.1:49374");
      expect(password).toBe("pw-discovered");
    } finally {
      process.env.AGORA_OC_URL = PLACEHOLDER_OC;
    }
  });
});

describe("ensureOnce memo/reset", () => {
  test("失败 memo 复用同一结果；resetLift 后真执行可成功", async () => {
    // Req: F4
    resetLift();
    rmSync(sjPath(), { force: true });
    const saved = process.env.AGORA_OC_URL;
    delete process.env.AGORA_OC_URL; // ensureOnce 走缺省 script：必传门快速拒，避免真拉 agorad
    const bad = await ensureOnce();
    process.env.AGORA_OC_URL = saved;
    expect(bad.ok).toBe(false);
    expect(await ensureOnce()).toBe(bad); // 未复位：memo 复用，不重跑
    const srv = Bun.serve({ port: 0, fetch: () => new Response(null, { status: 401 }) });
    writeFileSync(sjPath(), JSON.stringify({ pid: 1, port: srv.port }));
    try {
      resetLift();
      expect(await ensureOnce()).toEqual({ ok: true, port: srv.port }); // 复位后重新真执行
    } finally {
      srv.stop(true);
      resetLift();
    }
  });
});

describe("onInject credential 提取", () => {
  test("credential 消息→提取 token/tid 入缓存，getCredential 可读", () => {
    // Req: F4
    onInject({ sessionID: "s-inj", text: "[agora] credential t-9\ntoken: tok-9" });
    expect(getCredential("s-inj")).toMatchObject({
      token: "tok-9",
      tid: "t-9",
      role: "worker",
      sid: "s-inj",
    });
  });

  test("非 credential 消息不动 cred", () => {
    // Req: F4
    onInject({ sessionID: "s-plain", text: "普通消息 hello world" });
    expect(getCredential("s-plain")).toBeUndefined();
  });

  test("env_dir 来源按最小：沿用缓存既有 envDir，无缓存缺省 undefined（hook 走 deny 安全向）", () => {
    // Req: F4
    cacheCredential({ token: "tok-old", sid: "s-red", role: "worker", envDir: "/env-e" });
    onInject({ sessionID: "s-red", text: "[agora] credential t-7\ntoken: tok-7" });
    expect(getCredential("s-red")?.envDir).toBe("/env-e");
    onInject({ sessionID: "s-fresh", text: "[agora] credential t-6\ntoken: tok-6" });
    expect(getCredential("s-fresh")?.envDir).toBeUndefined();
  });

  test("T5 实证：v2.1.4 会话无插件可见目录——牌只入内存缓存，重派沿用 envDir", () => {
    // Req: F4
    onInject({ sessionID: "s-disk", text: "[agora] credential t-8\ntoken: tok-8" });
    expect(getCredential("s-disk")?.token).toBe("tok-8"); // 无落盘：重启后由 recover 重发牌兜底
  });
});
