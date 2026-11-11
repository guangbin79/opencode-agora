/**
 * scope 启用域（F12，R26/R29）：中央名册 $AGORA_HOME/.agora/inactive-repos 按仓休眠。
 * 名册＝仓根绝对路径一行一个（尾斜杠容错，空白/相对路径坏行跳过，缺失＝全员活跃）；
 * 每次调用重读名册（热载，即改即生效——去缓存后行为）；isDormant 精确路径匹配。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { agoraHome } from "./hook";

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

/** 会话仓根是否休眠：名册每次重读后精确匹配（worktree toplevel≠主检出路径→不受名册影响）。 */
export function isDormant(repoRoot: string): boolean {
  return loadInactiveRepos().includes(norm(repoRoot));
}
