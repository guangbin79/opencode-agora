# T5 对账记录：真 opencode API（v2.1.4）× stub 契约

> 实测基线：opencode v2.1.4（本机 `~/.opencode/bin/opencode`）；探针
> `tests/e2e/oc_probe.ts`（bun run），产物 `/tmp/opencode/oc-probe.json`。
> 方法：独立 `opencode serve --service` 实例（隔离 XDG_*，临时目录/独立端口，探完自清），
> 绝不触碰承载本会话的运行中服务。

## 七项探针裁决

| # | 项 | stub 旧契约（假定） | 实测形状（v2.1.4） | 裁决 |
|---|---|---|---|---|
| 1 | create | `POST /session {cwd,message}→{id}` | `POST /api/session {}→{data:{id,location}}`；`cwd/message` 字段被静默忽略；`title` 接受；目录绑定＝`POST /api/session/{sid}/move {directory}→204`（效果经 MCP 子进程 cwd 实证；紧随的 GET 读到旧值，落定有延迟） | **偏离→双侧修**（oc.py create＝create+move+prompt；stub 镜像） |
| 2 | inject | `POST /session/{sid}/inject {text}→{ok}`；busy 排队按序 | `POST /api/session/{sid}/prompt {text}→200 {data:{delivery:"steer"}}`；busy 会话连续 10 条全 200、终态消息序＝提交序（实测 inOrder:true）；未知/已删 sid→404 `SessionNotFoundError` | **偏离→双侧修**（排队语义成立，端点/体/错误码换） |
| 3 | alive/status | `GET /session/{sid}/status→{alive}`；404 vs 200 | `GET /api/session/{sid}→200 {data:{...}}`／404；`GET /api/session/active→{data:{sid:{type}}}` 仅含 running（idle 不在 map，死＝404） | **偏离→双侧修**（alive＝GET 会话存在性） |
| 4 | probe/health | `GET /health→{ok,version}` | 无 `/health`（SPA 兜底 200 text/html 冒充）；版本来源＝`GET /api/info→{version,pid,urls,paths}` | **偏离→双侧修**（probe 读 /info.version） |
| 5 | tool.execute.before 覆盖 MCP＋改参（前提①②） | 假定覆盖＋可改参 | **成立**：钩子对 MCP 工具触发（tool 名＝`<server>_<tool>`，如 `agoraprobe_echo`）；改 `event.input` 直达 MCP 服务端（实测 echo 收到 `{"a":"hooked-1","_token":"tok-from-hook"}`） | **前提①② PASS** |
| 6 | MCP spawn 拓扑（前提③） | 每实例一进程 vs 每会话 | **实例级（×location）**：serve 启动即 spawn（未等会话），pid 随 server；同一 location 的多会话共享一进程；两 location（server 根＋会话 moved 目录）各一进程 | **前提③ PASS**（非每会话，量级＝实例） |
| 7 | inject 事件插件侧＋端口发现 | 假定 inject 事件＋AGORA_OC_URL 来源待定 | 插件侧注入事件＝`ctx.session.hook("prompt", ev)`，ev `{sessionID, messageID, prompt:{text}, delivery}`；服务发现＝`$XDG_STATE_HOME/opencode/service.json`（serve --service 写 `{id,version,url,pid,password}`）；API＝`<url>/api/*`＋Basic `opencode:<password>` | **偏离→接线收紧**（lift/main/install 回填发现源） |

## 关键偏离与双侧修清单

1. **oc.py 全量重写**（`agorad/access/oc.py`）：/api 前缀＋Basic 认证＋`{data}` 信封＋`OcUnavailable.status`
   带 HTTP 码；create＝create+move+prompt 三步；inject 死会话 404→False；alive＝GET 存在性；probe→/info。
2. **stub_opencode.py 镜像重写**：/api 前缀、信封、move 204 无体、prompt→delivery:steer、
   404 SessionNotFoundError、/info、DELETE 204、kill＝会话消失（死＝404）。
3. **服务发现回填**：`oc.discover()` 读 `$XDG_STATE_HOME/opencode/service.json`；main.py
   env_config 缺省走它（env 显式优先），password 经 `AGORA_OC_PASSWORD` 透传；lift.ts 同源
   `discoverOc()`，拉起 agorad 时带上 url+password；install.sh 首装即从 service.json 回填。
4. **插件接线收紧**（`agora-plugin/index.ts`）：稳定运行时＝`{id, setup(ctx)}` plain-object
   （async-Hooks/`Plugin.define` 形态静默失效——假 ctx 实证 "Plugin must export a default
   definition with an id and an effect or setup function"）；钩子＝`ctx.tool.hook("execute.before")`
   ＋`ctx.session.hook("prompt")`；附牌＝对 `agora_*` 工具写 `input._token`（改参直达已实证）；
   cwd 取 `ctx.session.get({sessionID}).location.directory`（异步，失败回落 process.cwd()，
   deny 安全向）。
5. **script 路径发现**（T3/T4 追加项）：lift 解析序 `opts.script → env AGORAD_SCRIPT →
   $AGORA_HOME/.agora/agorad/main.py（install.sh 载荷随装）→ 同仓 dev 兜底`。
6. **credential 落盘退役**：v2.1.4 会话无插件可见落盘目录——牌只驻内存缓存，进程重启由服务侧
   recover reinject 重发兜底（cred.ts 的 read/writeCredential 保留为纯工具，inject 路径不调用）。

## 附牌三前提判定

- ① tool.execute.before 覆盖 MCP 工具：**PASS**（`agoraprobe_echo` 触发）
- ② 改参能力：**PASS**（input 改动直达 MCP 服务端，`_token` 附带成功）
- ③ MCP spawn 拓扑＝实例级共享（非每会话）：**PASS**

**无 BLOCKED 项**——三前提全过，未触发 R21 替代案上报。

## 遗留观察（记录在案，不阻塞）

- 新建会话短暂出现在 `/api/session/active`（type:"running"）随后消失——alive 不依赖该端点。
- move 后立刻 GET 会话读到旧 location；实际生效（MCP 子进程 cwd 已是新目录）。oc.create 的
  move→prompt 次序天然留出落定窗口。
- `/health`、`/global/health` 返回 200 text/html（web SPA 兜底）——就绪判据不能用「200 即就绪」，
  lift 对 agorad 的「任意应答」判据不受影响（agorad 是自家服务）。
- 插件 setup 在 serve 内被调用两次（两 location 上下文）——钩子注册幂等，无副作用。
