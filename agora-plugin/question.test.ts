/**
 * question 三分支红测——ag-4f03df（ag-f51f40 事故防线）：
 * worker 凭据拒并引导 post(kind=question)；主会话放行＋邮件外呼联动；
 * 无凭据零新行为；外呼缺配置静默、失败吞异常不阻塞弹框。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { mailQuestion, QUESTION_HINT, questionGate, setMailRunner } from "./hook";
import { cacheCredential, type Cred } from "./cred";
import { default as entry } from "./index";

let home: string; // $AGORA_HOME 夹具（.agora/email.json 按用例落/删）
let savedEnv: string | undefined;
const worker: Cred = { token: "tok-w", sid: "s-qw", role: "worker", tid: "ag-1", envDir: "/env-w" };
const main: Cred = { token: "tok-m", sid: "s-qm", role: "dispatcher", tid: "ag-9" };

function wire() {
  const hooks: Record<string, (ev: any) => Promise<void>> = {};
  entry.setup({
    tool: { hook: (name: string, fn: any) => { hooks[`tool.${name}`] = fn; } },
    session: {
      hook: (name: string, fn: any) => { hooks[`session.${name}`] = fn; },
      get: async () => ({ location: { directory: "/tmp" } }),
    },
  } as any);
  return hooks;
}

const flush = () => new Promise((r) => setTimeout(r, 20)); // fire-and-forget 外呼排空
const qInput = {
  questions: [{
    question: "主会话问题：A 还是 B？", header: "方案选择",
    options: [{ label: "A" }, { label: "B" }],
  }],
};

beforeAll(() => {
  home = mkdtempSync(join(tmpdir(), "agora-question-"));
  mkdirSync(join(home, ".agora"), { recursive: true });
  savedEnv = process.env.AGORA_HOME;
  process.env.AGORA_HOME = home;
});

afterAll(() => {
  if (savedEnv === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = savedEnv;
  setMailRunner(null);
  rmSync(home, { recursive: true, force: true });
});

describe("questionGate 纯判定", () => {
  test("worker（含半条缓存）→QUESTION_HINT；dispatcher/root/无凭据→放行", () => {
    // Req: ag-4f03df
    expect(questionGate(worker)).toBe(QUESTION_HINT);
    // 半条缓存（BOOT 到而 credential 缺）＝事故形态：worker 身份已定，同拒
    expect(questionGate({ token: "", sid: "s-half", role: "worker", tid: "ag-1", envDir: "/e" })).toBe(QUESTION_HINT);
    expect(questionGate(main)).toBeNull();
    expect(questionGate({ token: "tok-r", sid: "s-r", role: "root" })).toBeNull();
    expect(questionGate(undefined)).toBeNull();
  });

  test("拒因文案引导 post(kind=question)＋发完即结束回合", () => {
    // Req: ag-4f03df
    expect(QUESTION_HINT).toContain("post(kind=question)");
    expect(QUESTION_HINT).toContain("结束回合");
  });
});

describe("question 三分支接线（tool.execute.before）", () => {
  test("worker 凭据→拒＋拒因含 post(kind=question)；零外呼", async () => {
    // Req: ag-4f03df
    const hooks = wire();
    const calls: [string[], string][] = [];
    setMailRunner(async (cmd, text) => { calls.push([cmd, text]); });
    cacheCredential(worker);
    try {
      await expect(
        hooks["tool.execute.before"]({ tool: "question", sessionID: "s-qw", input: qInput }),
      ).rejects.toThrow(/post\(kind=question\)/);
      await flush();
      expect(calls.length).toBe(0); // 拒径不外呼
    } finally {
      setMailRunner(null);
    }
  });

  test("主会话（dispatcher 牌）→放行＋外呼：command 被调、stdin 摘要含 sid/tid/拍平问题文本", async () => {
    // Req: ag-4f03df
    const hooks = wire();
    writeFileSync(join(home, ".agora", "email.json"),
      JSON.stringify({ to: "op@example.com", command: "notifier.sh --to op@example.com" }));
    const calls: [string[], string][] = [];
    setMailRunner(async (cmd, text) => { calls.push([cmd, text]); });
    cacheCredential(main);
    try {
      await hooks["tool.execute.before"]({ tool: "question", sessionID: "s-qm", input: qInput });
      await flush();
      expect(calls.length).toBe(1);
      expect(calls[0][0]).toEqual(["notifier.sh", "--to", "op@example.com"]); // command 按空白切分
      const text = calls[0][1];
      expect(text).toContain("s-qm"); // 会话上下文
      expect(text).toContain("ag-9"); // 任务上下文
      expect(text).toContain("方案选择");
      expect(text).toContain("A 还是 B");
    } finally {
      setMailRunner(null);
      rmSync(join(home, ".agora", "email.json"));
    }
  });

  test("问题文本拍平：行分隔符不裂行（外呼摘要末行恰一行正文）", async () => {
    // Req: ag-4f03df——外呼文本单行纪律（F49 同口径），防问题文本伪造邮件头/正文结构
    const hooks = wire();
    writeFileSync(join(home, ".agora", "email.json"), JSON.stringify({ to: "a@b", command: "n" }));
    const calls: [string[], string][] = [];
    setMailRunner(async (cmd, text) => { calls.push([cmd, text]); });
    cacheCredential(main);
    try {
      await hooks["tool.execute.before"]({
        tool: "question", sessionID: "s-qm",
        input: { questions: [{ question: "q1\nq2\u2028q3", options: [] }] },
      });
      await flush();
      expect(calls.length).toBe(1);
      const body = calls[0][1].split("\n").at(-1) as string; // 末行＝问题文本段
      expect(body).toContain("q1");
      expect(body).not.toContain("\u2028"); // U+2028 已压平
    } finally {
      setMailRunner(null);
      rmSync(join(home, ".agora", "email.json"));
    }
  });

  test("email.json 缺失→放行零外呼零异常；无凭据缓存→零新行为（放行零外呼）", async () => {
    // Req: ag-4f03df
    const hooks = wire();
    const calls: [string[], string][] = [];
    setMailRunner(async (cmd, text) => { calls.push([cmd, text]); });
    try {
      await hooks["tool.execute.before"]({ tool: "question", sessionID: "s-qm", input: qInput }); // dispatcher 牌、无配置
      await flush();
      expect(calls.length).toBe(0);
      await hooks["tool.execute.before"]({ tool: "question", sessionID: "s-bare", input: qInput }); // 无凭据
      await flush();
      expect(calls.length).toBe(0);
    } finally {
      setMailRunner(null);
    }
  });

  test("command 非零退出→放行不抛（外呼尽力而为，同 health.send_email 口径）", async () => {
    // Req: ag-4f03df
    const hooks = wire();
    writeFileSync(join(home, ".agora", "email.json"), JSON.stringify({ to: "a@b", command: "n" }));
    const calls: number[] = [];
    setMailRunner(async () => { calls.push(1); throw new Error("exit 1"); });
    cacheCredential(main);
    try {
      await hooks["tool.execute.before"]({ tool: "question", sessionID: "s-qm", input: qInput });
      await flush();
      expect(calls.length).toBe(1); // 确已尝试外呼，失败被吞
    } finally {
      setMailRunner(null);
      rmSync(join(home, ".agora", "email.json"));
    }
  });

  // 评审环 low 补测：defaultRunner 真子进程面（fake runner 全覆盖之外的进程生命周期钉死）
  test("defaultRunner 真子进程：stdin 送达（tee 落盘断言）＋exit0 解析", async () => {
    // Req: ag-4f03df
    const cap = join(home, "captured.txt");
    writeFileSync(join(home, ".agora", "email.json"), JSON.stringify({ to: "a@b", command: `tee ${cap}` }));
    try {
      await mailQuestion("s-real", "ag-r", { questions: [{ question: "真子进程" }] });
      const text = readFileSync(cap, "utf8");
      expect(text).toContain("s-real");
      expect(text).toContain("ag-r");
      expect(text).toContain("真子进程");
    } finally {
      rmSync(join(home, ".agora", "email.json"));
      rmSync(cap, { force: true });
    }
  });

  test("defaultRunner 真子进程非零退出（false）：mailQuestion 吞败不抛", async () => {
    // Req: ag-4f03df——外呼尽力而为口径在真 runner 上钉死（非仅 fake 注入面）
    writeFileSync(join(home, ".agora", "email.json"), JSON.stringify({ to: "a@b", command: "false" }));
    try {
      await mailQuestion("s-real2", undefined, qInput);
    } finally {
      rmSync(join(home, ".agora", "email.json"));
    }
  });
});
