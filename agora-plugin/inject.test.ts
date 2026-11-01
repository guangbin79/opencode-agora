/**
 * inject 单消息双标记自举红测——issue-agent 持牌计划 Task 1。
 * 单条 prompt 同时携 BOOT 与 credential 标记（Task 2 单消息前缀形态）→一次缓存齐全
 * （tid+envDir+token）；旧两消息序（先 BOOT 后 credential）行为不变（①②④为回归锚）。
 */
import { describe, expect, test } from "bun:test";

import { cacheCredential, getCredential, type Cred } from "./cred";
import { onInject } from "./inject";

describe("onInject 单消息双标记自举", () => {
  test("①仅 BOOT 行＋env 行→缓存 {envDir, token 空}（半条缓存旧形态不变）", () => {
    // Req: issue-agent 持牌计划 Task 1
    onInject({ sessionID: "s-inj-boot", text: "[agora] gt-t1: 任务标题\npage: p.md\nenv: /env-one\ncontract: x" });
    expect(getCredential("s-inj-boot")).toEqual({
      token: "", sid: "s-inj-boot", role: "worker", tid: "gt-t1", envDir: "/env-one",
    });
  });

  test("②仅 credential 两行→缓存 token，envDir 继承既有（重派换牌不丢环境位）", () => {
    // Req: issue-agent 持牌计划 Task 1
    const seeded: Cred = { token: "tok-old", sid: "s-inj-cred", role: "worker", tid: "t-old", envDir: "/env-inherit" };
    cacheCredential(seeded);
    onInject({ sessionID: "s-inj-cred", text: "[agora] credential t-new\ntoken: tok-new" });
    expect(getCredential("s-inj-cred")).toEqual({
      token: "tok-new", sid: "s-inj-cred", role: "worker", tid: "t-new", envDir: "/env-inherit",
    });
  });

  test("③单条同含四行（BOOT＋env＋credential）→一次缓存三值齐", () => {
    // Req: issue-agent 持牌计划 Task 1
    onInject({
      sessionID: "s-inj-both",
      text: "[agora] gt-t3: 任务标题\npage: p.md\nenv: /env-both\n[agora] credential t-both\ntoken: tok-both",
    });
    expect(getCredential("s-inj-both")).toEqual({
      token: "tok-both", sid: "s-inj-both", role: "worker", tid: "gt-t3", envDir: "/env-both",
    });
  });

  test("④无标记→缓存不变", () => {
    // Req: issue-agent 持牌计划 Task 1
    const seeded: Cred = { token: "tok-keep", sid: "s-inj-plain", role: "worker", tid: "t-keep", envDir: "/env-keep" };
    cacheCredential(seeded);
    onInject({ sessionID: "s-inj-plain", text: "普通消息，无任何标记" });
    expect(getCredential("s-inj-plain")).toEqual(seeded);
  });

  test("⑤dotfiles daemon 逐字前缀（issue#222 生产者真实字节）→一次缓存三值齐", () => {
    // Req: F33 终审——消费端正则吃下生产者真实字节（tid=task- 前缀＋真实 env 绝对路径＋32hex 牌）
    onInject({
      sessionID: "s-inj-dotfiles",
      text: "[agora] task-issue-222: issue-agent round\nenv: /home/guangbin/Documents/TitanNavi\n[agora] credential task-issue-222\ntoken: b4f8e2a1c6d395708fa1b2c3d4e5f678",
    });
    expect(getCredential("s-inj-dotfiles")).toEqual({
      token: "b4f8e2a1c6d395708fa1b2c3d4e5f678", sid: "s-inj-dotfiles", role: "worker",
      tid: "task-issue-222", envDir: "/home/guangbin/Documents/TitanNavi",
    });
  });
});
