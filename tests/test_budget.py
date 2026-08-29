import tempfile
import unittest
from pathlib import Path

from revenue_os import budget
from revenue_os.budget import BudgetBlocked, PRESALE_CAP_USD, guard
from revenue_os.llm_spend import LlmSpendLog
from revenue_os.llm_workers import budget_gate
from revenue_os.revenue import RevenueLedger


def _spend(d, usd):
    log = LlmSpendLog(Path(d) / "llm_spend.json")
    log.add({"activity": "acquisition", "cost_usd": usd, "api_calls": 1})
    log.save()


def _sale(d, eur=29.9):
    led = RevenueLedger(Path(d) / "revenue.json")
    led.add({"candidate_name": "x", "amount": eur, "currency": "EUR",
             "received_at": "2026-01-01T00:00:00+00:00", "actor": "t",
             "ref": f"paypal:{eur}"})
    led.save()


class PreSaleCapTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_zero_spend_is_allowed(self):
        guard(self.d, 0.0)                       # no raise
        self.assertTrue(budget.presale_active(self.d))

    def test_2_50_plus_0_40_allowed(self):
        _spend(self.d, 2.50)
        guard(self.d, 0.40)                      # 2.90 <= 3.20 -> ok

    def test_2_50_plus_1_00_blocked(self):
        _spend(self.d, 2.50)
        with self.assertRaises(BudgetBlocked):
            guard(self.d, 1.00)                  # 3.50 > 3.20

    def test_at_cap_blocks_every_paid_op(self):
        _spend(self.d, PRESALE_CAP_USD)
        for est in (0.001, 0.01, 0.5):
            with self.assertRaises(BudgetBlocked):
                guard(self.d, est)

    def test_no_single_action_may_exceed_the_cap(self):
        with self.assertRaises(BudgetBlocked):
            guard(self.d, PRESALE_CAP_USD + 0.01)

    def test_reserved_17_stays_locked_while_presale(self):
        s = budget.status(self.d)
        self.assertEqual(s["reserved_growth_capital_eur"], 17.0)
        self.assertEqual(s["growth_capital_available_eur"], 0.0)

    def test_first_sale_lifts_presale_mode(self):
        _spend(self.d, 5.0)                      # over the pre-sale cap
        with self.assertRaises(BudgetBlocked):   # blocked while pre-sale
            guard(self.d, 0.01)
        _sale(self.d)                            # real revenue booked
        guard(self.d, 0.01)                      # now a no-op
        self.assertFalse(budget.presale_active(self.d))
        s = budget.status(self.d)
        self.assertEqual(s["growth_capital_available_eur"], 17.0)
        self.assertEqual(s["revenue_eur"], 29.9)

    def test_budget_gate_enforces_presale_before_the_llm_budget_cap(self):
        _spend(self.d, 3.0)                      # under llm_budget default 5, over pre-sale
        with self.assertRaisesRegex(ValueError, "pre-sale hard limit"):
            budget_gate(self.d, 0.5, 1.0)

    def test_budget_gate_ceiling_capped_to_presale_remaining(self):
        _spend(self.d, 2.9)                      # 0.30 left under the pre-sale cap
        ceiling = budget_gate(self.d, 0.05, 1.0)
        self.assertLessEqual(ceiling, budget.presale_remaining_usd(self.d) + 1e-9)


if __name__ == "__main__":
    unittest.main()
