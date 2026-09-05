"""CJ Affiliate (Commission Junction) real product search adapter (Demand-
First Affiliate architecture, Real Offer Discovery step).

Covers: missing credentials, HTTP/API errors, timeouts, 429 retry-then-
succeed, repeated-429 fail-closed, empty results, several candidates,
correct ProductIntent -> search-query translation, correct candidate
normalisation, malformed/incomplete rows never fabricating a candidate,
determinism, and `build_offer_source()` factory wiring (falls back to
`HumanSetupRequiredOfferSource` without credentials, returns a real
`CjOfferSource` with them).

No real network access, no real CJ credentials anywhere in this file -
every test injects a fixture `fetch` callable.
"""

from __future__ import annotations

import unittest
import unittest.mock
import urllib.error

from revenue_os.ecosystem import model
from revenue_os.ecosystem import cj_offer_source as cjs
from revenue_os.ecosystem.cj_offer_source import CjConfig, CjOfferSource, _parse_products
from revenue_os.ecosystem.offer_sources import (
    HumanSetupRequiredOfferSource,
    OfferCandidate,
    build_offer_source,
)
from revenue_os.ecosystem.product_intent import ProductIntent

_CFG = CjConfig(personal_access_token="test-pat", company_id="test-cid",
                advertiser_ids=("111", "222"))
_INTENT = ProductIntent(category_phrase="bt earbuds", intent="purchase_recommendation")

_sleep_patcher = None


def setUpModule():
    # every search() call goes through the real throttle/backoff sleep -
    # patch it once for the whole module so tests run fast and
    # deterministically, exactly like test_acquisition_sources.py does
    # for StackExchangeSource's own throttle/backoff tests.
    global _sleep_patcher
    _sleep_patcher = unittest.mock.patch.object(cjs.time, "sleep")
    _sleep_patcher.start()


def tearDownModule():
    _sleep_patcher.stop()


def _ok_body(rows):
    return {"data": {"products": {"resultList": rows}}}


class ConfigTests(unittest.TestCase):
    def test_missing_all_credentials_raises(self):
        with self.assertRaises(ValueError):
            CjConfig.from_env({})

    def test_missing_advertiser_ids_raises(self):
        env = {"CJ_PERSONAL_ACCESS_TOKEN": "x", "CJ_COMPANY_ID": "y"}
        with self.assertRaises(ValueError):
            CjConfig.from_env(env)

    def test_valid_env_parses_comma_separated_advertiser_ids(self):
        env = {"CJ_PERSONAL_ACCESS_TOKEN": "x", "CJ_COMPANY_ID": "y",
              "CJ_ADVERTISER_IDS": " 111, 222 ,333"}
        cfg = CjConfig.from_env(env)
        self.assertEqual(cfg.advertiser_ids, ("111", "222", "333"))


class AuthorizedPropertyTests(unittest.TestCase):
    def test_authorized_true_with_explicit_config(self):
        src = CjOfferSource(config=_CFG)
        self.assertTrue(src.authorized)

    def test_authorized_false_without_env_or_config(self):
        src = CjOfferSource(environ={})
        self.assertFalse(src.authorized)

    def test_authorized_check_never_calls_the_network(self):
        def boom(*a, **kw):
            raise AssertionError("authorized must never call fetch")

        src = CjOfferSource(environ={}, fetch=boom)
        self.assertFalse(src.authorized)   # must not raise via boom()


class MissingCredentialsSearchTests(unittest.TestCase):
    def test_search_returns_empty_list_without_credentials(self):
        def boom(*a, **kw):
            raise AssertionError("must never reach the network without credentials")

        src = CjOfferSource(environ={}, fetch=boom)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_search_returns_empty_list_without_a_category_phrase(self):
        called = []

        def track(*a, **kw):
            called.append(1)
            return _ok_body([])

        src = CjOfferSource(config=_CFG, fetch=track)
        self.assertEqual(src.search(ProductIntent(), 5), [])
        self.assertEqual(called, [])   # never even attempts a call


class ApiErrorAndTimeoutTests(unittest.TestCase):
    def test_timeout_returns_empty_list_not_raise(self):
        def timeout(*a, **kw):
            raise TimeoutError("simulated timeout")

        src = CjOfferSource(config=_CFG, fetch=timeout)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_url_error_returns_empty_list(self):
        def fail(*a, **kw):
            raise urllib.error.URLError("simulated DNS failure")

        src = CjOfferSource(config=_CFG, fetch=fail)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_malformed_json_response_returns_empty_list(self):
        def bad_json(*a, **kw):
            raise ValueError("simulated JSON decode error")

        src = CjOfferSource(config=_CFG, fetch=bad_json)
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_non_429_http_error_fails_closed_without_retry(self):
        calls = []

        def fail_500(*a, **kw):
            calls.append(1)
            raise urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)

        src = CjOfferSource(config=_CFG, fetch=fail_500)
        self.assertEqual(src.search(_INTENT, 5), [])
        self.assertEqual(len(calls), 1)   # no retry for a non-429 error

    def test_429_is_retried_once_then_succeeds(self):
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            return _ok_body([{"id": "p1", "title": "Retry Success Product",
                              "buyUrl": "https://example.com/p1"}])

        src = CjOfferSource(config=_CFG, fetch=flaky)
        out = src.search(_INTENT, 5)   # time.sleep patched module-wide, see setUpModule
        self.assertEqual(len(out), 1)
        self.assertEqual(len(calls), 2)

    def test_repeated_429_fails_closed(self):
        def always_429(*a, **kw):
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)

        src = CjOfferSource(config=_CFG, fetch=always_429)
        out = src.search(_INTENT, 5)
        self.assertEqual(out, [])


class EmptyAndMultipleResultsTests(unittest.TestCase):
    def test_empty_result_list_returns_empty(self):
        src = CjOfferSource(config=_CFG, fetch=lambda *a, **kw: _ok_body([]))
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_missing_data_key_returns_empty(self):
        src = CjOfferSource(config=_CFG, fetch=lambda *a, **kw: {})
        self.assertEqual(src.search(_INTENT, 5), [])

    def test_several_candidates_are_all_returned(self):
        rows = [
            {"id": "p1", "title": "Earbuds A", "buyUrl": "https://example.com/a",
             "price": {"amount": 19.99, "currency": "EUR"}, "availability": "in stock"},
            {"id": "p2", "title": "Earbuds B", "buyUrl": "https://example.com/b",
             "price": {"amount": 39.99, "currency": "EUR"}, "availability": "backorder"},
        ]
        src = CjOfferSource(config=_CFG, fetch=lambda *a, **kw: _ok_body(rows))
        out = src.search(_INTENT, 5)
        self.assertEqual(len(out), 2)
        self.assertEqual({c.title for c in out}, {"Earbuds A", "Earbuds B"})


class ProductIntentToQueryTests(unittest.TestCase):
    def test_category_phrase_is_sent_as_keywords(self):
        seen = {}

        def capture(query, variables, *, token):
            seen.update(variables)
            return _ok_body([])

        src = CjOfferSource(config=_CFG, fetch=capture)
        src.search(ProductIntent(category_phrase="mechanical keyboard",
                                 intent="purchase_recommendation"), 5)
        self.assertEqual(seen["keywords"], "mechanical keyboard")

    def test_advertiser_ids_from_config_are_sent(self):
        seen = {}

        def capture(query, variables, *, token):
            seen.update(variables)
            return _ok_body([])

        src = CjOfferSource(config=_CFG, fetch=capture)
        src.search(_INTENT, 5)
        self.assertEqual(seen["advertiserIds"], ["111", "222"])

    def test_token_is_passed_to_fetch_not_embedded_in_query(self):
        seen = {}

        def capture(query, variables, *, token):
            seen["token"] = token
            self.assertNotIn("test-pat", query)
            return _ok_body([])

        src = CjOfferSource(config=_CFG, fetch=capture)
        src.search(_INTENT, 5)
        self.assertEqual(seen["token"], "test-pat")

    def test_limit_is_bounded(self):
        seen = {}

        def capture(query, variables, *, token):
            seen.update(variables)
            return _ok_body([])

        src = CjOfferSource(config=_CFG, fetch=capture)
        src.search(_INTENT, 500)
        self.assertLessEqual(seen["limit"], 25)


class CandidateNormalizationTests(unittest.TestCase):
    def test_fields_are_mapped_correctly(self):
        row = {"id": "B0TEST12", "title": "Normalized Test Product",
              "buyUrl": "https://example.com/normalized",
              "price": {"amount": 24.5, "currency": "EUR"}, "availability": "In Stock"}
        out = _parse_products(_ok_body([row]))
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertIsInstance(c, OfferCandidate)
        self.assertEqual(c.network, "cj_affiliate")
        self.assertEqual(c.title, "Normalized Test Product")
        self.assertEqual(c.url, "https://example.com/normalized")
        self.assertEqual(c.product_id, "B0TEST12")
        self.assertEqual(c.price, 24.5)
        self.assertEqual(c.currency, "EUR")
        self.assertEqual(c.availability, "In Stock")
        self.assertEqual(c.provenance, "cj_affiliate:products")
        self.assertTrue(c.observed_at)

    def test_missing_title_never_fabricates_a_candidate(self):
        row = {"id": "p1", "title": "", "buyUrl": "https://example.com/x"}
        self.assertEqual(_parse_products(_ok_body([row])), [])

    def test_missing_url_never_fabricates_a_candidate(self):
        row = {"id": "p1", "title": "No URL Product", "buyUrl": ""}
        self.assertEqual(_parse_products(_ok_body([row])), [])

    def test_missing_price_defaults_to_zero_not_guessed(self):
        row = {"id": "p1", "title": "No Price Product", "buyUrl": "https://example.com/x"}
        out = _parse_products(_ok_body([row]))
        self.assertEqual(out[0].price, 0.0)
        self.assertEqual(out[0].currency, "")

    def test_malformed_row_types_are_skipped_not_crashed(self):
        rows = ["not a dict", 12345, None,
               {"id": "p1", "title": "Valid Product", "buyUrl": "https://example.com/x"}]
        out = _parse_products(_ok_body(rows))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "Valid Product")

    def test_completely_malformed_body_returns_empty_not_crash(self):
        for bad_body in (None, [], "a string", {"data": None}, {"data": {"products": None}}):
            self.assertEqual(_parse_products(bad_body), [])


class DeterminismTests(unittest.TestCase):
    def test_same_response_yields_identical_candidates(self):
        rows = [{"id": "p1", "title": "Stable Product", "buyUrl": "https://example.com/x",
                "price": {"amount": 10.0, "currency": "EUR"}}]
        src = CjOfferSource(config=_CFG, fetch=lambda *a, **kw: _ok_body(rows))
        a = src.search(_INTENT, 5)
        b = src.search(_INTENT, 5)
        self.assertEqual([c.to_dict()["title"] for c in a], [c.to_dict()["title"] for c in b])


class BuildOfferSourceWiringTests(unittest.TestCase):
    def test_no_credentials_falls_back_to_human_setup_required(self):
        src = build_offer_source("cj_affiliate", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_with_credentials_returns_real_cj_offer_source(self):
        env = {"CJ_PERSONAL_ACCESS_TOKEN": "x", "CJ_COMPANY_ID": "y",
              "CJ_ADVERTISER_IDS": "111"}
        src = build_offer_source("cj_affiliate", environ=env)
        self.assertIsInstance(src, CjOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_OK)

    def test_other_networks_are_unaffected(self):
        src = build_offer_source("amazon_associates", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertNotIsInstance(src, CjOfferSource)


if __name__ == "__main__":
    unittest.main()
