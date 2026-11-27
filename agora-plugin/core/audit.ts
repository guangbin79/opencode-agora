/**
 * 拒绝审计异步上报（Req: F176）：shim 把判定链的拒绝事件 fire-and-forget 上报
 * agorad POST /gate_deny——插件侧拒绝事件归中央审计账本（opencode 接线层为
 * 第一件；pi/dsh 后续 shim 同源复用本模块）。
 * 契约：节流同 (sid,tool,reason) 键 30s 窗至多一条、首次必发；fetch 失败/不可达
 * 一律静默吞——禁在判定路径 await、不得改变拒因文案与任何既有判定语义；端口由
 * 接线层经 lift 既有机制探得传入（与 /cred_for_sid 同源），lift 败态零上报。
 */
import { fold } from "./hook";
import { AGORAD_TIMEOUT_MS, agoradUrl } from "./lift";

export interface DenyReport {
  sid: string;
  tool: string;
  target: string;
  reason: string;
}

const WINDOW_MS = 30_000;
const CAPS = { sid: 128, tool: 64, target: 512, reason: 256 };
const seen = new Map<string, number>(); // "sid\x00tool\x00reason" → 上次上报时刻

/** 测试注入口：清节流窗。 */
export function resetAudit(): void {
  seen.clear();
}

/** F49 同款控制符压平（含 DEL/C1/Unicode 行界——Python splitlines 行界），恰一行不可伪造。
 * 含孤立代理段 \ud800-\udfff：JS/Python JSON 往返均物化该类，不压平则服务端
 * utf-8 落账编码失败丢行＝选择性湮灭审计（C 册 round1 实锤）。 */
function flatten(s: string): string {
  return s.replace(
    /[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
  );
}

/** 压平后按字段上限截断。 */
function cap(s: string, n: number): string {
  return flatten(s).slice(0, n);
}

/** 拒绝上报（fire-and-forget）：窗内同键静默跳过；port null（lift 败态）零网络
 * 零占窗——lift 恢复后首次拒绝照常首发；fetch 一切败态（拒连/超时/非 2xx）吞掉。 */
export function reportDeny(port: number | null, ev: DenyReport): void {
  if (port === null) return;
  // 节流键与服务端同口径（压平＋截断后取键）——raw 键会因控制符/>256 尾部分裂
  // 占窗（服务端照丢＝客户端白发）；reason 可嵌模型可控 target 路径（wrongDoorHint）。
  const sid = cap(ev.sid, CAPS.sid);
  const tool = cap(ev.tool, CAPS.tool);
  const reason = cap(ev.reason, CAPS.reason);
  const key = `${sid}\x00${tool}\x00${reason}`;
  const now = Date.now();
  const last = seen.get(key);
  if (last !== undefined && now - last < WINDOW_MS) return;
  for (const [k, ts] of seen) { // 出账：只留窗内活跃键，防常驻进程无界增长
    if (now - ts >= WINDOW_MS) seen.delete(k);
  }
  seen.set(key, now);
  // target 折叠口径与 index 拒绝日志一致：fold()（空目标不折叠——resolve("")＝cwd 假路径）
  const target = ev.target ? fold(ev.target) : "";
  void fetch(agoradUrl(port, "/gate_deny"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sid, tool,
      target: cap(target, CAPS.target),
      reason, ts: now,
    }),
    signal: AbortSignal.timeout(AGORAD_TIMEOUT_MS),
  }).catch(() => {}); // 尽力而为：审计绝不负影响判定链
}
