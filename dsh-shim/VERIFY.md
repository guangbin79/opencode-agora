# F182 容器级验证结论（Req: F182）

由 dsh-shim/verify.sh 生成（容器内真实 dsh headless 会话＋GLM 动力＋真 agora_mcp.py＋mock-agorad 三端点）。
复跑：cd dsh-shim && ./verify.sh（证据归档 dsh-shim/verify-out/<phase>/）。
生成时间：2026-11-28 12:09:39（宿主）

V1: PASS — enforce on 下越界写被拒（events.jsonl tool_result 含 wrongDoorHint 全文，与 opencode 版同源），目标未落盘，/gate_deny 审计落账 1 条
V2: PASS — enforce off 下同一越界写全放行（allowed.txt 实落盘），零 gate_deny
V3: PASS — mcp__agora__check_mail 调用达 agorad（mock 面实收 X-Agora-Token＝签发 worker 牌、X-Agora-Sid＝会话 sid，与 /cred_for_sid 拉取同 sid）
V4: PASS — 同会话连续 2 次调用均走 shadow 动态取定义（2 次成功、mock 面实收 2 次），无 stale 捕获
P4R: PASS — MCP 子进程被杀触发重同步（全局定义换代）后 shadow 再调成功（kill@1795838953 后仍有 tool 落账@1795838957.9463663，共 2 次成功）
P6B: PASS — bwrap 在位：read-only 档 bash 写被拒（p6b-ro.txt 未落盘）＋workspace-write 档工作区内写放行（p6b-ww.txt 落盘）
