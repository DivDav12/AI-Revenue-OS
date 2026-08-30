import unittest

from revenue_os.campaign_optimizer import (
    CampaignOptimizerAgent,
    build_optimization,
)
from revenue_os.messages import Task

_METRICS = [
    {"name": "problem-led", "impressions": 10000, "clicks": 200, "spend": 100, "conversions": 10},
    {"name": "outcome-led", "impressions": 9000, "clicks": 150, "spend": 120, "conversions": 2},
    {"name": "offer-led", "impressions": 8000, "clicks": 90, "spend": 90, "conversions": 0},
]


class BuildOptimizationTests(unittest.TestCase):
    def test_normal_input_ranks_by_cpa(self):
        o = build_optimization({}, _METRICS)
        self.assertIn("problem-led", o["winning_variants"])
        self.assertEqual(o["performance_summary"]["total_conversions"], 12)
        self.assertEqual(o["confidence"], "medium")

    def test_recommendations_only_never_applied(self):
        o = build_optimization({}, _METRICS)
        self.assertFalse(o["auto_applied"])
        self.assertTrue(o["human_gate_required"])
        self.assertTrue(all(a.startswith("RECOMMEND") for a in o["optimization_actions"]))

    def test_empty_metrics(self):
        o = build_optimization({}, [])
        self.assertEqual(o["confidence"], "low")
        self.assertEqual(o["winning_variants"], [])
        self.assertIn("collect more data", " ".join(o["optimization_actions"]))

    def test_malformed_rows_are_skipped(self):
        o = build_optimization({}, [_METRICS[0], "junk", {"name": "x"}])
        names = [r["name"] for r in o["performance_summary"]["variants"]]
        self.assertIn("problem-led", names)

    def test_deterministic(self):
        self.assertEqual(build_optimization({}, _METRICS),
                         build_optimization({}, _METRICS))


class CampaignOptimizerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return CampaignOptimizerAgent(name="campaign_optimizer").run(
            Task(objective="x", capability="optimize_campaigns", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"campaign_metrics": _METRICS}).status, "ok")

    def test_missing_metrics_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_malformed_metrics_is_an_error(self):
        self.assertEqual(self._run({"campaign_metrics": "nope"}).status, "error")

    def test_malformed_plan_is_an_error(self):
        self.assertEqual(
            self._run({"campaign_metrics": _METRICS, "campaign_plan": 5}).status, "error")


if __name__ == "__main__":
    unittest.main()
