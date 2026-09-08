"""social/ Phase 1 - shared foundation: compliance gate, intent gate,
action store, content renderers, affiliate-link resolution.

No network. Deterministic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import affiliate_model, model
from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta
from revenue_os.social import compliance, content, intent, links
from revenue_os.social.store import (
    STATE_HUMAN_REQUIRED,
    SocialAction,
    SocialActionStore,
    new_action_id,
)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _amazon_offer(**kw) -> affiliate_model.AffiliateOffer:
    base = dict(
        offer_id="aff-4564ed68a8e3", network="amazon_associates",
        program_name="Amazon PartnerNet",
        product_name="Amazon Basics Mini-USB-Kondensatormikrofon",
        product_url="https://www.amazon.de/dp/B0CL9BTQRF/", product_asin="B0CL9BTQRF",
        product_price=26.24, currency="EUR", price_is_estimate=False,
        price_observed_at="2026-09-07T20:24:04Z",
        category="mikrofone",
        keywords=("mikrofon", "usb-mikrofon", "budget microphone", "streaming",
                  "gaming", "podcast"),
        evidence=("Amazon.co.uk product title for ASIN B0CL9BTQRF: Cardioid "
                  "Pickup, 14mm Diaphragm, One-Tap Mute, Plug and Play",),
        status=model.POLICY_OK, tracking_param="tag", tracking_value="airevenue-21",
        active=True,
    )
    base.update(kw)
    return affiliate_model.AffiliateOffer(**base)


def _draft(title="Looking for a budget usb microphone for streaming",
           body="My current headset mic sounds terrible, what should I buy under 40 bucks?") -> OpportunityDraft:
    return OpportunityDraft(
        title=title, description=body, opportunity_type=model.TYPE_AFFILIATE,
        evidence=[title],
        source_meta=SourceMeta(source="reddit", source_type="demand_signal",
                               policy_status=model.POLICY_OK),
        source_id="t3_abc", discovered_at="2026-09-08T00:00:00",
        category="mikrofone", demand_hint=0.6)


# ---------------------------------------------------------------------------
# compliance
# ---------------------------------------------------------------------------

class TestCompliance(unittest.TestCase):
    def test_unknown_platform_fails_closed(self):
        v = compliance.check("tiktok", "", has_affiliate_link=True)
        self.assertTrue(v.human_required)
        self.assertFalse(v.auto_post_allowed)

    def test_reddit_unlisted_subreddit_is_human_required(self):
        v = compliance.check("reddit", "microphones", has_affiliate_link=True)
        self.assertTrue(v.human_required)
        self.assertFalse(v.auto_post_allowed)
        self.assertFalse(v.affiliate_link_allowed)
        self.assertTrue(v.disclosure_required)
        self.assertTrue(any("allow-list" in r for r in v.reasons))

    def test_reddit_never_auto_posts_even_with_attestation(self):
        v = compliance.check("reddit", "somesub", has_affiliate_link=False,
                             environ={"REDDIT_AUTOPOST_CONFIRMED": "1"})
        self.assertTrue(v.human_required)

    def test_pinterest_needs_operator_attestation_for_autopost(self):
        without = compliance.check("pinterest", "", has_affiliate_link=True,
                                   environ={})
        self.assertTrue(without.human_required)
        self.assertTrue(any("attestation" in r for r in without.reasons))

        with_att = compliance.check("pinterest", "", has_affiliate_link=True,
                                    environ={"PINTEREST_AUTOPOST_CONFIRMED": "1"})
        self.assertTrue(with_att.auto_post_allowed)
        self.assertFalse(with_att.human_required)
        self.assertTrue(with_att.affiliate_link_allowed)
        self.assertTrue(with_att.disclosure_required)

    def test_registered_allowing_subreddit(self):
        compliance.register_community_rule(compliance.CommunityRule(
            platform="reddit", community="test_affiliate_ok",
            affiliate_links_allowed=True, self_promo_allowed=True,
            disclosure_required=True, auto_post_allowed=True,
            citation="r/test_affiliate_ok rule 3 (verbatim): 'Affiliate links "
                     "are fine as long as you disclose them.'"))
        v = compliance.check("reddit", "test_affiliate_ok", has_affiliate_link=True,
                             environ={"REDDIT_AUTOPOST_CONFIRMED": "1"})
        self.assertTrue(v.affiliate_link_allowed)
        self.assertTrue(v.auto_post_allowed)
        self.assertFalse(v.human_required)


# ---------------------------------------------------------------------------
# intent
# ---------------------------------------------------------------------------

class TestIntent(unittest.TestCase):
    def test_buy_recommendation_with_category_is_relevant(self):
        a = intent.assess_text(
            title="What USB microphone should I buy for streaming under $40?",
            body="My headset mic is bad.")
        self.assertTrue(a.relevant)
        self.assertEqual(a.strength, intent.STRENGTH_MEDIUM)
        self.assertTrue(a.category_phrase)

    def test_explicit_pay_intent_with_category_is_high(self):
        a = intent.assess_text(
            title="Which USB microphone should I buy? I would pay for a good one",
            body="Sick of my terrible audio.")
        self.assertTrue(a.relevant, a.reason)
        self.assertEqual(a.strength, intent.STRENGTH_HIGH)

    def test_general_discussion_rejected(self):
        a = intent.assess_text(title="The history of the microphone",
                               body="Interesting how ribbon mics evolved.")
        self.assertFalse(a.relevant)

    def test_supplier_post_rejected(self):
        a = intent.assess_text(
            title="I built a USB microphone and launched it today",
            body="Check out my new product, feedback welcome!")
        self.assertFalse(a.relevant)

    def test_buying_words_without_category_rejected(self):
        a = intent.assess_text(title="What should I buy?", body="Need advice please")
        self.assertFalse(a.relevant)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class TestStore(unittest.TestCase):
    def test_roundtrip_and_dedup(self):
        d = _tmp()
        s = SocialActionStore.load(d)
        a = SocialAction(action_id=new_action_id(), platform="reddit",
                         community="test", target_ref="https://reddit.com/x",
                         offer_id="aff-4564ed68a8e3", state=STATE_HUMAN_REQUIRED)
        s.upsert(a)
        s.save()

        s2 = SocialActionStore.load(d)
        self.assertEqual(len(s2.all()), 1)
        self.assertTrue(s2.already_handled("reddit", "https://reddit.com/x",
                                           "aff-4564ed68a8e3"))
        self.assertFalse(s2.already_handled("reddit", "https://reddit.com/other",
                                            "aff-4564ed68a8e3"))
        self.assertEqual(s2.summary()["open"], 1)


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------

class TestContent(unittest.TestCase):
    def test_reddit_reply_has_disclosure_and_link_and_no_best_claim(self):
        r = content.reddit_reply(
            offer=_amazon_offer(),
            product_intent={"category_phrase": "usb microphone"},
            matched_terms=["mikrofon", "streaming"],
            assessment_reason="explicit purchase intent",
            link_url="https://divdav12.github.io/AI-Revenue-OS/looking-.../index.html",
            link_mode="guide", community="microphones")
        self.assertIn(content.REDDIT_DISCLOSURE, r.body)
        self.assertIn("github.io", r.body)
        self.assertNotIn("the best", r.body.lower())
        self.assertIn("26.24 EUR", r.body)

    def test_pinterest_pin_within_limits_and_disclosed(self):
        p = content.pinterest_pin(
            offer=_amazon_offer(),
            product_intent={"category_phrase": "budget usb microphone"},
            link_url="https://www.amazon.de/dp/B0CL9BTQRF/?tag=airevenue-21",
            link_mode="direct")
        self.assertLessEqual(len(p.title), 100)
        self.assertLessEqual(len(p.description), 480)
        self.assertIn("#ad", p.description)
        self.assertEqual(p.dominant_keyword, "budget usb microphone")
        self.assertIn("2:3", p.creative_brief)


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------

class TestLinks(unittest.TestCase):
    def _setup(self, d: Path, *, live_url: str):
        offers = affiliate_model.AffiliateOfferStore.load(d)
        offers.upsert(_amazon_offer())
        offers.save()
        assets = affiliate_model.AffiliateAssetStore.load(d)
        assets.upsert(affiliate_model.AffiliateAsset(
            asset_id="asset-x", opportunity_id="opp_1", offer_id="aff-4564ed68a8e3",
            slug="guide-x", file_path="index.html", live_url=live_url,
            quality_checks={"meets_min_words": True, "has_disclosure": True,
                            "has_cta": True, "has_evidence": True,
                            "has_demand_quote": True}))
        assets.save()

    def test_guide_mode_resolves_and_persists_attributable_link(self):
        d = _tmp()
        self._setup(d, live_url="https://divdav12.github.io/AI-Revenue-OS/guide-x/index.html")
        rl = links.resolve_link(
            d, draft=_draft(), offer_id="aff-4564ed68a8e3", opportunity_id="opp_1",
            platform="reddit", community="microphones", mode="guide")
        self.assertTrue(rl.ok, rl.reason)
        self.assertTrue(rl.link_id)
        self.assertEqual(rl.published_target, rl.guide_live_url)
        self.assertIn("tag=airevenue-21", rl.affiliate_url)

        # idempotent: second call reuses the same link row
        rl2 = links.resolve_link(
            d, draft=_draft(), offer_id="aff-4564ed68a8e3", opportunity_id="opp_1",
            platform="reddit", community="microphones", mode="guide")
        self.assertEqual(rl.link_id, rl2.link_id)

    def test_guide_mode_fails_closed_without_live_url(self):
        d = _tmp()
        self._setup(d, live_url="")
        rl = links.resolve_link(
            d, draft=_draft(), offer_id="aff-4564ed68a8e3", opportunity_id="opp_1",
            platform="reddit", mode="guide")
        self.assertFalse(rl.ok)
        self.assertIn("live_url", rl.reason)

    def test_direct_mode_uses_amazon_url_with_tag(self):
        d = _tmp()
        self._setup(d, live_url="")
        rl = links.resolve_link(
            d, draft=_draft(), offer_id="aff-4564ed68a8e3", opportunity_id="opp_1",
            platform="pinterest", mode="direct")
        self.assertTrue(rl.ok, rl.reason)
        self.assertIn("tag=airevenue-21", rl.published_target)

    def test_unusable_offer_rejected(self):
        d = _tmp()
        offers = affiliate_model.AffiliateOfferStore.load(d)
        offers.upsert(_amazon_offer(status=model.POLICY_HUMAN_SETUP_REQUIRED))
        offers.save()
        rl = links.resolve_link(
            d, draft=_draft(), offer_id="aff-4564ed68a8e3", opportunity_id="opp_1",
            platform="reddit", mode="direct")
        self.assertFalse(rl.ok)
        self.assertIn("not usable", rl.reason)


if __name__ == "__main__":
    unittest.main()
