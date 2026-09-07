"""THE end-to-end proof: one real opportunity, all the way through the
whole pipeline, using only fake/local adapters - no real money, no real
credentials, no third-party account ever touched.

    real public signal (curated file, origin=real)
      -> matched against a usable affiliate offer
      -> deterministic score -> SELECTED
      -> content asset generated (template-only, $0)
      -> quality gate passes
      -> deployed to a FAKE GitHub Pages adapter (no real GITHUB_TOKEN)
      -> a valid, tracked affiliate CTA link exists on the deployed page
      -> a Pinterest pin is drafted (rate-limited, never posted)
      -> a digital-product draft is generated in parallel (never blocking)
      -> measurement aggregates (zero clicks yet - honest, not guessed)
      -> optimization runs (no weighting yet - too few settled outcomes,
         which is itself the correct, honest behavior)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.affiliate_cycle import run_cycle
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.ecosystem import affiliate_sources
from revenue_os.ecosystem.affiliate_model import AffiliateAssetStore, AffiliateLinkStore
from revenue_os.ecosystem.pinterest_pins import PinterestPinStore


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


class FullPipelineE2ETests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.signal_path = self.d / "signals.json"
        self.signal_path.write_text(json.dumps([{
            "title": "Is there a tool for cheap VPS hosting for a side project?",
            "text": "I need cloud hosting that does not cost a fortune, ideally a VPS.",
            "url": "https://example.com/t/1", "external_id": "sig-1",
        }]), encoding="utf-8")

    def test_full_pipeline_reaches_a_live_asset_with_a_pin_draft(self):
        # a human already joined a real affiliate program (simulated by
        # ingesting a usable offer - the same ingestion path a human-fed
        # real program goes through).
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())

        report = run_cycle(
            self.d, source_names=("file",),
            source_kwargs={"file": {"path": str(self.signal_path)}},
            now_iso="2026-09-07T12:00:00Z",
            deployment_adapter=FakeDeploymentAdapter(),
        )

        # 1. Opportunity: real discovery + selection
        self.assertGreaterEqual(report["discovery"]["new"], 1, report)
        self.assertEqual(report["selection"]["status"], "SELECTED", report)

        # 2. Affiliate Chain: asset generated, QC passed, deployed live
        chain = report["chain"]
        self.assertEqual(chain["status"], "completed", chain)
        self.assertEqual(chain["next_step_class"], "SAFE_AUTONOMOUS")
        self.assertTrue(chain["asset_live_url"].startswith("https://fake.pages.test"))

        asset = AffiliateAssetStore.load(self.d).get(chain["asset_id"])
        self.assertIsNotNone(asset)
        self.assertTrue(asset.disclosure_included)
        self.assertTrue(asset.quality_checks.get("meets_min_words"))
        self.assertTrue(asset.quality_checks.get("has_evidence"))

        # 3. Valid, tracked affiliate CTA link
        link = AffiliateLinkStore.load(self.d).get(chain["link_id"])
        self.assertIsNotNone(link)
        self.assertTrue(link.target_url)
        self.assertIn("ref=", link.target_url)  # the offer's tracking_param applied

        # 4. Pinterest pin draft - present, unposted, points at the real live URL
        self.assertIn("pin", report)
        pin_id = report["pin"]["pin_id"]
        pin = PinterestPinStore.load(self.d).get(pin_id)
        self.assertIsNotNone(pin)
        self.assertEqual(pin.status, "draft")
        self.assertEqual(pin.dest_url, chain["asset_live_url"])

        # 5. Digital product: generated in parallel, never blocked the chain above
        self.assertIn("digital_product", report)
        self.assertNotIn("digital_product_error", report)

        # 6. Measurement: honest zero, not fabricated
        self.assertEqual(report["measurement"]["total_clicks"], 0)
        self.assertEqual(report["measurement"]["commissions"]["confirmed_or_paid_eur"], 0.0)

        # 7. Optimization: correctly applies no weighting yet (too few
        #    settled outcomes) - this IS the correct behavior, not a bug.
        self.assertIn("_note", report["optimization"]["priority_weights"])

        # 8. Human actions surfaced (Pinterest post + Gumroad/Payhip setup),
        #    but the chain itself needed no human step - GitHub Pages is
        #    the owned, autonomous channel.
        self.assertTrue(any("PINTEREST" in a for a in report["human_actions"]))
        self.assertTrue(any("DIGITAL PRODUCT" in a for a in report["human_actions"]))
        self.assertFalse(any("AFFILIATE CHAIN" in a for a in report["human_actions"]))

    def test_no_real_credential_is_ever_touched(self):
        # no GITHUB_TOKEN in the environment at all - the cycle must still
        # complete via the injected fake adapter, proving no hidden path
        # reads a real credential.
        import os
        backup = os.environ.pop("GITHUB_TOKEN", None)
        try:
            affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
            report = run_cycle(
                self.d, source_names=("file",),
                source_kwargs={"file": {"path": str(self.signal_path)}},
                now_iso="2026-09-07T12:00:00Z", deployment_adapter=FakeDeploymentAdapter())
            self.assertEqual(report["chain"]["status"], "completed")
        finally:
            if backup is not None:
                os.environ["GITHUB_TOKEN"] = backup

    def test_without_a_credential_or_fake_adapter_deployment_is_human_required(self):
        # the real (non-injected) path: no GITHUB_TOKEN configured ->
        # deployment is correctly reported as needing a human, never
        # silently skipped or fabricated as successful.
        import os
        backup = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GITHUB_PAGES_REPO")}
        try:
            affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
            report = run_cycle(
                self.d, source_names=("file",),
                source_kwargs={"file": {"path": str(self.signal_path)}},
                now_iso="2026-09-07T12:00:00Z")  # no deployment_adapter injected
            self.assertEqual(report["chain"]["status"], "human_required")
            self.assertEqual(report["chain"]["step"], "deploy")
            self.assertTrue(any("GITHUB_TOKEN" in r for r in report["chain"]["reason"]))
            self.assertTrue(any("AFFILIATE CHAIN" in a and "GITHUB_TOKEN" in a
                                for a in report["human_actions"]))
            self.assertNotIn("pin", report)  # never drafts a pin for a page that isn't live
        finally:
            for k, v in backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_content_build_and_qc_happen_even_without_a_deploy_credential(self):
        # build_asset/QC/create_link need no credential and are safe local
        # work - they must run and persist even when deploy is blocked,
        # not be thrown away by an unnecessary pre-check.
        import os
        backup = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GITHUB_PAGES_REPO")}
        try:
            affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
            report = run_cycle(
                self.d, source_names=("file",),
                source_kwargs={"file": {"path": str(self.signal_path)}},
                now_iso="2026-09-07T12:00:00Z")
            self.assertEqual(report["chain"]["status"], "human_required")
            asset_id = report["chain"]["asset_id"]
            link_id = report["chain"]["link_id"]
            self.assertTrue(asset_id)
            self.assertTrue(link_id)

            asset = AffiliateAssetStore.load(self.d).get(asset_id)
            self.assertIsNotNone(asset)
            self.assertTrue(asset.quality_checks.get("meets_min_words"))
            self.assertTrue(asset.quality_checks.get("has_disclosure"))
            self.assertEqual(asset.live_url, "")  # never fabricated - not actually live

            link = AffiliateLinkStore.load(self.d).get(link_id)
            self.assertIsNotNone(link)
            self.assertTrue(link.target_url)
        finally:
            for k, v in backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_no_usable_offer_stops_at_opportunity_step(self):
        report = run_cycle(
            self.d, source_names=("file",),
            source_kwargs={"file": {"path": str(self.signal_path)}},
            now_iso="2026-09-07T12:00:00Z")
        self.assertEqual(report["selection"]["status"], "REJECTED")
        self.assertIn("HUMAN SETUP REQUIRED", report["selection"]["reason"])
        self.assertNotIn("chain", report)

    def test_second_cycle_never_duplicates_the_page_it_already_built(self):
        # re-running the cycle with the SAME real signal must never
        # produce a second page for a topic already covered - the
        # content-opportunity engine's own duplicate-prevention
        # (`already_has_a_page`) correctly rejects it before a second
        # chain attempt ever starts. This is the honest idempotency
        # guarantee at the cycle level; run_affiliate_chain's own
        # per-call idempotency is covered directly in
        # test_affiliate_chain_agent.py.
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
        adapter = FakeDeploymentAdapter()
        r1 = run_cycle(self.d, source_names=("file",),
                       source_kwargs={"file": {"path": str(self.signal_path)}},
                       now_iso="2026-09-07T12:00:00Z", deployment_adapter=adapter)
        self.assertEqual(r1["chain"]["status"], "completed")

        r2 = run_cycle(self.d, source_names=("file",),
                       source_kwargs={"file": {"path": str(self.signal_path)}},
                       now_iso="2026-09-07T13:00:00Z", deployment_adapter=adapter)
        self.assertEqual(r2["selection"]["status"], "REJECTED")
        self.assertNotIn("chain", r2)

        self.assertEqual(len(AffiliateAssetStore.load(self.d).all()), 1)
        self.assertEqual(len(AffiliateLinkStore.load(self.d).all()), 1)
        self.assertEqual(len(PinterestPinStore.load(self.d).all()), 1)


if __name__ == "__main__":
    unittest.main()
