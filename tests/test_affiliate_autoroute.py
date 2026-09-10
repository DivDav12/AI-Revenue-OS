"""Autonomous affiliate routing (minimal scope).

A REAL, EXPLICIT-purchase-intent demand signal (ecosystem.demand_sources)
that the EXISTING demand-quality layer already promoted to
TYPE_DIGITAL_PRODUCT, whose EXISTING ProductIntent extraction found a
concrete product category, and which the EXISTING affiliate_matching gate
matches to a VALIDATED (usable) affiliate offer with a >= 0.5 match score
AND positive EXISTING affiliate_profitability projection, is routed to the
affiliate pipeline (`select().recommended == "AFFILIATE"`) and its
opportunity type is re-stamped to TYPE_AFFILIATE on the existing
`discovery` namespace.

Nothing here defines a new score, touches demand_ranking's advisory
buyer/problem confidence, or adds new state. Every negative case is
handled by an EXISTING gate:
  weak discussion       -> _infer_opportunity_type -> TYPE_OTHER -> verify REJECTED
  no offer / bad offer  -> best_usable_match -> None
  wrong product         -> match_offers score < 0.5
  low profitability     -> affiliate_profitability.expected_profit <= 0
  amazon_associates     -> excluded from autonomous selection (JBL guard)

No network: a fixture AcqSearchable, never the real fetchers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import demand_sources, model
from revenue_os.ecosystem import pipeline as eco_pipeline
from revenue_os.ecosystem.affiliate_model import (
    AffiliateOffer,
    AffiliateOfferStore,
    CommissionModel,
)
from revenue_os.ecosystem.affiliate_sources import ingest_affiliate_offer
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.opportunity_store import load_opportunities

_NOW = "2026-09-06T00:00:00+00:00"

# a real-shaped, EXPLICIT purchase-intent recommendation request
_STRONG_TITLE = ("Which PDF editor would you recommend for OCR and editing "
                 "scanned contracts? I will pay, Acrobat is too expensive")
_STRONG_TEXT = ("Acrobat subscription is too expensive, I would pay ~100 EUR "
                "for a one-time PDF editor license with OCR.")
_WEAK_TITLE = "Thoughts on the PDF format and Adobe"
_WEAK_TEXT = "Just a general discussion about PDF history and Adobe."


class _FixtureSource:
    def __init__(self, records: list[AcqRecord]) -> None:
        self._records = records

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        return list(self._records)[:limit]


def _record(title: str, text: str, oid: str = "40000001") -> AcqRecord:
    return AcqRecord(title=title, url=f"https://news.ycombinator.com/item?id={oid}",
                     text=text, author="x", posted_at="2026-09-01T00:00:00+00:00",
                     platform="Hacker News", source="hn-algolia", query="q")


def _pdf_offer_payload(**over) -> dict:
    base = {
        "schema_version": 1,
        "network": "generic_saas_program",
        "program_name": "ProPDF Partner Program",
        "product_name": "ProPDF Editor",
        "product_url": "https://propdf.example/buy?ref=base",
        "product_price": 129.99,
        "currency": "USD",
        "commission_kind": "fixed",
        "commission_fixed_amount": 80.0,
        "commission_evidence": ["ProPDF partner dashboard: 'USD 80 bounty per sale'"],
        "evidence": ["ProPDF product page: PDF editor with OCR, conversion, forms, e-sign"],
        "category": "pdf-editor",
        "keywords": ["pdf", "pdf editor", "edit pdf", "ocr", "convert pdf",
                     "acrobat", "acrobat alternative", "pdf forms"],
        "human_confirmed_joined": True,
        "tracking_param": "ref",
    }
    base.update(over)
    return base


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _discover(self, title: str, text: str, oid: str = "40000001") -> str:
        url = f"https://news.ycombinator.com/item?id={oid}"
        src = demand_sources.DemandDiscoverySource(
            "hn-algolia", _FixtureSource([_record(title, text, oid)]),
            queries=("q",), now_iso=_NOW)
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = next(r for r in load_opportunities(self.d).all()
                   if r["discovery"].get("source_url") == url)
        return rec["id"]

    def _select(self, oid: str) -> dict:
        eco_pipeline.evaluate(self.d, oid)
        return eco_pipeline.select(self.d, oid)

    def _type(self, oid: str) -> str:
        return load_opportunities(self.d).get(oid)["discovery"]["opportunity_type"]


class StrongDemandRoutesToAffiliate(_Base):
    def test_strong_intent_plus_matching_validated_offer_becomes_affiliate(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        # sanity: the EXISTING layers classified it as a strong product signal
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_DIGITAL_PRODUCT)
        self.assertIn(rec["discovery"]["verification"]["status"], model.PLANNABLE)
        self.assertEqual(rec["discovery"]["demand_evidence"]["intent_level"],
                         "EXPLICIT_PURCHASE_INTENT")
        self.assertEqual(rec["discovery"]["product_intent"]["category_phrase"], "pdf editor")

        ingest_affiliate_offer(self.d, _pdf_offer_payload())
        sel = self._select(oid)

        self.assertEqual(sel["recommended"], model.STRAT_AFFILIATE, sel["reason"])
        self.assertIn("match", sel["reason"])
        # opportunity type re-stamped on the EXISTING discovery namespace
        self.assertEqual(self._type(oid), model.TYPE_AFFILIATE)
        # verification verdict is untouched (still PLANNABLE)
        self.assertIn(load_opportunities(self.d).get(oid)["discovery"]["verification"]["status"],
                      model.PLANNABLE)

    def test_re_select_is_idempotent(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        ingest_affiliate_offer(self.d, _pdf_offer_payload())
        self.assertEqual(self._select(oid)["recommended"], model.STRAT_AFFILIATE)
        # second pass: type is now TYPE_AFFILIATE, still routes to AFFILIATE
        self.assertEqual(self._select(oid)["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_AFFILIATE)


class WeakDiscussionNeverRoutes(_Base):
    def test_weak_discussion_is_rejected_and_never_affiliate(self):
        oid = self._discover(_WEAK_TITLE, _WEAK_TEXT)
        ingest_affiliate_offer(self.d, _pdf_offer_payload())
        rec = load_opportunities(self.d).get(oid)
        # existing gate: no purchase intent -> TYPE_OTHER -> verify REJECTED
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_OTHER)
        self.assertNotIn(rec["discovery"]["verification"]["status"], model.PLANNABLE)
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_OTHER)


class MissingOfferDoesNotRoute(_Base):
    def test_no_offer_on_file_stays_non_affiliate(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        self.assertEqual(len(AffiliateOfferStore.load(self.d).all()), 0)
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_DIGITAL_PRODUCT)


class UnconfirmedOfferDoesNotRoute(_Base):
    def test_offer_not_human_confirmed_is_not_usable_and_not_selected(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        out = ingest_affiliate_offer(self.d, _pdf_offer_payload(human_confirmed_joined=False))
        self.assertFalse(out["usable"])
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_DIGITAL_PRODUCT)

    def test_inactive_offer_is_not_selected(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        ingest_affiliate_offer(self.d, _pdf_offer_payload())
        store = AffiliateOfferStore.load(self.d)
        o = store.all()[0]
        o.active = False
        store.upsert(o)
        store.save()
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)


class WrongProductDoesNotMatch(_Base):
    def test_unrelated_offer_never_routes_the_demand(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        ingest_affiliate_offer(self.d, _pdf_offer_payload(
            program_name="VPN Partner Program", product_name="FastVPN",
            product_url="https://fastvpn.example/buy?ref=base", category="vpn",
            keywords=["vpn", "privacy", "no-logs", "streaming unblock"]))
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_DIGITAL_PRODUCT)

    def test_single_keyword_coincidence_is_below_the_autonomous_threshold(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        # shares exactly ONE token ("pdf") and nothing else
        ingest_affiliate_offer(self.d, _pdf_offer_payload(
            program_name="Scanner App Partner", product_name="ScanBot",
            product_url="https://scanbot.example/buy?ref=base", category="scanner-app",
            keywords=["scanner", "document scanner", "pdf"]))
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)


class LowProfitabilityDoesNotAutoExecute(_Base):
    def test_matching_offer_with_weak_economics_is_not_auto_routed(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        # matches strongly on keywords, but the EXISTING
        # affiliate_profitability projection is <= 0 (tiny commission vs.
        # the fixed content cost) -> no autonomous action
        ingest_affiliate_offer(self.d, _pdf_offer_payload(
            commission_kind="percent", commission_rate=0.05,
            commission_fixed_amount=0.0, product_price=0.0,
            commission_evidence=["ProPDF dashboard: '5% per sale'"]))
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_DIGITAL_PRODUCT)


class AmazonAssociatesIsNeverAutoSelected(_Base):
    """`keine automatische Verwendung des JBL-Angebots` - Amazon Associates
    offers require manual, reviewed link placement per their Operating
    Agreement and are excluded from autonomous selection."""

    def test_amazon_associates_offer_matching_the_demand_is_not_routed(self):
        oid = self._discover(
            "Which USB microphone would you recommend for streaming? I will pay, my old one died",
            "My USB mic stopped working, I would pay ~80 EUR for a good streaming microphone.")
        # an Amazon offer that DOES match the demand on keywords
        ingest_affiliate_offer(self.d, {
            "schema_version": 1, "network": "amazon_associates",
            "program_name": "Amazon.de PartnerNet", "product_name": "StreamMic USB Microphone",
            "product_url": "https://www.amazon.de/dp/B0TEST0001", "product_asin": "B0TEST0001",
            "product_price": 79.99, "currency": "EUR",
            "commission_kind": "percent", "commission_rate": 0.03,
            "commission_evidence": ["Amazon PartnerNet standard fee schedule"],
            "evidence": ["Amazon listing: USB condenser microphone for streaming and podcasts"],
            "category": "usb-microphone", "keywords": ["microphone", "usb microphone",
                                                       "streaming", "podcast", "mic"],
            "human_confirmed_joined": True, "tracking_param": "tag", "tracking_value": "test-21",
        })
        sel = self._select(oid)
        self.assertNotEqual(sel["recommended"], model.STRAT_AFFILIATE)
        self.assertEqual(self._type(oid), model.TYPE_DIGITAL_PRODUCT)


class NightlyTickIntegration(_Base):
    def test_affiliate_tick_picks_up_the_routed_demand_and_runs_the_chain(self):
        from unittest import mock

        from revenue_os.deployment import FakeDeploymentAdapter
        from revenue_os.ecosystem.affiliate_pipeline import run_affiliate_tick

        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        ingest_affiliate_offer(self.d, _pdf_offer_payload())

        with mock.patch(
                "revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                return_value=FakeDeploymentAdapter()):
            out = run_affiliate_tick(self.d, limit=10, now_iso=_NOW)

        self.assertIn(oid, out["planned"], out)
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_AFFILIATE)
        plan = rec["strategy"]["plan"]
        self.assertEqual(plan["kind"], "affiliate_chain")
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["next_step_class"], "SAFE_AUTONOMOUS")

    def test_tick_leaves_weak_and_unmatched_demand_alone(self):
        from revenue_os.ecosystem.affiliate_pipeline import run_affiliate_tick

        weak = self._discover(_WEAK_TITLE, _WEAK_TEXT, oid="40000002")
        strong_no_offer = self._discover(_STRONG_TITLE, _STRONG_TEXT, oid="40000003")
        out = run_affiliate_tick(self.d, limit=10, now_iso=_NOW)
        self.assertEqual(out["planned"], [])
        self.assertNotIn(weak, out["attempted"])          # REJECTED, not PLANNABLE
        self.assertEqual(self._type(strong_no_offer), model.TYPE_DIGITAL_PRODUCT)


class ExistingBehaviourRegression(_Base):
    def test_type_affiliate_demand_still_routes_via_the_existing_path(self):
        # the pre-existing _prefer_affiliate_if_matched case: an already
        # TYPE_AFFILIATE draft + a matching usable offer -> AFFILIATE,
        # unchanged.
        from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta

        meta = SourceMeta(source="hn-algolia", source_type="demand_signal",
                          access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
                          requires_login=False, policy_status=model.POLICY_OK)
        draft = OpportunityDraft(
            title="Is there a tool for cheap VPS hosting for a side project?",
            description="I need cloud hosting that does not cost a fortune, a VPS.",
            opportunity_type=model.TYPE_AFFILIATE,
            evidence=["Is there a tool for cheap VPS hosting for a side project?"],
            source_meta=meta, source_id="9", discovered_at=_NOW, category="hosting",
            demand_hint=0.6, raw={"buyer_confidence": {"total": 0.55},
                                  "problem_confidence": {"total": 0.7}})
        from revenue_os.ecosystem.discovery import _draft_to_opportunity
        from revenue_os.ecosystem import verification

        opp = _draft_to_opportunity(draft, verification.verify(draft))
        store = load_opportunities(self.d)
        store.upsert(opp)
        store.record_discovery(opp.id, opp.discovery)
        store.save()

        ingest_affiliate_offer(self.d, {
            "schema_version": 1, "network": "generic_saas_program",
            "program_name": "Acme Hosting Affiliates", "product_name": "Acme Cloud Hosting",
            "product_url": "https://acme.example/hosting?ref=base", "product_price": 200.0,
            "currency": "EUR", "commission_kind": "recurring_percent", "commission_rate": 0.30,
            "commission_evidence": ["Acme dashboard: 30% recurring"],
            "evidence": ["Acme pricing page: EUR 200/mo business VPS"],
            "category": "hosting", "keywords": ["hosting", "server", "cloud", "vps"],
            "human_confirmed_joined": True, "tracking_param": "ref",
        })
        sel = self._select(opp.id)
        self.assertEqual(sel["recommended"], model.STRAT_AFFILIATE)

    def test_strong_product_demand_without_any_offer_still_picks_product(self):
        oid = self._discover(_STRONG_TITLE, _STRONG_TEXT)
        sel = self._select(oid)
        self.assertEqual(sel["recommended"], model.STRAT_PRODUCT)


if __name__ == "__main__":
    unittest.main()
