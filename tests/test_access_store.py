"""T1 访问层契约：store 账本四原语＋flock（真文件，无 mock）。"""
import json
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

from agorad.access.store import Store


class TestAccessStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st = Store(self.tmp)
        # brief 原文测试行 open("a").write(...) 不 close，滤掉 ResourceWarning 噪音
        warnings.filterwarnings("ignore", category=ResourceWarning)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

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

    def test_write_ephemeral_overwrites(self):  # Req: F6
        self.st.write_ephemeral("service.json", {"pid": 1})
        self.st.write_ephemeral("service.json", {"pid": 2})
        self.assertEqual(json.loads(self.tmp.joinpath("service.json").read_text()), {"pid": 2})

    def test_read_ephemeral_roundtrip(self):  # Req: F6
        self.assertIsNone(self.st.read_ephemeral("tokens"))
        self.st.write_ephemeral("tokens", {"tk": ["s1", "worker"]})
        self.assertEqual(self.st.read_ephemeral("tokens"), {"tk": ["s1", "worker"]})


if __name__ == "__main__":
    unittest.main()
