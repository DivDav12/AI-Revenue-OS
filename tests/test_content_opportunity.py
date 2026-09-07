"""Content Opportunity Engine.

Covers: relevance/buyer-intent scoring reuses the EXISTING, unmodified
`affiliate_matching` engine (no parallel matching logic), competition is
always reported as real "no data source" rather than a guessed number,
recommend_page is fail-closed (below-threshold and irrelevant real
signals are never recommended - including the exact real false-positive
class found live: a generic marketing-tagline product_name spilling
individual filler words like "all"/"one"/"business" into the relevance
signal), duplicate prevention (a topic that already has a deployed asset
is never re-recommended), synthetic/editorial-pick records are excluded
(only independently-arising real demand counts), and determinism.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import content_opportunity as co
from revenue_os.ecosystem import editorial
from revenue_os.ecosystem.affiliate_model import AffiliateOffer, AffiliateOfferStore, CommissionModel
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta
from revenue_os.ecosystem import model
from revenue_os.opportunity_store import load_opportunities


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


class _OneDraftSource:
    def __init__(self, draft: OpportunityDraft) -> None:
        self._draft = draft
        self.meta = draft.source_meta

    def discover(self, limit: int):
        return [self._draft]


def _real_draft(title: str, *, evidence=None, category: str = "other") -> OpportunityDraft:
    meta = SourceMeta(source="hn-algolia", source_type="demand_signal",
                      access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
                      policy_status=model.POLICY_OK)
    return OpportunityDraft(
        title=title, description="", opportunity_type=model.TYPE_AFFILIATE,
        evidence=list(evidence) if evidence is not None else [title],
        source_meta=meta, category=category, demand_hint=0.5,
        raw={"buyer_confidence": {"total": 0.6}, "problem_confidence": {"total": 0.4}})


def _offer(**kw) -> AffiliateOffer:
    base = dict(
        offer_id="aff-systeme", network="systeme_io", program_name="systeme.io Affiliate Program",
        product_name="systeme.io", product_price=17.0, currency="USD",
        commission=CommissionModel(kind="recurring_percent", rate=0.6),
        category="online-business-platform",
        keywords=("funnel", "funnels", "sales funnel", "funnel builder", "webinar", "crm", "pipelines"),
        status=model.POLICY_OK, active=True,
    )
    base.update(kw)
    return AffiliateOffer(**base)


class ScoringReusesExistingMatchingTests(unittest.TestCase):
    def test_relevant_real_demand_scores_and_recommends(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("looking for a good funnel builder for my online course",
                           category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertGreater(c.relevance_to_offer, 0.0)
        self.assertGreater(c.buyer_intent, 0.0)
        self.assertTrue(c.recommend_page)
        self.assertIn("funnel", c.matched_terms)

    def test_unrelated_real_demand_never_recommended(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("PDF Editor for Windows 10 recommendations", category="software")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].relevance_to_offer, 0.0)
        self.assertFalse(candidates[0].recommend_page)

    def test_generic_filler_word_product_name_never_creates_a_false_positive(self):
        # regression guard for the exact real bug found live: a marketing
        # tagline product_name ("...all-in-one...online business
        # platform...") tokenizes into "all"/"one"/"business"/"platform" -
        # generic words that matched totally unrelated real demand
        # ("Google My Business verification...", relevance 0.75).
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer(product_name="Acme - all-in-one business platform for everyone"))
        store.save()
        draft = _real_draft(
            "Google My Business verification for vital health service unsuccessful",
            category="local-seo")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].recommend_page,
                         f"false positive: {candidates[0].matched_terms}")


class CompetitionNeverFabricatedTests(unittest.TestCase):
    def test_competition_estimate_is_always_none_never_a_guessed_number(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("looking for a good funnel builder", category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(candidates[0].competition_estimate, None)
        self.assertIn("no real", candidates[0].competition_note)


class DuplicatePreventionTests(unittest.TestCase):
    def test_opportunity_with_an_existing_deployed_asset_is_never_recommended(self):
        from revenue_os.ecosystem.affiliate_model import AffiliateAsset, AffiliateAssetStore

        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("looking for a good funnel builder", category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        oid = load_opportunities(d).all()[0]["id"]

        astore = AffiliateAssetStore.load(d)
        astore.upsert(AffiliateAsset(asset_id="asset-x", opportunity_id=oid, offer_id="aff-systeme",
                                     live_url="https://example.test/x"))
        astore.save()

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].already_has_a_page)
        self.assertFalse(candidates[0].recommend_page)

    def test_a_qc_failed_not_yet_deployed_asset_never_permanently_blocks_a_retry(self):
        # regression: a topic whose ONLY past attempt failed the quality
        # gate (e.g. the offer had no evidence yet at build time) must
        # stay retryable once the offer is fixed - it must never be
        # treated the same as a topic that already has a real page.
        from revenue_os.ecosystem.affiliate_model import AffiliateAsset, AffiliateAssetStore

        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("looking for a good funnel builder", category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)
        oid = load_opportunities(d).all()[0]["id"]

        astore = AffiliateAssetStore.load(d)
        astore.upsert(AffiliateAsset(
            asset_id="asset-failed", opportunity_id=oid, offer_id="aff-systeme",
            live_url="",  # never deployed
            quality_checks={"meets_min_words": True, "has_disclosure": True,
                            "has_cta": True, "has_evidence": False,
                            "has_demand_quote": True}))
        astore.save()

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].already_has_a_page)
        self.assertTrue(candidates[0].recommend_page)


class ExclusionTests(unittest.TestCase):
    def test_synthetic_records_are_excluded(self):
        from revenue_os.ecosystem.sources import SyntheticSource

        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        DiscoveryEngine(d, sources=[SyntheticSource(seed=1)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(candidates, [])

    def test_editorial_picks_are_excluded_never_treated_as_demand_evidence(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        editorial.ingest_editorial_pick(
            d, title="Choosing a funnel builder", description="A buying guide.",
            note="Commonly searched category.", category="online-business-platform")

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(candidates, [])

    def test_unknown_offer_id_returns_empty_not_a_crash(self):
        d = _tmp()
        self.assertEqual(co.find_content_opportunities(d, offer_id="does-not-exist"), [])


class ReportAndDeterminismTests(unittest.TestCase):
    def test_report_is_honest_when_nothing_qualifies(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        report = co.content_opportunity_report(d, offer_id="aff-systeme")
        self.assertEqual(report["recommended_count"], 0)
        self.assertTrue(report["note"])

    def test_deterministic_same_data_same_result(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        draft = _real_draft("looking for a good funnel builder", category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        a = co.content_opportunity_report(d, offer_id="aff-systeme")
        b = co.content_opportunity_report(d, offer_id="aff-systeme")
        self.assertEqual(a, b)

    def test_no_fabricated_evidence_comes_straight_from_the_real_draft(self):
        d = _tmp()
        store = AffiliateOfferStore.load(d)
        store.upsert(_offer())
        store.save()
        real_quote = "Is there any all-in-one funnel builder you'd actually recommend?"
        draft = _real_draft(real_quote, evidence=[real_quote], category="online-business-platform")
        DiscoveryEngine(d, sources=[_OneDraftSource(draft)]).run(limit_per_source=5)

        candidates = co.find_content_opportunities(d, offer_id="aff-systeme")
        self.assertEqual(candidates[0].demand_evidence, (real_quote,))


if __name__ == "__main__":
    unittest.main()
