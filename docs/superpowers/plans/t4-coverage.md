# T4 验收对应表 — 计划②核销（2026-10-27 更新）

> ①部分见前版（132 用例全绿已并入 main）。本版核销①标记②的行——T6（t2_enforce.sh 七例）＋T7（t3_chain.sh 十二场景 54 断言）真 opencode e2e 落锚，红项清零。

| 需求验收行（①标②项） | e2e 锚 | 状态 |
|---|---|---|
| F1 无凭据写必拒（插件 hook 面） | T6 S1：error 工具部＋引导文案＋文件缺失三重断言 | ✅ |
| F1 豁免三例（插件面） | T6 S3a/b/c：非 git//tmp/opencode 含 .git/enforce off→on＋审计 | ✅ |
| F1 自身仓不豁免 | T6 S4：拒＋文件缺失（狗粮） | ✅ |
| F1 服务不可达默认拒 | T6 S5：拒因含 resetLift 恢复指引 | ✅ |
| F1 越权读拦截·读引导 | T6 S6：state/messages 拒＋引导、events/wiki 放行（bash 路径单测锚，e2e 接受面记录） | ✅ |
| F1 bash 不拦（接受面） | T6 S7：直写落盘在案 | ✅ 记录性 |
| F1 非 root 开 OFF 无效 | T7：worker 牌 /enforce→403＋enforce_on 不动＋auth_denied | ✅ |
| F2 驳回起重 | T7 S2 折入：驳回下行实时注入→wait_idle→重报 registered | ✅ |
| F4 在线 ≤5s（真 opencode 半边） | T7 S3：2.37s（created−t0 诚实上界口径，剔轮询） | ✅ |
| F4 连续 10 条按序（真半边） | T7：[0..9] 全序（API 新在前的展示序经反转＋稳定排序处理） | ✅ |
| F4 blocked 拍人（注入主会话） | T7 携带口径：main sid 绑定走留库补拉（T6 实证）＋升级四类单测锚 | ✅ 口径记录 |
| F5 无 cron/stalled 语义扫 | T7 收尾：ast 剔注释/docstring，cron/crontab/watchdog/看门狗/巡检/全局扫描/stalled 零命中 | ✅ |
| F6 杀 agorad 重启续 | T7：kill -9→恢复→旧牌 200→续至 closed | ✅ |
| F6 杀 opencode 重启 | T7：同 URL 重启→会话持久化→tick 探活→续至 closed（password 轮换两态记档＋msg.py 根修） | ✅ |
| F6 会话死亡重注入 | T7：DELETE→tick→reinject 新 sid＋BOOT/凭据重发→**旧牌 done 受理走全门至 closed** | ✅ |
| F6 done 崩溃窗口 | T7：in_gate kill -9→重启重排→幂等重跑→单 done/merged/closed 零双死 | ✅ |
| F11 门内 cancel 排队生效（真半边） | T7：pending_cancel 置位→门尾 cancelled＋env 回收＋旧凭据 401 | ✅ |
| F3 can_spawn 名册语义 | 复核口径＝oc.create 失败熔断（T7 dispatch 全链实证）；名册实体语义无需求面 | ✅ 口径记录 |
| F12 休眠仓直写放行＋agora_* 拒（启用域） | T1 bun：scope.test.ts 8 例（名册容错/精确匹配/worktree 不误伤/直通零 HTTP/agora_* 拒）＋install 名册骨架；真 opencode 会话层未单测——随实装冒烟核销（插件层证据已足，照实标注） | ✅ 插件层锚 |

**红项：零。** 平台风险两项记档（serve password 轮换：msg.py 落库兜底＋运维重启；opencode v2.1.4 MCP spawn 间歇缺失：S3 走服务面绕开，对账锚职责）。
