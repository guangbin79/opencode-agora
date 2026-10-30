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


if __name__ == "__main__":
    unittest.main()
