"""Offer Discovery architecture (Demand-First Affiliate architecture,
Offer Discovery MVP).

Covers: the `OfferSource` protocol shape, `OfferCandidate` (discovery-only
data, never `usable`), `HumanSetupRequiredOfferSource` (always `[]`, no
network call, correct policy_status), `build_offer_source()` factory
parity with `sources.build_source()`, and the `affiliate_sources.
offer_candidate_to_payload()` bridge - proving a candidate can never
silently become a usable `AffiliateOffer` without a human supplying the
missing commission/program facts.

No network access anywhere in this file.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import model
from revenue_os.ecosystem.affiliate_model import NETWORK_POLICY, network_policy
from revenue_os.ecosystem.affiliate_sources import (
    IngestionError,
    offer_candidate_to_payload,
    parse_offer_json,
)
from revenue_os.ecosystem.offer_sources import (
    HumanSetupRequiredOfferSource,
    OfferCandidate,
    build_offer_source,
)
from revenue_os.ecosystem.product_intent import ProductIntent


class OfferCandidateShapeTests(unittest.TestCase):
    """An OfferCandidate carries only discovery-time data - no status, no
    `usable` concept, nothing that would let it be mistaken for a
    validated, ingested AffiliateOffer."""

    def test_candidate_has_no_status_or_usable_field(self):
        cand = OfferCandidate(network="amazon_associates", title="Test Widget")
        self.assertFalse(hasattr(cand, "status"))
        self.assertFalse(hasattr(cand, "usable"))
        self.assertFalse(hasattr(cand, "active"))

    def test_defaults_are_all_empty_never_guessed(self):
        cand = OfferCandidate(network="amazon_associates", title="Test Widget")
        self.assertEqual(cand.url, "")
        self.assertEqual(cand.product_id, "")
        self.assertEqual(cand.price, 0.0)
        self.assertEqual(cand.currency, "")
        self.assertEqual(cand.availability, "")
        self.assertEqual(cand.observed_at, "")
        self.assertEqual(cand.provenance, "")
        self.assertEqual(cand.confidence, 0.0)

    def test_to_dict_round_trips_every_field(self):
        cand = OfferCandidate(
            network="amazon_associates", title="Test Earbuds XYZ (synthetic test data)",
            url="https://www.amazon.de/dp/B0TEST00000", product_id="B0TEST00000",
            price=29.99, currency="EUR", availability="In Stock",
            observed_at="2026-09-06T00:00:00+00:00",
            provenance="amazon_associates:SearchItems", confidence=0.8)
        d = cand.to_dict()
        for key in ("network", "title", "url", "product_id", "price", "currency",
                    "availability", "observed_at", "provenance", "confidence"):
            self.assertIn(key, d)
        self.assertEqual(d["product_id"], "B0TEST00000")


class HumanSetupRequiredOfferSourceTests(unittest.TestCase):
    def test_search_always_returns_empty_list(self):
        src = HumanSetupRequiredOfferSource("amazon_associates")
        intent = ProductIntent(category_phrase="bt earbuds",
                               intent="purchase_recommendation")
        self.assertEqual(src.search(intent, 10), [])

    def test_search_with_empty_or_no_intent_still_returns_empty(self):
        src = HumanSetupRequiredOfferSource("amazon_associates")
        self.assertEqual(src.search(ProductIntent(), 10), [])
        self.assertEqual(src.search(ProductIntent(), 0), [])

    def test_policy_status_is_human_setup_required(self):
        src = HumanSetupRequiredOfferSource("amazon_associates")
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertFalse(src.meta.automation_allowed)
        self.assertTrue(src.meta.requires_human)

    def test_setup_steps_come_from_the_existing_network_policy_table(self):
        src = HumanSetupRequiredOfferSource("amazon_associates")
        expected = network_policy("amazon_associates")
        self.assertEqual(src.setup_steps, expected["setup_steps"])
        self.assertEqual(src.note, expected["note"])

    def test_no_network_call_is_ever_made(self):
        # structural guarantee: this module imports no HTTP/network
        # library at all - a network call is not merely unused at
        # runtime, it is impossible from this file's own imports.
        import ast
        import inspect

        from revenue_os.ecosystem import offer_sources

        tree = ast.parse(inspect.getsource(offer_sources))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.update((alias.name or "").split(".")[0]
                               for alias in node.names)
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
        for forbidden in ("urllib", "http", "requests", "socket"):
            self.assertNotIn(forbidden, imported)

    def test_unknown_network_defaults_correctly_via_network_policy_fail_closed(self):
        src = HumanSetupRequiredOfferSource("some_never_configured_network")
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertEqual(src.search(ProductIntent(category_phrase="x", intent="y"), 5), [])


class BuildOfferSourceFactoryTests(unittest.TestCase):
    def test_amazon_associates_returns_human_setup_required_source(self):
        src = build_offer_source("amazon_associates")
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_every_known_network_except_human_fed_is_registered(self):
        for network in NETWORK_POLICY:
            if network == "human_fed":
                continue
            src = build_offer_source(network)
            self.assertIsInstance(src, HumanSetupRequiredOfferSource)

    def test_human_fed_is_not_a_searchable_network(self):
        with self.assertRaises(ValueError):
            build_offer_source("human_fed")

    def test_unknown_network_raises_clear_error(self):
        with self.assertRaises(ValueError):
            build_offer_source("totally_unknown_network")

    def test_case_and_whitespace_insensitive(self):
        src = build_offer_source("  Amazon_Associates  ")
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)


class OfferCandidateToPayloadBridgeTests(unittest.TestCase):
    """The one-way, non-fabricating translation into parse_offer_json()'s
    schema - proving a candidate cannot silently become a usable offer."""

    def _candidate(self, **kw) -> OfferCandidate:
        base = dict(
            network="amazon_associates", title="Synthetic Test Earbuds (test fixture)",
            url="https://www.amazon.de/dp/B0TESTXXXX",
            product_id="B0TESTXXXX", price=24.99, currency="EUR",
            availability="In Stock", observed_at="2026-09-06T00:00:00+00:00",
            provenance="amazon_associates:SearchItems (synthetic test)", confidence=0.75)
        base.update(kw)
        return OfferCandidate(**base)

    def test_payload_never_contains_commission_fields(self):
        payload = offer_candidate_to_payload(self._candidate())
        for forbidden in ("commission_kind", "commission_rate",
                         "commission_fixed_amount", "commission_evidence"):
            self.assertNotIn(forbidden, payload)

    def test_payload_never_contains_program_name_or_human_confirmed_joined(self):
        payload = offer_candidate_to_payload(self._candidate())
        self.assertNotIn("program_name", payload)
        self.assertNotIn("human_confirmed_joined", payload)

    def test_candidate_alone_cannot_pass_validation(self):
        payload = offer_candidate_to_payload(self._candidate())
        with self.assertRaises(IngestionError) as ctx:
            parse_offer_json(payload)
        msg = str(ctx.exception)
        self.assertIn("commission_kind", msg)
        self.assertIn("human_confirmed_joined", msg)
        self.assertIn("program_name", msg)

    def test_candidate_becomes_valid_only_once_a_human_supplies_the_rest(self):
        # proves the bridge is not simply broken - adding the REAL,
        # human-supplied facts a search result could never provide makes
        # it a normal, valid ingestion payload, unchanged validation path.
        payload = offer_candidate_to_payload(self._candidate())
        payload.update({
            "program_name": "Amazon.de PartnerNet (test)",
            "commission_kind": "percent",
            "commission_rate": 0.03,
            "commission_evidence": ["synthetic test evidence - not a real rate card quote"],
            "human_confirmed_joined": True,
        })
        parsed = parse_offer_json(payload)   # must not raise
        self.assertEqual(parsed["product_name"], "Synthetic Test Earbuds (test fixture)")

    def test_real_fields_are_copied_verbatim_not_altered(self):
        cand = self._candidate()
        payload = offer_candidate_to_payload(cand)
        self.assertEqual(payload["product_name"], cand.title)
        self.assertEqual(payload["product_url"], cand.url)
        self.assertEqual(payload["product_asin"], cand.product_id)
        self.assertEqual(payload["product_price"], cand.price)
        self.assertEqual(payload["currency"], cand.currency)
        self.assertEqual(payload["price_observed_at"], cand.observed_at)
        self.assertFalse(payload["price_is_estimate"])

    def test_missing_optional_fields_are_simply_absent_not_defaulted(self):
        cand = OfferCandidate(network="amazon_associates", title="Bare Test Product")
        payload = offer_candidate_to_payload(cand)
        for absent in ("product_url", "product_asin", "product_price",
                      "currency", "price_observed_at", "evidence"):
            self.assertNotIn(absent, payload)

    def test_no_price_means_no_price_is_estimate_flag_either(self):
        cand = OfferCandidate(network="amazon_associates", title="No Price Test Product")
        payload = offer_candidate_to_payload(cand)
        self.assertNotIn("product_price", payload)
        self.assertNotIn("price_is_estimate", payload)


if __name__ == "__main__":
    unittest.main()
