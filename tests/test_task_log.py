import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.messages import Result, Task
from revenue_os.task_log import TaskLog, summarize


class TaskLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "task_log.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_record_captures_lineage_and_summary(self):
        log = TaskLog(self.path)
        root = Task(objective="discover", capability="discover")
        child = Task(objective="evaluate x", capability="evaluate",
                     parent_id=root.id, depth=1)
        log.record(root, Result(task_id=root.id, agent="market_scanner",
                                status="ok", output={"opportunities": [1, 2, 3]}))
        log.record(child, Result(task_id=child.id, agent="evaluator", status="ok",
                                 output={"opportunity_name": "x", "total": 3.0}))
        log.save()

        entries = TaskLog.load(self.path).entries()
        self.assertEqual([e["agent"] for e in entries],
                         ["market_scanner", "evaluator"])
        self.assertIsNone(entries[0]["parent_id"])
        self.assertEqual(entries[1]["parent_id"], root.id)
        self.assertEqual(entries[1]["depth"], 1)
        self.assertEqual(entries[0]["summary"], {"count": 3})
        self.assertEqual(entries[1]["summary"]["total"], 3.0)

    def test_error_result_has_empty_summary_and_keeps_error(self):
        log = TaskLog(self.path)
        t = Task(objective="x", capability="evaluate")
        log.record(t, Result(task_id=t.id, agent="evaluator", status="error",
                              error="bad payload"))
        self.assertEqual(log.entries()[0]["summary"], {})
        self.assertEqual(log.entries()[0]["error"], "bad payload")

    def test_save_caps_at_max_entries(self):
        from revenue_os import task_log as tl_mod
        log = TaskLog(self.path)
        for i in range(tl_mod.MAX_ENTRIES + 50):
            log.add({"ts": str(i), "agent": "x"})
        log.save()
        self.assertEqual(len(json.loads(self.path.read_text())), tl_mod.MAX_ENTRIES)

    def test_summarize_counts_lists(self):
        self.assertEqual(
            summarize({"kept": ["a", "b"], "shortlist": ["a"], "total": 3.0}),
            {"kept": 2, "shortlist": 1, "total": 3.0},
        )

    def test_missing_file_loads_empty(self):
        self.assertEqual(TaskLog.load(self.path).entries(), [])


if __name__ == "__main__":
    unittest.main()
