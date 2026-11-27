/**
 * F159 两形读牌红测——readRootToken：v2 install 纯文本 / v3 authsvc JSON
 * {token}（agora CLI/board_tui 同形）；坏形（非对象 JSON/缺 token 键/空串）
 * 一律 null 安全向；缺文件 null。
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { readRootToken } from "./cred";

let root: string;
let home: string;
let saved: string | undefined;

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "agora-cred-"));
  home = join(root, "plaza");
  mkdirSync(join(home, ".agora"), { recursive: true });
  saved = process.env.AGORA_HOME;
  process.env.AGORA_HOME = home;
});

afterAll(() => {
  if (saved === undefined) delete process.env.AGORA_HOME;
  else process.env.AGORA_HOME = saved;
  rmSync(root, { recursive: true, force: true });
});

const tokPath = () => join(home, ".agora", "root.token");

describe("readRootToken 两形读牌（F159）", () => {
  test("v2 纯文本：trim 后原样返回", () => {
    writeFileSync(tokPath(), "tok-plain\n");
    expect(readRootToken()).toBe("tok-plain");
  });

  test("v3 JSON {token}：取 token 键", () => {
    writeFileSync(tokPath(), '{"token":"tok-json"}\n');
    expect(readRootToken()).toBe("tok-json");
  });

  test("坏形回落 null：非对象 JSON/缺 token 键/token 非串/空文件", () => {
    for (const bad of ['{"token"', '{"no_token":1}', '{"token":42}',
                       '{"token":""}', ""]) {
      writeFileSync(tokPath(), bad);
      expect(readRootToken()).toBeNull();
    }
    rmSync(tokPath(), { force: true });
    expect(readRootToken()).toBeNull(); // 缺文件同安全向
  });
});
