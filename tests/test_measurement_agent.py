"""Measurement Agent: click aggregation (automatic, self-hosted) +
commission rollup (human-fed, honestly labeled - never a live feed)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem.affiliate_model import (
    AffiliateLink,
    AffiliateLinkStore,
    ClickEvent,
    ClickStore,
)
from revenue_os.ecosystem.affiliate_revenue import record_pending_commission
from revenue_os.measurement_agent import MeasurementAgent, measure_opportunity
from revenue_os.messages import Task


class MeasurementAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def _seed_link(self, link_id="link-1", opportunity_id="opp-1") -> None:
        store = AffiliateLinkStore.load(self.d)
        store.upsert(AffiliateLink(
            link_id=link_id, opportunity_id=opportunity_id, asset_id="asset-1",
            offer_id="aff-1", target_url="https://example.com/product?ref=x"))
        store.save()

    def test_no_data_reports_zero_not_fabricated(self):
        self._seed_link()
        result = measure_opportunity(self.d, "opp-1")
        self.assertEqual(result["total_clicks"], 0)
        self.assertEqual(result["commissions"]["pending_estimated_eur"], 0.0)
        self.assertEqual(result["commissions"]["confirmed_or_paid_eur"], 0.0)
        self.assertIn("no live conversion feed", result["note"])

    def test_clicks_are_counted_per_link(self):
        self._seed_link("link-1", "opp-1")
        clicks = ClickStore.load(self.d)
        clicks.record(ClickEvent(click_id="c1", link_id="link-1", ts="t1", channel="pinterest"))
        clicks.record(ClickEvent(click_id="c2", link_id="link-1", ts="t2", channel="own_site"))
        clicks.save()

        result = measure_opportunity(self.d, "opp-1")
        self.assertEqual(result["total_clicks"], 2)
        self.assertEqual(result["links"][0]["click_count"], 2)

    def test_human_recorded_pending_commission_is_reflected_not_settled(self):
        self._seed_link()
        record_pending_commission(self.d, link_id="link-1", opportunity_id="opp-1",
                                  offer_id="aff-1", amount=12.5, currency="EUR",
                                  is_estimate=True, now_iso="t")
        result = measure_opportunity(self.d, "opp-1")
        self.assertEqual(result["commissions"]["pending_estimated_eur"], 12.5)
        self.assertEqual(result["commissions"]["confirmed_or_paid_eur"], 0.0)

    def test_agent_run_requires_data_dir_and_opportunity_id(self):
        agent = MeasurementAgent(name="measurement_agent")
        result = agent.run(Task(objective="measure", payload={"data_dir": str(self.d)}))
        self.assertEqual(result.status, "error")

    def test_agent_run_end_to_end(self):
        self._seed_link()
        clicks = ClickStore.load(self.d)
        clicks.record(ClickEvent(click_id="c1", link_id="link-1", ts="t1", channel="pinterest"))
        clicks.save()

        agent = MeasurementAgent(name="measurement_agent")
        result = agent.run(Task(objective="measure", payload={
            "data_dir": str(self.d), "opportunity_id": "opp-1"}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["total_clicks"], 1)


if __name__ == "__main__":
    unittest.main()
