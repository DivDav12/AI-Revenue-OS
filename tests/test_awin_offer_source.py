"""Awin real physical-product data-feed search adapter (Demand-First
Affiliate architecture, Real Offer Discovery step - Germany/EU physical
goods).

Covers: config validation, the `authorized` no-network-call guarantee,
feed-list/feed-download transport errors (fail closed, never raise),
empty/no-matching-advertiser results, real multi-row candidate
normalisation, category/keyword matching (relevant vs unrelated demand),
budget-constraint filtering (currency-matched only - never a guessed FX
conversion), malformed-row skipping (never fabricates), duplicate-row
dedup, exact affiliate URL (`aw_deep_link`) preservation, determinism,
`build_offer_source()` factory wiring, and the existing
`affiliate_matching`/`offer_selection` relevance gate applied UNCHANGED
to an Awin-sourced `AffiliateOffer`.

No real network access anywhere in this file - every test injects a
fixture `fetch` callable.
"""

from __future__ import annotations

import unittest
import unittest.mock
import urllib.error

from revenue_os.ecosystem import model
from revenue_os.ecosystem import awin_offer_source as awn
from revenue_os.ecosystem.affiliate_matching import match_offers
from revenue_os.ecosystem.affiliate_model import AffiliateOffer, CommissionModel, NETWORK_AWIN, network_policy
from revenue_os.ecosystem.affiliate_sources import IngestionError, offer_candidate_to_payload, parse_offer_json
from revenue_os.ecosystem.awin_offer_source import AwinConfig, AwinOfferSource
from revenue_os.ecosystem.model import OpportunityDraft
from revenue_os.ecosystem.offer_selection import select_best_offer
from revenue_os.ecosystem.offer_sources import HumanSetupRequiredOfferSource, OfferCandidate, build_offer_source
from revenue_os.ecosystem.product_intent import ProductIntent

_CFG = AwinConfig(api_key="test-key", advertiser_ids=("1234", "5678"))
_INTENT = ProductIntent(category_phrase="bluetooth headphones", intent="purchase_recommendation")

_sleep_patcher = None


def setUpModule():
    global _sleep_patcher
    _sleep_patcher = unittest.mock.patch.object(awn.time, "sleep")
    _sleep_patcher.start()


def tearDownModule():
    _sleep_patcher.stop()


_FEED_LIST_CSV = (
    "Advertiser ID,Advertiser Name,URL\n"
    "1234,Test Electronics Retailer,https://productdata.awin.com/download/feed1234\n"
    "5678,Other Test Retailer,https://productdata.awin.com/download/feed5678\n"
    "9999,Unapproved Retailer,https://productdata.awin.com/download/feed9999\n"
)

_HEADPHONES_ROW = (
    "Bluetooth Headphones XZ200,"
    "https://www.awin1.com/cread.php?awinmid=1234&awinaffid=999&p=https%3A%2F%2Fexample.com%2Fxz200,"
    "Over-ear noise cancelling bluetooth headphones,Headphones,79.99,EUR,1,SKU-XZ200"
)
_MOUSE_ROW = (
    "Ergonomic Wireless Mouse M5,"
    "https://www.awin1.com/cread.php?awinmid=1234&awinaffid=999&p=https%3A%2F%2Fexample.com%2Fm5,"
    "Wireless ergonomic mouse for home office,Mice,29.99,EUR,1,SKU-M5"
)
_HEADER = "product_name,aw_deep_link,description,merchant_category,search_price,currency,in_stock,aw_product_id"


def _feed_csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


def _fetch_map(mapping: dict) -> callable:
    def fetch(url):
        if url not in mapping:
            raise AssertionError(f"unexpected fetch URL: {url}")
        body = mapping[url]
        return body.encode("utf-8") if isinstance(body, str) else body
    return fetch


class ConfigTests(unittest.TestCase):
    def test_missing_all_credentials_raises(self):
        with self.assertRaises(awn.ConfigError):
            AwinConfig.from_env({})

    def test_missing_advertiser_ids_raises(self):
        with self.assertRaises(awn.ConfigError):
            AwinConfig.from_env({"AWIN_DATAFEED_API_KEY": "x"})

    def test_valid_env_parses_comma_separated_advertiser_ids(self):
        env = {"AWIN_DATAFEED_API_KEY": "x", "AWIN_ADVERTISER_IDS": " 1234, 5678 ,999"}
        cfg = AwinConfig.from_env(env)
        self.assertEqual(cfg.advertiser_ids, ("1234", "5678", "999"))


class AuthorizedPropertyTests(unittest.TestCase):
    def test_authorized_true_with_explicit_config(self):
        self.assertTrue(AwinOfferSource(config=_CFG).authorized)

    def test_authorized_false_without_env_or_config(self):
        self.assertFalse(AwinOfferSource(environ={}).authorized)

    def test_authorized_check_never_calls_the_network(self):
        def boom(*a, **kw):
            raise AssertionError("authorized must never call fetch")

        src = AwinOfferSource(environ={}, fetch=boom)
        self.assertFalse(src.authorized)


class MissingCredentialsSearchTests(unittest.TestCase):
    def test_search_returns_empty_list_without_credentials(self):
        def boom(*a, **kw):
            raise AssertionError("must never reach the network without credentials")

        src = AwinOfferSource(environ={}, fetch=boom)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_search_returns_empty_list_without_a_category_phrase(self):
        called = []

        def track(url):
            called.append(url)
            return b""

        src = AwinOfferSource(config=_CFG, fetch=track)
        self.assertEqual(src.search(ProductIntent(), 5), [])
        self.assertEqual(called, [])

    def test_zero_or_negative_limit_returns_nothing_without_a_call(self):
        def boom(*a, **kw):
            raise AssertionError("must never fetch for a non-positive limit")

        src = AwinOfferSource(config=_CFG, fetch=boom)
        self.assertEqual(src.search(_INTENT, 0), [])
        self.assertEqual(src.search(_INTENT, -1), [])


class TransportErrorTests(unittest.TestCase):
    def test_feed_list_http_error_fails_closed(self):
        def fail(url):
            raise urllib.error.HTTPError(url, 500, "err", {}, None)

        src = AwinOfferSource(config=_CFG, fetch=fail)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_feed_list_timeout_fails_closed(self):
        def fail(url):
            raise TimeoutError("simulated timeout")

        src = AwinOfferSource(config=_CFG, fetch=fail)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_one_bad_feed_does_not_kill_the_whole_search(self):
        feed_url_1234 = "https://productdata.awin.com/download/feed1234"
        feed_url_5678 = "https://productdata.awin.com/download/feed5678"

        def fetch(url):
            if url == _FEED_LIST_URL():
                return _FEED_LIST_CSV.encode("utf-8")
            if url == feed_url_1234:
                raise urllib.error.URLError("simulated DNS failure")
            if url == feed_url_5678:
                return _feed_csv(_HEADPHONES_ROW).encode("utf-8")
            raise AssertionError(f"unexpected url {url}")

        src = AwinOfferSource(config=_CFG, fetch=fetch)
        out = src.search(_INTENT, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Bluetooth Headphones XZ200")


def _FEED_LIST_URL() -> str:
    return awn._FEED_LIST_URL.format(api_key="test-key")


class EmptyAndNoMatchTests(unittest.TestCase):
    def test_empty_feed_list_returns_empty(self):
        src = AwinOfferSource(config=_CFG, fetch=_fetch_map({_FEED_LIST_URL(): ""}))
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_no_configured_advertiser_id_in_feed_list_returns_empty(self):
        cfg = AwinConfig(api_key="test-key", advertiser_ids=("does-not-exist",))
        src = AwinOfferSource(config=cfg, fetch=_fetch_map({_FEED_LIST_URL(): _FEED_LIST_CSV}))
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_unapproved_advertiser_feed_is_never_fetched(self):
        def fetch(url):
            if "feed9999" in url:
                raise AssertionError("must never fetch a feed for an unapproved advertiser id")
            if url == _FEED_LIST_URL():
                return _FEED_LIST_CSV.encode("utf-8")
            return _feed_csv().encode("utf-8")

        src = AwinOfferSource(config=_CFG, fetch=fetch)
        src.search(_INTENT, 5)   # must not raise via the assertion above


class ValidProductsTests(unittest.TestCase):
    def _src(self, *rows: str) -> AwinOfferSource:
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": _feed_csv(*rows),
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        return AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))

    def test_relevant_product_is_returned(self):
        out = self._src(_HEADPHONES_ROW).search(_INTENT, 5)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertIsInstance(c, OfferCandidate)
        self.assertEqual(c.network, "awin")
        self.assertEqual(c.title, "Bluetooth Headphones XZ200")
        self.assertEqual(c.price, 79.99)
        self.assertEqual(c.currency, "EUR")
        self.assertEqual(c.availability, "1")
        self.assertEqual(c.product_id, "SKU-XZ200")
        self.assertEqual(c.provenance, "awin:datafeed")
        self.assertTrue(c.observed_at)

    def test_several_matching_products_are_returned(self):
        second = _HEADPHONES_ROW.replace("XZ200", "XZ300").replace("SKU-XZ200", "SKU-XZ300").replace(
            "example.com%2Fxz200", "example.com%2Fxz300")
        out = self._src(_HEADPHONES_ROW, second).search(_INTENT, 5)
        self.assertEqual(len(out), 2)
        self.assertEqual({c.title for c in out}, {"Bluetooth Headphones XZ200",
                                                   "Bluetooth Headphones XZ300"})

    def test_unrelated_product_is_not_returned(self):
        out = self._src(_MOUSE_ROW).search(_INTENT, 5)
        self.assertEqual(out, [])

    def test_mixed_relevant_and_unrelated_only_relevant_returned(self):
        out = self._src(_HEADPHONES_ROW, _MOUSE_ROW).search(_INTENT, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Bluetooth Headphones XZ200")

    def test_category_matching_for_mouse_intent(self):
        intent = ProductIntent(category_phrase="wireless mouse", intent="purchase_recommendation")
        out = self._src(_HEADPHONES_ROW, _MOUSE_ROW).search(intent, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Ergonomic Wireless Mouse M5")

    def test_limit_is_respected(self):
        rows = []
        for i in range(5):
            rows.append(_HEADPHONES_ROW.replace("XZ200", f"XZ{i}").replace(
                "SKU-XZ200", f"SKU-XZ{i}").replace("example.com%2Fxz200", f"example.com%2Fxz{i}"))
        out = self._src(*rows).search(_INTENT, 2)
        self.assertEqual(len(out), 2)


class BudgetConstraintTests(unittest.TestCase):
    def _src(self, *rows: str) -> AwinOfferSource:
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": _feed_csv(*rows),
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        return AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))

    def test_over_budget_product_is_excluded(self):
        intent = ProductIntent(category_phrase="bluetooth headphones",
                               intent="purchase_recommendation", constraints=("budget:50EUR",))
        out = self._src(_HEADPHONES_ROW).search(intent, 5)   # 79.99 EUR > 50 EUR budget
        self.assertEqual(out, [])

    def test_within_budget_product_is_included(self):
        intent = ProductIntent(category_phrase="bluetooth headphones",
                               intent="purchase_recommendation", constraints=("budget:100EUR",))
        out = self._src(_HEADPHONES_ROW).search(intent, 5)
        self.assertEqual(len(out), 1)

    def test_mismatched_currency_never_guesses_fx_conversion(self):
        # a USD budget against a EUR-priced product - must never silently
        # exclude based on a guessed conversion rate.
        intent = ProductIntent(category_phrase="bluetooth headphones",
                               intent="purchase_recommendation", constraints=("budget:10USD",))
        out = self._src(_HEADPHONES_ROW).search(intent, 5)
        self.assertEqual(len(out), 1)

    def test_no_budget_constraint_never_filters(self):
        out = self._src(_HEADPHONES_ROW).search(_INTENT, 5)
        self.assertEqual(len(out), 1)


class MalformedDataTests(unittest.TestCase):
    def _src(self, csv_body: str) -> AwinOfferSource:
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": csv_body,
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        return AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))

    def test_missing_title_never_fabricates_a_candidate(self):
        row = ",https://example.com/x,bluetooth headphones,Headphones,10,EUR,1,SKU"
        self.assertEqual(self._src(_feed_csv(row)).search(_INTENT, 5), [])

    def test_missing_url_never_fabricates_a_candidate(self):
        row = "Bluetooth Headphones,,bluetooth headphones,Headphones,10,EUR,1,SKU"
        self.assertEqual(self._src(_feed_csv(row)).search(_INTENT, 5), [])

    def test_missing_price_defaults_to_zero_not_guessed(self):
        row = "Bluetooth Headphones,https://example.com/x,bluetooth headphones,Headphones,,EUR,1,SKU"
        out = self._src(_feed_csv(row)).search(_INTENT, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].price, 0.0)

    def test_completely_empty_or_garbage_body_returns_empty_not_crash(self):
        for body in ("", "not,even,a,proper\nheader", "\x00\x01\x02"):
            self.assertEqual(self._src(body).search(_INTENT, 5), [])


class DuplicateAndDeterminismTests(unittest.TestCase):
    def test_duplicate_rows_are_deduped(self):
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": _feed_csv(_HEADPHONES_ROW, _HEADPHONES_ROW),
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        src = AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))
        out = src.search(_INTENT, 5)
        self.assertEqual(len(out), 1)

    def test_same_response_yields_identical_candidates(self):
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": _feed_csv(_HEADPHONES_ROW),
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        src = AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))
        a = src.search(_INTENT, 5)
        b = src.search(_INTENT, 5)
        self.assertEqual([c.to_dict()["title"] for c in a], [c.to_dict()["title"] for c in b])


class AffiliateAttributionTests(unittest.TestCase):
    def test_aw_deep_link_preserved_exactly(self):
        mapping = {
            _FEED_LIST_URL(): _FEED_LIST_CSV,
            "https://productdata.awin.com/download/feed1234": _feed_csv(_HEADPHONES_ROW),
            "https://productdata.awin.com/download/feed5678": _feed_csv(),
        }
        src = AwinOfferSource(config=_CFG, fetch=_fetch_map(mapping))
        out = src.search(_INTENT, 5)
        expected_url = _HEADPHONES_ROW.split(",")[1]
        self.assertEqual(out[0].url, expected_url)


class BuildOfferSourceWiringTests(unittest.TestCase):
    def test_no_credentials_falls_back_to_human_setup_required(self):
        src = build_offer_source("awin", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_with_credentials_returns_real_awin_offer_source(self):
        env = {"AWIN_DATAFEED_API_KEY": "x", "AWIN_ADVERTISER_IDS": "1234"}
        src = build_offer_source("awin", environ=env)
        self.assertIsInstance(src, AwinOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_OK)

    def test_other_networks_are_unaffected(self):
        src = build_offer_source("amazon_associates", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertNotIsInstance(src, AwinOfferSource)


class NetworkPolicyTests(unittest.TestCase):
    def test_awin_is_registered(self):
        policy = network_policy(NETWORK_AWIN)
        self.assertEqual(policy["status"], model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertTrue(policy["setup_steps"])


class OfferCandidateToPayloadBridgeTests(unittest.TestCase):
    def test_candidate_alone_cannot_pass_validation(self):
        cand = OfferCandidate(network=NETWORK_AWIN, title="Bluetooth Headphones XZ200",
                              url="https://www.awin1.com/cread.php?p=x", price=79.99, currency="EUR",
                              provenance="awin:datafeed", confidence=1.0)
        payload = offer_candidate_to_payload(cand)
        self.assertNotIn("human_confirmed_joined", payload)
        self.assertNotIn("commission_kind", payload)
        with self.assertRaises(IngestionError):
            parse_offer_json(payload)


# ---------------------------------------------------------------------------
# ProductIntent -> AffiliateOffer matching, via the EXISTING, unmodified
# affiliate_matching/offer_selection gates - no Awin-specific bypass.
# ---------------------------------------------------------------------------

_AWIN_HEADPHONES_OFFER = AffiliateOffer(
    offer_id="aff-awin-test", network=NETWORK_AWIN,
    program_name="Test Electronics Retailer (Awin)",
    product_name="Bluetooth Headphones XZ200",
    product_url="https://www.awin1.com/cread.php?p=x", product_price=79.99, currency="EUR",
    price_is_estimate=False,
    commission=CommissionModel(kind="percent", rate=0.05, currency="EUR", is_estimate=True,
                               evidence=("SYNTHETIC TEST commission - not a verified real rate",)),
    category="headphones",
    keywords=("headphones", "bluetooth", "wireless", "over-ear", "noise cancelling"),
    status=model.POLICY_OK, active=True,
)

_JBL_OFFER = AffiliateOffer(
    offer_id="aff-jbl-test", network="amazon_associates",
    program_name="Amazon.de PartnerNet", product_name="JBL Quantum Stream Talk",
    product_url="https://www.amazon.de/dp/B0CQP5NL72", product_asin="B0CQP5NL72",
    product_price=39.99, currency="EUR", price_is_estimate=True,
    commission=CommissionModel(kind="percent", rate=0.03, currency="EUR", is_estimate=False,
                               evidence=("Amazon PartnerNet standard fee schedule",)),
    category="usb-microphone-streaming",
    keywords=("microphone", "usb-mikrofon", "usb microphone", "streaming", "discord",
             "gaming", "podcast", "creator", "voice chat"),
    status=model.POLICY_OK, tracking_param="tag", tracking_value="airevenue-21", active=True,
)


def _draft(title: str, category: str = "other") -> OpportunityDraft:
    return OpportunityDraft(title=title, description="", evidence=[], category=category)


class ProductIntentMatchingTests(unittest.TestCase):
    def test_relevant_demand_matches_awin_offer(self):
        draft = _draft("what bluetooth headphones would you recommend for the office",
                      category="headphones")
        matches = match_offers(draft, [_AWIN_HEADPHONES_OFFER])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].offer.offer_id, _AWIN_HEADPHONES_OFFER.offer_id)

    def test_unrelated_demand_does_not_match(self):
        draft = _draft("what usb microphone should I get for podcasting",
                      category="usb-microphone-streaming")
        matches = match_offers(draft, [_AWIN_HEADPHONES_OFFER])
        self.assertEqual(matches, [])

    def test_relevance_threshold_still_enforced(self):
        draft = _draft("just chatting about audio gear in general", category="chit-chat")
        matches = match_offers(draft, [_AWIN_HEADPHONES_OFFER], min_score=0.15)
        self.assertTrue(all(m.match_score >= 0.15 for m in matches))


class SelectionBetweenOffersTests(unittest.TestCase):
    def test_relevant_offer_wins(self):
        draft = _draft("what bluetooth over-ear headphones should I buy", category="headphones")
        matches = match_offers(draft, [_AWIN_HEADPHONES_OFFER, _JBL_OFFER])
        selected = select_best_offer(matches)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _AWIN_HEADPHONES_OFFER.offer_id)

    def test_other_offer_wins_when_actually_relevant(self):
        draft = _draft("need a replacement for my usb microphone for streaming",
                      category="usb-microphone-streaming")
        matches = match_offers(draft, [_AWIN_HEADPHONES_OFFER, _JBL_OFFER])
        selected = select_best_offer(matches)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _JBL_OFFER.offer_id)


if __name__ == "__main__":
    unittest.main()
