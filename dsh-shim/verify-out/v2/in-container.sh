source /work/out/.boot.sh
start_mock
run_dsh "用 write 工具把文本 hi 写入绝对路径 /work/foreign-repo/allowed.txt（必须用 write 工具真调用一次），然后说明结果并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
