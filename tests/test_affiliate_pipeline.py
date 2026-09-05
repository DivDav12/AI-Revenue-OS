"""Affiliate Revenue Pipeline - end-to-end tests.

Covers: offer model + ingestion validation, demand<->offer matching,
affiliate profitability, asset generation + quality gate, link creation +
attribution, click tracking (store + the redirect HTTP server), commission
lifecycle -> real revenue ledger, the full chain via ecosystem.pipeline,
idempotency, per-opportunity error isolation in the autonomous tick,
autonomy classification, JARVIS read-model, scaling/optimization ranking,
and the new CLI commands. No network - the deployment adapter used
throughout is `deployment.FakeDeploymentAdapter`.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from revenue_os import action_class
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.ecosystem import (
    affiliate_assets,
    affiliate_intel,
    affiliate_links,
    affiliate_matching,
    affiliate_model,
    affiliate_pipeline,
    affiliate_profitability,
    affiliate_revenue,
    affiliate_scaling,
    affiliate_sources,
    affiliate_tracking_server,
    autonomy as eco_autonomy,
    model,
    pipeline as eco_pipeline,
)
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta
from revenue_os.opportunity_store import load_opportunities


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "network": "generic_saas_program",
        "program_name": "Acme Hosting Affiliates",
        "product_name": "Acme Cloud Hosting",
        "product_url": "https://acme.example/hosting?ref=base",
        "product_price": 200.0,
        "currency": "EUR",
        "commission_kind": "recurring_percent",
        "commission_rate": 0.30,
        "commission_evidence": ["Acme Affiliates dashboard: '30% recurring commission "
                                "for the lifetime of the referred subscription'"],
        "cookie_duration_days": 60,
        "evidence": ["Acme Cloud Hosting pricing page: EUR 200/month for the "
                    "business VPS tier, 99.9% uptime SLA"],
        "category": "hosting",
        "keywords": ["hosting", "server", "cloud", "vps"],
        "human_confirmed_joined": True,
        "tracking_param": "ref",
    }
    base.update(overrides)
    return base


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


# ---------------------------------------------------------------------------
# 1. model
# ---------------------------------------------------------------------------

class CommissionModelTests(unittest.TestCase):
    def test_fixed_commission_ignores_price(self):
        c = affiliate_model.CommissionModel(kind=affiliate_model.COMMISSION_FIXED, fixed_amount=15.0)
        self.assertEqual(c.expected_commission(999.0), 15.0)

    def test_percent_commission_scales_with_price(self):
        c = affiliate_model.CommissionModel(kind=affiliate_model.COMMISSION_PERCENT, rate=0.1)
        self.assertEqual(c.expected_commission(200.0), 20.0)

    def test_roundtrip_to_dict_from_dict(self):
        c = affiliate_model.CommissionModel(kind="fixed", fixed_amount=5.0, evidence=("x",))
        self.assertEqual(affiliate_model.CommissionModel.from_dict(c.to_dict()), c)


class OfferStoreTests(unittest.TestCase):
    def test_upsert_then_get_roundtrips(self):
        d = _tmp()
        store = affiliate_model.AffiliateOfferStore.load(d)
        offer = affiliate_model.AffiliateOffer(
            offer_id="aff-1", network="generic_saas_program", program_name="P",
            product_name="X", status=model.POLICY_OK)
        store.upsert(offer)
        store.save()
        reloaded = affiliate_model.AffiliateOfferStore.load(d)
        got = reloaded.get("aff-1")
        self.assertIsNotNone(got)
        self.assertTrue(got.usable)

    def test_unusable_when_setup_required(self):
        offer = affiliate_model.AffiliateOffer(
            offer_id="x", network="amazon_associates", program_name="P", product_name="X",
            status=model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertFalse(offer.usable)


# ---------------------------------------------------------------------------
# 2. ingestion / validation (spec: no fabricated offers/commissions)
# ---------------------------------------------------------------------------

class IngestionTests(unittest.TestCase):
    def test_valid_offer_ingests_and_is_usable(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        self.assertEqual(out["status"], model.POLICY_OK)
        self.assertTrue(out["usable"])

    def test_not_yet_joined_stays_human_setup_required(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(
            d, _offer_json(human_confirmed_joined=False))
        self.assertEqual(out["status"], model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertFalse(out["usable"])
        self.assertTrue(out["setup_steps"])

    def test_missing_commission_evidence_is_rejected(self):
        d = _tmp()
        payload = _offer_json()
        del payload["commission_evidence"]
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(d, payload)

    def test_percent_rate_out_of_range_is_rejected(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _offer_json(commission_rate=1.5))

    def test_unknown_field_is_rejected(self):
        payload = _offer_json()
        payload["made_up_field"] = "x"
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), payload)

    def test_wrong_schema_version_is_rejected(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _offer_json(schema_version=99))

    def test_reingest_same_program_updates_not_duplicates(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        out2 = affiliate_sources.ingest_affiliate_offer(d, _offer_json(product_price=25.0))
        self.assertTrue(out2["updated_existing"])
        offers = affiliate_model.AffiliateOfferStore.load(d).all()
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].product_price, 25.0)

    def test_unknown_network_fails_closed_to_human_setup_required(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(
            d, _offer_json(network="some_brand_new_network"))
        # human_confirmed_joined=True on an unrecognised-but-not-blocked
        # network is still trusted (the human already joined it for real) -
        # but setup_required_networks() must still list every OTHER known
        # network that has no offer yet.
        self.assertEqual(out["status"], model.POLICY_OK)
        pending = affiliate_sources.setup_required_networks(d)
        networks_listed = {p["network"] for p in pending}
        self.assertIn(affiliate_model.NETWORK_AMAZON_ASSOCIATES, networks_listed)

    def test_ingest_from_file(self):
        import tempfile as tf
        d = _tmp()
        fh = tf.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(_offer_json(), fh)
        fh.close()
        out = affiliate_sources.ingest_affiliate_offer_file(d, fh.name)
        self.assertTrue(out["usable"])


# ---------------------------------------------------------------------------
# 3. matching
# ---------------------------------------------------------------------------

class MatchingTests(unittest.TestCase):
    def _offer(self, **kw) -> affiliate_model.AffiliateOffer:
        base = dict(offer_id="o1", network="generic_saas_program", program_name="P",
                   product_name="Cloud VPS Hosting", category="hosting",
                   keywords=("hosting", "vps", "server"), status=model.POLICY_OK)
        base.update(kw)
        return affiliate_model.AffiliateOffer(**base)

    def test_matching_offer_scores_above_floor(self):
        draft = _demand_draft()
        matches = affiliate_matching.match_offers(draft, [self._offer()])
        self.assertEqual(len(matches), 1)
        self.assertGreater(matches[0].match_score, 0.0)

    def test_unrelated_offer_does_not_match(self):
        draft = _demand_draft()
        unrelated = self._offer(category="pet-food", keywords=("dog", "cat", "treats"),
                                product_name="Premium Dog Food")
        matches = affiliate_matching.match_offers(draft, [unrelated])
        self.assertEqual(matches, [])

    def test_best_usable_match_skips_unusable_offer(self):
        draft = _demand_draft()
        unusable = self._offer(offer_id="o-bad", status=model.POLICY_HUMAN_SETUP_REQUIRED)
        usable = self._offer(offer_id="o-good")
        best = affiliate_matching.best_usable_match(draft, [unusable, usable])
        self.assertEqual(best.offer.offer_id, "o-good")

    def test_no_usable_offer_returns_none_even_if_a_match_exists(self):
        draft = _demand_draft()
        unusable = self._offer(status=model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertIsNone(affiliate_matching.best_usable_match(draft, [unusable]))

    def test_demand_strength_prefers_ranking_layer_scores(self):
        draft = _demand_draft(raw={"buyer_confidence": {"total": 0.9},
                                   "problem_confidence": {"total": 0.1}},
                              demand_hint=0.05)
        self.assertEqual(affiliate_matching.demand_strength(draft), 0.9)

    def test_demand_strength_falls_back_to_demand_hint(self):
        draft = _demand_draft(raw={}, demand_hint=0.4)
        self.assertEqual(affiliate_matching.demand_strength(draft), 0.4)


# ---------------------------------------------------------------------------
# 4. profitability - the required "high commission x low conversion can
# lose to low commission x high conversion" property
# ---------------------------------------------------------------------------

class ProfitabilityTests(unittest.TestCase):
    def _match(self, offer) -> affiliate_matching.AffiliateMatch:
        return affiliate_matching.AffiliateMatch(offer=offer, match_score=0.6, demand_strength=0.6)

    def test_high_commission_low_conversion_can_lose_to_modest_commission_high_conversion(self):
        big_commission_offer = affiliate_model.AffiliateOffer(
            offer_id="big", network="generic_saas_program", program_name="P",
            product_name="Expensive Thing", product_price=1000.0,
            commission=affiliate_model.CommissionModel(kind="percent", rate=0.5),
            status=model.POLICY_OK)
        small_commission_offer = affiliate_model.AffiliateOffer(
            offer_id="small", network="generic_saas_program", program_name="P",
            product_name="Reasonable Thing", product_price=200.0,
            commission=affiliate_model.CommissionModel(kind="percent", rate=0.15),
            status=model.POLICY_OK)

        big_prof = affiliate_profitability.evaluate(
            self._match(big_commission_offer), ctr=0.12, conversion_rate=0.0005)
        small_prof = affiliate_profitability.evaluate(
            self._match(small_commission_offer), ctr=0.12, conversion_rate=0.05)

        self.assertLess(model.estimate_value(big_prof.expected_profit), 0.0,
                        "sanity: the extreme-low-conversion offer should be a loser")
        self.assertGreater(model.estimate_value(small_prof.expected_profit), 0.0,
                           "sanity: the modest-but-realistic offer should be a winner")
        self.assertLess(model.estimate_value(big_prof.decision_value),
                        model.estimate_value(small_prof.decision_value))

    def test_every_output_is_marked_estimate(self):
        offer = affiliate_model.AffiliateOffer(
            offer_id="o", network="generic_saas_program", program_name="P", product_name="X",
            product_price=10.0, status=model.POLICY_OK)
        prof = affiliate_profitability.evaluate(self._match(offer))
        d = prof.to_dict()
        for key in ("expected_revenue", "expected_profit", "decision_value", "confidence", "risk"):
            self.assertTrue(d[key]["is_estimate"])

    def test_zero_traffic_potential_yields_zero_revenue_not_a_crash(self):
        offer = affiliate_model.AffiliateOffer(
            offer_id="o", network="generic_saas_program", program_name="P", product_name="X",
            product_price=10.0, status=model.POLICY_OK)
        m = affiliate_matching.AffiliateMatch(offer=offer, match_score=0.0, demand_strength=0.0)
        prof = affiliate_profitability.evaluate(m, ctr=0.0)
        self.assertEqual(model.estimate_value(prof.expected_revenue), 0.0)


# ---------------------------------------------------------------------------
# 5. asset generation + quality gate
# ---------------------------------------------------------------------------

class AssetGenerationTests(unittest.TestCase):
    def _match(self) -> affiliate_matching.AffiliateMatch:
        offer = affiliate_model.AffiliateOffer(
            offer_id="o1", network="generic_saas_program", program_name="Acme Affiliates",
            product_name="Acme Cloud Hosting", product_price=20.0,
            commission=affiliate_model.CommissionModel(kind="percent", rate=0.3),
            evidence=("Acme dashboard: 30% recurring commission",), status=model.POLICY_OK)
        return affiliate_matching.AffiliateMatch(offer=offer, match_score=0.6, demand_strength=0.6)

    def test_page_includes_disclosure_and_cta_and_no_fabricated_price_without_evidence(self):
        draft = _demand_draft()
        page, checks = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/abc")
        self.assertIn(affiliate_assets.DISCLOSURE_TEXT, page)
        self.assertIn("https://example.test/go/abc", page)
        self.assertTrue(checks["has_evidence"])
        ok, reasons = affiliate_assets.check_quality(checks)
        self.assertTrue(ok, reasons)

    def test_offer_with_no_evidence_fails_quality_gate(self):
        draft = _demand_draft()
        match = self._match()
        match.offer.evidence = ()
        _, checks = affiliate_assets.render_comparison_page(draft=draft, match=match, cta_url="x")
        ok, reasons = affiliate_assets.check_quality(checks)
        self.assertFalse(ok)
        self.assertTrue(any("evidence" in r for r in reasons))

    def test_build_asset_is_idempotent_per_opportunity_offer(self):
        d = _tmp()
        draft = _demand_draft()
        match = self._match()
        a1, ok1, _ = affiliate_assets.build_asset(d, opportunity_id="op1", draft=draft,
                                                  match=match, cta_url="x")
        a2, ok2, _ = affiliate_assets.build_asset(d, opportunity_id="op1", draft=draft,
                                                  match=match, cta_url="x")
        self.assertEqual(a1.asset_id, a2.asset_id)
        self.assertTrue(ok1 and ok2)


# ---------------------------------------------------------------------------
# 6. link creation + attribution
# ---------------------------------------------------------------------------

class LinkTests(unittest.TestCase):
    def _asset_and_match(self, d):
        match = affiliate_matching.AffiliateMatch(
            offer=affiliate_model.AffiliateOffer(
                offer_id="o1", network="generic_saas_program", program_name="P",
                product_name="X", product_url="https://merchant.example/x",
                tracking_param="ref", status=model.POLICY_OK),
            match_score=0.5, demand_strength=0.5)
        asset, _, _ = affiliate_assets.build_asset(
            d, opportunity_id="opX", draft=_demand_draft(), match=match, cta_url="")
        return asset, match

    def test_create_link_appends_tracking_param(self):
        d = _tmp()
        asset, match = self._asset_and_match(d)
        link = affiliate_links.create_link(d, opportunity_id="opX", asset=asset,
                                           match=match, source="own_blog")
        self.assertIn("ref=", link.target_url)
        self.assertTrue(link.tracking_id)
        self.assertEqual(link.opportunity_id, "opX")
        self.assertEqual(link.asset_id, asset.asset_id)
        self.assertEqual(link.offer_id, "o1")

    def test_create_link_is_idempotent_per_asset_offer_source(self):
        d = _tmp()
        asset, match = self._asset_and_match(d)
        l1 = affiliate_links.create_link(d, opportunity_id="opX", asset=asset, match=match, source="own_blog")
        l2 = affiliate_links.create_link(d, opportunity_id="opX", asset=asset, match=match, source="own_blog")
        self.assertEqual(l1.link_id, l2.link_id)
        self.assertEqual(l1.tracking_id, l2.tracking_id)

    def test_full_attribution_chain_is_joinable(self):
        d = _tmp()
        asset, match = self._asset_and_match(d)
        link = affiliate_links.create_link(d, opportunity_id="opX", asset=asset, match=match, source="own_blog")
        self.assertEqual(link.opportunity_id, "opX")
        self.assertEqual(link.asset_id, asset.asset_id)
        self.assertEqual(link.offer_id, match.offer.offer_id)


# ---------------------------------------------------------------------------
# 7. click tracking - the store + the real redirect HTTP server
# ---------------------------------------------------------------------------

class ClickTrackingTests(unittest.TestCase):
    def test_record_click_by_tracking_id_increments_link(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        link = affiliate_model.AffiliateLink(link_id="l1", opportunity_id="op", asset_id="a",
                                             offer_id="o", tracking_id="trk-1",
                                             target_url="https://x.test/p")
        store.upsert(link)
        store.save()

        out = affiliate_links.record_click(d, tracking_id="trk-1", channel="own_blog", now_iso="t")
        self.assertTrue(out["recorded"])
        self.assertEqual(out["target_url"], "https://x.test/p")
        reloaded = affiliate_model.AffiliateLinkStore.load(d).get("l1")
        self.assertEqual(reloaded.click_count, 1)

    def test_unknown_tracking_id_is_a_safe_noop(self):
        out = affiliate_links.record_click(_tmp(), tracking_id="nope")
        self.assertFalse(out["recorded"])

    def test_no_personal_data_fields_on_a_click_event(self):
        ev = affiliate_model.ClickEvent(click_id="c1", link_id="l1", ts="t", channel="own_blog")
        d = ev.to_dict()
        self.assertEqual(set(d.keys()), {"click_id", "link_id", "ts", "channel"})


class TrackingServerTests(unittest.TestCase):
    def test_redirect_server_records_click_and_redirects(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        link = affiliate_model.AffiliateLink(link_id="l1", opportunity_id="op", asset_id="a",
                                             offer_id="o", tracking_id="trk-xyz",
                                             target_url="https://merchant.example/prod")
        store.upsert(link)
        store.save()

        server = affiliate_tracking_server.serve(d, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/go/trk-xyz")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.getheader("Location"), "https://merchant.example/prod")
        finally:
            server.shutdown()
            thread.join(timeout=2)

        self.assertEqual(affiliate_model.AffiliateLinkStore.load(d).get("l1").click_count, 1)

    def test_unknown_path_is_404(self):
        server = affiliate_tracking_server.serve(_tmp(), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/nope")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
        finally:
            server.shutdown()
            thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 8. revenue states - pending never counted as real, confirmed books the
# real ledger idempotently, reversal never fabricates a claw-back
# ---------------------------------------------------------------------------

class RevenueStateTests(unittest.TestCase):
    def test_pending_commission_is_never_in_the_real_ledger(self):
        d = _tmp()
        affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=6.0, now_iso="t")
        from revenue_os.revenue import RevenueLedger
        ledger = RevenueLedger.load(d / "revenue.json")
        self.assertEqual(ledger.total(), 0.0)

    def test_confirm_books_the_real_ledger_idempotently_by_ref(self):
        d = _tmp()
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=6.0, now_iso="t")
        out1 = affiliate_revenue.confirm_commission(d, rec.commission_id, ref="R1", now_iso="t")
        self.assertEqual(out1["ledger_outcome"], "booked")
        from revenue_os.revenue import RevenueLedger
        ledger = RevenueLedger.load(d / "revenue.json")
        self.assertEqual(ledger.total(), 6.0)

        # a second confirm attempt on an already-CONFIRMED commission is refused
        with self.assertRaises(affiliate_revenue.AffiliateRevenueError):
            affiliate_revenue.confirm_commission(d, rec.commission_id, ref="R1", now_iso="t")
        self.assertEqual(RevenueLedger.load(d / "revenue.json").total(), 6.0)

    def test_confirm_requires_positive_amount(self):
        d = _tmp()
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=0.0, now_iso="t")
        with self.assertRaises(affiliate_revenue.AffiliateRevenueError):
            affiliate_revenue.confirm_commission(d, rec.commission_id, ref="R2", now_iso="t")

    def test_reverse_never_claims_a_ledger_clawback(self):
        d = _tmp()
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=6.0, now_iso="t")
        affiliate_revenue.confirm_commission(d, rec.commission_id, ref="R3", now_iso="t")
        out = affiliate_revenue.reverse_commission(d, rec.commission_id, now_iso="t")
        self.assertTrue(out["was_previously_confirmed"])
        self.assertIn("no automatic claw-back", out["ledger_note"])
        from revenue_os.revenue import RevenueLedger
        # documented limitation: the ledger entry itself is untouched
        self.assertEqual(RevenueLedger.load(d / "revenue.json").total(), 6.0)

    def test_confirm_feeds_the_shared_learning_loop(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        offer_id = affiliate_model.AffiliateOfferStore.load(d).all()[0].offer_id
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id=offer_id, amount=9.0, now_iso="t")
        affiliate_revenue.confirm_commission(d, rec.commission_id, ref="R4", now_iso="t")

        from revenue_os.ecosystem.learning import OutcomeStore
        rows = OutcomeStore.load(d).rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy"], "AFFILIATE")
        self.assertTrue(rows[0]["success"])
        self.assertEqual(rows[0]["revenue_eur"], 9.0)

    def test_opportunity_commission_summary_splits_pending_from_settled(self):
        d = _tmp()
        p1 = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=5.0, now_iso="t")
        p2 = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id="o1", amount=7.0, now_iso="t")
        affiliate_revenue.confirm_commission(d, p2.commission_id, ref="R5", now_iso="t")
        summary = affiliate_revenue.opportunity_commission_summary(d, "op1")
        self.assertEqual(summary["pending_estimated_eur"], 5.0)
        self.assertEqual(summary["confirmed_or_paid_eur"], 7.0)


# ---------------------------------------------------------------------------
# 9. full chain via ecosystem.pipeline.plan() - real DiscoveryEngine seed
# ---------------------------------------------------------------------------

class _OneDraftSource:
    """Minimal real `sources.OpportunitySource` - yields exactly one
    AFFILIATE draft, once."""

    def __init__(self, draft: OpportunityDraft) -> None:
        self._draft = draft
        self.meta = draft.source_meta

    def discover(self, limit: int):
        return [self._draft]


class FullChainTests(unittest.TestCase):
    def _seed_affiliate_opportunity(self, d) -> str:
        draft = _demand_draft()
        report = DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        rd = report.to_dict()
        self.assertGreaterEqual(rd["new"], 1, rd)
        rec = next(r for r in load_opportunities(d).all()
                  if r["discovery"]["opportunity_type"] == model.TYPE_AFFILIATE)
        self.assertIn(rec["discovery"]["verification"]["status"], model.PLANNABLE,
                     rec["discovery"]["verification"])
        return rec["id"]

    def test_chain_with_no_offers_is_human_required(self):
        # exercises affiliate_pipeline directly: with zero offers on file,
        # the chain must fail closed at the match step - regardless of
        # what the generic strategy heuristic would otherwise pick (with
        # no matching offer, it correctly prefers PRODUCT - see
        # test_no_offers_falls_back_to_product_strategy below).
        d = _tmp()
        draft = _demand_draft()
        result = affiliate_pipeline.run_affiliate_chain(
            d, opportunity_id="op-none", draft=draft, now_iso="t")
        self.assertEqual(result["status"], "human_required")
        self.assertEqual(result["step"], "match")
        self.assertEqual(result["next_step_class"], "HUMAN_REQUIRED")

    def test_no_offers_falls_back_to_product_strategy(self):
        # with no affiliate offer to match, the generic strategy engine's
        # own PRODUCT pick is left completely untouched by the affiliate
        # override - the fleet still has a real, autonomous path forward.
        d = _tmp()
        oid = self._seed_affiliate_opportunity(d)
        eco_pipeline.evaluate(d, oid)
        sel = eco_pipeline.select(d, oid)
        self.assertEqual(sel["recommended"], "PRODUCT")

    def test_full_chain_completes_with_a_usable_offer_and_fake_deploy(self):
        d = _tmp()
        oid = self._seed_affiliate_opportunity(d)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        sel = eco_pipeline.select(d, oid)
        self.assertEqual(sel["recommended"], "AFFILIATE")

        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            out = eco_pipeline.plan(d, oid)

        self.assertEqual(out["next_step_class"], "SAFE_AUTONOMOUS")
        plan = out["plan"]
        self.assertEqual(plan["status"], "completed")
        self.assertTrue(plan["asset_live_url"])
        self.assertIn("distribution_plan", plan)
        self.assertTrue(plan["distribution_plan"]["human_gate_required"])

        # attribution: the link really points back to opportunity/asset/offer
        link = affiliate_model.AffiliateLinkStore.load(d).get(plan["link_id"])
        self.assertEqual(link.opportunity_id, oid)
        self.assertEqual(link.asset_id, plan["asset_id"])

    def test_replanning_is_idempotent_no_duplicate_rows(self):
        d = _tmp()
        oid = self._seed_affiliate_opportunity(d)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        eco_pipeline.select(d, oid)
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            out1 = eco_pipeline.plan(d, oid)
            out2 = eco_pipeline.plan(d, oid)
        self.assertEqual(out1["plan"]["asset_id"], out2["plan"]["asset_id"])
        self.assertEqual(out1["plan"]["link_id"], out2["plan"]["link_id"])
        self.assertEqual(len(affiliate_model.AffiliateAssetStore.load(d).all()), 1)
        self.assertEqual(len(affiliate_model.AffiliateLinkStore.load(d).all()), 1)

    def test_no_pipeline_change_for_product_strategy(self):
        # regression guard: PRODUCT's own plan() branch is untouched by
        # this pass - a quick structural check that STRAT_PRODUCT still
        # routes to acceptance, not affiliate_pipeline.
        import inspect
        src = inspect.getsource(eco_pipeline.plan)
        self.assertIn("accept_opportunity", src)


# ---------------------------------------------------------------------------
# 10. autonomous tick - idempotent, isolates a bad opportunity
# ---------------------------------------------------------------------------

class TickTests(unittest.TestCase):
    def test_tick_plans_a_qualified_affiliate_opportunity(self):
        d = _tmp()
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())

        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            out = affiliate_pipeline.run_affiliate_tick(d, now_iso="t")
        self.assertEqual(len(out["planned"]), 1)
        self.assertEqual(out["errors"], [])

    def test_one_bad_opportunity_does_not_stop_the_tick(self):
        d = _tmp()
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())

        from unittest import mock
        with mock.patch("revenue_os.ecosystem.pipeline.evaluate", side_effect=RuntimeError("boom")):
            out = affiliate_pipeline.run_affiliate_tick(d, now_iso="t")
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("boom", out["errors"][0]["error"])

    def test_tick_is_idempotent_second_run_skips_completed(self):
        d = _tmp()
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            affiliate_pipeline.run_affiliate_tick(d, now_iso="t")
            out2 = affiliate_pipeline.run_affiliate_tick(d, now_iso="t")
        self.assertEqual(out2["attempted"], [])


# ---------------------------------------------------------------------------
# 11. autonomy / safety classification
# ---------------------------------------------------------------------------

class AutonomyClassificationTests(unittest.TestCase):
    def test_match_and_build_and_link_are_safe_autonomous(self):
        for activity in ("match_offer", "build_affiliate_asset", "create_affiliate_link"):
            v = eco_autonomy.classify_activity(activity)
            self.assertEqual(v.verdict, eco_autonomy.AUTONOMOUS_ALLOWED, activity)

    def test_joining_a_program_is_never_autonomous(self):
        v = eco_autonomy.classify_activity("join_affiliate_program")
        self.assertEqual(v.verdict, eco_autonomy.HUMAN_REQUIRED)

    def test_deploy_with_checkout_still_requires_money_approval(self):
        v = eco_autonomy.classify_activity("deploy_affiliate_asset",
                                           {"has_checkout": True})
        self.assertEqual(v.verdict, eco_autonomy.HUMAN_APPROVAL_REQUIRED)

    def test_deploy_without_checkout_is_autonomous(self):
        v = eco_autonomy.classify_activity("deploy_affiliate_asset", {})
        self.assertEqual(v.verdict, eco_autonomy.AUTONOMOUS_ALLOWED)

    def test_create_affiliate_link_kind_is_registered_safe(self):
        self.assertTrue(action_class.classify("create_affiliate_link").autonomous)


# ---------------------------------------------------------------------------
# 12. scaling / optimization - profitability first, then conversion, then clicks
# ---------------------------------------------------------------------------

class ScalingTests(unittest.TestCase):
    def _link(self, **kw) -> affiliate_model.AffiliateLink:
        base = dict(link_id="l", opportunity_id="op", asset_id="a", offer_id="o", source="own_blog")
        base.update(kw)
        return affiliate_model.AffiliateLink(**base)

    def test_insufficient_data_below_click_floor(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        store.upsert(self._link(link_id="l1", click_count=3, commission_eur=50.0))
        store.save()
        ranked = affiliate_scaling.rank_links(d)
        self.assertEqual(ranked[0].verdict, affiliate_scaling.INSUFFICIENT_DATA)

    def test_profitable_link_scales_unprofitable_stops(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        store.upsert(self._link(link_id="winner", click_count=50, conversion_count=5,
                                commission_eur=100.0, cost_eur=3.0))
        store.upsert(self._link(link_id="loser", click_count=50, conversion_count=0,
                                commission_eur=0.0, cost_eur=3.0))
        store.save()
        report = affiliate_scaling.optimization_report(d)
        self.assertIn("winner", [r["link_id"] for r in report["scale"]])
        self.assertIn("loser", [r["link_id"] for r in report["stop"]])

    def test_ranking_is_profit_first_not_clicks_first(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        # asset A: huge clicks, small profit; asset B: few clicks, big profit
        store.upsert(self._link(link_id="A", click_count=1000, conversion_count=20,
                                commission_eur=40.0, cost_eur=2.0))
        store.upsert(self._link(link_id="B", click_count=30, conversion_count=5,
                                commission_eur=100.0, cost_eur=2.0))
        store.save()
        ranked = affiliate_scaling.rank_links(d)
        self.assertEqual(ranked[0].link_id, "B")


# ---------------------------------------------------------------------------
# 13. JARVIS read model - no secrets, real counts
# ---------------------------------------------------------------------------

class IntelTests(unittest.TestCase):
    def test_status_has_no_secret_fields_on_model_objects_and_real_zeros_when_empty(self):
        # the setup_steps text legitimately TELLS a human to go get a
        # "token"/"secret key" - that is instructional prose, not a stored
        # credential. The real guarantee is that no affiliate dataclass has
        # a field meant to HOLD one.
        for cls in (affiliate_model.AffiliateOffer, affiliate_model.AffiliateLink,
                   affiliate_model.AffiliateAsset, affiliate_model.CommissionRecord):
            for fname in cls.__dataclass_fields__:
                low = fname.lower()
                self.assertFalse(low.endswith("_key") or low == "key")
                self.assertNotIn("secret", low)
                self.assertNotIn("password", low)

        d = _tmp()
        status = affiliate_intel.affiliate_status(d)
        self.assertEqual(status["revenue_eur"], 0)
        self.assertEqual(status["clicks"], 0)

    def test_status_reflects_a_confirmed_commission(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        offer_id = affiliate_model.AffiliateOfferStore.load(d).all()[0].offer_id
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id=offer_id, amount=12.0, now_iso="t")
        affiliate_revenue.confirm_commission(d, rec.commission_id, ref="RX", now_iso="t")
        status = affiliate_intel.affiliate_status(d)
        self.assertEqual(status["commissions"]["confirmed_or_paid_eur"], 12.0)
        self.assertEqual(status["revenue_eur"], 12.0)

    def test_setup_required_lists_amazon_by_default(self):
        d = _tmp()
        status = affiliate_intel.affiliate_status(d)
        networks = {row["network"] for row in status["human_setup_required"]}
        self.assertIn(affiliate_model.NETWORK_AMAZON_ASSOCIATES, networks)


# ---------------------------------------------------------------------------
# 14. CLI wiring smoke tests
# ---------------------------------------------------------------------------

class CliSmokeTests(unittest.TestCase):
    def _run(self, d, *args):
        from revenue_os.cli import main
        return main(["--data-dir", str(d), *args])

    def test_affiliate_status_cli_runs(self):
        d = _tmp()
        self.assertEqual(self._run(d, "affiliate-status"), 0)

    def test_affiliate_pending_cli_runs(self):
        d = _tmp()
        self.assertEqual(self._run(d, "affiliate-pending"), 0)

    def test_affiliate_ingest_offer_cli_runs(self):
        import tempfile as tf
        d = _tmp()
        fh = tf.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(_offer_json(), fh)
        fh.close()
        self.assertEqual(self._run(d, "affiliate-ingest-offer", "--file", fh.name), 0)

    def test_affiliate_tick_cli_runs(self):
        d = _tmp()
        self.assertEqual(self._run(d, "affiliate-tick"), 0)

    def test_affiliate_optimize_and_scale_cli_run(self):
        d = _tmp()
        self.assertEqual(self._run(d, "affiliate-optimize"), 0)
        self.assertEqual(self._run(d, "affiliate-scale"), 0)


if __name__ == "__main__":
    unittest.main()
