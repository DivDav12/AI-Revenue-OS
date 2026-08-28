import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.agent_log import AgentLog


class AgentLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "agent_log.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_is_empty(self):
        self.assertEqual(len(AgentLog.load(self.path)), 0)
        self.assertIsNone(AgentLog.load(self.path).latest())

    def test_round_trip(self):
        log = AgentLog.load(self.path)
        log.add({"cycle": 0, "action": "discover", "reason": "cold start"})
        log.add({"cycle": 1, "action": "stop", "reason": "done"})
        log.save()
        again = AgentLog.load(self.path)
        self.assertEqual(len(again), 2)
        self.assertEqual(again.latest()["action"], "stop")

    def test_corrupt_and_non_list_raise(self):
        self.path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            AgentLog.load(self.path)
        self.path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            AgentLog.load(self.path)


if __name__ == "__main__":
    unittest.main()
