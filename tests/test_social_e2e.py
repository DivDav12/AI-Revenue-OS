"""social/ end-to-end: real demand -> opportunity/offer -> content ->
compliance -> HUMAN_REQUIRED or publication -> real affiliate link ->
measurement. No network (fake platform clients).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import affiliate_model, model
from revenue_os.social import compliance, links, measure, report
from revenue_os.social import pinterest_flow, reddit_flow
from revenue_os.social.store import (
    STATE_MEASURED,
    STATE_PUBLISHED,
    SocialActionStore,
)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _offer() -> affiliate_model.AffiliateOffer:
    return affiliate_model.AffiliateOffer(
        offer_id="aff-4564ed68a8e3", network="amazon_associates",
        program_name="Amazon PartnerNet",
        product_name="Amazon Basics Mini-USB-Kondensatormikrofon",
        product_url="https://www.amazon.de/dp/B0CL9BTQRF/", product_asin="B0CL9BTQRF",
        product_price=26.24, currency="EUR", price_is_estimate=False,
        price_observed_at="2026-09-07T20:24:04Z", category="mikrofone",
        keywords=("usb microphone", "budget microphone", "streaming", "gaming", "podcast"),
        evidence=("Cardioid pickup, 14mm diaphragm, one-tap mute, plug and play",),
        status=model.POLICY_OK, tracking_param="tag", tracking_value="airevenue-21",
        active=True)


def _deployed_asset(d: Path):
    offers = affiliate_model.AffiliateOfferStore.load(d)
    offers.upsert(_offer())
    offers.save()
    assets = affiliate_model.AffiliateAssetStore.load(d)
    assets.upsert(affiliate_model.AffiliateAsset(
        asset_id="asset-mic", opportunity_id="opp_mic", offer_id="aff-4564ed68a8e3",
        title="Budget USB microphone for streaming", slug="mic-guide",
        file_path="index.html",
        live_url="https://divdav12.github.io/AI-Revenue-OS/mic-guide/index.html",
        quality_checks={"meets_min_words": True, "has_disclosure": True,
                        "has_cta": True, "has_evidence": True, "has_demand_quote": True}))
    assets.save()


class FakeReddit:
    available = True
    can_write = False

    def __init__(self, records):
        self._records = records

    def search(self, q, *, subreddit="", limit=10, sort="new", time_filter="month"):
        return list(self._records)


class FakePinterest:
    available = True

    def __init__(self):
        self.analytics_calls = []

    def resolve_board_id(self, n):
        return "board_1"

    def list_boards(self):
        return [{"id": "board_1", "name": "Tech Picks"}]

    def create_pin(self, **kw):
        assert kw["i_have_read_the_rules"]
        return {"created": True, "pin_id": "pin_1",
                "url": "https://www.pinterest.com/pin/pin_1/", "raw": {}}

    def pin_analytics(self, pin_id, *, start_date, end_date, metrics="X"):
        self.analytics_calls.append(pin_id)
        return {"all": {"daily_metrics": [], "summary_metrics": {
            "IMPRESSION": 40, "PIN_CLICK": 3, "OUTBOUND_CLICK": 2}}}


class TestSocialE2E(unittest.TestCase):
    def test_reddit_full_workflow(self):
        d = _tmp()
        _deployed_asset(d)
        rec = AcqRecord(
            title="What USB microphone should I buy for streaming under $40?",
            url="https://www.reddit.com/r/streaming/comments/e2e/x/",
            text="My headset mic sounds bad on stream.", author="u",
            posted_at="2026-09-07T10:00:00+00:00", platform="r/streaming",
            source="reddit", query="usb microphone recommendation",
            meta={"fullname": "t3_e2e", "subreddit": "streaming", "over_18": False})

        out = reddit_flow.scan_reddit(
            d, offer_id="aff-4564ed68a8e3", subreddits=["streaming"],
            queries=["usb microphone recommendation"], client=FakeReddit([rec]))
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["counts"]["new_actions"], 1)

        store = SocialActionStore.load(d)
        act = store.all()[0]
        # real affiliate link, real tag, disclosure, helpful (not "best")
        self.assertIn("tag=airevenue-21", act.draft["resolved_link"]["affiliate_url"])
        self.assertIn("Disclosure", act.draft["body"])
        self.assertNotIn("the best", act.draft["body"].lower())
        self.assertTrue(act.link_id)
        self.assertEqual(act.state, "HUMAN_REQUIRED")

        # human posts it, confirms URL
        act.state = STATE_PUBLISHED
        act.published_url = "https://www.reddit.com/r/streaming/comments/e2e/x/comment/z/"
        act.published_by = "human-owner"
        store.upsert(act)
        store.save()

        # measurement: clicks still 0 (no public redirect server), no fabrication
        m = measure.measure(d)
        self.assertEqual(m["errors"], [])
        rrow = [r for r in m["per_action"] if r["action_id"] == act.action_id]
        self.assertTrue(rrow)
        self.assertEqual(rrow[0]["metrics"].get("affiliate_clicks", 0), 0)

        st = report.status(d)
        self.assertEqual(st["summary"]["published"], 1)

    def test_pinterest_full_workflow_autopublish_and_measure(self):
        d = _tmp()
        _deployed_asset(d)
        fp = FakePinterest()
        out = pinterest_flow.plan_pinterest(
            d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
            image_url="https://img.example/mic.png", link_mode=links.LINK_MODE_DIRECT,
            client=fp, environ={"PINTEREST_AUTOPOST_CONFIRMED": "1"})
        self.assertEqual(out["state"], STATE_PUBLISHED)

        m = measure.measure(d, pinterest_client=fp)
        self.assertEqual(fp.analytics_calls, ["pin_1"])
        act = SocialActionStore.load(d).all()[0]
        self.assertEqual(act.state, STATE_MEASURED)
        self.assertEqual(
            act.metrics["pinterest_analytics"]["all"]["summary_metrics"]["IMPRESSION"], 40)


if __name__ == "__main__":
    unittest.main()
