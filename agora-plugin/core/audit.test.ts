/**
 * F176 拒绝审计上报红测——core/audit.ts fire-and-forget 面（shim 侧拒绝事件
 * 中央审计的第一件：opencode 接线层；pi/dsh 后续 shim 同源复用）。
 * 三锚：载荷形状（fold 折叠＋F49 同款控制符压平＋字段上限）；30s 节流窗
 * （同 (sid,tool,reason) 键首条必发、窗内第二条零网络）；fetch 失败/不可达
 * 静默不抛（禁阻塞判定链——绝不以任何败态影响拒因产出）。
 */
import { beforeEach, describe, expect, test } from "bun:test";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { reportDeny, resetAudit } from "./audit";

const flush = () => new Promise((r) => setTimeout(r, 20)); // fire-and-forget 排空

describe("audit 拒绝审计上报（Req: F176）", () => {
  beforeEach(() => resetAudit());

  test("载荷形状：{sid,tool,target,reason,ts}——target 经 fold 折叠、控制符压平、字段上限", async () => {
    // Req: F176
    const seen: Array<Record<string, unknown>> = [];
    const srv = Bun.serve({
      port: 0,
      fetch(req) {
        return req.json().then((body) => {
          seen.push(body as Record<string, unknown>);
          return Response.json({ ok: true });
        });
      },
    });
    try {
      const dir = mkdtempSync(join(tmpdir(), "audit-f176-"));
      const file = join(dir, "a.md");
      writeFileSync(file, "x");
      reportDeny(srv.port, {
        sid: "s".repeat(200), tool: "edit",
        target: file, reason: "被拦：演示\n拒因\ud800",
      });
      await flush();
      expect(seen.length).toBe(1);
      const p = seen[0];
      expect(Object.keys(p).sort()).toEqual(["reason", "sid", "target", "tool", "ts"]);
      expect(p.sid).toBe("s".repeat(128)); // 字段上限
      expect(p.tool).toBe("edit");
      expect(p.target).toBe(realpathSync(file)); // fold 折叠（存在路径＝realpath 本尊）
      expect(p.reason).toBe("被拦：演示\\u000a拒因\\ud800"); // F49 同款控制符压平＋孤立代理压平（C 册 round1）
      expect(typeof p.ts).toBe("number");
    } finally {
      srv.stop(true);
    }
  });

  test("30s 节流窗：同 (sid,tool,reason) 键首条必发、窗内第二条零网络；异 reason/异 sid 不受累", async () => {
    // Req: F176
    const n: string[] = [];
    const srv = Bun.serve({
      port: 0,
      fetch() {
        n.push("hit");
        return Response.json({ ok: true });
      },
    });
    try {
      const ev = { sid: "s-t", tool: "edit", target: "/x.md", reason: "r1" };
      reportDeny(srv.port, ev);
      reportDeny(srv.port, ev); // 同键窗内→零网络
      await flush();
      expect(n.length).toBe(1);
      reportDeny(srv.port, { ...ev, reason: "r2" }); // 异 reason→首发
      reportDeny(srv.port, { ...ev, sid: "s-t2" }); // 异 sid→首发
      await flush();
      expect(n.length).toBe(3);
    } finally {
      srv.stop(true);
    }
  });

  test("fetch 失败/不可达静默不抛（禁阻塞判定链）", async () => {
    // Req: F176
    expect(() =>
      reportDeny(1, { sid: "s-x", tool: "edit", target: "/x", reason: "r" }), // 端口 1 必拒＝不可达形态
    ).not.toThrow();
    const srv = Bun.serve({ port: 0, fetch: () => new Response("{}", { status: 500 }) });
    try {
      expect(() =>
        reportDeny(srv.port, { sid: "s-x", tool: "edit", target: "/x", reason: "r2" }),
      ).not.toThrow(); // 非 2xx 一律静默吞
      await flush(); // 无未处理拒绝
    } finally {
      srv.stop(true);
    }
  });
});
