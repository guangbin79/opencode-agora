source /work/out/.boot.sh
start_mock
run_dsh "用 bash 工具执行命令：echo p6b > /work/task-env/p6b-ww.txt（必须用 bash 工具真执行一次），然后报告命令结果并结束。"
RC=$?
kill $MOCK_PID 2>/dev/null
exit $RC
