# T4 验收对应表 — 计划①agorad 核心（R15）

> 生成法：`grep -oh "Req: F<n>" tests/*.py | sort | uniq -c` ＋ 需求 §4 逐验收行人工映射。缺用例的验收行＝红。
> 计划②核销项：真 opencode 半边（标②）。

| 需求验收行 | 用例锚（文件::代表性用例） | 状态 |
|---|---|---|
| **F1** 无凭据写 git 仓必拒 | test_transport_httpd::401×3/403 矩阵（HTTP 面）；插件 hook 层 | ②（hook 拦截本体） |
| F1 持凭据 cwd∈环境放行 | test_access_gitx::sibling/env | ✅（服务侧）；hook 侧 ② |
| F1 豁免三例 | enforce(off) root 生效 test_core_auth＋test_transport_httpd | ✅；非 git/临时目录豁免 ② |
| F1 自身仓不豁免 | — | ②（插件面） |
| F1 非 root 开 OFF／审计 | test_core_auth::set_enforce 非 root＋审计；httpd enforce 403 | ✅ |
| F1 越权读拦截 | /view=board 403＋auth_denied | ✅（HTTP 面）；文件读引导 ② |
| **F2** 创建后可查 | test_core_task::create seeds queryable row | ✅ |
| F2 状态迁移服务权威 | test_core_state 全迁移矩阵 | ✅ |
| F2 回填→登记/驳回各一例 | test_core_task::happy path＋Rejected | ✅（驳回起重＝消息层，②核销口径已记） |
| F2 worker 改 verify 必拒＋审计 | test_core_task::worker rejected and audited | ✅ |
| **F3** 链建立登记 | test_core_task::dispatch 全链＋pages 重渲 | ✅ |
| F3 超深/超配额必拒报因 | test_core_task::四保险丝 | ✅ |
| F3 can_spawn | test_core_task（oc.create 失败熔断口径） | ✅（名册语义②复核） |
| F3 跨仓顺序 e2e | B7 after 前驱拒；真链 e2e | ✅（服务侧）／②（e2e） |
| **F4** 在线 ≤5s | test_core_msg 在线路径；计时断言 | ✅（stub）／②（真 opencode） |
| F4 离线补拉 | test_core_msg::check_mail 游标 | ✅ |
| F4 done/blocked 迁移＋审计 | gate/task 全路径 | ✅ |
| F4 连续 10 条按序 | test_transport_httpd::10 条按序 | ✅（stub）／② |
| F4 blocked 5s 拍主会话 | test_core_health::四类 escalate 达 main_sid | ✅（stub） |
| **F5** 死亡→恢复或上报 | test_core_health/recover | ✅ |
| F5 board/doctor 查 last_seen | test_core_report::doctor_view | ✅ |
| F5 每类升级一例 | test_core_health::four kinds | ✅ |
| F5 无 cron/stalled 代码路径 | test_architecture（import 面）＋无 stalled 代码 | ✅（弱锚，②补语义扫） |
| **F6** kill -9 状态一致 | test_access_store::torn line／test_core_auth 重启恢复 | ✅ |
| F6 in-flight 续跑 | test_core_recover::running 重注入（牌重绑） | ✅（stub） |
| F6 dispatcher 死通知父/根 | test_core_health::dispatcher-dead | ✅ |
| **F7** verify 败→verify_failed＋尾部 | test_core_gate | ✅ |
| F7 集成败字节级回滚＋reopened | test_core_gate::rollback before_sha | ✅ |
| F7 非 ff 拒并 reopened | test_core_gate::non-ff（streak 不变） | ✅ |
| F7 reopened 通知＋环境在位／3 连败 blocked | test_core_gate | ✅ |
| F7 merge 仅经 verify 绿（无手动面） | 无 merge 端点＋empty-cmd 双侧拒 | ✅ |
| F7 评审未绿 done 拒 | test_core_gate::anchor 三分支 | ✅（锚自写接受面已记） |
| F7 闸体→升级通知 | test_core_gate::gatefiles | ✅ |
| **F8** grep 完整事件链 | test_access_store::read_events＋审计行散布各套 | ✅ |
| **F10** 五桶/结论/commit | test_core_report::buckets/detail | ✅ |
| F10 取消例＋跨链展开 | test_core_task::cancel 级联／chain_view | ✅ |
| **F11** 两级链中止各断言 | test_core_task::cascade＋gate 尾补齐 | ✅ |
| F11 merged 不回滚 | test_core_task::spares merged | ✅ |
| F11 越权 cancel 拒＋审计 | auth 矩阵＋httpd 403 | ✅ |
| F11 cancelled 旧凭据必拒 | test_core_task::redispatch revoke／gate 尾 revoke | ✅ |
| F11 门临界段排队生效 | test_core_gate::pending-cancel 双分支 | ✅ |
| F11 空链幂等＋审计 | test_core_task | ✅ |

红项（①范围应测未测）：无——全部行绿或显式标②。
锚分布：F1×15 F2×14 F3×9 F4×12 F5×12 F6×16 F7×21 F8×3 F10×7 F11×8（共 117 用例）。
