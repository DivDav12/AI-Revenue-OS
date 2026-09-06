"""Editorial Pick - human-authorized, proactive affiliate guide topic.

Covers: required-field validation (never silently defaulted), the
correct SourceMeta/raw shape (`source_type="editorial_pick"`, never
"demand_signal"/"human_fed"), real ingestion through the UNMODIFIED
discovery/verification pipeline (QUALIFIED, PLANNABLE), idempotent
re-ingestion, `affiliate_intel.affiliate_funnel_status()`'s `demand_basis`
correctly reporting "editorial_pick" (never "discovered_real"), and that
`affiliate_assets.render_comparison_page()` NEVER frames an editorial
note as "people have said, in their own words" - the same neutral
framing an opportunity with zero evidence already gets.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import (
    affiliate_assets,
    affiliate_intel,
    affiliate_matching,
    affiliate_model,
    affiliate_sources,
    editorial,
    model,
)
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.opportunity_store import load_opportunities


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1, "network": "generic_saas_program",
        "program_name": "Acme SaaS Affiliates", "product_name": "Acme All-in-One Platform",
        "product_url": "https://acme.example/platform?ref=base", "product_price": 17.0,
        "currency": "USD", "commission_kind": "recurring_percent", "commission_rate": 0.60,
        "commission_evidence": ["Acme's own affiliate page: '60% recurring commission'"],
        "evidence": ["Acme's own homepage: 'the only tool you need to launch your online business'"],
        "category": "online-business-platform",
        "keywords": ["funnel", "funnel builder", "marketing automation", "website builder",
                    "landing page", "online business"],
        "human_confirmed_joined": True,
    }
    base.update(overrides)
    return base


class BuildEditorialDraftTests(unittest.TestCase):
    def test_missing_title_raises(self):
        with self.assertRaises(editorial.EditorialError):
            editorial.build_editorial_draft(title="", description="x", note="x")

    def test_missing_description_raises(self):
        with self.assertRaises(editorial.EditorialError):
            editorial.build_editorial_draft(title="x", description="", note="x")

    def test_missing_note_raises(self):
        with self.assertRaises(editorial.EditorialError):
            editorial.build_editorial_draft(title="x", description="x", note="")

    def test_source_type_is_editorial_pick_not_demand_signal(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched.")
        self.assertEqual(draft.source_meta.source_type, "editorial_pick")
        self.assertNotEqual(draft.source_meta.source_type, "demand_signal")
        self.assertNotEqual(draft.source_meta.source_type, "human_fed")

    def test_raw_marks_editorial_pick_and_carries_the_real_note(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched.", actor="human-owner")
        self.assertTrue(draft.raw["editorial_pick"])
        self.assertEqual(draft.raw["editorial_note"], "This category is commonly searched.")
        self.assertEqual(draft.raw["authorized_by"], "human-owner")

    def test_note_becomes_the_only_evidence_never_a_fake_quote(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched.")
        self.assertEqual(list(draft.evidence), ["This category is commonly searched."])

    def test_opportunity_type_is_affiliate(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.", note="x")
        self.assertEqual(draft.opportunity_type, model.TYPE_AFFILIATE)


class IngestEditorialPickTests(unittest.TestCase):
    def test_ingests_and_qualifies(self):
        d = _tmp()
        out = editorial.ingest_editorial_pick(
            d, title="Choosing an all-in-one marketing platform",
            description="A buying guide for people starting an online business.",
            note="This is a widely, continuously searched product category.",
            category="online-business-platform")
        self.assertTrue(out["opportunity_id"])
        self.assertEqual(out["verification_status"], model.V_QUALIFIED)
        self.assertTrue(out["qualified"])

    def test_re_ingesting_the_same_title_is_idempotent(self):
        d = _tmp()
        out1 = editorial.ingest_editorial_pick(
            d, title="Choosing an all-in-one marketing platform",
            description="A buying guide.", note="Commonly searched category.")
        out2 = editorial.ingest_editorial_pick(
            d, title="Choosing an all-in-one marketing platform",
            description="A buying guide, refreshed.", note="Commonly searched category.")
        self.assertEqual(out1["opportunity_id"], out2["opportunity_id"])
        self.assertEqual(len(load_opportunities(d).all()), 1)

    def test_origin_is_real_but_demand_basis_is_editorial_pick(self):
        d = _tmp()
        out = editorial.ingest_editorial_pick(
            d, title="Choosing an all-in-one marketing platform",
            description="A buying guide.", note="Commonly searched category.")
        rec = load_opportunities(d).get(out["opportunity_id"])
        self.assertEqual(rec["origin"], model.ORIGIN_REAL)   # real product, real page - not test data
        self.assertEqual(rec["discovery"]["source_type"], "editorial_pick")


class AssetFramingTests(unittest.TestCase):
    """The core honesty guarantee: an editorial note is never presented as
    a stranger's verbatim words."""

    def _match(self, offer_kw: dict | None = None) -> affiliate_matching.AffiliateMatch:
        offer = affiliate_model.AffiliateOffer(
            offer_id="o1", network="generic_saas_program", program_name="Acme Affiliates",
            product_name="Acme All-in-One Platform", product_price=17.0,
            commission=affiliate_model.CommissionModel(kind="recurring_percent", rate=0.6),
            evidence=("Acme's own homepage: 'the only tool you need'",),
            status=model.POLICY_OK, **(offer_kw or {}))
        return affiliate_matching.AffiliateMatch(offer=offer, match_score=0.6, demand_strength=0.0)

    def test_editorial_draft_never_gets_in_their_own_words_framing(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched by new online business owners.")
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/x")
        self.assertNotIn("have said, in their own words", page)
        self.assertIn("This is a common need", page)

    def test_real_discovered_draft_with_evidence_still_gets_quote_framing(self):
        # regression guard: this fix must not change behaviour for a real,
        # independently-discovered demand signal.
        from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta

        meta = SourceMeta(source="hn-algolia", source_type="demand_signal",
                          access_method=model.ACCESS_OFFICIAL_API, policy_status=model.POLICY_OK)
        draft = OpportunityDraft(
            title="Is there a tool for building a sales funnel?",
            description="", opportunity_type=model.TYPE_AFFILIATE,
            evidence=["Is there a tool for building a sales funnel?"], source_meta=meta)
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/x")
        self.assertIn("have said, in their own words", page)

    def test_editorial_framing_survives_persist_and_reconstruct_round_trip(self):
        # regression guard: draft_from_record() does not currently restore
        # `draft.raw` - only `source_meta` - so the editorial check must
        # not rely solely on `raw.editorial_pick` or a real deploy (which
        # always goes through the persisted record) would silently
        # misattribute the editorial note as a customer's own words.
        from revenue_os.ecosystem.model import OpportunityDraft
        from revenue_os.ecosystem.pipeline import draft_from_record

        d = _tmp()
        out = editorial.ingest_editorial_pick(
            d, title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched by new online business owners.")
        rec = load_opportunities(d).get(out["opportunity_id"])
        reconstructed = draft_from_record(rec)
        self.assertIsInstance(reconstructed, OpportunityDraft)
        self.assertEqual(reconstructed.raw, {"target_customer": ""})   # raw is NOT restored
        self.assertEqual(reconstructed.source_meta.source_type, "editorial_pick")

        page, _ = affiliate_assets.render_comparison_page(
            draft=reconstructed, match=self._match(), cta_url="https://example.test/go/x")
        self.assertNotIn("have said, in their own words", page)
        self.assertIn("This is a common need", page)

    def test_editorial_asset_still_has_disclosure_and_cta_and_passes_quality_gate(self):
        draft = editorial.build_editorial_draft(
            title="Choosing a funnel builder", description="A buying guide.",
            note="This category is commonly searched.")
        page, checks = affiliate_assets.render_comparison_page(
            draft=draft, match=self._match(), cta_url="https://example.test/go/x")
        self.assertIn(affiliate_assets.DISCLOSURE_TEXT, page)
        self.assertIn("https://example.test/go/x", page)
        ok, reasons = affiliate_assets.check_quality(checks)
        self.assertTrue(ok, reasons)


class FullChainWithEditorialPickTests(unittest.TestCase):
    def test_editorial_pick_can_go_all_the_way_to_a_deployed_asset(self):
        # An editorial pick is a HUMAN-DECIDED strategy already - it goes
        # through affiliate-deploy (run_affiliate_chain directly), the same
        # path a human uses when they do not need the generic profitability
        # heuristic to "discover" that AFFILIATE is the right strategy (a
        # conservative, cheap entry-tier product's tiny commission-per-sale
        # can legitimately score negative under the generic baseline-
        # traffic assumptions even when the human has already decided to
        # promote it - see cli._cmd_affiliate_deploy's docstring).
        from revenue_os.cli import main

        d = _tmp()
        out = editorial.ingest_editorial_pick(
            d, title="Choosing an all-in-one marketing platform for a new online business",
            description="A buying guide covering funnel builders, email marketing, and "
                        "website builders for people starting an online business.",
            note="This product category is commonly and continuously searched.",
            category="online-business-platform")
        oid = out["opportunity_id"]
        affiliate_sources.ingest_affiliate_offer(d, _offer_json())

        from unittest import mock
        with mock.patch("revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            rc = main(["--data-dir", str(d), "affiliate-deploy", oid])
        self.assertEqual(rc, 0)

        status = affiliate_intel.affiliate_funnel_status(d, oid)
        self.assertEqual(status["demand_basis"], "editorial_pick")
        self.assertFalse(status["ready"]["demand_source_real"])
        self.assertTrue(status["ready"]["page_deployed"])
        self.assertTrue(status["ready"]["asset_generated"])
        self.assertEqual(status["offer"]["affiliate_url"], "https://acme.example/platform?ref=base")


class CliSmokeTests(unittest.TestCase):
    def test_ingest_editorial_pick_cli_runs(self):
        from revenue_os.cli import main
        d = _tmp()
        rc = main(["--data-dir", str(d), "ingest-editorial-pick",
                  "--title", "Choosing a funnel builder",
                  "--description", "A buying guide.",
                  "--note", "Commonly searched category."])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
