import unittest

from revenue_os.budget_allocator import BudgetAllocatorAgent, build_allocation
from revenue_os.messages import Task

_OPTIONS = [
    {"name": "search", "min": 20, "max": 200, "expected_roi": 2.0},
    {"name": "community", "min": 0, "max": 100, "expected_roi": 1.2},
]


class BuildAllocationTests(unittest.TestCase):
    def test_normal_input_respects_budget_and_bounds(self):
        a = build_allocation(150, _OPTIONS)
        self.assertLessEqual(sum(a["recommended_allocation"].values()), 150.0 + 0.01)
        self.assertGreaterEqual(a["recommended_allocation"]["search"], 20.0)
        self.assertLessEqual(a["recommended_allocation"]["community"], 100.0)

    def test_never_authorizes_or_unlocks(self):
        a = build_allocation(150, _OPTIONS)
        self.assertFalse(a["authorizes_spend"])
        self.assertFalse(a["unlocks_growth_capital"])
        self.assertTrue(a["respects_presale_cap"])
        self.assertTrue(a["human_gate_required"])

    def test_zero_budget(self):
        a = build_allocation(0, _OPTIONS)
        self.assertEqual(sum(a["recommended_allocation"].values()), 0.0)

    def test_no_options(self):
        a = build_allocation(100, [])
        self.assertEqual(a["recommended_allocation"], {})

    def test_three_scenarios(self):
        a = build_allocation(100, _OPTIONS)
        self.assertEqual([s["scenario"] for s in a["scenarios"]],
                         ["conservative", "base", "aggressive"])

    def test_deterministic(self):
        self.assertEqual(build_allocation(150, _OPTIONS), build_allocation(150, _OPTIONS))


class BudgetAllocatorAgentTests(unittest.TestCase):
    def _run(self, payload):
        return BudgetAllocatorAgent(name="budget_allocator").run(
            Task(objective="x", capability="allocate_budget", payload=payload))

    def test_ok(self):
        self.assertEqual(
            self._run({"available_budget": 100, "campaign_options": _OPTIONS}).status, "ok")

    def test_missing_budget_is_an_error(self):
        self.assertEqual(self._run({"campaign_options": _OPTIONS}).status, "error")

    def test_bool_budget_is_an_error(self):
        self.assertEqual(
            self._run({"available_budget": True, "campaign_options": _OPTIONS}).status,
            "error")

    def test_missing_options_is_an_error(self):
        self.assertEqual(self._run({"available_budget": 100}).status, "error")

    def test_allocation_never_exceeds_budget(self):
        out = self._run({"available_budget": 50, "campaign_options": _OPTIONS}).output
        self.assertLessEqual(sum(out["recommended_allocation"].values()), 50.01)


if __name__ == "__main__":
    unittest.main()
