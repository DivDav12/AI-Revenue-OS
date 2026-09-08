"""social/ Phase 3 - Pinterest distribution flow. No network."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import affiliate_model, model
from revenue_os.social import compliance, links, oauth_store
from revenue_os.social import pinterest_flow
from revenue_os.social.store import STATE_HUMAN_REQUIRED, STATE_PUBLISHED, SocialActionStore


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _offer(**kw) -> affiliate_model.AffiliateOffer:
    base = dict(
        offer_id="aff-4564ed68a8e3", network="amazon_associates",
        program_name="Amazon PartnerNet",
        product_name="Amazon Basics Mini-USB-Kondensatormikrofon",
        product_url="https://www.amazon.de/dp/B0CL9BTQRF/", product_asin="B0CL9BTQRF",
        product_price=26.24, currency="EUR", price_is_estimate=False,
        category="mikrofone", keywords=("usb microphone", "budget microphone", "streaming"),
        evidence=("Cardioid pickup, plug and play",), status=model.POLICY_OK,
        tracking_param="tag", tracking_value="airevenue-21", active=True)
    base.update(kw)
    return affiliate_model.AffiliateOffer(**base)


def _setup(d: Path, *, live_url="https://divdav12.github.io/AI-Revenue-OS/mic-guide/index.html"):
    offers = affiliate_model.AffiliateOfferStore.load(d)
    offers.upsert(_offer())
    offers.save()
    assets = affiliate_model.AffiliateAssetStore.load(d)
    assets.upsert(affiliate_model.AffiliateAsset(
        asset_id="asset-mic", opportunity_id="opp_mic", offer_id="aff-4564ed68a8e3",
        title="Budget USB microphone for streaming and podcasting", slug="mic-guide",
        file_path="index.html", live_url=live_url,
        quality_checks={"meets_min_words": True, "has_disclosure": True,
                        "has_cta": True, "has_evidence": True, "has_demand_quote": True}))
    assets.save()


class FakePinterestClient:
    def __init__(self, *, available=True, boards=None, created=None):
        self.available = available
        self._boards = boards or [{"id": "board_123", "name": "Tech Picks"}]
        self._created = created if created is not None else []

    def list_boards(self):
        return list(self._boards)

    def resolve_board_id(self, name_or_id):
        for b in self._boards:
            if str(b["id"]) == name_or_id or b["name"].lower() == (name_or_id or "").lower():
                return str(b["id"])
        return ""

    def create_pin(self, *, board_id, title, description, link, alt_text="",
                   image_url="", image_base64="", i_have_read_the_rules=False):
        assert i_have_read_the_rules and board_id and image_url
        self._created.append({"board_id": board_id, "title": title, "link": link})
        return {"created": True, "pin_id": "pin_999",
                "url": "https://www.pinterest.com/pin/pin_999/", "raw": {}}


class TestPinterestFlow(unittest.TestCase):
    def test_draft_only_when_not_connected(self):
        d = _tmp()
        _setup(d)
        out = pinterest_flow.plan_pinterest(
            d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
            client=FakePinterestClient(available=False), environ={})
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["state"], STATE_HUMAN_REQUIRED)
        self.assertIn("#ad", out["draft"]["description"])
        self.assertIn("airevenue-21", out["draft"]["resolved_link"]["affiliate_url"])
        act = SocialActionStore.load(d).all()[0]
        self.assertIn("connect the Pinterest API", act.human_action_needed)

    def test_draft_only_without_creative_even_if_connected_and_attested(self):
        d = _tmp()
        _setup(d)
        out = pinterest_flow.plan_pinterest(
            d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
            client=FakePinterestClient(), environ={"PINTEREST_AUTOPOST_CONFIRMED": "1"})
        self.assertEqual(out["state"], STATE_HUMAN_REQUIRED)
        self.assertTrue(out["draft"]["creative_brief"])
        self.assertIn(out["dominant_keyword"], ("usb microphone", "budget microphone"))
        self.assertEqual(out["draft"]["keyword_basis"], "offer_keyword")
        act = SocialActionStore.load(d).all()[0]
        self.assertIn("--image-url", act.human_action_needed)

    def test_autopublish_when_connected_attested_and_creative_supplied(self):
        d = _tmp()
        _setup(d)
        created = []
        out = pinterest_flow.plan_pinterest(
            d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
            image_url="https://img.example/mic.png", link_mode=links.LINK_MODE_DIRECT,
            client=FakePinterestClient(created=created),
            environ={"PINTEREST_AUTOPOST_CONFIRMED": "1"})
        self.assertEqual(out["state"], STATE_PUBLISHED)
        self.assertEqual(out["published_url"], "https://www.pinterest.com/pin/pin_999/")
        self.assertEqual(len(created), 1)
        self.assertIn("tag=airevenue-21", created[0]["link"])
        act = SocialActionStore.load(d).all()[0]
        self.assertEqual(act.state, STATE_PUBLISHED)
        self.assertEqual(act.draft["pin_id"], "pin_999")

    def test_dedup(self):
        d = _tmp()
        _setup(d)
        for _ in range(2):
            out = pinterest_flow.plan_pinterest(
                d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
                client=FakePinterestClient(available=False), environ={})
        self.assertEqual(out["counts"].get("already_handled"), 1)
        self.assertEqual(len(SocialActionStore.load(d).all()), 1)

    def test_operator_keyword_override(self):
        d = _tmp()
        _setup(d)
        out = pinterest_flow.plan_pinterest(
            d, offer_id="aff-4564ed68a8e3", board="Tech Picks",
            keyword="usb condenser mic", client=FakePinterestClient(available=False),
            environ={})
        self.assertEqual(out["dominant_keyword"], "usb condenser mic")
        self.assertEqual(out["draft"]["keyword_basis"], "operator")

    def test_oauth_store_roundtrip(self):
        d = _tmp()
        oauth_store.put(d, "pinterest", refresh_token="rt-abc", access_token="at-xyz",
                        expires_in=3600)
        row = oauth_store.get(d, "pinterest")
        self.assertEqual(row["refresh_token"], "rt-abc")
        self.assertTrue(oauth_store.access_token_valid(row))


if __name__ == "__main__":
    unittest.main()
