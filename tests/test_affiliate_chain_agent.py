"""Affiliate Chain Agent: wraps the existing, tested
ecosystem.affiliate_pipeline.run_affiliate_chain (match -> build asset ->
QC -> link -> deploy) as one roster agent. Uses FakeDeploymentAdapter -
no real GitHub credential, no network call, no third-party account touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.ecosystem import affiliate_sources
from revenue_os.ecosystem.affiliate_model import AffiliateAssetStore, AffiliateLinkStore
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta
from revenue_os.ecosystem import model
from revenue_os.affiliate_chain_agent import AffiliateChainAgent
from revenue_os.messages import Task
from revenue_os.opportunity_store import load_opportunities


def _offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1, "network": "generic_saas_program",
        "program_name": "Acme Hosting Affiliates", "product_name": "Acme Cloud Hosting",
        "product_url": "https://acme.example/hosting?ref=base", "product_price": 200.0,
        "currency": "EUR", "commission_kind": "recurring_percent", "commission_rate": 0.30,
        "commission_evidence": ["Acme Affiliates dashboard: '30% recurring commission'"],
        "cookie_duration_days": 60,
        "evidence": ["Acme Cloud Hosting pricing page: EUR 200/month, 99.9% uptime SLA"],
        "category": "hosting", "keywords": ["hosting", "server", "cloud", "vps"],
        "human_confirmed_joined": True, "tracking_param": "ref",
    }
    base.update(overrides)
    return base


class _OneDraftSource:
    def __init__(self, draft: OpportunityDraft) -> None:
        self._draft = draft
        self.meta = draft.source_meta

    def discover(self, limit: int) -> list:
        return [self._draft]


def _demand_draft(**overrides) -> OpportunityDraft:
    meta = SourceMeta(source="hn-algolia", source_type="demand_signal",
                      access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
                      requires_login=False, policy_status=model.POLICY_OK)
    base = dict(
        title="Is there a tool for cheap VPS hosting for a side project?",
        description="I need cloud hosting that does not cost a fortune, ideally a VPS.",
        opportunity_type=model.TYPE_AFFILIATE,
        evidence=["Is there a tool for cheap VPS hosting for a side project?"],
        source_meta=meta, source_id="1", discovered_at="2026-09-01T00:00:00",
        category="hosting", demand_hint=0.6,
        raw={"buyer_confidence": {"total": 0.55}, "problem_confidence": {"total": 0.7}},
    )
    base.update(overrides)
    return OpportunityDraft(**base)


class AffiliateChainAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.agent = AffiliateChainAgent(name="affiliate_chain_agent")

    def _seed_opportunity(self) -> str:
        draft = _demand_draft()
        DiscoveryEngine(self.d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        rec = next(r for r in load_opportunities(self.d).all()
                  if r["discovery"]["opportunity_type"] == model.TYPE_AFFILIATE)
        return rec["id"]

    def test_missing_payload_fields_error(self):
        result = self.agent.run(Task(objective="x", payload={}))
        self.assertEqual(result.status, "error")

    def test_unknown_opportunity_id_errors(self):
        result = self.agent.run(Task(objective="x", payload={
            "data_dir": str(self.d), "opportunity_id": "does-not-exist"}))
        self.assertEqual(result.status, "error")

    def test_no_usable_offer_is_human_required_not_deployed(self):
        oid = self._seed_opportunity()
        result = self.agent.run(Task(objective="chain", payload={
            "data_dir": str(self.d), "opportunity_id": oid}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["status"], "human_required")
        self.assertEqual(result.output["step"], "match")
        self.assertEqual(result.output["next_step_class"], "HUMAN_REQUIRED")

    def test_full_chain_reaches_a_live_deployed_asset_with_fake_adapter(self):
        oid = self._seed_opportunity()
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())

        result = self.agent.run(Task(objective="chain", payload={
            "data_dir": str(self.d), "opportunity_id": oid,
            "deployment_adapter": FakeDeploymentAdapter(),
        }))
        self.assertEqual(result.status, "ok")
        out = result.output
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["next_step_class"], "SAFE_AUTONOMOUS")
        self.assertTrue(out["asset_live_url"])

        asset = AffiliateAssetStore.load(self.d).get(out["asset_id"])
        self.assertIsNotNone(asset)
        self.assertEqual(asset.live_url, out["asset_live_url"])

        link = AffiliateLinkStore.load(self.d).get(out["link_id"])
        self.assertIsNotNone(link)
        self.assertEqual(link.opportunity_id, oid)
        self.assertEqual(link.asset_id, asset.asset_id)
        # a real, non-empty, tracked outbound target - never fabricated
        self.assertTrue(link.target_url)

    def test_idempotent_rerun_produces_no_duplicate_asset_or_link(self):
        oid = self._seed_opportunity()
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
        payload = {"data_dir": str(self.d), "opportunity_id": oid,
                  "deployment_adapter": FakeDeploymentAdapter()}
        r1 = self.agent.run(Task(objective="chain", payload=payload))
        r2 = self.agent.run(Task(objective="chain", payload=payload))
        self.assertEqual(r1.output["asset_id"], r2.output["asset_id"])
        self.assertEqual(r1.output["link_id"], r2.output["link_id"])
        self.assertEqual(len(AffiliateAssetStore.load(self.d).all()), 1)
        self.assertEqual(len(AffiliateLinkStore.load(self.d).all()), 1)


if __name__ == "__main__":
    unittest.main()
