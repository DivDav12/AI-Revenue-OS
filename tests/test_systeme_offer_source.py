"""systeme.io curated affiliate offer source (Demand-First Affiliate
architecture, Real Offer Discovery step).

Covers: config validation (missing/malformed/mismatched `sa=`), the
`authorized` no-network-call guarantee, the category-relevance gate (real
systeme.io-relevant categories match, unrelated consumer-product demand
does not), candidate normalisation (exact URL preservation, no fabricated
price/commission/availability), `build_offer_source()` factory wiring,
the existing `affiliate_matching`/`offer_selection` relevance gate applied
UNCHANGED to a systeme.io `AffiliateOffer`, and that an irrelevant but
"profitable" systeme.io offer can never outrank a relevant one.

No real network access anywhere in this file. The real affiliate id/link
the user supplied are used ONLY as fixtures here - never live-called.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import model
from revenue_os.ecosystem.affiliate_matching import match_offers
from revenue_os.ecosystem.affiliate_model import (
    AffiliateOffer,
    CommissionModel,
    NETWORK_POLICY,
    NETWORK_SYSTEME_IO,
    network_policy,
)
from revenue_os.ecosystem.affiliate_sources import IngestionError, offer_candidate_to_payload, parse_offer_json
from revenue_os.ecosystem.model import OpportunityDraft
from revenue_os.ecosystem.offer_selection import select_best_offer
from revenue_os.ecosystem.offer_sources import HumanSetupRequiredOfferSource, OfferCandidate, build_offer_source
from revenue_os.ecosystem.product_intent import ProductIntent
from revenue_os.ecosystem.systeme_offer_source import (
    ConfigError,
    SystemeIoConfig,
    SystemeIoOfferSource,
)

#: the real values the user supplied - used only as test fixtures, never
#: dialed live.
_REAL_AFFILIATE_ID = "sa0280859903879bd9c30e8335e36983c5a1ffb0de"
_REAL_AFFILIATE_URL = f"https://systeme.io/de?sa={_REAL_AFFILIATE_ID}"
_VALID_ENV = {"SYSTEME_IO_AFFILIATE_ID": _REAL_AFFILIATE_ID,
             "SYSTEME_IO_AFFILIATE_URL": _REAL_AFFILIATE_URL}
_CFG = SystemeIoConfig(affiliate_id=_REAL_AFFILIATE_ID, affiliate_url=_REAL_AFFILIATE_URL)


class ConfigTests(unittest.TestCase):
    def test_missing_env_raises(self):
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env({})

    def test_missing_url_only_raises(self):
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env({"SYSTEME_IO_AFFILIATE_ID": _REAL_AFFILIATE_ID})

    def test_valid_env_parses(self):
        cfg = SystemeIoConfig.from_env(_VALID_ENV)
        self.assertEqual(cfg.affiliate_id, _REAL_AFFILIATE_ID)
        self.assertEqual(cfg.affiliate_url, _REAL_AFFILIATE_URL)

    def test_non_https_url_fails_closed(self):
        env = dict(_VALID_ENV, SYSTEME_IO_AFFILIATE_URL=f"http://systeme.io/de?sa={_REAL_AFFILIATE_ID}")
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env(env)

    def test_wrong_host_fails_closed(self):
        env = dict(_VALID_ENV,
                  SYSTEME_IO_AFFILIATE_URL=f"https://evil-redirector.example/?sa={_REAL_AFFILIATE_ID}")
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env(env)

    def test_missing_sa_param_fails_closed(self):
        env = dict(_VALID_ENV, SYSTEME_IO_AFFILIATE_URL="https://systeme.io/de")
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env(env)

    def test_mismatched_sa_param_fails_closed(self):
        env = dict(_VALID_ENV, SYSTEME_IO_AFFILIATE_URL="https://systeme.io/de?sa=someone-elses-id")
        with self.assertRaises(ConfigError):
            SystemeIoConfig.from_env(env)


class AuthorizedPropertyTests(unittest.TestCase):
    def test_authorized_true_with_explicit_config(self):
        self.assertTrue(SystemeIoOfferSource(config=_CFG).authorized)

    def test_authorized_false_without_env_or_config(self):
        self.assertFalse(SystemeIoOfferSource(environ={}).authorized)

    def test_authorized_true_with_valid_env(self):
        self.assertTrue(SystemeIoOfferSource(environ=_VALID_ENV).authorized)


class NoNetworkCallStructuralTests(unittest.TestCase):
    def test_module_imports_no_network_capable_library(self):
        # urllib.parse (pure string parsing, used to validate the
        # affiliate URL) is fine and expected here - urllib.request,
        # http.client, requests, socket (anything that can actually open a
        # connection) must never appear.
        import ast
        import inspect

        from revenue_os.ecosystem import systeme_offer_source

        tree = ast.parse(inspect.getsource(systeme_offer_source))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = ("urllib.request", "http.client", "requests", "socket")
        for mod in imported:
            for f in forbidden:
                self.assertFalse(mod == f or mod.startswith(f + "."),
                                 f"unexpected network-capable import: {mod!r}")

    def test_declares_itself_as_curated_not_a_search_api(self):
        self.assertEqual(SystemeIoOfferSource.discovery_mode, "curated")
        self.assertFalse(SystemeIoOfferSource.product_search_available)


class SearchGatingTests(unittest.TestCase):
    _RELEVANT_PHRASES = (
        "funnel software", "sales funnel builder", "marketing automation",
        "website builder", "funnel builder", "online business platform",
        "email marketing automation", "creator business software",
    )
    _IRRELEVANT_PHRASES = (
        "usb microphone", "bluetooth headphones", "gaming headset",
        "vpn service", "mechanical keyboard", "wireless earbuds",
        # real false positive found via a live discovery run against
        # genuine demand (opp_80128ad91d4d) before the bare "platform"/
        # "business" tokens were replaced with anchored phrases - kept as
        # a permanent regression guard.
        "open source food delivery platform",
    )

    def test_relevant_categories_return_the_one_offer(self):
        src = SystemeIoOfferSource(config=_CFG)
        for phrase in self._RELEVANT_PHRASES:
            with self.subTest(phrase=phrase):
                out = src.search(ProductIntent(category_phrase=phrase,
                                               intent="purchase_recommendation"), 5)
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0].network, NETWORK_SYSTEME_IO)

    def test_unrelated_categories_return_nothing(self):
        src = SystemeIoOfferSource(config=_CFG)
        for phrase in self._IRRELEVANT_PHRASES:
            with self.subTest(phrase=phrase):
                out = src.search(ProductIntent(category_phrase=phrase,
                                               intent="purchase_recommendation"), 5)
                self.assertEqual(out, [])

    def test_empty_category_phrase_returns_nothing(self):
        src = SystemeIoOfferSource(config=_CFG)
        self.assertEqual(src.search(ProductIntent(), 5), [])

    def test_zero_or_negative_limit_returns_nothing(self):
        src = SystemeIoOfferSource(config=_CFG)
        intent = ProductIntent(category_phrase="funnel builder", intent="purchase_recommendation")
        self.assertEqual(src.search(intent, 0), [])
        self.assertEqual(src.search(intent, -1), [])

    def test_missing_config_returns_nothing_even_if_relevant(self):
        src = SystemeIoOfferSource(environ={})
        intent = ProductIntent(category_phrase="funnel builder", intent="purchase_recommendation")
        self.assertEqual(src.search(intent, 5), [])


class CandidateNormalizationTests(unittest.TestCase):
    def _one_candidate(self) -> OfferCandidate:
        src = SystemeIoOfferSource(config=_CFG)
        out = src.search(ProductIntent(category_phrase="funnel builder",
                                       intent="purchase_recommendation"), 5)
        self.assertEqual(len(out), 1)
        return out[0]

    def test_affiliate_url_preserved_exactly(self):
        c = self._one_candidate()
        self.assertEqual(c.url, _REAL_AFFILIATE_URL)
        self.assertIn(f"sa={_REAL_AFFILIATE_ID}", c.url)

    def test_no_fabricated_price(self):
        c = self._one_candidate()
        self.assertEqual(c.price, 0.0)
        self.assertEqual(c.currency, "")

    def test_no_fabricated_availability_or_product_id(self):
        c = self._one_candidate()
        self.assertEqual(c.availability, "")
        self.assertEqual(c.product_id, "")

    def test_provenance_and_observed_at_are_set(self):
        c = self._one_candidate()
        self.assertEqual(c.provenance, "systeme_io:curated_affiliate_link")
        self.assertTrue(c.observed_at)

    def test_two_searches_yield_duplicate_equivalent_candidates(self):
        a = self._one_candidate()
        b = self._one_candidate()
        self.assertEqual(a.network, b.network)
        self.assertEqual(a.title, b.title)
        self.assertEqual(a.url, b.url)


class BuildOfferSourceWiringTests(unittest.TestCase):
    def test_no_config_falls_back_to_human_setup_required(self):
        src = build_offer_source("systeme_io", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_with_config_returns_real_systeme_io_offer_source(self):
        src = build_offer_source("systeme_io", environ=_VALID_ENV)
        self.assertIsInstance(src, SystemeIoOfferSource)

    def test_other_networks_are_unaffected(self):
        src = build_offer_source("amazon_associates", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertNotIsInstance(src, SystemeIoOfferSource)


class NetworkPolicyTests(unittest.TestCase):
    def test_systeme_io_is_registered(self):
        self.assertIn(NETWORK_SYSTEME_IO, NETWORK_POLICY)

    def test_policy_defaults_to_human_setup_required(self):
        policy = network_policy(NETWORK_SYSTEME_IO)
        self.assertEqual(policy["status"], model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertTrue(policy["setup_steps"])


class OfferCandidateToPayloadBridgeTests(unittest.TestCase):
    """Same fail-closed bridge every other network already goes through -
    a discovered candidate is never, by itself, a joined/usable program."""

    def test_candidate_alone_cannot_pass_validation(self):
        cand = OfferCandidate(network=NETWORK_SYSTEME_IO, title="systeme.io", url=_REAL_AFFILIATE_URL,
                              provenance="systeme_io:curated_affiliate_link", confidence=1.0)
        payload = offer_candidate_to_payload(cand)
        self.assertNotIn("human_confirmed_joined", payload)
        self.assertNotIn("commission_kind", payload)
        with self.assertRaises(IngestionError):
            parse_offer_json(payload)

    def test_becomes_valid_only_once_a_human_supplies_the_rest(self):
        cand = OfferCandidate(network=NETWORK_SYSTEME_IO, title="systeme.io", url=_REAL_AFFILIATE_URL,
                              provenance="systeme_io:curated_affiliate_link", confidence=1.0)
        payload = offer_candidate_to_payload(cand)
        payload.update({
            "program_name": "systeme.io Affiliate Program",
            "commission_kind": "recurring_percent",
            "commission_rate": 0.40,
            "commission_evidence": ["SYNTHETIC TEST evidence - not a verified real rate card quote"],
            "human_confirmed_joined": True,
        })
        parsed = parse_offer_json(payload)   # must not raise
        self.assertEqual(parsed["product_url"], _REAL_AFFILIATE_URL)


# ---------------------------------------------------------------------------
# ProductIntent -> AffiliateOffer matching, via the EXISTING, unmodified
# affiliate_matching/offer_selection gates - no systeme.io-specific bypass.
# ---------------------------------------------------------------------------

#: a hypothetical, already-ingested systeme.io AffiliateOffer, shaped the
#: way a human would actually fill it in after confirming they joined -
#: the commission rate/terms are clearly-labelled SYNTHETIC TEST data
#: (this session verified no official published commission figure), never
#: a claim about systeme.io's real payout.
_SYSTEME_OFFER = AffiliateOffer(
    offer_id="aff-systeme-io-test", network=NETWORK_SYSTEME_IO,
    program_name="systeme.io Affiliate Program",
    product_name="systeme.io - all-in-one funnel, email & online business platform",
    product_url=_REAL_AFFILIATE_URL, product_price=0.0, currency="EUR",
    commission=CommissionModel(kind="recurring_percent", rate=0.40, currency="EUR",
                               is_estimate=True,
                               evidence=("SYNTHETIC TEST commission - not a verified real rate",)),
    category="online-business-platform",
    keywords=("funnel", "funnels", "sales funnel", "funnel builder", "marketing automation",
             "email marketing", "website builder", "landing page", "crm",
             "online business", "creator"),
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
    def test_relevant_demand_matches_systeme_offer(self):
        draft = _draft("what sales funnel builder would you recommend for an online course",
                      category="online-business-platform")
        matches = match_offers(draft, [_SYSTEME_OFFER])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].offer.offer_id, _SYSTEME_OFFER.offer_id)

    def test_unrelated_demand_does_not_match(self):
        draft = _draft("what usb microphone would you recommend for streaming",
                      category="usb-microphone-streaming")
        matches = match_offers(draft, [_SYSTEME_OFFER])
        self.assertEqual(matches, [])

    def test_relevance_threshold_still_enforced(self):
        # a single, weak, incidental token overlap must not clear the gate
        draft = _draft("just chatting about business ideas today", category="chit-chat")
        matches = match_offers(draft, [_SYSTEME_OFFER], min_score=0.15)
        self.assertTrue(all(m.match_score >= 0.15 for m in matches))


class SelectionBetweenOffersTests(unittest.TestCase):
    def test_relevant_offer_wins_regardless_of_the_other_offers_economics(self):
        draft = _draft("need a replacement for my usb microphone for streaming",
                      category="usb-microphone-streaming")
        matches = match_offers(draft, [_SYSTEME_OFFER, _JBL_OFFER])
        selected = select_best_offer(matches)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _JBL_OFFER.offer_id)

    def test_systeme_offer_wins_when_actually_relevant(self):
        draft = _draft("which funnel builder / marketing automation platform should I use "
                      "for my online business", category="online-business-platform")
        matches = match_offers(draft, [_SYSTEME_OFFER, _JBL_OFFER])
        selected = select_best_offer(matches)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _SYSTEME_OFFER.offer_id)

    def test_irrelevant_high_commission_systeme_offer_cannot_beat_a_relevant_offer(self):
        # systeme.io's (synthetic-test) 40% recurring commission dwarfs
        # JBL's 3% one-off - if selection ever ranked by profitability
        # alone, systeme.io would win here despite being irrelevant to the
        # demand. The Stage-1 relevance gate must prevent that.
        draft = _draft("what usb microphone should I get for podcasting",
                      category="usb-microphone-streaming")
        all_offers = [_SYSTEME_OFFER, _JBL_OFFER]
        matches = match_offers(draft, all_offers, min_score=0.0)
        # confirm the premise: systeme.io really did score below the gate
        systeme_match = next(m for m in matches if m.offer.offer_id == _SYSTEME_OFFER.offer_id)
        self.assertLess(systeme_match.match_score, 0.15)
        selected = select_best_offer(matches, min_relevance=0.15)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _JBL_OFFER.offer_id)


if __name__ == "__main__":
    unittest.main()
