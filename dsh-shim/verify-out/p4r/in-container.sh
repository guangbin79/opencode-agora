source /work/out/.boot.sh
start_mock
LOG=/work/agora-home/mock-agorad.jsonl
( run_dsh "第一步：调用一次工具 mcp__agora__check_mail。第二步：MCP 服务随后会被外部重启，期间的调用可能报错——请等 3 秒后再调用一次 mcp__agora__check_mail（失败就再等 3 秒重试，直到成功）。两次都成功后输出 DONE 并结束。"
  echo $? > /work/out/dsh.rc ) &
DSH=$!
for _ in $(seq 1 120); do
  grep -q '"kind": "tool"' "$LOG" 2>/dev/null && break; sleep 1
done
sleep 2   # 首调稳定落账后动手：杀 MCP 子进程→客户端重连→工具重注册→shim 重同步
date +%s > /work/out/kill.ts
pkill -f 'agora_mcp.py' 2>/dev/null || true
wait $DSH
kill $MOCK_PID 2>/dev/null
exit "$(cat /work/out/dsh.rc)"
