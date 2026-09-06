"""Wondershare PDFelement in the affiliate offer CATALOG (ingestion +
model + matching), separate from the discovery-side
test_wondershare_offer_source.py.

Covers, deterministically and offline:
  - a valid PDFelement offer ingests and is usable
  - the exact Awin tracking URL is preserved byte-for-byte (ingest + link)
  - real commission evidence is validated and recorded (rate 0.30, not an
    estimate, entry-tier quote retained)
  - missing / invalid commission evidence is rejected (fail closed)
  - the Awin advertiser id (awinmid=20202) / publisher id (awinaffid=
    3077697) survive ingestion, and a non-Awin / malformed tracking URL
    is rejected
  - the offer matches real PDF-editor demand via the EXISTING, unmodified
    affiliate_matching gate, and does not match unrelated demand
  - fail-closed behaviour: not-yet-joined stays HUMAN_SETUP_REQUIRED /
    unusable; the offer is never auto-accepted / deployed / turned into an
    opportunity by ingestion

No network. The real advertiser / publisher ids and the verified 2026-09-06
commission quote are used only as fixtures here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import affiliate_sources, model
from revenue_os.ecosystem.affiliate_matching import best_usable_match, match_offers
from revenue_os.ecosystem.affiliate_model import AffiliateAsset, AffiliateOfferStore
from revenue_os.ecosystem.affiliate_matching import AffiliateMatch
from revenue_os.ecosystem.affiliate_links import create_link
from revenue_os.ecosystem.model import OpportunityDraft

_TRACKING_URL = (
    "https://www.awin1.com/cread.php?awinmid=20202&awinaffid=3077697"
    "&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"
)
_COMMISSION_QUOTE = (
    "Wondershare official PDFelement affiliate page "
    "(pdf.wondershare.com/affiliate.html), retrieved 2026-09-06, verbatim "
    "tiered commission table: 'Monthly Sales | Commission rate | Less than "
    "USD$1,000 | 30% | USD$1,000 to USD$5,000 | 35% | USD$5,000 to "
    "USD$10,000 | 40% | USD$10,000 to USD$100,000 | 45% | USD$100,000 or "
    "higher | 50%'. 30% is the entry tier and the guaranteed floor."
)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _payload(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "network": "awin",
        "program_name": "Wondershare PDFelement Affiliate Program (Awin, advertiser 20202)",
        "product_name": "Wondershare PDFelement",
        "product_url": _TRACKING_URL,
        "currency": "USD",
        "commission_kind": "percent",
        "commission_rate": 0.30,
        "commission_evidence": [_COMMISSION_QUOTE],
        "category": "pdf-editor",
        "keywords": ["pdf", "pdfelement", "pdf editor", "ocr", "forms",
                     "acrobat", "acrobat alternative", "convert pdf"],
        "evidence": ["Wondershare PDFelement product page "
                     "(pdf.wondershare.com/pdfelement.html), retrieved "
                     "2026-09-06: 'Edit PDF like Word', OCR across 26 languages, "
                     "convert to/from PDF, forms, e-sign, compress, organize."],
        "human_confirmed_joined": True,
        "preserve_exact_url": True,
    }
    base.update(overrides)
    return base


class ValidOfferTests(unittest.TestCase):
    def test_valid_pdfelement_offer_ingests_and_is_usable(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload(), actor="human-owner")
        self.assertEqual(out["network"], "awin")
        self.assertEqual(out["status"], model.POLICY_OK)
        self.assertTrue(out["usable"])
        self.assertFalse(out["updated_existing"])

        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertIsNotNone(offer)
        self.assertTrue(offer.usable)
        self.assertEqual(offer.product_name, "Wondershare PDFelement")
        self.assertEqual(offer.category, "pdf-editor")
        self.assertTrue(offer.preserve_exact_url)

    def test_reingest_updates_not_duplicates(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _payload())
        out2 = affiliate_sources.ingest_affiliate_offer(d, _payload(commission_rate=0.35))
        self.assertTrue(out2["updated_existing"])
        self.assertEqual(len(AffiliateOfferStore.load(d).all()), 1)

    def test_ingestion_does_not_accept_deploy_or_create_an_opportunity(self):
        d = _tmp()
        affiliate_sources.ingest_affiliate_offer(d, _payload())
        # ingestion touches ONLY the offer catalog file - nothing else
        self.assertTrue((d / "affiliate_offers.json").exists())
        for never in ("affiliate_assets.json", "affiliate_links.json",
                      "opportunities.json", "candidates.json", "tasks.json"):
            self.assertFalse((d / never).exists(), f"{never} must not be created by ingestion")


class TrackingUrlPreservationTests(unittest.TestCase):
    def test_exact_url_survives_ingestion(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload())
        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertEqual(offer.product_url, _TRACKING_URL)

    def test_link_creation_never_modifies_the_awin_url(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload())
        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        match = AffiliateMatch(offer=offer, match_score=0.5, matched_terms=["pdf"],
                               demand_strength=0.4)
        asset = AffiliateAsset(asset_id="a1", opportunity_id="opp1", offer_id=offer.offer_id)
        link = create_link(d, opportunity_id="opp1", asset=asset, match=match,
                           source="own_site", now_iso="2026-09-06T00:00:00+00:00")
        # preserve_exact_url => not even a subid= is appended
        self.assertEqual(link.target_url, _TRACKING_URL)
        self.assertNotIn("subid", link.target_url)


class CommissionEvidenceTests(unittest.TestCase):
    def test_valid_evidence_is_recorded_as_fact_not_estimate(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload())
        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertEqual(offer.commission.kind, "percent")
        self.assertEqual(offer.commission.rate, 0.30)
        self.assertFalse(offer.commission.is_estimate)   # human-supplied + evidenced
        self.assertEqual(len(offer.commission.evidence), 1)
        self.assertIn("pdf.wondershare.com/affiliate.html", offer.commission.evidence[0])
        self.assertIn("30%", offer.commission.evidence[0])

    def test_missing_commission_evidence_is_rejected(self):
        payload = _payload()
        del payload["commission_evidence"]
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), payload)

    def test_empty_commission_evidence_is_rejected(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _payload(commission_evidence=[]))

    def test_blank_commission_evidence_string_is_rejected(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _payload(commission_evidence=["   "]))

    def test_out_of_range_commission_rate_is_rejected(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _payload(commission_rate=1.5))

    def test_missing_commission_kind_is_rejected(self):
        payload = _payload()
        del payload["commission_kind"]
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), payload)


class AwinAdvertiserIdTests(unittest.TestCase):
    def test_advertiser_and_publisher_ids_survive_ingestion(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload())
        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(offer.product_url).query)
        self.assertEqual(q["awinmid"][0], "20202")
        self.assertEqual(q["awinaffid"][0], "3077697")

    def test_non_awin_host_is_rejected(self):
        bad = _payload(product_url=(
            "https://evil-redirect.example/cread.php?awinmid=20202"
            "&awinaffid=3077697&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"))
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), bad)

    def test_wrong_path_is_rejected(self):
        bad = _payload(product_url=_TRACKING_URL.replace("/cread.php", "/click.php"))
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), bad)

    def test_missing_awinmid_is_rejected(self):
        bad = _payload(product_url=(
            "https://www.awin1.com/cread.php?awinaffid=3077697"
            "&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"))
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), bad)

    def test_missing_product_url_is_rejected_for_awin(self):
        payload = _payload()
        del payload["product_url"]
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), payload)


class MatchingTests(unittest.TestCase):
    def _offer(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(d, _payload())
        return AffiliateOfferStore.load(d).get(out["offer_id"])

    def _draft(self, title: str, category: str = "pdf-editor") -> OpportunityDraft:
        return OpportunityDraft(title=title, description="", evidence=[], category=category)

    def test_matches_real_pdf_editor_demand(self):
        offer = self._offer()
        for title in (
            "Is there a good PDF editor with OCR for scanned documents, cheaper than Acrobat?",
            "looking for an adobe acrobat alternative to edit and sign pdf forms",
            "best tool to convert PDF to Word and fill in PDF forms",
        ):
            with self.subTest(title=title):
                matches = match_offers(self._draft(title), [offer])
                self.assertEqual(len(matches), 1)
                self.assertGreaterEqual(matches[0].match_score, 0.25)
                self.assertIn("pdf", matches[0].matched_terms)

    def test_best_usable_match_returns_the_offer(self):
        offer = self._offer()
        draft = self._draft("which pdf editor should I buy - need OCR and forms, not Acrobat")
        self.assertIsNotNone(best_usable_match(draft, [offer]))

    def test_unrelated_demand_does_not_match(self):
        offer = self._offer()
        for title, category in (
            ("which mechanical keyboard for coding", "mechanical-keyboard"),
            ("cheap VPS hosting for a side project", "hosting"),
            ("best USB microphone for streaming", "usb-microphone"),
        ):
            with self.subTest(title=title):
                self.assertEqual(match_offers(self._draft(title, category), [offer]), [])


class FailClosedTests(unittest.TestCase):
    def test_not_yet_joined_stays_human_setup_required_and_unusable(self):
        d = _tmp()
        out = affiliate_sources.ingest_affiliate_offer(
            d, _payload(human_confirmed_joined=False))
        self.assertEqual(out["status"], model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertFalse(out["usable"])
        offer = AffiliateOfferStore.load(d).get(out["offer_id"])
        self.assertFalse(offer.usable)
        # an unusable offer is still never picked by the usable-match gate
        draft = OpportunityDraft(title="need a pdf editor with ocr like acrobat",
                                 description="", evidence=[], category="pdf-editor")
        self.assertIsNone(best_usable_match(draft, [offer]))

    def test_human_confirmed_joined_must_be_a_bool(self):
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), _payload(human_confirmed_joined="yes"))

    def test_unknown_field_is_rejected(self):
        payload = _payload()
        payload["definitely_not_a_field"] = 1
        with self.assertRaises(affiliate_sources.IngestionError):
            affiliate_sources.ingest_affiliate_offer(_tmp(), payload)


if __name__ == "__main__":
    unittest.main()
