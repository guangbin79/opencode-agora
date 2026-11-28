/**
 * shim 装载入口（Req: F182）——dsh patch overlay 的源 TS 直载形态（F181 探针实证路线）。
 * 静态 import 会在任何代码执行前求值，故共享核心的装载必须由本入口先装好桥接再
 * 动态 import：① 注册 ts-hooks.mjs（无扩展名补 .ts＋import.meta.dir 改写）；
 * ② polyfill Bun 全局（lift.ts 的 Bun.spawn/Bun.sleep——容器内 agorad 先于 dsh 起，
 * 正常不走 spawn 分支；缺位时 node 等价实现兜底，spawn 签名差异在适配层吸收）；
 * ③ 动态 import ./agora-shim.ts 并转发插件导出。
 */
import { register } from "node:module"
import { spawn } from "node:child_process"

const g = globalThis as unknown as Record<string, unknown>
if (!g.Bun) {
  g.Bun = {
    // Bun.spawn(cmd, opts) 单数组签名 → node spawn(cmd[0], rest, opts)
    spawn: (cmd: string[], opts: Record<string, unknown>) =>
      spawn(cmd[0], cmd.slice(1), opts as never),
    sleep: (ms: number) => new Promise((r) => setTimeout(r, ms)),
  }
}

register("./ts-hooks.mjs", import.meta.url)

const mod = await import("./agora-shim.ts")
export const name: string = mod.name
export const inject: string[] = mod.inject
export const apply = mod.apply
