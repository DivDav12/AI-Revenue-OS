"""Optimization Agent: deterministic priority-weight recompute (reuses
ecosystem.learning.OutcomeStore unmodified) + zero-traffic flagging
(recommendation only - never auto-deletes anything)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem.affiliate_model import (
    AffiliateAsset,
    AffiliateAssetStore,
    AffiliateLink,
    AffiliateLinkStore,
    ClickEvent,
    ClickStore,
)
from revenue_os.ecosystem.learning import Outcome, record_outcome
from revenue_os.messages import Task
from revenue_os.optimization_agent import OptimizationAgent, optimize, zero_traffic_assets


class ZeroTrafficTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def _seed_asset_and_link(self, asset_id, link_id, live_url="https://x/y"):
        astore = AffiliateAssetStore.load(self.d)
        astore.upsert(AffiliateAsset(asset_id=asset_id, opportunity_id=f"opp-{asset_id}",
                                     offer_id="aff-1", live_url=live_url))
        astore.save()
        lstore = AffiliateLinkStore.load(self.d)
        lstore.upsert(AffiliateLink(link_id=link_id, opportunity_id=f"opp-{asset_id}",
                                    asset_id=asset_id, offer_id="aff-1",
                                    target_url="https://example.com/p"))
        lstore.save()

    def test_undeployed_asset_is_never_flagged(self):
        astore = AffiliateAssetStore.load(self.d)
        astore.upsert(AffiliateAsset(asset_id="a1", opportunity_id="opp-a1",
                                     offer_id="aff-1", live_url=""))
        astore.save()
        self.assertEqual(zero_traffic_assets(self.d), [])

    def test_deployed_zero_click_asset_is_flagged(self):
        self._seed_asset_and_link("a1", "l1")
        flagged = zero_traffic_assets(self.d)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["asset_id"], "a1")

    def test_deployed_asset_with_clicks_is_not_flagged(self):
        self._seed_asset_and_link("a1", "l1")
        clicks = ClickStore.load(self.d)
        clicks.record(ClickEvent(click_id="c1", link_id="l1", ts="t", channel="pinterest"))
        clicks.save()
        self.assertEqual(zero_traffic_assets(self.d), [])


class PriorityWeightsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def test_below_minimum_settled_outcomes_applies_no_weighting(self):
        result = optimize(self.d)
        self.assertIn("_note", result["priority_weights"])
        self.assertEqual(result["priority_weights"]["category"], {})

    def test_weights_appear_once_enough_settled_outcomes_exist(self):
        for i in range(6):
            record_outcome(self.d, Outcome(
                opportunity_id=f"opp-{i}", category="mikrofone", strategy="AFFILIATE",
                source="hn", success=(i % 2 == 0), revenue_eur=10.0 if i % 2 == 0 else 0.0,
                execution_time_hours=1.0, settled=True))
        result = optimize(self.d)
        self.assertNotIn("_note", result["priority_weights"])
        self.assertIn("category", result["priority_weights"])


class OptimizationAgentTaskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def test_run_requires_data_dir(self):
        agent = OptimizationAgent(name="optimization_agent")
        result = agent.run(Task(objective="optimize", payload={}))
        self.assertEqual(result.status, "error")

    def test_run_end_to_end(self):
        agent = OptimizationAgent(name="optimization_agent")
        result = agent.run(Task(objective="optimize", payload={"data_dir": str(self.d)}))
        self.assertEqual(result.status, "ok")
        self.assertIn("priority_weights", result.output)
        self.assertIn("zero_traffic_assets", result.output)


if __name__ == "__main__":
    unittest.main()
