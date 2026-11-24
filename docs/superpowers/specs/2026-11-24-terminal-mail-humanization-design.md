# F171 终态邮件人话化——邮件面单飞渲染

日期：2026-11-24　状态：已批准（用户 2026-11-24 方案 A 裁定）
需求条目：`docs/reqs/opencode-agora.md` F171

## 背景

agora 终态邮件（F63）正文＝发往 dispatcher 会话的同一行服务合成文本，两去处
共用：①会话注入（agent 消费，需要机器位 tid/sha/ahead/状态码）②外呼邮件到人
（email.json opt-in，email-send.sh 取首行作主题）。现状邮件就是这行密语原样
上屏，人类不可读。实例（2026-11-24 实发）：

```
任务 ag-7d7e1c merged @678f691 local=synced——沉淀: 聚本任务 note/task page
走 knowledge ingest——origin 未同步（ahead=1），请派发方自推
```

任务 state 有人类标题（title 字段，tasksvc 建单即存）但邮件未用。

## 方案裁定（用户 2026-11-24，方案 A）

**邮件与会话行分家**：邮件单独人话渲染，会话行逐字节不变。修订 F63
「正文＝会话行同源」口径——「零 worker 文本插值」原则保留（原始通知行兜底
携带服务合成行，邮件面不引入任何 worker 可控文本）。

**单点收口**：`agorad/core/msg.py` 的 `notify_transition()` 里
`self._mail(tid, text)` 改为传 `_human_mail(tid, to, text, task)` 的返回值；
会话注入路径（`notify_dispatcher`）零改动。gate/task/tasksvc/gatedelivery
全部 `notify_transition` 调用点的 detail/body 原样——它们的邮件经单点渲染
自动人话化，会话行逐字节不变。

## 渲染契约

`_human_mail(tid, to, text, task) -> str`（msg.py 模块级私有函数，纯字符串
拼接，不查 git 不 IO 不抛；`to` 已由 notify_transition 的 `_TERMINAL_MAIL`
闸在四终态集内，函数内不防）：

- **首行（＝邮件主题）**：`[agora] {状态词}：{title}（{tid}）`
  - 状态词映射：merged→已合并／closed→已收口／blocked→受阻／cancelled→已取消
  - title＝`task.get("title")`，缺失回退 tid——回退形态主题为
    `[agora] {状态词}：{tid}`（不重复 tid）
- **正文**（与主题行空一行后）：
  - 概述行：merged→`「{title}」已通过验证并自动合并。`／closed→`任务收口
    （环境已回收）。`／blocked→`任务受阻——需重派（dispatch）或取消
    （cancel）。`／cancelled→`任务已取消。`
  - 事实行（sha 在案才给，缺则整行省略）：`· 仓库/分支：{project_dir 末段} ·
    {branch}（提交 {sha 前 7 位}）`——branch 优先 `task["delivery"]
    ["target_branch"]` 回退 `task["target_branch"]`；sha 取 `task.get
    ("commit")` 或 `task["delivery"]["commit"]`
  - merged 专属两行：`· 本地已同步`（`delivery.origin_ahead` 为 >0 整数时
    改为：`· 本地已同步；远端尚未推送（领先 {n} 个提交）→ 需要你手动 git
    push`）＋ `· 收尾提醒：本任务笔记/任务页做一次知识沉淀（knowledge
    ingest）`
  - 兜底行恒在：`· 原始通知：{text}`

**ahead 投影**：`agorad/core/gate.py` `_finish_local_delivered()` 把
publication 调用前移到 delivery dict 组装处，`pub.get("ahead")` 写入
`d["origin_ahead"]`（int；publication 异常或非 int 时省略该键，同既有 F54
降级口径）；detail 拼接改用已取的 pub（行为等价）。F110 超龄判据零变化
（仍用 delivered_at）。

## 边界（不做）

- 不动会话注入路径与文案：[agora] 头、kind=result、接线点 detail/body
  逐字节不变。
- 不动 email-send.sh／notify-owner／health 的 stalled/escalate 邮件合成（二期）。
- 不动 F63 窗去重/必达/审计：`_mailed`、`email_sent` 审计事件原样。
- 不做 HTML 邮件/附件/多收件人。
- title 主题净化（评审 C 册 R1）：title 可源自 worker 嵌套建单（tasksvc 压平
  只除空白）——渲染时剥 C0/C1 控制字符＋截 120 字符、净化后空值回退 tid；
  纯文本 title 的社工残余为方案 A 设计固有（邮件本就携带服务合成 text），
  泄露面（repo 目录末段/branch/sha 前 7 位发往 email.json 收件人）较改动前
  的 detail 全文上屏为边际缩小，备案接受。
- 测试面：既有 F63 锚中 4 处「邮件体==会话行」断言随口径修订改为人话形
  （会话行断言逐字节不动；test_core_health.py 直调 send_email 的 F63 锚
  零改动即绿）。
