import unittest

from revenue_os.messages import Task
from revenue_os.profit_master import ProfitMasterAgent, build_profit_read


class BuildProfitReadTests(unittest.TestCase):
    def test_normal_input(self):
        r = build_profit_read({
            "booked_revenue": 300, "actual_costs": 40,
            "llm_spend": 5, "marketing_spend": 100, "refunds": 30,
        })
        self.assertEqual(r["revenue"], 300.0)
        self.assertEqual(r["net_revenue_after_refunds"], 270.0)
        self.assertEqual(r["cost"], 145.0)
        self.assertEqual(r["gross_profit"], 230.0)      # 270 - 40
        self.assertEqual(r["net_profit"], 125.0)        # 270 - 145
        self.assertEqual(r["missing_inputs"], [])

    def test_missing_components_are_listed_not_invented(self):
        r = build_profit_read({"booked_revenue": 100})
        self.assertEqual(r["cost"], 0.0)
        self.assertIn("actual_costs", r["missing_inputs"])
        self.assertIn("marketing_spend", r["missing_inputs"])
        self.assertIsNone(r["ROI"])

    def test_zero_revenue_margin_is_none(self):
        r = build_profit_read({"actual_costs": 10})
        self.assertIsNone(r["margin"])

    def test_deterministic(self):
        self.assertEqual(build_profit_read({"booked_revenue": 1}),
                         build_profit_read({"booked_revenue": 1}))


class ProfitMasterAgentTests(unittest.TestCase):
    def _run(self, payload):
        return ProfitMasterAgent(name="profit_master").run(
            Task(objective="x", capability="manage_profit", payload=payload))

    def test_ok_with_values_dict(self):
        self.assertEqual(self._run({"values": {"booked_revenue": 100}}).status, "ok")

    def test_ok_with_flat_keys(self):
        self.assertEqual(self._run({"booked_revenue": 100, "llm_spend": 2}).status, "ok")

    def test_no_values_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_non_numeric_value_is_an_error(self):
        self.assertEqual(self._run({"booked_revenue": "lots"}).status, "error")

    def test_no_spending_authority_language(self):
        out = self._run({"booked_revenue": 10}).output
        self.assertIn("no ledger was modified", out["note"])


if __name__ == "__main__":
    unittest.main()
