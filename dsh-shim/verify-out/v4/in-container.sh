source /work/out/.boot.sh
start_mock
run_dsh "连续两次调用工具 mcp__agora__check_mail（每次都是真实独立调用，共两次），两次都完成后输出 DONE 并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
