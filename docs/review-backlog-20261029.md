# 存量 11 findings 验尸分拣报告（task-bb4ad2，2026-10-29）

来源：F27 收殓 worker 全范围评审，派发方仅持单行域摘要（board_tui×3；app.py TOCTOU×3；
install.sh token 泄露面×3；gitx prune×1；agora CLI 解析×1）。本报告为逐域代码审读复勘后
的验尸定性与处置。判例基线：608ccb/gt-e38e41 已修「拉起日志 0600＋死后抹牌行」。

| # | 域 | finding | 定性 | 严重度 | 处置 |
|---|----|---------|------|--------|------|
| 1 | board_tui | apply/fold 缓存帧重建无条件清 err（`_rebuild` 内 `err=None`，board_tui.py L564）——轮询离线横幅被无新数据的重建洗掉，违背模块自述「err 语义＝轮询离线」 | 真问题（逻辑） | low | 修复：err 清除移入 poll 成功路；run() 级回归测试（`TestRunErrSemantics`） |
| 2 | board_tui | `_dw` 死代码：零引用，`_pad` L51 内联同一东亚宽度规则——双副本漂移温床 | 真问题（可维护性） | low | 修复：删 `_dw` |
| 3 | board_tui | `import unicodedata` 模块中部（渲染段落横幅下，非导入块） | 真问题（风格/E402） | low | 修复：归首部导入块 |
| 4 | app.py TOCTOU | `bind_worker` 改绑与 `set_fields(worker_sid)` 凭单次快照、无终态复检——HTTP 线程读快照与门线程迁移（done→merged）并发时终态行 worker_sid 被覆写；镜像方法 `bind_dispatcher` L120 有「TOCTOU 复检」而 bind_worker 缺（镜像缺口） | 真问题（并发/安全） | medium | 修复：改绑前零活动行复检＋两处覆写循环逐行终态复检；陈旧快照注入测试×2 |
| 5 | app.py TOCTOU | `bind_worker` 嵌套 dispatcher_sid 覆写循环同缺复检（同窗同害：merged 行 authorship 被改） | 真问题（并发） | medium | 修复：同处补复检；写窗内竞态测试（`test_bind_worker_midwindow_settle_skips_frozen_row_only`） |
| 6 | app.py TOCTOU | `bind_dispatcher` oc.alive 探活窗（网络 I/O，秒级）后 `auth.rebind` 无条件执行——hit 全数窗内结案时零消费方仍换 root 牌身份（rebind 是持久身份换绑，无活动行即无收益） | 真问题（并发/安全） | medium | 修复：rebind 前零活动行复检；测试 `test_bind_dispatcher_stale_snapshot_all_settled_no_rebind` |
| 7 | install.sh token | 启动失败路径 `cat "$LOG" >&2` 转储含 root 牌行——token 行先于 ready 行 flush（main.py L142 vs L152），起而不就绪时 cleanup 的抹牌尚在 EXIT 之后，牌已泼进终端/agent 转录 | 真问题（安全） | high | 修复：转储改流式 `sed '/^AGORA_ROOT_TOKEN: /d'` 抹行（无 -i，GNU/BSD 同形）；源级锁断言 |
| 8 | install.sh token | cleanup 只抹 `$LOG`——前轮被信号打断（INT/TERM 不触发 EXIT trap）时牌行滞留旧日志，本轮 `mv` 成 `.1` 后长驻留档 | 真问题（安全） | medium | 修复：cleanup 对 `$LOG`/`$LOG.1` 一并抹；源级锁断言 |
| 9 | install.sh token | 拉起行 `env AGORA_OC_PASSWORD=… setsid nohup python3…`——OC 密码（opencode API Basic 凭据）经 argv 落 `/proc/<pid>/cmdline`，本地全体用户可读（env 进程存活窗口，毫秒级但真实） | 真问题（安全，窗口小） | medium | 修复：子壳 `export`＋`exec`（凭据只走 environ）；setsid/nohup 两分支与 `$!`=daemon pid 语义不变；场景 1 行为套件护航 |
| 10 | gitx prune | `remove_env` 无 `worktree prune`——kill 窗口/外部 `rm -rf` 留下 `.git/worktrees/<name>` 注册元数据永驻，且分支删除被其挡（`used by worktree`），-D 升级后也仅删支不消注册 | 真问题（卫生） | low | 修复：分支级联前 `worktree prune`（check=False）；ghost 注册测试（先 rmtree 后回收，断言 `worktree list` 收敛） |
| 11 | agora CLI 解析 | `run_status` 报因不带 task id（`fetch` 同契约尾附 tid「看门狗聚合 stderr 可归任务」，L84 自述）；另 `/doctor` 响应缺 `stale_candidates` 键（版本偏差）时裸 KeyError 出 `agora: 'stale_candidates'` | 真问题（可观测性/健壮性） | low | 修复：Fail/Transient 尾附 tid＋`doc.get("stale_candidates", [])` 降级；测试断言×2 |

## 误报/已修留痕（勘误记录，非上表行）

- `bind_main`（app.py L39-42）占位守卫的 check-then-rebind 竞态：**误报**——单线程
  HTTPServer（httpd.py L290/L316「串行口径」）下 bind 面互斥，门/health 后台线程均不写
  `worker_sid`，无并发写者即无窗。
- install.sh 另核两处疑点：`env` 子进程 argv 瞬态暴露即 #9 本体（非独立 finding）；
  daemon 持 fd 旧 inode 残留（sed -i 换 inode）在「第 8 步先 kill 后 cleanup」时序下不成立
  （gt-e38e41 已证），不另立案。
- 608ccb 覆盖核对：#7/#8/#9 三处均不在其「日志 0600＋死后抹牌」修法面内，均未覆盖。

## 处置汇总

- 真且小（本单修复＋测试）：#1-#11 全部——各域净改动 ≤50 行。
- 待立项（架构级）：无。
- 测试：全量 `python3 -m unittest discover -q tests` 399 绿（基线 392＋新增 7）＋
  `bash tests/test_install.sh` 53 pass 0 fail。
- 评审环：OCR 4 轮修复至绿——R1×4（.bak 残牌面／断言虚锁）→R2×3→R3×2→R4×2→终轮 0；
  全程零 blocker/high，各项均当轮修复（详见 .review-loop/20261029-1450-task-bb4ad2.md）。
- 提交：e68a7df（报告＋11 项主修）＋a85018b/104d376/49776a3/a57dc43（评审环轮次修复），
  锚 .last-reviewed == a57dc43e8fe3e2c90b3069603619abd88d67140a。
