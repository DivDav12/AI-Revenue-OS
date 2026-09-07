"""Hard $3.00 LLM/API budget ceiling. Fail-closed on exhaustion AND on an
unverifiable cost estimate - no auto-reload, no override path exists."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import budget_guard


class BudgetGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_cap_is_exactly_three_dollars(self):
        self.assertEqual(budget_guard.CAP_USD, 3.00)

    def test_guard_allows_a_call_within_budget(self):
        budget_guard.guard(self.data_dir, estimated_cost_usd=0.10)  # must not raise

    def test_guard_blocks_a_call_that_would_exceed_the_cap(self):
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd=3.01)

    def test_spend_accumulates_and_eventually_exhausts(self):
        for _ in range(3):
            budget_guard.guard(self.data_dir, estimated_cost_usd=1.00)
            budget_guard.record_spend(self.data_dir, activity="content", cost_usd=1.00, ts="t")
        self.assertAlmostEqual(budget_guard.spent(self.data_dir), 3.00)
        self.assertAlmostEqual(budget_guard.remaining(self.data_dir), 0.00)
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd=0.001)

    def test_guard_fails_closed_on_none_estimate(self):
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd=None)

    def test_guard_fails_closed_on_non_numeric_estimate(self):
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd="unknown")

    def test_guard_fails_closed_on_nan_and_negative(self):
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd=float("nan"))
        with self.assertRaises(budget_guard.BudgetExhausted):
            budget_guard.guard(self.data_dir, estimated_cost_usd=-0.01)

    def test_no_runtime_override_of_the_cap_exists(self):
        # the only way to change the cap is editing the module constant -
        # there is no function that accepts a new cap value.
        public_names = [n for n in dir(budget_guard) if not n.startswith("_")]
        for name in public_names:
            self.assertNotIn("override", name.lower())
            self.assertNotIn("reload", name.lower())
            self.assertNotIn("raise_cap", name.lower())

    def test_ledger_persists_across_loads(self):
        budget_guard.record_spend(self.data_dir, activity="a", cost_usd=0.50, ts="t1")
        raw = json.loads((self.data_dir / "budget_guard_spend.json").read_text())
        self.assertEqual(len(raw), 1)
        self.assertEqual(budget_guard.spent(self.data_dir), 0.50)
        budget_guard.record_spend(self.data_dir, activity="b", cost_usd=0.25, ts="t2")
        self.assertEqual(budget_guard.spent(self.data_dir), 0.75)

    def test_corrupt_ledger_file_is_treated_as_empty_not_crashed(self):
        (self.data_dir / "budget_guard_spend.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(budget_guard.spent(self.data_dir), 0.0)


if __name__ == "__main__":
    unittest.main()
