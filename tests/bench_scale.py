#!/usr/bin/env python3
"""F92 账本读路径基准：固定规模数据＋热身后重复调用，输出 JSON 中位数。

非正确性断言、不设绝对时延阈值：仅比较同一机器/同一负载下的实现前后读
路径成本。setup（建任务/消息/事件）不计时；每项先热身一次再测 --reads 次。
"""
import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # python3 tests/... 直跑导入面

from agorad.access.store import Store
from agorad.core import report
from agorad.core.msg import Router
from agorad.core.state import State


class _Oc:
    def inject(self, sid, text):
        return True

    def inject_silent(self, sid, text):
        return True

    def inject_notify(self, sid, text):
        return True


def setup_store(root: Path, tasks: int, messages: int, events: int) -> tuple:
    store = Store(root)
    now = 0.0
    rows = {}
    for i in range(tasks):
        tid = f"ag-{i:06x}"
        rows[tid] = {
            "title": f"task {i}", "goal": "bench", "project_dir": "/repo",
            "target_branch": None, "req_id": None, "model": None,
            "deliverable": None, "env_dir": None, "after": None,
            "verify": {"cmd": "true", "state": "registered",
                       "registered_by": "bench", "registered_at": now,
                       "timeout": None},
            "verify_pending": False, "quick": False, "state": "running",
            "dispatcher_sid": "bench-main", "worker_sid": f"w{i}",
            "root_sid": "bench-main", "chain_depth": 1, "fail_streak": 0,
            "last_seen": None, "created": now, "updated": now,
        }
    store.append_state({"version": 1, "tasks": rows, "mail_read": {}})
    for i in range(messages):
        store.write_message(
            {"ts": 1.0, "task": f"ag-{i % tasks:06x}", "kind": "note",
             "from": f"w{i % tasks}", "to": "bench-reader"}, f"message {i}")
    for i in range(events):
        store.append_event({"ts": 1.0, "actor": "bench", "event": "bench",
                            "task": f"ag-{i % tasks:06x}", "note": str(i)})
    return store, rows


def median_seconds(fn, reads: int) -> float:
    fn()  # 热身：排除首载/页缓存噪声；首载语义由正常测试覆盖
    samples = []
    for _ in range(reads):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--messages", type=int, default=1000)
    parser.add_argument("--events", type=int, default=5000)
    parser.add_argument("--reads", type=int, default=20)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="agora-bench-"))
    try:
        store, rows = setup_store(root, args.tasks, args.messages, args.events)
        state = State(store)
        router = Router(store, state, _Oc())
        snap = state.snapshot()
        # check_mail 热身会把消息标已投，后续轮仍走同一过滤路径（不重复返回）
        result = {
            "scale": {"tasks": args.tasks, "messages": args.messages,
                      "events": args.events, "reads": args.reads},
            "seconds_median": {
                "read_messages": median_seconds(store.read_messages, args.reads),
                "read_events": median_seconds(store.read_events, args.reads),
                "board_full_view": median_seconds(
                    lambda: report.full_view(snap, store), args.reads),
                "check_mail": median_seconds(
                    lambda: router.check_mail("bench-reader"), args.reads),
            },
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
