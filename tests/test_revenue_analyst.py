import unittest

from revenue_os.messages import Task
from revenue_os.revenue_analyst import RevenueAnalystAgent, build_revenue_analysis


def _roi(**cands):
    total_r = sum(c["revenue"] for c in cands.values())
    total_s = sum(c["spent"] for c in cands.values())
    return {
        "grand_revenue": total_r, "grand_spent": total_s,
        "grand_net": round(total_r - total_s, 2),
        "candidates": {
            n: {**c, "net": round(c["revenue"] - c["spent"], 2),
                "roi_ratio": (round((c["revenue"] - c["spent"]) / c["spent"], 2)
                              if c["spent"] else None)}
            for n, c in cands.items()
        },
    }


class BuildRevenueAnalysisTests(unittest.TestCase):
    def test_portfolio_math_best_and_worst(self):
        roi = _roi(
            alpha={"status": "earning", "revenue": 300.0, "spent": 50.0},
            beta={"status": "launched", "revenue": 0.0, "spent": 40.0},
        )
        cands = [{"name": "alpha", "status": "earning"},
                 {"name": "beta", "status": "launched"}]
        a = build_revenue_analysis(roi, {"ready": False}, cands)
        self.assertEqual(a["portfolio"]["revenue"], 300.0)
        self.assertEqual(a["portfolio"]["spent"], 90.0)
        self.assertEqual(a["portfolio"]["net"], 210.0)
        self.assertEqual(a["portfolio"]["earning"], 1)
        self.assertEqual(a["portfolio"]["launched"], 1)
        self.assertEqual(a["best"], {"name": "alpha", "net": 250.0})
        self.assertEqual(a["worst"], {"name": "beta", "net": -40.0})
        self.assertEqual(a["spend_efficiency"], round(210.0 / 90.0, 2))
        self.assertIn("Portfolio", a["readout"])
        self.assertEqual(a["outcome_signal"], "not enough outcomes yet")

    def test_spend_only_readout(self):
        roi = _roi(x={"status": "validated", "revenue": 0.0, "spent": 12.0})
        a = build_revenue_analysis(roi, {}, [{"name": "x", "status": "validated"}])
        self.assertIsNone(a["best"])
        self.assertIn("No revenue recorded yet", a["readout"])

    def test_empty_ledger(self):
        a = build_revenue_analysis({"grand_revenue": 0.0, "grand_spent": 0.0,
                                    "grand_net": 0.0, "candidates": {}}, {}, [])
        self.assertEqual(a["per_candidate"], [])
        self.assertIn("No revenue or spend", a["readout"])

    def test_outcome_signal_from_ready_retro(self):
        a = build_revenue_analysis(
            {"grand_revenue": 0.0, "grand_spent": 0.0, "grand_net": 0.0,
             "candidates": {}},
            {"ready": True, "most_predictive": ["demand", "competition", "cost"]},
            [],
        )
        self.assertEqual(a["outcome_signal"], "demand, competition")


class RevenueAnalystAgentTests(unittest.TestCase):
    def test_agent_wraps_analysis(self):
        agent = RevenueAnalystAgent(name="revenue_analyst")
        roi = _roi(a={"status": "earning", "revenue": 100.0, "spent": 10.0})
        r = agent.run(Task(objective="r", capability="analyze_revenue",
                           payload={"roi": roi, "outcomes": {},
                                    "candidates": [{"name": "a", "status": "earning"}]}))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["portfolio"]["net"], 90.0)

    def test_agent_bad_payload(self):
        agent = RevenueAnalystAgent(name="revenue_analyst")
        r = agent.run(Task(objective="r", capability="analyze_revenue", payload={}))
        self.assertEqual(r.status, "error")


if __name__ == "__main__":
    unittest.main()
