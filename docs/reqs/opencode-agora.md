### F18 插件侧 worker PATH 注入（2026-10-28，gt-26c027）
生产实证（gt-71b6af 踩坑）：agorad 拉起全量透传（lift.ts Bun.spawn），但 worker 会话（opencode serve 服务生成）PATH 被剥成 ".opencode/bin:/usr/local/bin:/usr/bin:/bin"——剥离在 opencode 服务层，worker 跑 bun 套件须手动补 ~/.bun/bin。用户裁决：不做 worker 自救，插件/装配侧一次做对。接缝勘察定夺（v2.2.0 二进制实证）：shell 工具与终端同经 Shell.create→trigger("shell","create.before",payload)，payload.env 按引用直改直达 spawn env——插件 setup 可选挂载 ctx.shell.hook("create.before")（旧运行时无 shell 命名空间静默跳过），spawn 前基于既有 PATH 去重前插 $HOME/.bun/bin 与 $HOME/.local/bin（不硬编码全量；幂等；payload 缺 env 安全向不动）。
**验收**：注入路径插件测试锁死（补全/去重/幂等/接线原地改 env/缺 env 与旧运行时安全向）；`grep -rqF ".bun/bin" agora-plugin/` 过；bun test agora-plugin/ 全绿；install.sh 整目录复制天然携带新模块零改动。

### F19 worker 牌换号自愈——活动任务漂移即改绑（2026-10-28）
会话续接会更换调用 sid，dispatcher 侧已有 bind_main 自愈（R25-A），worker 牌无此待遇：续接 worker（token 仍有效）调 done 因牌面 sid 与任务行漂移死锁（实证 gt-f23e94：活已干完 done 死锁，靠 cancel＋cherry-pick 收割 gt-dba437）。设计（对齐派发方同款）：worker 牌调用携 X-Agora-Sid 且与注册 sid 漂移、注册 sid 是某活动任务（dispatched/running/done/verify_failed/reopened/blocked）的 worker_sid → auth.rebind（旧 sid 入血统，token 本体不变）＋任务行 worker_sid 同步覆写（与 recover.reinject 同口径）。守卫：终态（merged/closed/cancelled）与无主 sid 不触发；dispatcher bind_main 与 reinject 行为不变；改绑后 done 走既有五拒，验收一克不减。
**验收**：续接 worker 调 done 受理＋行覆写＋血统入册；无牌/他牌拒且零改绑；终态不触发改绑；reinject 与 dispatcher 改绑既有测试保持绿；全量测试绿。
>>>>>>> c5558c7 (feat(auth): worker 牌换号自愈——活动任务 worker 漂移即改绑（gt-d55b9e）)