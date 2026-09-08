"""social/ Phase 2 - Reddit distribution flow. No network: a fake client
returns canned AcqRecords.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import affiliate_model, model
from revenue_os.social import compliance, links
from revenue_os.social import reddit_flow
from revenue_os.social.store import (
    STATE_HUMAN_REQUIRED,
    STATE_PUBLISHED,
    SocialActionStore,
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
        category="mikrofone",
        keywords=("mikrofon", "usb microphone", "budget microphone", "streaming",
                  "gaming", "podcast", "mic"),
        evidence=("Cardioid pickup, 14mm diaphragm, one-tap mute, plug and play",),
        status=model.POLICY_OK, tracking_param="tag", tracking_value="airevenue-21",
        active=True)
    base.update(kw)
    return affiliate_model.AffiliateOffer(**base)


def _setup(d: Path, *, live_url="https://divdav12.github.io/AI-Revenue-OS/mic-guide/index.html"):
    offers = affiliate_model.AffiliateOfferStore.load(d)
    offers.upsert(_amazon_offer())
    offers.save()
    assets = affiliate_model.AffiliateAssetStore.load(d)
    assets.upsert(affiliate_model.AffiliateAsset(
        asset_id="asset-mic", opportunity_id="opp_mic", offer_id="aff-4564ed68a8e3",
        title="Budget USB microphone for streaming", slug="mic-guide",
        file_path="index.html", live_url=live_url,
        quality_checks={"meets_min_words": True, "has_disclosure": True,
                        "has_cta": True, "has_evidence": True,
                        "has_demand_quote": True}))
    assets.save()


class FakeRedditClient:
    def __init__(self, records, *, available=True, can_write=False, posts=None):
        self._records = records
        self.available = available
        self.can_write = can_write
        self._posts = posts if posts is not None else []

    def search(self, query, *, subreddit="", limit=10, sort="new", time_filter="month"):
        return list(self._records)

    def submit_comment(self, *, thing_fullname, text, i_have_read_the_rules=False):
        assert i_have_read_the_rules
        self._posts.append((thing_fullname, text))
        return {"posted": True, "url": f"https://www.reddit.com/r/x/comments/{thing_fullname}/c/",
                "raw": {}}


_RELEVANT = AcqRecord(
    title="Which USB microphone should I buy for streaming under $40?",
    url="https://www.reddit.com/r/streaming/comments/aaa/which_usb_mic/",
    text="My headset mic sounds terrible on stream. Looking for something cheap.",
    author="user_a", posted_at="2026-09-07T10:00:00+00:00",
    platform="r/streaming", source="reddit", query="usb microphone recommendation",
    meta={"fullname": "t3_aaa", "subreddit": "streaming", "over_18": False})

_IRRELEVANT = AcqRecord(
    title="The history of ribbon microphones",
    url="https://www.reddit.com/r/audio/comments/bbb/history/",
    text="A fun read about how ribbon mics were developed in the 1930s.",
    author="user_b", posted_at="2026-09-07T10:00:00+00:00",
    platform="r/audio", source="reddit", query="microphone recommendation",
    meta={"fullname": "t3_bbb", "subreddit": "audio", "over_18": False})


class TestRedditFlow(unittest.TestCase):
    def test_not_configured_returns_human_required_with_setup(self):
        d = _tmp()
        _setup(d)
        out = reddit_flow.scan_reddit(
            d, offer_id="aff-4564ed68a8e3", subreddits=["streaming"],
            client=FakeRedditClient([], available=False))
        self.assertEqual(out["status"], "HUMAN_REQUIRED")
        self.assertEqual(out["blocker"], "reddit_not_configured")
        self.assertTrue(any("reddit.com/prefs/apps" in s for s in out["setup"]))

    def test_guide_mode_without_deployed_guide_blocks(self):
        d = _tmp()
        _setup(d, live_url="")
        out = reddit_flow.scan_reddit(
            d, offer_id="aff-4564ed68a8e3", subreddits=["streaming"],
            client=FakeRedditClient([_RELEVANT]))
        self.assertEqual(out["status"], "HUMAN_REQUIRED")
        self.assertEqual(out["blocker"], "no_deployed_guide")

    def test_relevant_thread_creates_human_required_draft_no_post(self):
        d = _tmp()
        _setup(d)
        posts = []
        out = reddit_flow.scan_reddit(
            d, offer_id="aff-4564ed68a8e3", subreddits=["streaming"],
            queries=["usb microphone recommendation"],
            client=FakeRedditClient([_RELEVANT, _IRRELEVANT], posts=posts))
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["counts"]["relevant"], 1)
        self.assertEqual(out["counts"]["rejected_no_buying_intent"], 1)
        self.assertEqual(out["counts"]["new_actions"], 1)
        self.assertEqual(posts, [])   # never auto-posts to a normal subreddit

        store = SocialActionStore.load(d)
        actions = store.all()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act.state, STATE_HUMAN_REQUIRED)
        self.assertIn("airevenue-21", act.draft["resolved_link"]["affiliate_url"])
        self.assertIn(compliance.__dict__.get("PLATFORM_REDDIT", "reddit"),
                      act.compliance["platform"])
        self.assertIn("Disclosure", act.draft["body"])
        self.assertTrue(act.link_id)
        self.assertTrue(act.human_action_needed)

    def test_dedup_second_run_creates_nothing(self):
        d = _tmp()
        _setup(d)
        for _ in range(2):
            out = reddit_flow.scan_reddit(
                d, offer_id="aff-4564ed68a8e3", subreddits=["streaming"],
                queries=["usb microphone recommendation"],
                client=FakeRedditClient([_RELEVANT]))
        self.assertEqual(out["counts"]["already_handled"], 1)
        self.assertEqual(out["counts"]["new_actions"], 0)
        self.assertEqual(len(SocialActionStore.load(d).all()), 1)

    def test_autopost_path_when_subreddit_allowlisted_and_attested(self):
        d = _tmp()
        _setup(d)
        compliance.register_community_rule(compliance.CommunityRule(
            platform="reddit", community="ourtestsub",
            affiliate_links_allowed=True, self_promo_allowed=True,
            disclosure_required=True, auto_post_allowed=True,
            citation="r/ourtestsub rule 5 (verbatim): 'Affiliate links allowed "
                     "if disclosed and relevant.'"))
        posts = []
        rec = AcqRecord(
            title="Which USB microphone should I buy for podcasting?",
            url="https://www.reddit.com/r/ourtestsub/comments/ccc/x/",
            text="Need a cheap mic.", author="u", posted_at="2026-09-07T10:00:00+00:00",
            platform="r/ourtestsub", source="reddit", query="q",
            meta={"fullname": "t3_ccc", "subreddit": "ourtestsub", "over_18": False})
        out = reddit_flow.scan_reddit(
            d, offer_id="aff-4564ed68a8e3", subreddits=["ourtestsub"],
            queries=["q"], link_mode=links.LINK_MODE_DIRECT,
            environ={"PINTEREST_AUTOPOST_CONFIRMED": "0",
                     "REDDIT_AUTOPOST_CONFIRMED": "1"},
            client=FakeRedditClient([rec], can_write=True, posts=posts))
        self.assertEqual(out["counts"]["new_actions"], 1)
        self.assertEqual(len(posts), 1)
        act = SocialActionStore.load(d).all()[0]
        self.assertEqual(act.state, STATE_PUBLISHED)
        self.assertEqual(act.published_by, "auto")
        self.assertTrue(act.published_url)


if __name__ == "__main__":
    unittest.main()
