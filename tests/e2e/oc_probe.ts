/**
 * T5 七项探针：独立 opencode serve --service 实例（隔离 XDG_*，绝不触碰本机会话的服务）。
 * 用法：bun run tests/e2e/oc_probe.ts   （产物 /tmp/opencode/oc-probe.json）
 * 实测基线：opencode v2.1.4。探完自停（杀 serve＋删临时目录＋删临时会话）。
 * Req: F1（附牌三前提）/F4（inject 队列）/T5 对账。
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = "/tmp/opencode";
const SANDBOX = join(ROOT, `oc-probe-${Date.now()}`);
const OUT = join(ROOT, "oc-probe.json");
const MCP_SERVER = "agoraprobe";
const CMD = process.env.OC_BIN || "opencode";

const probe: Record<string, any> = { started: new Date().toISOString(), sandbox: SANDBOX, opencode: CMD };
const step = (k: string, v: unknown) => {
  probe[k] = v;
  console.error(`[probe] ${k}: ${JSON.stringify(v)?.slice(0, 260)}`);
};
const j = async (r: Response) => { try { return await r.json(); } catch { return null; } };
const authHeader = (auth: string | null): Record<string, string> =>
  auth ? { Authorization: `Basic ${btoa(`opencode:${auth}`)}` } : {};

const PLUGIN_TS = `
const LOG = process.env.AGORA_PROBE_LOG;
const log = (o) => { try { require("node:fs").appendFileSync(LOG, JSON.stringify(o) + "\\n"); } catch {} };
let n = 0;
export default {
  id: "agora-probe",
  setup(ctx) {
    log({ type: "setup", ctxKeys: Object.keys(ctx) });
    (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: new AbortController().signal })) {
          const t = String(event?.type || "?");
          if (t.includes("delta") || t === "file.watcher.updated") { log({ type: "event-noisy", eventType: t }); continue; }
          log({ type: "event", eventType: t, data: event?.data });        }
      } catch (e) { log({ type: "event-loop-error", error: String(e) }); }
    })();
    ctx.session.hook("prompt", async (event) => { log({ type: "session.prompt", event }); });
    ctx.tool.hook("execute.before", async (event) => {
      log({ type: "execute.before", tool: event?.tool, sessionID: event?.sessionID, input: event?.input });
      if (String(event?.tool || "").includes("${MCP_SERVER}") && event?.input && typeof event.input === "object") {
        event.input.a = "hooked-" + ++n;
        event.input._token = "tok-from-hook";
        log({ type: "execute.before.mutated", input: event.input });
      }
    });
  },
};
`;

const MCP_PY = `#!/usr/bin/env python3
"""极简 stdio MCP（newline JSON-RPC）：spawn/call 全量落盘——探针取证用。"""
import json, os, sys, time
def rec(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\\n")
rec(os.environ["AGORA_PROBE_SPAWN"], {"pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv,
                                      "cwd": os.getcwd(), "ts": time.time()})
TOOL = {"name": "echo", "description": "echo back the given argument",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    if "id" not in req:
        continue  # 通知不应答
    m = req.get("method", "")
    if m == "initialize":
        out = {"protocolVersion": req["params"].get("protocolVersion", "2024-11-05"),
               "capabilities": {"tools": {}}, "serverInfo": {"name": "${MCP_SERVER}", "version": "0.0.1"}}
    elif m == "tools/list":
        out = {"tools": [TOOL]}
    elif m == "tools/call":
        rec(os.environ["AGORA_PROBE_CALLS"], {"args": req["params"].get("arguments"),
                                              "pid": os.getpid(), "ts": time.time()})
        out = {"content": [{"type": "text", "text": "echo:" + json.dumps(req["params"].get("arguments"))}]}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": out}), flush=True)
`;

function rd(p: string): any[] {
  if (!existsSync(p)) return [];
  return readFileSync(p, "utf8").trim().split("\n").filter(Boolean)
    .map((l) => { try { return JSON.parse(l); } catch { return l; } });
}

async function waitIdle(api: any, sid: string, timeoutMs: number): Promise<string> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const active = await j(await api.get("/session/active"));
    const st = active?.data?.[sid];
    if (!st || st.type === "idle") return st ? "idle" : "absent";
    await Bun.sleep(1500);
  }
  return "timeout";
}

async function main(): Promise<number> {
  for (const d of [ROOT, SANDBOX]) mkdirSync(d, { recursive: true });
  for (const d of ["data", "state", "cache", "config/opencode/plugins", "mcp", "proj"]) {
    mkdirSync(join(SANDBOX, d), { recursive: true });
  }
  const cfgPath = join(SANDBOX, "config/opencode/opencode.json");
  const pluginPath = join(SANDBOX, "config/opencode/plugins/agora_probe_plugin.ts");
  writeFileSync(cfgPath, JSON.stringify({
    $schema: "https://opencode.ai/config.json",
    mcp: { [MCP_SERVER]: { type: "local", command: ["python3", join(SANDBOX, "mcp/probe_mcp.py")] } },
  }, null, 1));
  writeFileSync(pluginPath, PLUGIN_TS);
  writeFileSync(join(SANDBOX, "mcp/probe_mcp.py"), MCP_PY);
  const envSpawn = join(SANDBOX, "mcp/spawn.jsonl"), envCalls = join(SANDBOX, "mcp/calls.jsonl");
  const env: Record<string, string> = {
    ...process.env,
    XDG_DATA_HOME: join(SANDBOX, "data"), XDG_STATE_HOME: join(SANDBOX, "state"),
    XDG_CACHE_HOME: join(SANDBOX, "cache"), XDG_CONFIG_HOME: join(SANDBOX, "config"),
    AGORA_PROBE_LOG: join(SANDBOX, "plugin-log.jsonl"),
    AGORA_PROBE_SPAWN: envSpawn, AGORA_PROBE_CALLS: envCalls,
  } as any;

  // --service：写 $XDG_STATE_HOME/opencode/service.json（AGORA_OC_URL 发现来源实证）
  const proc = Bun.spawn([CMD, "serve", "--service", "--hostname", "127.0.0.1", "--port", "0"], {
    cwd: join(SANDBOX, "proj"), env, stdout: "pipe", stderr: "pipe",
  });
  const svcPath = join(SANDBOX, "state/opencode/service.json");
  const t0 = Date.now();
  let svc: any = null;
  while (Date.now() - t0 < 30000) {
    if (existsSync(svcPath)) {
      svc = JSON.parse(readFileSync(svcPath, "utf8"));
      try {
        const r = await fetch(`${svc.url}/api/info`, { headers: authHeader(svc.password || null) });
        if (r.ok) break;
      } catch {}
    }
    await Bun.sleep(300);
  }
  step("p7_service_json", svc); // 服务发现：$XDG_STATE_HOME/opencode/service.json {url,password,pid,version}
  if (!svc) { step("fatal", "serve 30s 未就绪"); writeFileSync(OUT, JSON.stringify(probe, null, 1)); killServe(proc, null); return 1; }

  const base = svc.url;
  const auth = svc.password || null;
  const api = {
    get: (p: string) => fetch(base + "/api" + p, { headers: authHeader(auth) }),
    post: (p: string, body: any) => fetch(base + "/api" + p, { method: "POST", headers: { "Content-Type": "application/json", ...authHeader(auth) }, body: JSON.stringify(body) }),
    del: (p: string) => fetch(base + "/api" + p, { method: "DELETE", headers: authHeader(auth) }),
  };

  // —— P4 probe/health：版本来源＝GET /api/info；无 /health、/global/health ——
  const info = await j(await api.get("/info"));
  step("p4_api_info", info);
  const healthPaths: any = {};
  for (const p of ["/health", "/global/health", "/api/health", "/api/hello"]) {
    const r = await fetch(base + p, { headers: authHeader(auth) });
    healthPaths[p] = { status: r.status, ctype: r.headers.get("content-type") };
  }
  step("p4_health_paths", healthPaths);

  // —— P1 create：POST /api/session（{}；stub 契约形状字段是否被接受；title）＋ move 绑目录 ——
  const c1 = await j(await api.post("/session", {}));
  step("p1_create_empty", { status: 200, data: c1?.data });
  const sid = c1?.data?.id;
  const c2 = await api.post("/session", { cwd: join(SANDBOX, "proj"), message: "boot" }); // stub 契约形状
  step("p1_create_stub_shape", { status: c2.status, note: "cwd/message 是否生效（对照 location）" });
  const c3 = await j(await api.post("/session", { title: "probe-titled" }));
  step("p1_create_titled", { status: 200, data: c3?.data });
  const sid2 = c3?.data?.id;
  const proj2 = join(SANDBOX, "proj2"); // 第二 location：前提③证据面（双 location 双 MCP 连接）
  mkdirSync(proj2, { recursive: true });
  // move 落定有延迟（紧随 GET 读旧值）：settle 后再读
  if (sid) {
    const mv = await api.post(`/session/${sid}/move`, { directory: join(SANDBOX, "proj") });
    await Bun.sleep(1500);
    const after = await j(await api.get(`/session/${sid}`));
    step("p1_move_directory", { status: mv.status, locationAfter: after?.data?.location });
  }
  if (sid2) {
    const mv2 = await api.post(`/session/${sid2}/move`, { directory: proj2 });
    await Bun.sleep(1500);
    const after2 = await j(await api.get(`/session/${sid2}`));
    step("p1_move_directory_2", { status: mv2.status, locationAfter: after2?.data?.location });
  }

  // —— P3 alive/status：/session/active（idle 不出现）；GET 未知/已删 sid；prompt 未知 sid ——
  const a0 = await j(await api.get("/session/active"));
  step("p3_active_fresh", { data: a0?.data, note: "新建 idle 会话是否在 active map" });
  const missGet = await api.get("/session/ses_missing000000000000000000");
  step("p3_get_missing", { status: missGet.status, body: await j(missGet) });
  const missPrompt = await api.post("/session/ses_missing000000000000000000/prompt", { text: "x" });
  step("p3_prompt_missing", { status: missPrompt.status, body: await j(missPrompt) });
  const c4 = await j(await api.post("/session", { title: "probe-delete-me" })); // 一次性：删后取证
  const sid4 = c4?.data?.id;
  if (sid4) {
    const del = await api.del(`/session/${sid4}`);
    const delGet = await j(await api.get(`/session/${sid4}`));
    step("p3_after_delete", { deleteStatus: del.status, getAfter: delGet });
  }

  // —— P2 inject/queue：busy 会话连续 10 条按序（真实模型小回合；队列＝delivery:steer）——
  const k0 = await j(await api.post(`/session/${sid}/prompt`, { text: "probe kickoff. Reply with exactly: OK" }));
  step("p2_prompt_first", { status: k0?.status ?? 200, delivery: k0?.data?.delivery });
  const busyNow = await j(await api.get("/session/active"));
  step("p2_active_while_busy", { data: busyNow?.data });
  const deliveries: any[] = [];
  for (let i = 1; i <= 10; i++) {
    const r = await api.post(`/session/${sid}/prompt`, { text: `agora-inject-${i}` });
    const b = await j(r);
    deliveries.push({ status: r.status, delivery: b?.data?.delivery });
  }
  step("p2_queue_10", deliveries);
  const settle = await waitIdle(api, sid, 300000);
  step("p2_settle", settle);
  const msgs = await j(await api.get(`/session/${sid}/message`));
  const order: string[] = [];
  for (const m of msgs?.data || []) {
    if (m.type === "user" && String(m.text || "").startsWith("agora-inject-")) order.push(m.text);
  }
  order.reverse(); // 列表新→旧，注入序＝旧→新
  step("p2_inject_order", {
    observed: order,
    inOrder: order.join(",") === Array.from({ length: 10 }, (_, i) => `agora-inject-${i + 1}`).join(","),
  });

  // —— P5/P6 tool.execute.before 对 MCP 工具的覆盖与改参；MCP spawn 拓扑 ——
  // 前提③证据面：两 location 各自调用（sid 已在 proj；sid2 已在 proj2），proj 内重复调用证同 pid 共享
  let spawned = false;
  for (let i = 0; i < 40 && !spawned; i++) { spawned = existsSync(envSpawn) && rd(envSpawn).length > 0; if (!spawned) await Bun.sleep(500); }
  step("p6_mcp_spawn_wait", { spawned, log: rd(envSpawn) });
  const p5a = await api.post(`/session/${sid}/prompt`,
    { text: `Call the ${MCP_SERVER} echo tool with argument a set to exactly "original" (call one). Then reply DONE.` });
  step("p5_prompt_call1", { status: p5a.status });
  step("p5_settle_call1", await waitIdle(api, sid, 180000));
  const p5b = await api.post(`/session/${sid}/prompt`,
    { text: `Call the ${MCP_SERVER} echo tool again with argument a set to exactly "original" (call two). Then reply DONE.` });
  step("p5_prompt_call2", { status: p5b.status });
  step("p5_settle_call2", await waitIdle(api, sid, 180000));
  let p5c = null;
  if (sid2) {
    p5c = await api.post(`/session/${sid2}/prompt`,
      { text: `Call the ${MCP_SERVER} echo tool with argument a set to exactly "original" (other location). Then reply DONE.` });
    step("p5_prompt_loc2", { status: p5c.status });
    step("p5_settle_loc2", await waitIdle(api, sid2, 180000));
  }
  step("p5_mcp_calls", rd(envCalls));
  step("p6_mcp_spawn_log", rd(envSpawn));

  // —— P7 inject 事件插件侧形状＋服务发现 ——
  const plog = rd(join(SANDBOX, "plugin-log.jsonl"));
  const setups = plog.filter((e: any) => e.type === "setup");
  const prompts = plog.filter((e: any) => e.type === "session.prompt");
  const before = plog.filter((e: any) => e.type === "execute.before");
  const muts = plog.filter((e: any) => e.type === "execute.before.mutated");
  const eventTypes: Record<string, number> = {};
  for (const e of plog) if (e.type === "event") eventTypes[e.eventType] = (eventTypes[e.eventType] || 0) + 1;
  step("p7_plugin_setup", setups);
  step("p7_prompt_hook_sample", prompts.slice(0, 2));
  step("p7_prompt_hook_count", prompts.length);
  step("p7_event_types", eventTypes);
  step("p5_execute_before_all", { count: before.length, tools: [...new Set(before.map((e: any) => e.tool))] });
  step("p5_execute_before_mcp", before.filter((e: any) => String(e.tool || "").includes(MCP_SERVER)).slice(0, 3));
  step("p5_hook_mutations", muts);
  step("p7_mcp_status_events", plog.filter((e: any) => e.eventType === "mcp.status.changed").slice(0, 6));

  // —— 清理：删临时会话→杀 serve→删沙箱（P5/P6 未过保留现场）——
  for (const s of [sid, sid2, sid4].filter(Boolean)) { try { await api.del(`/session/${s}`); } catch {} }
  step("done", new Date().toISOString());
  writeFileSync(OUT, JSON.stringify(probe, null, 1));
  killServe(proc, svc);
  const calls = rd(envCalls);
  const spawns = rd(envSpawn);
  const ok = spawned && spawns.length >= 2 && calls.length >= 3 && muts.length > 0;
  if (ok) rmSync(SANDBOX, { recursive: true, force: true });
  else console.error(`[probe] P5/P6 未过——现场保留 ${SANDBOX}（plugin-log.jsonl／log 可查）`);
  console.error(`[probe] 产物 ${OUT}；spawns=${spawns.length} calls=${calls.length} mutations=${muts.length}`);
  return ok ? 0 : 2;
}

function killServe(proc: Bun.Subprocess, svc: any): void {
  try { if (svc?.pid) process.kill(svc.pid, "SIGTERM"); } catch {}
  try { proc.kill(); } catch {}
}

process.exit(await main());
