/**
 * scope 启用域（F12，R26/R29）：中央名册 $AGORA_HOME/.agora/inactive-repos 按仓休眠。
 * 名册＝仓根绝对路径一行一个（尾斜杠容错，空白/相对路径坏行跳过，缺失＝全员活跃）；
 * 启动时缓存＋每调用轻查（不热载；新会话生效）；isDormant 精确路径匹配（无前缀/模糊）。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { agoraHome } from "./hook";

let cache: string[] | null = null;  // 兼容测试 API（resetScope）；生产路径已去缓存

function norm(p: string): string {
  return p.replace(/\/+$/, "");
}

/** 读名册：缺失/不可读＝空名册（全员活跃，安全向＝现行行为）。每次调用重读（R26 修订：去进程缓存，名册改动即生效）。 */
export function loadInactiveRepos(): string[] {
  try {
    return readFileSync(join(agoraHome(), ".agora", "inactive-repos"), "utf8")
      .split("\n")
      .map((l) => norm(l.trim()))
      .filter((l) => l.startsWith("/"));
  } catch {
    return [];
  }
}

/** 会话仓根是否休眠：对启动时缓存精确匹配（worktree toplevel≠主检出路径→不受名册影响）。 */
export function isDormant(repoRoot: string): boolean {
  return loadInactiveRepos().includes(norm(repoRoot));
}

/** 新会话重置（R26 不热载：名册变更经重启会话生效；亦供测试隔离）。 */
export function resetScope(): void {
  cache = null;
}
