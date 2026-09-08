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

    def test_preserve_exact_url_defaults_false(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).all()[0]
        self.assertFalse(offer.preserve_exact_url)

    def test_preserve_exact_url_true_is_ingested(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _offer_json(preserve_exact_url=True))
        offer = affiliate_model.AffiliateOfferStore.load(d).all()[0]
        self.assertTrue(offer.preserve_exact_url)

    def test_preserve_exact_url_must_be_boolean(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(
                _tmp(), _offer_json(preserve_exact_url="yes"))

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

    def test_page_has_mobile_friendly_viewport_meta(self):
        draft = _demand_draft()
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/abc")
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)

    def test_page_has_a_breadcrumb_linking_to_its_real_category(self):
        draft = _demand_draft()
        match = self._match()
        match.offer.category = "usb-microphone-streaming"
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=match, cta_url="https://example.test/go/abc")
        self.assertIn('class="breadcrumb"', page)
        self.assertIn('href="/kategorie/mikrofone/"', page)
        self.assertIn(">Mikrofone<", page)

    def test_unmapped_category_breadcrumb_falls_back_to_sonstiges(self):
        draft = _demand_draft()
        match = self._match()   # category defaults to "other"
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=match, cta_url="https://example.test/go/abc")
        self.assertIn('href="/kategorie/sonstiges/"', page)

    def test_breadcrumb_start_link_is_prefixed_under_a_real_project_deploy(self):
        # regression: a GitHub Pages PROJECT site (served at
        # https://owner.github.io/<repo>/) needs the guide page's own
        # breadcrumb "Start" link (and its category link) prefixed with
        # that repo subpath too - a bare href="/" 404s there.
        real_env = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
        base = "https://DivDav12.github.io/AI-Revenue-OS"
        page, _ = affiliate_assets.render_comparison_page(
            draft=_demand_draft(), match=self._match(),
            cta_url="https://example.test/go/abc", environ=real_env)
        self.assertIn(f'<a href="{base}/">Start</a>', page)
        self.assertIn(f'href="{base}/kategorie/', page)
        self.assertNotIn('<a href="/">Start</a>', page)

    def test_breadcrumb_start_link_stays_root_relative_without_real_config(self):
        page, _ = affiliate_assets.render_comparison_page(
            draft=_demand_draft(), match=self._match(), cta_url="https://example.test/go/abc")
        self.assertIn('<a href="/">Start</a>', page)

    def test_curated_guide_title_avoids_a_redundant_meta_description(self):
        # regression guard for a real bug found live: an editorial title
        # that already restated the product name produced a duplicated,
        # broken <title>/description once wrapped by the new headline
        # pattern - a curated guide_title must be used for the meta
        # description too, not just the on-page headline.
        draft = _demand_draft(title="systeme.io: does it solve building a sales funnel?")
        match = self._match()
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=match, cta_url="https://example.test/go/abc",
            guide_title="Sales-Funnel-Software für Anfänger")
        self.assertIn(
            '<meta name="description" content="Acme Cloud Hosting: '
            'Sales-Funnel-Software für Anfänger">', page)
        self.assertNotIn("does it solve", page)

    def test_related_links_rendered_when_supplied(self):
        draft = _demand_draft()
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/abc",
            related_links=(("What is a sales funnel?", "https://example.test/what-is-a-sales-funnel/"),))
        self.assertIn("https://example.test/what-is-a-sales-funnel/", page)
        self.assertIn("What is a sales funnel?", page)

    def test_no_related_links_section_when_none_supplied(self):
        # byte-identical to before this parameter existed for the default case.
        draft = _demand_draft()
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/abc")
        self.assertNotIn("More guides", page)

    def test_related_links_persist_and_survive_a_redeploy(self):
        d = _tmp()
        match = self._match()
        links = (("What is a sales funnel?", "https://example.test/what-is-a-sales-funnel/"),)
        asset, ok, _ = affiliate_assets.build_asset(
            d, opportunity_id="opRel", draft=_demand_draft(), match=match,
            cta_url="https://example.test/go/x", related_links=links)
        self.assertTrue(ok)
        self.assertEqual(asset.related_links, links)

        reloaded = affiliate_model.AffiliateAssetStore.load(d).get(asset.asset_id)
        self.assertEqual(reloaded.related_links, links)

        out = affiliate_assets.deploy_asset(asset=reloaded, draft=_demand_draft(), match=match,
                                            cta_url="https://example.test/go/x",
                                            adapter=FakeDeploymentAdapter())
        self.assertTrue(out["deployed"])

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

    def _preserve_exact_asset_and_match(self, d, *, url: str):
        match = affiliate_matching.AffiliateMatch(
            offer=affiliate_model.AffiliateOffer(
                offer_id="o-systeme", network="systeme_io", program_name="P",
                product_name="X", product_url=url, preserve_exact_url=True,
                status=model.POLICY_OK),
            match_score=0.5, demand_strength=0.5)
        asset, _, _ = affiliate_assets.build_asset(
            d, opportunity_id="opPreserve", draft=_demand_draft(), match=match, cta_url="")
        return asset, match

    def test_preserve_exact_url_never_appends_subid(self):
        d = _tmp()
        url = "https://systeme.io/de?sa=sa0280859903879bd9c30e8335e36983c5a1ffb0de"
        asset, match = self._preserve_exact_asset_and_match(d, url=url)
        link = affiliate_links.create_link(d, opportunity_id="opPreserve", asset=asset,
                                           match=match, source="own_blog")
        self.assertEqual(link.target_url, url)

    def test_preserve_exact_url_wins_even_if_tracking_param_were_set(self):
        # belt-and-braces: preserve_exact_url must win over tracking_param
        # too, not just the bare subid fallback.
        d = _tmp()
        url = "https://systeme.io/de?sa=real-id"
        match = affiliate_matching.AffiliateMatch(
            offer=affiliate_model.AffiliateOffer(
                offer_id="o-systeme2", network="systeme_io", program_name="P",
                product_name="X", product_url=url, preserve_exact_url=True,
                tracking_param="ref", status=model.POLICY_OK),
            match_score=0.5, demand_strength=0.5)
        asset, _, _ = affiliate_assets.build_asset(
            d, opportunity_id="opPreserve2", draft=_demand_draft(), match=match, cta_url="")
        link = affiliate_links.create_link(d, opportunity_id="opPreserve2", asset=asset,
                                           match=match, source="own_blog")
        self.assertEqual(link.target_url, url)

    def test_default_offer_without_tracking_param_still_appends_subid(self):
        # regression guard: preserve_exact_url defaults to False - every
        # existing offer's behaviour is byte-identical to before this field
        # existed.
        d = _tmp()
        asset, match = self._asset_and_match(d)   # tracking_param="ref" already set
        link = affiliate_links.create_link(d, opportunity_id="opX2", asset=asset,
                                           match=match, source="own_blog")
        self.assertIn("ref=", link.target_url)


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

    def test_healthz_returns_200_and_reveals_no_click_data(self):
        server = affiliate_tracking_server.serve(_tmp(), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b"ok")
        finally:
            server.shutdown()
            thread.join(timeout=2)

    def test_concurrent_clicks_are_all_recorded_no_lost_updates(self):
        # regression guard for a real race condition: record_click()'s
        # load -> increment -> save is not atomic on its own: without the
        # lock, near-simultaneous requests can silently lose a click.
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        link = affiliate_model.AffiliateLink(link_id="l1", opportunity_id="op", asset_id="a",
                                             offer_id="o", tracking_id="trk-concurrent",
                                             target_url="https://merchant.example/prod")
        store.upsert(link)
        store.save()

        server = affiliate_tracking_server.serve(d, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        n = 25
        try:
            port = server.server_address[1]

            def _hit():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/go/trk-concurrent")
                resp = conn.getresponse()
                resp.read()
                conn.close()

            workers = [threading.Thread(target=_hit) for _ in range(n)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=5)
        finally:
            server.shutdown()
            thread.join(timeout=2)

        self.assertEqual(affiliate_model.AffiliateLinkStore.load(d).get("l1").click_count, n)
        self.assertEqual(len(affiliate_model.ClickStore.load(d).by_link("l1")), n)

    def test_run_forever_starts_serves_and_closes_cleanly(self):
        from unittest import mock

        fake_server = mock.Mock()
        fake_server.server_port = 8788
        with mock.patch.object(affiliate_tracking_server, "serve", return_value=fake_server) as m:
            affiliate_tracking_server.run_forever(_tmp(), host="127.0.0.1", port=8788)
        m.assert_called_once_with(mock.ANY, host="127.0.0.1", port=8788)
        fake_server.serve_forever.assert_called_once()
        fake_server.server_close.assert_called_once()

    def test_run_forever_cli_wiring(self):
        from unittest import mock

        from revenue_os.cli import main
        with mock.patch("revenue_os.ecosystem.affiliate_tracking_server.run_forever") as m:
            rc = main(["--data-dir", str(_tmp()), "serve-affiliate-tracker", "--port", "8788"])
        self.assertEqual(rc, 0)
        m.assert_called_once()


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
# 13a. traffic-engine readiness read model
# ---------------------------------------------------------------------------

class TrafficReadinessTests(unittest.TestCase):
    def test_empty_state_is_all_waiting_paid_ads_always_disabled(self):
        d = _tmp()
        r = affiliate_intel.traffic_readiness(d)
        self.assertEqual(r["content_assets"], {"deployed": 0, "waiting": 0})
        self.assertEqual(r["traffic_channels"]["seo"], "WAITING")
        self.assertEqual(r["traffic_channels"]["organic_communities"], "WAITING")
        self.assertEqual(r["traffic_channels"]["paid_ads"], "DISABLED")
        self.assertEqual(r["conversions_confirmed"], 0)
        self.assertEqual(r["revenue_confirmed_eur"], 0)

    def test_deployed_asset_makes_seo_ready_and_lists_the_real_url(self):
        d = _tmp()
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        oid = next(r["id"] for r in load_opportunities(d).all()
                  if r["discovery"]["opportunity_type"] == model.TYPE_AFFILIATE)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        eco_pipeline.select(d, oid)
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            eco_pipeline.plan(d, oid)

        r = affiliate_intel.traffic_readiness(d)
        self.assertEqual(r["content_assets"]["deployed"], 1)
        self.assertEqual(r["traffic_channels"]["seo"], "READY")
        self.assertEqual(r["traffic_channels"]["organic_communities"], "READY")
        self.assertEqual(len(r["deployed_page_urls"]), 1)
        self.assertTrue(r["deployed_page_urls"][0].startswith("https://"))

    def test_click_tracking_reported_active_only_when_the_chain_says_so(self):
        d = _tmp()
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        oid = next(r["id"] for r in load_opportunities(d).all()
                  if r["discovery"]["opportunity_type"] == model.TYPE_AFFILIATE)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        eco_pipeline.select(d, oid)
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()), \
             mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AFFILIATE_TRACKING_BASE_URL", None)
            eco_pipeline.plan(d, oid)

        r = affiliate_intel.traffic_readiness(d)
        self.assertIn("not_active", r["tracking"]["affiliate_click_tracking"])

    def test_revenue_never_inferred_from_clicks(self):
        d = _tmp()
        store = affiliate_model.AffiliateLinkStore.load(d)
        store.upsert(affiliate_model.AffiliateLink(
            link_id="l1", opportunity_id="op", asset_id="a", offer_id="o",
            click_count=1000, conversion_count=50))   # lots of real clicks, zero settled commission
        store.save()
        r = affiliate_intel.traffic_readiness(d)
        self.assertEqual(r["revenue_confirmed_eur"], 0)
        self.assertEqual(r["conversions_confirmed"], 0)

    def test_cli_traffic_readiness_runs(self):
        from revenue_os.cli import main
        self.assertEqual(main(["--data-dir", str(_tmp()), "traffic-readiness"]), 0)


# ---------------------------------------------------------------------------
# 13b. per-opportunity funnel status - READY vs WAITING, never fabricated
# ---------------------------------------------------------------------------

class AffiliateFunnelStatusTests(unittest.TestCase):
    def _seed(self, d) -> str:
        draft = _demand_draft()
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        rec = next(r for r in load_opportunities(d).all()
                  if r["discovery"]["opportunity_type"] == model.TYPE_AFFILIATE)
        return rec["id"]

    def test_unknown_opportunity_raises(self):
        d = _tmp()
        with self.assertRaises(affiliate_intel.FunnelStatusError):
            affiliate_intel.affiliate_funnel_status(d, "does-not-exist")

    def test_before_any_plan_everything_not_ready(self):
        d = _tmp()
        oid = self._seed(d)
        status = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertFalse(status["ready"]["offer_selected"])
        self.assertFalse(status["ready"]["asset_generated"])
        self.assertFalse(status["ready"]["page_deployed"])
        self.assertIn("no AFFILIATE chain", status["next_action"])

    def test_after_full_chain_ready_true_and_waiting_zero_no_fabricated_revenue(self):
        d = _tmp()
        oid = self._seed(d)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        eco_pipeline.select(d, oid)
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            eco_pipeline.plan(d, oid)

        status = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertTrue(status["ready"]["demand_source_real"])
        self.assertTrue(status["ready"]["offer_selected"])
        self.assertTrue(status["ready"]["offer_usable"])
        self.assertTrue(status["ready"]["asset_generated"])
        self.assertTrue(status["ready"]["page_deployed"])
        self.assertTrue(status["ready"]["affiliate_url_configured"])
        self.assertEqual(status["waiting_on"]["real_traffic"], 0)
        self.assertEqual(status["waiting_on"]["real_affiliate_conversion"], 0)
        self.assertEqual(status["waiting_on"]["commission_confirmation_eur"], 0)
        self.assertIn("waiting for a real visitor", status["next_action"])
        self.assertTrue(status["asset_live_url"])
        self.assertEqual(status["offer"]["affiliate_url"], "https://acme.example/hosting?ref=base")

    def test_synthetic_demand_never_reported_as_real(self):
        d = _tmp()
        from revenue_os.ecosystem.sources import SyntheticSource
        DiscoveryEngine(d, sources=[SyntheticSource(seed=1)]).run(limit_per_source=3)
        rec = load_opportunities(d).all()[0]
        status = affiliate_intel.affiliate_funnel_status(d, rec["id"])
        self.assertFalse(status["ready"]["demand_source_real"])

    def test_a_real_click_and_confirmed_commission_show_up_as_waiting_resolved(self):
        d = _tmp()
        oid = self._seed(d)
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())
        eco_pipeline.evaluate(d, oid)
        eco_pipeline.select(d, oid)
        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            eco_pipeline.plan(d, oid)

        rec = load_opportunities(d).get(oid)
        plan = rec["strategy"]["plan"]
        link_id = plan["link_id"]
        offer_id = plan["match"]["offer_id"]

        affiliate_links.record_click(d, link_id=link_id, channel="own_blog", now_iso="t")
        status_after_click = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertEqual(status_after_click["waiting_on"]["real_traffic"], 1)
        self.assertIn("waiting for a real conversion", status_after_click["next_action"])

        link_store = affiliate_model.AffiliateLinkStore.load(d)
        link = link_store.get(link_id)
        link.conversion_count += 1
        link_store.upsert(link)
        link_store.save()
        status_after_conv = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertEqual(status_after_conv["waiting_on"]["real_affiliate_conversion"], 1)
        self.assertIn("confirm it via the affiliate network", status_after_conv["next_action"])

        pending = affiliate_revenue.record_pending_commission(
            d, link_id=link_id, opportunity_id=oid, offer_id=offer_id, amount=60.0, now_iso="t")
        affiliate_revenue.confirm_commission(d, pending.commission_id, ref="REF1", now_iso="t")
        status_after_commission = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertEqual(status_after_commission["waiting_on"]["commission_confirmation_eur"], 60.0)
        self.assertEqual(status_after_commission["next_action"], "commission confirmed - nothing pending")

    def test_cli_affiliate_funnel_runs(self):
        from revenue_os.cli import main
        d = _tmp()
        oid = self._seed(d)
        self.assertEqual(main(["--data-dir", str(d), "affiliate-funnel", oid]), 0)

    def test_cli_affiliate_funnel_unknown_id_fails_closed(self):
        from revenue_os.cli import main
        d = _tmp()
        self.assertEqual(main(["--data-dir", str(d), "affiliate-funnel", "nope"]), 1)


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


# ---------------------------------------------------------------------------
# 15. real Amazon Affiliate Loop (first live integration) - schema, partner
# id handling, URL/ASIN fail-closed checks, observed/timestamped price,
# no self-purchase/fake-click path, no URL obfuscation.
# ---------------------------------------------------------------------------

def _amazon_offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "network": "amazon_associates",
        "program_name": "Amazon.de PartnerNet",
        "product_name": "JBL Quantum Stream Talk",
        "product_url": "https://www.amazon.de/JBL-Quantum-Stream-Talk-super-kardioidem/dp/B0CQP5NL72",
        "product_asin": "B0CQP5NL72",
        "product_price": 39.99,
        "currency": "EUR",
        "price_is_estimate": True,
        "price_observed_at": "2026-09-05",
        "price_source_note": "Corroborated via third-party price comparison, not live-scraped from Amazon.",
        "commission_kind": "percent",
        "commission_rate": 0.03,
        "commission_evidence": [
            "Amazon PartnerNet standard fee schedule, retrieved 2026-09-05: "
            "catch-all 'Alle anderen Kategorien' rate is 3.0%."
        ],
        "cookie_duration_days": 1,
        "category": "usb-microphone-streaming",
        "keywords": ["microphone", "mikrofon", "usb-mikrofon", "streaming", "discord",
                    "gaming", "podcast", "creator", "home-office"],
        "evidence": [
            "Amazon.de product page (verified 2026-09-05): super-cardioid pickup "
            "pattern for single-voice recording, multi-function mute/gain control knob",
            "Product listing: includes JBL QuantumENGINE PC software (EQ, noise "
            "reduction, mic test), 3.5mm headphone monitoring jack",
        ],
        "human_confirmed_joined": True,
        "tracking_param": "tag",
        "tracking_value": "airevenue-21",
    }
    base.update(overrides)
    return base


class AmazonAffiliateLoopTests(unittest.TestCase):
    def test_real_amazon_offer_ingests_and_is_usable(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        self.assertEqual(out["status"], model.POLICY_OK)
        self.assertTrue(out["usable"])
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertEqual(offer.product_asin, "B0CQP5NL72")
        self.assertEqual(offer.price_observed_at, "2026-09-05")

    def test_non_amazon_domain_url_fails_closed(self):
        d = _tmp()
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(
                d, _amazon_offer_json(product_url="https://bit.ly/xyz123"))

    def test_shortener_style_redirector_fails_closed(self):
        d = _tmp()
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(
                d, _amazon_offer_json(product_url="https://amzn.to/abc123"))

    def test_wrong_asin_format_fails_closed(self):
        d = _tmp()
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(
                d, _amazon_offer_json(product_asin="not-a-real-asin"))

    def test_asin_not_matching_url_fails_closed(self):
        d = _tmp()
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(
                d, _amazon_offer_json(product_asin="B0000000ZZ"))

    def test_missing_product_url_fails_closed(self):
        d = _tmp()
        payload = _amazon_offer_json()
        payload["product_url"] = ""
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(d, payload)

    def test_price_is_never_stored_as_a_hard_fact(self):
        # spec: "Preis nicht als dauerhaft/fest speichern" - the price
        # must always carry is_estimate + an observation timestamp, never
        # look like a durable, source-guaranteed number.
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertTrue(offer.price_is_estimate)
        self.assertTrue(offer.price_observed_at)
        self.assertTrue(offer.price_source_note)

    def test_link_uses_the_real_static_partner_tag_not_an_internal_id(self):
        # THE core correctness property for a real Amazon link: the tag=
        # value must be the human's actual, pre-registered Associates id -
        # never a fresh per-link id we invented (Amazon does not support
        # inventing tag values on the fly, and the spec explicitly forbids
        # "personenbezogene dynamische Subtag-Zuweisung").
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        match = affiliate_matching.AffiliateMatch(offer=offer, match_score=0.8, demand_strength=0.6)
        draft = _demand_draft(category="usb-microphone-streaming",
                              title="Good affordable USB microphone for streaming and Discord")
        asset, ok, _ = affiliate_assets.build_asset(d, opportunity_id="op-amz", draft=draft,
                                                    match=match, cta_url="")
        self.assertTrue(ok)
        link = affiliate_links.create_link(d, opportunity_id="op-amz", asset=asset,
                                           match=match, source="own_site")
        self.assertEqual(link.target_url,
                         "https://www.amazon.de/JBL-Quantum-Stream-Talk-super-kardioidem/"
                         "dp/B0CQP5NL72?tag=airevenue-21")
        self.assertNotIn(link.tracking_id, link.target_url)

    def test_link_target_stays_on_amazon_no_obfuscation(self):
        from urllib.parse import urlparse
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        match = affiliate_matching.AffiliateMatch(offer=offer, match_score=0.8, demand_strength=0.6)
        draft = _demand_draft(category="usb-microphone-streaming")
        asset, _, _ = affiliate_assets.build_asset(d, opportunity_id="op-amz2", draft=draft,
                                                   match=match, cta_url="")
        link = affiliate_links.create_link(d, opportunity_id="op-amz2", asset=asset,
                                           match=match, source="own_site")
        host = urlparse(link.target_url).netloc.lower()
        self.assertIn(host, ("www.amazon.de", "amazon.de"))

    def test_matches_streaming_discord_demand_by_keyword_overlap(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        draft = _demand_draft(
            title="Is there a good USB microphone for Discord and streaming?",
            description="Looking for something plug-and-play for home-office calls.",
            category="usb-microphone-streaming", evidence=[])
        best = affiliate_matching.best_usable_match(draft, [offer])
        self.assertIsNotNone(best)
        self.assertEqual(best.offer.offer_id, offer.offer_id)

    def test_unrelated_demand_does_not_match_the_microphone_offer(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        draft = _demand_draft(title="Is there a tool for managing Kubernetes clusters?",
                              description="Need something for cluster ops.",
                              category="devops", evidence=[])
        self.assertIsNone(affiliate_matching.best_usable_match(draft, [offer]))

    def test_full_attribution_chain_for_the_real_offer(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        match = affiliate_matching.AffiliateMatch(offer=offer, match_score=0.7, demand_strength=0.5)
        draft = _demand_draft(category="usb-microphone-streaming",
                              title="Good affordable USB microphone for streaming and Discord",
                              evidence=[])
        asset, ok, _ = affiliate_assets.build_asset(
            d, opportunity_id="op-chain", draft=draft, match=match, cta_url="",
            guide_title="Bestes USB-Mikrofon fuer Streaming & Discord")
        self.assertTrue(ok)
        link = affiliate_links.create_link(d, opportunity_id="op-chain", asset=asset,
                                           match=match, source="own_site")
        click = affiliate_links.record_click(d, tracking_id=link.tracking_id,
                                             channel="own_site", now_iso="2026-09-05T12:00:00")
        self.assertTrue(click["recorded"])
        self.assertEqual(click["target_url"], link.target_url)

        commission = affiliate_revenue.record_pending_commission(
            d, link_id=link.link_id, opportunity_id="op-chain", offer_id=offer.offer_id,
            amount=1.20, is_estimate=True, now_iso="2026-09-05T12:05:00")
        summary = affiliate_revenue.opportunity_commission_summary(d, "op-chain")
        self.assertEqual(summary["pending_estimated_eur"], 1.20)
        self.assertEqual(summary["confirmed_or_paid_eur"], 0.0)
        # full chain is joinable end to end
        self.assertEqual(link.opportunity_id, "op-chain")
        self.assertEqual(link.asset_id, asset.asset_id)
        self.assertEqual(link.offer_id, offer.offer_id)
        self.assertEqual(commission.link_id, link.link_id)

    def test_no_self_purchase_or_fake_click_mechanism_exists(self):
        # there is no function anywhere in the affiliate module surface
        # that simulates/injects a click or a purchase without going
        # through the real, explicit record_click (a real HTTP hit) or a
        # human-supplied confirm_commission call with a real --ref.
        import inspect
        for mod in (affiliate_links, affiliate_revenue):
            names = {n for n, _ in inspect.getmembers(mod, inspect.isfunction)}
            for forbidden in ("simulate_click", "fake_click", "auto_click",
                             "simulate_purchase", "fake_purchase", "auto_convert"):
                self.assertNotIn(forbidden, names)

    def test_confirm_commission_requires_a_stable_ref_not_a_guess(self):
        # guards against a "just mark it paid" shortcut that could be
        # used to fabricate revenue without a real network reference.
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        rec = affiliate_revenue.record_pending_commission(
            d, link_id="l1", opportunity_id="op1", offer_id=offer.offer_id,
            amount=1.20, now_iso="t")
        with self.assertRaises(TypeError):
            affiliate_revenue.confirm_commission(d, rec.commission_id, now_iso="t")  # missing ref=

    def test_asset_content_has_no_fabricated_reviews_or_star_ratings(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _amazon_offer_json())
        offer = affiliate_model.AffiliateOfferStore.load(d).get(out["offer_id"])
        match = affiliate_matching.AffiliateMatch(offer=offer, match_score=0.7, demand_strength=0.5)
        draft = _demand_draft(category="usb-microphone-streaming", evidence=[])
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=match, cta_url="https://www.amazon.de/dp/B0CQP5NL72?tag=airevenue-21",
            guide_title="Bestes USB-Mikrofon fuer Streaming & Discord")
        low = page.lower()
        for forbidden in ("★", "5 stars", "5/5", "customers say", "verified purchase",
                         "amazing sound quality", "best mic i've ever",
                         "5 sterne", "kundenmeinung", "kundenbewertung", "klingt fantastisch"):
            self.assertNotIn(forbidden, low)
        self.assertIn("nicht selbst getestet", low)


if __name__ == "__main__":
    unittest.main()
