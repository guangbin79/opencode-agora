/**
 * node 模块钩子（Req: F182）——让 dsh（node24 原生 ESM 直载 TS）能够源级
 * import ../agora-plugin/core/*：共享核心为 Bun 目标（bun test 跑测），两处不兼容
 * 由本钩子桥接，不改 core 源码（F176 边界：共享核心零污染）：
 *   ① resolve：core 内部相对导入无扩展名（'./hook'）→ 失败后补 '.ts' 重试；
 *   ② load：lift.ts 模块级 `join(import.meta.dir, ...)` 是 Bun 专属（node 下
 *      undefined）→ 装载期改写为 node 等价 import.meta.dirname。
 * 仅作用于 /agora-plugin/ 路径，其余模块零干预（load 钩子只改写命中文本，
 * 未命中原样透传；node24 对 .ts 的 load source 以 Buffer 返回，转 utf8 处理）。
 */
export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context)
  } catch (err) {
    if (
      (specifier.startsWith("./") || specifier.startsWith("../")) &&
      !/\.(ts|mjs|cjs|js|json|node)$/.test(specifier)
    ) {
      try {
        return await nextResolve(specifier + ".ts", context)
      } catch {
        /* fallthrough：抛原始错误 */
      }
    }
    throw err
  }
}

export async function load(url, context, nextLoad) {
  const r = await nextLoad(url, context)
  if (url.includes("/agora-plugin/") && r.format === "module-typescript") {
    const s =
      typeof r.source === "string" ? r.source : r.source ? r.source.toString("utf8") : ""
    if (s.includes("import.meta.dir")) {
      return { ...r, source: s.replaceAll("import.meta.dir", "import.meta.dirname") }
    }
  }
  return r
}
