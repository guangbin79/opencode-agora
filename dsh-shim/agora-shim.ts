/**
 * dsh worker shim（Req: F182）——agora 多 harness 步 3 的 dsh 接线层，语义对齐
 * opencode 版（agora-plugin/index.ts）：
 *   - 写闸：tools/pre-execute waterfall（异步）＝完整判定链（拉凭据/enforce 旗/
 *     shouldBlock 豁免面）；ctx.tools.guard()＝同步兜底（缓存未命中即拒，fail-closed）。
 *   - 判定核心源级复用（F176 资产，禁拷贝）：shouldBlock/fold/豁免面/拒因文案全部
 *     import ../agora-plugin/core/*——越界写拒因与 opencode 版同源。
 *   - 附牌：tools/change 发现 mcp__agora__* 全局注册后，在 worker agent 的
 *     agent.ctx 作用域重注册同名 shadow，包装 execute 注入 _token/_sid 再委托
 *     ctx.tools.get() 逐调用动态取的全局定义（F181 P4 实证机制；换代即重同步）。
 *   - 身份：sid＝dsh Session id（exec.agent.id / agent.session.id 同一标识；
 *     headless --json 的 session 事件同源）——与 agorad 将来记录的 worker_sid
 *     对齐的单值来源。
 *   - 审计：core/audit.ts reportDeny fire-and-forget POST /gate_deny。
 *   - question 闸（F181 P6 选型）：headless 无弹框通道——对 worker 在
 *     pre-execute 直接 deny（结构化错误回流模型，不弹框不僵死）。
 * 本文件只做接线（与 index.ts 同层）；一切判定语义在 core，禁在此复制。
 */
import {
  ensureOnce, resetLift, LIFT_RECOVER, AGORAD_TIMEOUT_MS, agoradUrl,
  type LiftResult,
} from "../agora-plugin/core/lift.ts"
import {
  getCredential, pullCredential, readRootToken, type PullResult,
} from "../agora-plugin/core/cred.ts"
import { reportDeny } from "../agora-plugin/core/audit.ts"
import {
  shouldBlock, fold, isWriteTool, readishGated, enforceRelevant, questionGate,
  NO_CRED_HINT,
} from "../agora-plugin/core/hook.ts"
import { isDormant } from "../agora-plugin/scope.ts"

export const name = "agora-dsh-shim"
export const inject = ["agents", "tools"]

/** dsh 工具面与共享核心判定类的映射：fs 写类＝edit/write（同名直通）＋
 * apply_patch/str_replace_editor（核心无此名，映射为 write 进同一判定链）。
 * 读类 read/bash 与核心同名。MCP 工具走附牌面（isAgora）。 */
const WRITE_TOOLS_DSH = new Set(["edit", "write", "apply_patch", "str_replace_editor"])

function coreToolName(tool: string): string {
  return tool === "apply_patch" || tool === "str_replace_editor" ? "write" : tool
}

/** 写目标提取（与 opencode 版 input.filePath||input.path 同旨，dsh 参数名 file_path）；
 * apply_patch 的路径藏在 patch 文本里（*** Update/Add/Delete File: 行）——逐条提取
 * 全部判（多文件补丁单看首个会漏越界尾文件）。 */
function targetsOf(args: unknown): string[] {
  if (!args || typeof args !== "object" || Array.isArray(args)) return []
  const a = args as Record<string, unknown>
  if (typeof a.patch === "string") {
    const out: string[] = []
    // Move to:＝apply_patch 的改名目标（Update 块内可选跟随）——改名落点同样过闸；
    // C 册 R1：缺此动词则改名面绕过写判定。
    for (const m of a.patch.matchAll(
      /^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$/gm,
    )) {
      const p = (m[1] ?? m[2] ?? "").trim()
      if (p) out.push(p)
    }
    if (out.length) return out
  }
  const direct =
    (typeof a.file_path === "string" && a.file_path) ||
    (typeof a.filePath === "string" && a.filePath) ||
    (typeof a.path === "string" && a.path) ||
    ""
  return direct ? [direct] : []
}

/** 会话自证 sid：dsh Agent.id＝Session.id（Agent 接口固定二者同值）；
 * agentless 调用回落空串（判定链按无凭据走安全向）。 */
function sidOf(exec: { agent?: { id?: unknown; session?: { id?: unknown } } }): string {
  const id = exec?.agent?.id ?? exec?.agent?.session?.id
  return typeof id === "string" ? id : ""
}

/** 会话 cwd：Session.header.cwd（会话创建时钉死的绝对工作目录）；缺省回落进程 cwd。 */
function cwdOf(exec: { agent?: { session?: { header?: { cwd?: unknown } } } }): string {
  const cwd = exec?.agent?.session?.header?.cwd
  return typeof cwd === "string" && cwd ? cwd : process.cwd()
}

/** enforce 旗拉取（§5.1 核验面，与 index.ts pullEnforce 同式）：lift 定端口→
 * root 牌头读 /health.enforce_on；不可达/异常一律 true＝默认拒（安全向）。
 * 结果按 sid 记忆（off 记忆带 30s 窗——与 cred 拉取节流同界）：供同步 guard
 * 兜底判 enforce off 放行面（V2）；窗外的陈旧 off 不作为放行依据（B 册 R1：
 * 旗翻回 on 且瀑布被旁路时，兜底面不得 fail-open），过期回落凭据核验。 */
const ENFORCE_MEMO_TTL_MS = 30_000
const enforceMemo = new Map<string, { on: boolean; at: number }>()

async function pullEnforce(): Promise<boolean> {
  const lift = await ensureOnce()
  if (!lift.ok) return true
  try {
    const tok = readRootToken()
    if (tok === null) return true
    const r = await fetch(agoradUrl(lift.port, "/health"), {
      headers: { "X-Agora-Token": tok },
      signal: AbortSignal.timeout(AGORAD_TIMEOUT_MS),
    })
    if (!r.ok) return true
    return ((await r.json()) as { enforce_on?: boolean }).enforce_on !== false
  } catch {
    return true
  }
}

function debug(line: string): void {
  if (process.env.AGORA_HOOK_DEBUG) process.stderr.write(`[agora-dsh] ${line}\n`)
}

/** 控制符压平（F49 同款，与 index 拒绝日志一致——恰一行不可伪造）。 */
function flattenCtrl(s: string): string {
  return s.replace(
    /[\x00-\x1f\x7f-\x9f\u2028\u2029]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
  )
}

type Exec = {
  name: string
  arguments?: unknown
  agent?: { id?: unknown; session?: { id?: unknown; header?: { cwd?: unknown } } }
}
type Decision = { kind: string; reason?: string; [k: string]: unknown }
type Next = () => Promise<Decision>

export function apply(ctx: unknown): void {
  const c = ctx as {
    on: (ev: string, fn: (...args: unknown[]) => unknown) => void
    tools: {
      guard: (g: (exec: unknown) => string | undefined) => void
      definitions: () => { name?: unknown }[]
      get: (name: string) => unknown
    }
    agents: { list: () => unknown[] }
  }

  /** 判定链本体（返回 Decision＝拦截决策；null＝放行交还瀑布）。 */
  const decide = async (exec: Exec): Promise<Decision | null> => {
    const tool = exec.name
    const sid = sidOf(exec)
    const cwd = cwdOf(exec)

    // question 闸（先于休眠/lift/拉旗——零 HTTP 零文件读，与 opencode 版同序）：
    // worker 凭据→拒并引导 post(kind=question)；其余零新行为（headless 无 UI 时
    // dsh 自身的审批面已 fail-closed，P6 实证不弹框不僵死）。
    if (tool === "ask_user_question") {
      const qHint = questionGate(getCredential(sid))
      if (qHint) return { kind: "deny", reason: qHint }
      return null
    }

    // R29：休眠分支短路在 lift/pullEnforce 之前——休眠仓零 HTTP 零文件读。
    const isAgora = tool.startsWith("mcp__agora__")
    if (isDormant(cwd)) {
      if (isAgora) return { kind: "deny", reason: "此仓未启用 agora" }
      return null
    }

    const mapped = coreToolName(tool)
    const targets = targetsOf(exec.arguments)
    const relevant = enforceRelevant(mapped)
    const on = relevant ? await pullEnforce() : false
    // memo 只记「真读数」（R2 ①）：非判定类工具的 false 短路不是旗读数——写入
    // 会让 guard 在 30s 窗内据假 off 放行；跳过写入＝guard 回落凭据核验（安全向）。
    if (relevant) enforceMemo.set(sid, { on, at: Date.now() })
    let lift: LiftResult | null = null
    if (on || isAgora) lift = await ensureOnce()
    if (isAgora && lift && !lift.ok) resetLift() // 冷启动自愈：败态即复位 memo（LIFT_RECOVER 承诺）
    let cred = getCredential(sid)
    let pull: PullResult | null = null
    // 拉取闭合三面（与 index.ts 同口径）：write/readish 判定面（enforce on 才消费）
    // ＋agora_* 附牌面（不论旗）；半条缓存（缺 token 或缺 envDir）＝未命中。
    if (
      (!cred?.token || !cred.envDir) && lift?.ok &&
      (isAgora || (on && (isWriteTool(mapped) || targets.some((t) => readishGated(mapped, t)))))
    ) {
      pull = await pullCredential(sid, lift.port)
      if (pull.cred) cred = pull.cred
    }
    debug(
      `tool=${tool} sid=${sid} cred=${!cred?.token || !cred.envDir ? (pull ? pull.reason : "miss") : "cache"} pull=${pull?.reason ?? "-"} enforce=${on}`,
    )
    // 判定链：逐目标判，首个 hint 即拒（多目标补丁不漏越界尾文件）。
    let hint: string | null = null
    let hitTarget = ""
    for (const t of targets) {
      hint = shouldBlock(mapped, t, cwd, cred, on, pull?.reason ?? null)
      if (hint) {
        hitTarget = t
        break
      }
    }
    if (hint) {
      debug(`deny tool=${tool} sid=${sid} target=${flattenCtrl(fold(hitTarget))} hint=${hint}`)
      // 拒绝审计（Req: F176）：fire-and-forget，节流/失败静默——绝不负影响判定。
      reportDeny(lift?.ok ? lift.port : null, {
        sid, tool, target: hitTarget, reason: hint,
      })
      const reason = lift && !lift.ok ? `${hint}（${LIFT_RECOVER}；拒因：${lift.reason}）` : hint
      return { kind: "deny", reason }
    }
    return null
  }

  c.on("tools/pre-execute", async (rawExec: unknown, rawNext: unknown): Promise<Decision> => {
    const next = rawNext as Next
    // 判定链异常＝deny fail-closed（B 册 R1：闸本体此前是全链唯一无兜底的入口，
    // 异常逃逸进 dsh 瀑布的处置语义未知——宁可拒不可旁路）。next() 不入 try：
    // 后续监听器的错误归后续监听器。
    let d: Decision | null = null
    try {
      d = await decide(rawExec as Exec)
    } catch (e) {
      debug(`pre-execute error: ${e instanceof Error ? e.message : String(e)}`)
      return { kind: "deny", reason: "agora shim: 判定链异常（fail-closed 拒）" }
    }
    if (d) return d
    return next()
  })

  // 同步兜底（guard 在 pre-execute 之后、工具体之前；无 allow 结果＝只收不放的
  // 单调拒绝面）：写类工具在「enforce on（或未见旗/记忆过期）＋凭据缓存未命中」
  // 时拒——异步瀑布万一被跳过/中断，写面仍 fail-closed；新鲜 enforce off 记忆
  // （30s 窗内）命中则放行（V2）。guard 本体异常同样 fail-closed 拒。
  c.tools.guard((rawExec) => {
    try {
      const exec = rawExec as Parameters<typeof sidOf>[0] & { name: string }
      if (!exec || !WRITE_TOOLS_DSH.has(exec.name)) return undefined
      const sid = sidOf(exec)
      const memo = enforceMemo.get(sid)
      if (memo && !memo.on && Date.now() - memo.at < ENFORCE_MEMO_TTL_MS) return undefined
      const cred = getCredential(sid)
      if (cred?.token && cred.envDir) return undefined
      debug(`guard-deny tool=${exec.name} sid=${sid}`)
      return NO_CRED_HINT
    } catch {
      return NO_CRED_HINT
    }
  })

  // ── 附牌：mcp__agora__* 的 scoped shadow（F181 P4 机制） ──
  type ShadowRec = { dispose: () => void; def: unknown }
  const shadowLayers = new Map<object, Map<string, ShadowRec>>()

  /** shadow execute：注入 _token/_sid 后委托「逐调用动态取」的全局定义
   * （P4R：全局定义换代后 shadow 仍取到新定义执行）。换牌落空即不附
   * （MCP 壳 -32001 安全向，与 opencode 版同口径）。 */
  function shadowExecute(name: string) {
    return async (args: unknown, exec: unknown): Promise<unknown> => {
      const def = c.tools.get(name) as
        | { execute: (a: unknown, e: unknown) => Promise<unknown> }
        | undefined
      if (!def) throw new Error(`agora shim: ${name} 全局定义当前不可用`)
      const sid = sidOf(exec as Parameters<typeof sidOf>[0])
      const cred = getCredential(sid)
      if (
        cred?.token && args && typeof args === "object" && !Array.isArray(args)
      ) {
        return def.execute(
          { ...(args as Record<string, unknown>), _token: cred.token, _sid: sid },
          exec,
        )
      }
      return def.execute(args, exec)
    }
  }

  /** 单 agent 层重同步：全局 mcp__agora__* 与已建 shadow 对照——换代（定义对象
   * 身份变化）/注销→拆旧建新；无变化→零操作（幂等，防 tools/change 自激环）。 */
  function syncAgentShadows(agent: { ctx: { tools: { register: (d: unknown) => () => void } } }): void {
    let layer = shadowLayers.get(agent)
    if (!layer) {
      layer = new Map()
      shadowLayers.set(agent, layer)
    }
    const current = new Map<string, unknown>()
    for (const def of c.tools.definitions()) {
      if (typeof def?.name === "string" && def.name.startsWith("mcp__agora__")) {
        current.set(def.name, def)
      }
    }
    for (const [name, rec] of layer) {
      if (current.get(name) !== rec.def) {
        rec.dispose()
        layer.delete(name)
      }
    }
    for (const [name, def] of current) {
      if (layer.get(name)?.def === def) continue
      // 先占位再注册：register() 会同步触发 tools/change，重入的 sync 会看到
      // 占位条目而跳过（否则重复注册抛"already registered"且中断本轮循环）。
      layer.set(name, { dispose: () => {}, def })
      try {
        const shadow = { ...(def as object), execute: shadowExecute(name) }
        const dispose = agent.ctx.tools.register(shadow)
        layer.set(name, { dispose, def })
        debug(`shadow ${name} registered`)
      } catch (e) {
        layer.delete(name)
        debug(`shadow register failed ${name}: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
  }

  function syncAllShadows(): void {
    let agents: unknown[] = []
    try {
      agents = c.agents.list()
    } catch (e) {
      debug(`agents.list failed: ${e instanceof Error ? e.message : String(e)}`)
      return
    }
    for (const agent of agents as Parameters<typeof syncAgentShadows>[]) {
      try {
        syncAgentShadows(agent)
      } catch (e) {
        debug(`shadow sync failed: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
  }

  // 触发面①：tools/change——MCP 注册/换代/注销（eager 同步晚于 agent 创建的常态路径）。
  c.on("tools/change", () => syncAllShadows())
  // 触发面②：agent/created——agent 建立时 MCP 已注册的路径（serial 事件，建后即挂）。
  c.on("agent/created", (raw: unknown) => {
    try {
      const { agent } = raw as { agent: Parameters<typeof syncAgentShadows>[0] }
      syncAgentShadows(agent)
    } catch (e) {
      debug(`agent/created sync failed: ${e instanceof Error ? e.message : String(e)}`)
    }
  })
  // 触发面③：agent/disposed——层随 agent 拆（scoped 注册随 ctx dispose，此处只清账；
  // 坏 payload 不外抛——同步事件回调异常可能中断 dsh 的 emit 循环）。
  c.on("agent/disposed", (raw: unknown) => {
    try {
      const { agent } = raw as { agent: object }
      shadowLayers.delete(agent)
    } catch {
      /* 账本清理尽力而为 */
    }
  })
}
