source /work/out/.boot.sh
start_mock
run_dsh "调用工具 mcp__agora__check_mail（空参数即可，必须真调用），然后报告工具返回并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
