"""The execution event log."""

import tempfile
import unittest
from pathlib import Path

from revenue_os.events import EVENT_TYPES, EventLog, load_events


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_emit_assigns_monotonic_seq(self):
        log = EventLog(self.d / "e.json")
        a = log.emit("TASK_CREATED", task_id="t1", opportunity_id="o1")
        b = log.emit("TASK_STARTED", task_id="t1", opportunity_id="o1")
        self.assertEqual(a["seq"], 1)
        self.assertEqual(b["seq"], 2)
        self.assertEqual(a["type"], "TASK_CREATED")
        self.assertIn("ts", a)

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            EventLog(self.d / "e.json").emit("NOT_A_TYPE")

    def test_data_kwargs_are_captured(self):
        log = EventLog(self.d / "e.json")
        e = log.emit("TASK_FAILED", task_id="t1", error="boom", attempt=2)
        self.assertEqual(e["data"], {"error": "boom", "attempt": 2})

    def test_since_returns_only_newer(self):
        log = EventLog(self.d / "e.json")
        for i in range(5):
            log.emit("TASK_READY", task_id=f"t{i}")
        self.assertEqual([e["seq"] for e in log.since(2)], [3, 4, 5])
        self.assertEqual(log.since(5), [])

    def test_persistence_and_seq_continuity_across_restart(self):
        log = load_events(self.d)
        log.emit("TASK_CREATED", task_id="t1")
        log.emit("TASK_STARTED", task_id="t1")
        log.save()

        log2 = load_events(self.d)
        self.assertEqual(len(log2), 2)
        self.assertEqual(log2.last_seq(), 2)
        c = log2.emit("TASK_SUCCEEDED", task_id="t1")
        self.assertEqual(c["seq"], 3)          # continues, never restarts
        log2.save()

        log3 = load_events(self.d)
        self.assertEqual([e["seq"] for e in log3.all()], [1, 2, 3])

    def test_corrupt_file_does_not_raise(self):
        (self.d / "execution_events.json").write_text("{not json")
        log = load_events(self.d)
        self.assertEqual(len(log), 0)
        self.assertEqual(log.emit("TASK_READY")["seq"], 1)

    def test_by_type_and_by_task(self):
        log = EventLog(self.d / "e.json")
        log.emit("TASK_CREATED", task_id="a")
        log.emit("TASK_CREATED", task_id="b")
        log.emit("TASK_SUCCEEDED", task_id="a")
        self.assertEqual(len(log.by_type("TASK_CREATED")), 2)
        self.assertEqual({e["type"] for e in log.by_task("a")},
                         {"TASK_CREATED", "TASK_SUCCEEDED"})

    def test_all_spec_event_types_present(self):
        for name in ("TASK_CREATED", "TASK_READY", "TASK_STARTED",
                     "TASK_SUCCEEDED", "TASK_FAILED", "TASK_RETRY_SCHEDULED",
                     "TASK_BLOCKED", "TASK_CANCELLED", "OPPORTUNITY_TRANSITIONED"):
            self.assertIn(name, EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
