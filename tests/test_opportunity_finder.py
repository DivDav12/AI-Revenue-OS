import unittest

from revenue_os.messages import Task
from revenue_os.opportunity_finder import OpportunityFinderAgent


def _task(scored, **kw):
    return Task(objective="select", capability="select",
               payload={"scored": scored, **kw})


class OpportunityFinderTests(unittest.TestCase):
    def setUp(self):
        self.agent = OpportunityFinderAgent(name="opportunity_finder")

    def test_ranks_gates_and_shortlists(self):
        scored = [
            {"name": "low", "total": 1.0},
            {"name": "high", "total": 5.0},
            {"name": "mid", "total": 3.0},
        ]
        out = self.agent.run(_task(scored, min_score=2.0, shortlist_n=1)).output
        self.assertEqual(out["ranking"], ["high", "mid", "low"])
        self.assertEqual(out["kept"], ["high", "mid"])
        self.assertEqual(out["dropped"], ["low"])
        self.assertEqual(out["shortlist"], ["high"])

    def test_no_gate_keeps_all(self):
        out = self.agent.run(_task(
            [{"name": "a", "total": 0.0}, {"name": "b", "total": 1.0}]
        )).output
        self.assertEqual(set(out["kept"]), {"a", "b"})
        self.assertEqual(out["dropped"], [])

    def test_bad_payload_is_an_error_result(self):
        r = self.agent.run(Task(objective="x", capability="select",
                                payload={"scored": "nope"}))
        self.assertEqual(r.status, "error")


if __name__ == "__main__":
    unittest.main()
