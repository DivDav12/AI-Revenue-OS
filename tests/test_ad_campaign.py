"""Ad Campaign Readiness architecture (NOT ACTIVE - no ad platform, no
spend, ever, in this module).

Covers: no plan supplied -> NOT_READY; insufficient real confirmed
conversions -> NOT_READY with an honest reason; a real, settled
confirmed-commission floor being met -> READY_FOR_HUMAN_APPROVAL;
`always_requires_human_approval` is always True regardless; the floor
is never silently lowered; unsettled (PENDING) commissions never count
as evidence; CLI wiring.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import affiliate_revenue
from revenue_os.ecosystem.ad_campaign import AdCampaignPlan, ad_campaign_readiness


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _confirm_n_commissions(d, n: int, *, offer_id: str = "aff-systeme", amount: float = 10.0):
    for i in range(n):
        rec = affiliate_revenue.record_pending_commission(
            d, link_id=f"l{i}", opportunity_id="op1", offer_id=offer_id,
            amount=amount, now_iso="t")
        affiliate_revenue.confirm_commission(d, rec.commission_id, ref=f"REF{i}", now_iso="t")


class NoPlanTests(unittest.TestCase):
    def test_no_plan_is_not_ready(self):
        d = _tmp()
        out = ad_campaign_readiness(d, offer_id="aff-systeme")
        self.assertEqual(out["status"], "NOT_READY")
        self.assertIsNone(out["plan"])
        self.assertTrue(any("no AdCampaignPlan" in r for r in out["reasons"]))

    def test_never_spends_or_connects_anywhere(self):
        # structural guarantee - no network/http import anywhere in the module.
        import ast
        import inspect

        from revenue_os.ecosystem import ad_campaign

        tree = ast.parse(inspect.getsource(ad_campaign))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in ("urllib", "http", "requests", "socket"):
            for mod in imported:
                self.assertFalse(mod == forbidden or mod.startswith(forbidden + "."))


class EvidenceGateTests(unittest.TestCase):
    def _plan(self, **kw) -> AdCampaignPlan:
        base = dict(offer_id="aff-systeme", channel="google_search", budget_eur=50.0,
                   max_cpc_eur=0.5, max_cpa_eur=15.0, stop_loss_eur=20.0)
        base.update(kw)
        return AdCampaignPlan(**base)

    def test_zero_conversions_not_ready(self):
        d = _tmp()
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertEqual(out["status"], "NOT_READY")
        self.assertEqual(out["confirmed_conversions"], 0)
        self.assertFalse(out["evidence_ready"])

    def test_below_floor_not_ready(self):
        d = _tmp()
        _confirm_n_commissions(d, 2)
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertEqual(out["confirmed_conversions"], 2)
        self.assertEqual(out["status"], "NOT_READY")

    def test_meeting_the_default_floor_is_ready(self):
        d = _tmp()
        _confirm_n_commissions(d, 3, amount=10.0)
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertEqual(out["status"], "READY_FOR_HUMAN_APPROVAL")
        self.assertEqual(out["confirmed_revenue_eur"], 30.0)

    def test_always_requires_human_approval_even_when_ready(self):
        d = _tmp()
        _confirm_n_commissions(d, 5)
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertTrue(out["always_requires_human_approval"])

    def test_pending_unsettled_commissions_never_count_as_evidence(self):
        d = _tmp()
        for i in range(5):
            affiliate_revenue.record_pending_commission(
                d, link_id=f"l{i}", opportunity_id="op1", offer_id="aff-systeme",
                amount=10.0, now_iso="t")   # never confirmed
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertEqual(out["confirmed_conversions"], 0)
        self.assertEqual(out["status"], "NOT_READY")

    def test_a_human_can_raise_but_the_floor_is_never_auto_lowered(self):
        d = _tmp()
        _confirm_n_commissions(d, 3)
        strict_plan = self._plan(min_confirmed_conversions=10)
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=strict_plan)
        self.assertEqual(out["min_confirmed_conversions_required"], 10)
        self.assertEqual(out["status"], "NOT_READY")

    def test_conversions_for_a_different_offer_never_count(self):
        d = _tmp()
        _confirm_n_commissions(d, 5, offer_id="aff-other-offer")
        out = ad_campaign_readiness(d, offer_id="aff-systeme", plan=self._plan())
        self.assertEqual(out["confirmed_conversions"], 0)


class CliSmokeTests(unittest.TestCase):
    def test_no_plan_cli_runs(self):
        from revenue_os.cli import main
        self.assertEqual(main(["--data-dir", str(_tmp()), "ad-campaign-readiness", "aff-x"]), 0)

    def test_partial_plan_args_fail_closed(self):
        from revenue_os.cli import main
        rc = main(["--data-dir", str(_tmp()), "ad-campaign-readiness", "aff-x",
                  "--budget-eur", "10"])   # missing the other required plan fields
        self.assertEqual(rc, 1)

    def test_full_plan_args_cli_runs(self):
        from revenue_os.cli import main
        rc = main(["--data-dir", str(_tmp()), "ad-campaign-readiness", "aff-x",
                  "--budget-eur", "10", "--max-cpc-eur", "0.5",
                  "--max-cpa-eur", "5", "--stop-loss-eur", "10"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
