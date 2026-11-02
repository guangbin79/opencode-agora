"""T7 access/pages 契约（R6）：任务页＝wiki/tasks/<id>.md 渲染视图（真文件 tmp 夹具）。"""
import tempfile
import unittest
from pathlib import Path

from agorad.access.pages import write_task_page

ROW = {"goal": "修好构建", "project_dir": "/repo", "env_dir": None,
       "verify": {"cmd": "", "state": "pending", "registered_by": None,
                  "registered_at": None},
       "state": "todo", "chain_depth": 1}


class TestAccessPages(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_path_and_content(self):  # Req: F2
        p = write_task_page(self.tmp, "gt-x1", ROW)
        self.assertEqual(p, self.tmp / "wiki" / "tasks" / "gt-x1.md")
        text = p.read_text(encoding="utf-8")
        self.assertIn("state: todo", text)
        self.assertIn("修好构建", text)
        self.assertIn("verify: pending", text)

    def test_rerender_reflects_current_row(self):  # Req: F2
        row = dict(ROW, state="dispatched", env_dir="/envs/gt-x1")
        write_task_page(self.tmp, "gt-x1", ROW)
        write_task_page(self.tmp, "gt-x1", row)  # 任务页只存当前态
        text = (self.tmp / "wiki" / "tasks" / "gt-x1.md").read_text(encoding="utf-8")
        self.assertIn("state: dispatched", text)
        self.assertIn("/envs/gt-x1", text)

    def test_req_id_on_page_for_worker_pickup(self):  # Req: F34
        p = write_task_page(self.tmp, "ag-x1", dict(ROW, req_id="F34"))
        self.assertIn("req_id: F34", p.read_text(encoding="utf-8"))  # worker 从任务页取号

    def test_f44_relations_section_on_nested_page(self):  # Req: F44
        row = dict(ROW, dispatcher_sid="ses-par", worker_sid="ses-w",
                   after="ag-a", created=1000.0)
        tasks = {
            "ag-m": row,
            "ag-p": {"worker_sid": "ses-par", "state": "dispatched",
                     "title": "父任务标题"},  # 父：worker_sid==本任务 dispatcher_sid
            "ag-c": {"dispatcher_sid": "ses-w", "state": "running"},  # 子
            "ag-a": {"state": "merged"},  # after 前驱
            "ag-s": {"after": "ag-m", "state": "todo"},  # 后继
        }
        events = [{"ts": 1000.0 + i, "task": "ag-m",
                   "event": "task_created" if i == 0 else "dispatched"}
                  for i in range(9)]
        events[1]["note"] = "x" * 80
        p = write_task_page(self.tmp, "ag-m", row, tasks=tasks, events=events)
        text = p.read_text(encoding="utf-8")
        self.assertIn("## 关联", text)
        for rid in ("ag-p", "ag-a", "ag-c", "ag-s"):
            self.assertIn(rid, text)  # 全部关联 id 内联
        self.assertIn("running", text)  # 子任务 state
        ev = [ln for ln in text.splitlines() if ln.startswith("- 事件:")]
        self.assertEqual(len(ev), 8)  # 最近 8 条
        self.assertTrue(any("dispatched" in ln for ln in ev))
        self.assertFalse(any("x" * 61 in ln for ln in ev))  # note 截 ≤60 字符

    def test_f44_orphan_flat_page_has_no_relations_section(self):  # Req: F44
        p = write_task_page(self.tmp, "ag-o", dict(ROW, id="ag-o"),
                            tasks={"ag-o": dict(ROW)}, events=[])
        self.assertNotIn("关联", p.read_text(encoding="utf-8"))  # 无关联不输出空标题


if __name__ == "__main__":
    unittest.main()
