/**
 * hook/cred/index 判定链红测——F1 强制隔离执行（插件面）。
 * 判定语义须与 agorad 服务侧一致：非 git 豁免、临时目录豁免、主会话同拦、读引导。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { DISPATCH_HINT, READ_HINT, shouldBlock } from "./hook";
import { attachToken, cacheCredential, readCredential, type Cred } from "./cred";
import { default as entry, setEnforce } from "./index";

let root: string; // 非豁免夹具根（/tmp 下但不在 /tmp/opencode 内）
let repo: string; // git 仓夹具（.git 目录桩）
let plain: string; // 非 git 目录夹具
let envDir: string; // worker 任务环境夹具
let home: string; // $AGORA_HOME 夹具
let tmpRepo: string; // /tmp/opencode 下带 .git 的目录——临时豁免压过 git 判定
let savedEnv: string | undefined;
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
});

afterAll(() => {
  if (savedEnv === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = savedEnv;
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

  test("git 仓无凭据 edit/write→拒＋派发引导文案（write 目标可尚未存在）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(repo, "a.md"), plain, null, true)).toBe(DISPATCH_HINT);
    expect(shouldBlock("write", join(repo, "new.md"), plain, null, true)).toBe(DISPATCH_HINT);
  });

  test("realpath 折叠：非 git 目录下符号链接指向仓内文件→拒（折叠后入链）", () => {
    // Req: F1
    expect(shouldBlock("edit", join(plain, "link.md"), plain, null, true)).toBe(DISPATCH_HINT);
  });

  test("持活动凭据 cwd∈env→放行；cwd∉env→拒", () => {
    // Req: F1
    expect(shouldBlock("edit", join(repo, "a.md"), envDir, worker, true)).toBeNull();
    expect(shouldBlock("edit", join(repo, "a.md"), plain, worker, true)).toBe(DISPATCH_HINT);
  });

  test("主会话（root 牌）对仓库代码写入同拦", () => {
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

describe("index 插件入口（plain-object）", () => {
  test("tool.execute.before：拦 edit 无凭据抛引导；豁免面静默", () => {
    // Req: F1
    setEnforce(true);
    try {
      expect(() => entry["tool.execute.before"]({ tool: "edit" }, { args: { filePath: join(repo, "a.md") }, cwd: plain })).toThrow(/派发/);
      expect(() => entry["tool.execute.before"]({ tool: "edit" }, { args: { filePath: join(plain, "x.md") }, cwd: plain })).not.toThrow();
    } finally {
      setEnforce(false);
    }
  });
});
