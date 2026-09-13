"""TikTok slideshow content drafting - mirrors test_pinterest_pins.py's
conventions. No network, no LLM call: pure template rendering + local
JSON persistence over real, already-verified AffiliateOffer objects.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from revenue_os import action_class
from revenue_os.ecosystem.affiliate_model import AffiliateOffer, CommissionModel
from revenue_os.ecosystem.tiktok_content import (
    CONTENT_DISCLOSURE,
    TikTokDraftError,
    draft_content,
    mark_posted,
    pending_drafts,
)


def _offer(**overrides) -> AffiliateOffer:
    defaults = dict(
        offer_id="off-1", network="amazon_associates", program_name="Amazon.de PartnerNet",
        product_name="Test Mouse", product_url="https://www.amazon.de/dp/B000000001",
        product_asin="B000000001", product_price=29.99, currency="EUR",
        price_is_estimate=False, price_observed_at="2026-09-10",
        price_source_note="observed", commission=CommissionModel(kind="percent", rate=0.03),
        category="maeuse", keywords=("gaming mouse", "desk setup"),
        terms_url="https://partnernet.amazon.de/help/operating/agreement",
        join_url="https://partnernet.amazon.de/", eligibility_note="standard",
        evidence=("verified",), status="OK", tracking_param="tag",
        tracking_value="airevenue-21", image_urls=("https://example.com/a.jpg",),
        short_context="a mouse", verification_status="verified", verified_at="2026-09-10",
        added_at="2026-09-10T00:00:00Z", added_by="human-owner",
    )
    defaults.update(overrides)
    return AffiliateOffer(**defaults)


_SOUND = dict(sound_name="Trend Sound XYZ", sound_source="TikTok Creative Center",
             sound_rationale="strong current usage this week", sound_researched_at="2026-09-13T10:00:00Z")


class DraftContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.offers = [_offer(offer_id=f"off-{i}", product_name=f"Product {i}") for i in range(3)]

    def test_drafts_from_real_offers(self):
        draft = draft_content(self.data_dir, offers=self.offers, num_products=2, **_SOUND)
        self.assertTrue(draft.draft_id)
        self.assertEqual(draft.category, "maeuse")
        self.assertEqual(len(draft.offer_ids), 2)
        self.assertIn(CONTENT_DISCLOSURE, draft.caption)
        self.assertIn(draft.hook, __import__("revenue_os.ecosystem.tiktok_content",
                                             fromlist=["HOOKS"]).HOOKS)
        self.assertTrue(draft.hashtags)

    def test_refuses_without_sound_research(self):
        with self.assertRaises(TikTokDraftError):
            draft_content(self.data_dir, offers=self.offers, sound_name="", sound_source="",
                          sound_rationale="", sound_researched_at="")

    def test_refuses_unknown_category_with_no_offers(self):
        with self.assertRaises(TikTokDraftError):
            draft_content(self.data_dir, offers=self.offers, category="nonexistent", **_SOUND)

    def test_num_products_never_exceeds_available_offers(self):
        draft = draft_content(self.data_dir, offers=self.offers, num_products=99, **_SOUND)
        self.assertEqual(len(draft.offer_ids), 3)

    def test_rotation_avoids_immediate_repeat_offers(self):
        first = draft_content(self.data_dir, offers=self.offers, num_products=1, **_SOUND)
        second = draft_content(self.data_dir, offers=self.offers, num_products=1, **_SOUND)
        self.assertNotEqual(first.offer_ids, second.offer_ids)

    def test_rotation_avoids_immediate_repeat_hook(self):
        first = draft_content(self.data_dir, offers=self.offers, num_products=1, **_SOUND)
        second = draft_content(self.data_dir, offers=self.offers, num_products=1, **_SOUND)
        self.assertNotEqual(first.hook, second.hook)

    def test_mark_posted_and_pending(self):
        draft = draft_content(self.data_dir, offers=self.offers, num_products=1, **_SOUND)
        self.assertEqual(len(pending_drafts(self.data_dir)), 1)
        updated = mark_posted(self.data_dir, draft.draft_id, now_iso="2026-09-13T12:00:00Z")
        self.assertEqual(updated.status, "posted")
        self.assertEqual(pending_drafts(self.data_dir), [])

    def test_mark_posted_rejects_unknown_id(self):
        with self.assertRaises(ValueError):
            mark_posted(self.data_dir, "no-such-id")


class SafetyGateTests(unittest.TestCase):
    def test_drafting_is_safe_autonomous(self):
        verdict = action_class.classify("prepare_tiktok_content")
        self.assertTrue(verdict.autonomous)

    def test_posting_to_tiktok_is_not_permitted_automation(self):
        self.assertFalse(action_class.posting_permitted("tiktok"))


if __name__ == "__main__":
    unittest.main()
