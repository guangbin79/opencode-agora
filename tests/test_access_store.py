"""T1 访问层契约：store 账本四原语＋flock（真文件，无 mock）。"""
import json
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

from agorad.access.store import COMPACT_LINES, TAIL_BYTES, Store


class TestAccessStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st = Store(self.tmp)
        # brief 原文测试行 open("a").write(...) 不 close，滤掉 ResourceWarning 噪音
        warnings.filterwarnings("ignore", category=ResourceWarning)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _big_ledger(self, text: str) -> Path:
        path = self.tmp / "state" / "tasks.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_tail_read_hits_last_line_in_big_file(self):  # Req: F20
        """>1MB 账本启动只扫尾窗：巨行把首快照推出窗外，末行胜出照常命中。"""
        self._big_ledger('{"v": 1}\n' + "x" * (2 * TAIL_BYTES) + "\n" + '{"v": 2}\n')
        self.assertEqual(Store(self.tmp).load_state(), {"v": 2})

    def test_tail_read_falls_back_when_window_has_no_complete_line(self):  # Req: F20
        """尾窗被无换行巨行独占（无完整行）→ 回退全文件扫，不误判空账本。"""
        self._big_ledger('{"v": 1}\n' + "y" * (2 * TAIL_BYTES))
        self.assertEqual(Store(self.tmp).load_state(), {"v": 1})

    def test_tail_read_torn_last_line_keeps_prev_complete(self):  # Req: F20
        """尾读下断尾末行丢弃回退上一完整行——与全扫断尾容错语义一致。"""
        self._big_ledger("z" * (2 * TAIL_BYTES) + "\n" + '{"v": 9}\n{"v": 8}\n{"torn')
        self.assertEqual(Store(self.tmp).load_state(), {"v": 8})

    def test_compact_triggers_at_threshold_keeps_last_complete_line(self):  # Req: F20
        """行数超阈值即原子重写仅留刚追加的末完整行；阈值内保持纯追加。"""
        for i in range(COMPACT_LINES):
            self.st.append_state({"v": i})
        path = self.tmp / "state" / "tasks.jsonl"
        self.assertEqual(path.read_text(encoding="utf-8").count("\n"), COMPACT_LINES)
        self.st.append_state({"v": COMPACT_LINES})  # 第 501 行触发
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l) for l in lines], [{"v": COMPACT_LINES}])

    def test_compact_is_atomic_no_tmp_and_state_roundtrips(self):  # Req: F20
        for i in range(COMPACT_LINES + 1):
            self.st.append_state({"v": i})
        self.assertFalse(self.tmp.joinpath("state/tasks.jsonl.tmp").exists())
        self.assertEqual(Store(self.tmp).load_state(), {"v": COMPACT_LINES})

    def test_compact_failure_keeps_durable_append_and_retries_later(self):  # Req: F20
        """瘦身属维护动作：compact 失败不报败已持久化的追加，阻碍解除后下轮追加自动重试。"""
        for i in range(COMPACT_LINES):
            self.st.append_state({"v": i})
        tmp = self.tmp / "state" / "tasks.jsonl.tmp"
        tmp.mkdir()  # 占住 tmp 路径：compact 写 tmp 必败（IsADirectoryError）
        self.st.append_state({"v": -1})  # 第 501 行：追加成功、瘦身静默失败
        self.assertEqual(tmp.parent.joinpath("tasks.jsonl").read_text(encoding="utf-8").count("\n"), 501)
        tmp.rmdir()  # 阻碍解除，计数器仍超阈
        self.st.append_state({"v": -2})  # 本轮追加重试瘦身成功
        lines = self.tmp.joinpath("state/tasks.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l) for l in lines], [{"v": -2}])

    def test_torn_last_line_falls_back(self):  # Req: F6
        st = Store(self.tmp)
        st.append_state({"v": 1}); self.tmp.joinpath("state/tasks.jsonl").open("a").write('{"torn')
        self.assertEqual(Store(self.tmp).load_state(), {"v": 1})

    def test_msg_id_global_monotonic(self):  # Req: F4
        a = self.st.write_message({"task": "t1"}, "x"); b = self.st.write_message({"task": "t2"}, "y")
        self.assertEqual((b - a), 1)

    def test_state_append_only(self):  # Req: F6
        self.st.append_state({"v": 1})
        before = self.tmp.joinpath("state/tasks.jsonl").read_bytes()
        self.st.append_state({"v": 2})
        after = self.tmp.joinpath("state/tasks.jsonl").read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual([json.loads(l) for l in after.decode().splitlines()], [{"v": 1}, {"v": 2}])

    def test_events_append_only(self):  # Req: F8
        self.st.append_event({"e": 1})
        before = self.tmp.joinpath("events.jsonl").read_bytes()
        self.st.append_event({"e": 2})
        after = self.tmp.joinpath("events.jsonl").read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual([json.loads(l) for l in after.decode().splitlines()], [{"e": 1}, {"e": 2}])

    def test_flock_second_instance_false(self):  # Req: F1
        self.assertTrue(self.st.flock_singleton())
        self.assertFalse(Store(self.tmp).flock_singleton())

    def test_msg_id_recovers_from_dir_scan(self):  # Req: F4
        a = self.st.write_message({"task": "t1"}, "x")
        fresh = Store(self.tmp)
        self.assertEqual(fresh.scan_max_msg_id(), a)
        self.assertEqual(fresh.write_message({"task": "t2"}, "y"), a + 1)

    def test_msg_filename_sort_equals_time_order(self):  # Req: F4
        ids = [self.st.write_message({"task": "t"}, "") for _ in range(12)]
        names = sorted(p.name for p in self.tmp.joinpath("state/messages").glob("*.md"))
        self.assertEqual(names, [f"{i:08d}.md" for i in ids])

    def test_read_messages_skips_torn_files(self):  # Req: F4
        """F-5：门中崩溃撕裂件（坏头/空文件）跳过，不拖垮全体 check_mail。"""
        a = self.st.write_message({"task": "t1"}, "x")
        msgs_dir = self.tmp / "state" / "messages"
        (msgs_dir / "00000003.md").write_text("id: 3\ntorn", encoding="utf-8")  # 坏头
        (msgs_dir / "00000004.md").write_text("", encoding="utf-8")  # 空文件
        b = self.st.write_message({"task": "t2"}, "y")
        self.assertEqual([m["id"] for m in self.st.read_messages()], [a, b])

    def test_write_ephemeral_overwrites(self):  # Req: F6
        self.st.write_ephemeral("service.json", {"pid": 1})
        self.st.write_ephemeral("service.json", {"pid": 2})
        self.assertEqual(json.loads(self.tmp.joinpath("service.json").read_text()), {"pid": 2})

    def test_read_ephemeral_roundtrip(self):  # Req: F6
        self.assertIsNone(self.st.read_ephemeral("tokens"))
        self.st.write_ephemeral("tokens", {"tk": ["s1", "worker"]})
        self.assertEqual(self.st.read_ephemeral("tokens"), {"tk": ["s1", "worker"]})

    def test_read_events_all_and_by_task(self):  # Req: F8
        self.st.append_event({"e": 1, "task": "t1"})
        self.st.append_event({"e": 2, "task": "t2"})
        self.tmp.joinpath("events.jsonl").open("a").write('{"torn')
        self.assertEqual([e["e"] for e in self.st.read_events()], [1, 2])
        self.assertEqual([e["e"] for e in self.st.read_events("t1")], [1])


if __name__ == "__main__":
    unittest.main()
