"""Wondershare DE / PDFelement curated Awin affiliate offer source
(Demand-First Affiliate architecture, Real Offer Discovery step).

Covers: config validation (missing / non-https / wrong host / wrong path /
wrong advertiser / mismatched publisher id / non-wondershare destination),
the `authorized` no-network-call guarantee, the category-relevance gate
(real PDF-editing demand matches, unrelated consumer-product demand does
not), candidate normalisation (exact tracking-URL preservation, no
fabricated price / commission / availability), `build_offer_source()`
factory wiring (datafeed still wins when configured; curated link is the
fallback; nothing configured -> HUMAN_SETUP_REQUIRED), the fail-closed
`offer_candidate_to_payload()` bridge (a discovered candidate is never, by
itself, a joined/usable program), and the EXISTING, unmodified
`affiliate_matching` / `offer_selection` relevance gate applied to a
Wondershare `AffiliateOffer`.

No real network access anywhere in this file. The real advertiser /
publisher ids the user supplied are used ONLY as fixtures here - never
live-called.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import model
from revenue_os.ecosystem.affiliate_matching import match_offers
from revenue_os.ecosystem.affiliate_model import (
    AffiliateOffer,
    CommissionModel,
    NETWORK_AWIN,
)
from revenue_os.ecosystem.affiliate_sources import (
    IngestionError,
    offer_candidate_to_payload,
    parse_offer_json,
)
from revenue_os.ecosystem.model import OpportunityDraft
from revenue_os.ecosystem.offer_selection import select_best_offer
from revenue_os.ecosystem.offer_sources import (
    HumanSetupRequiredOfferSource,
    OfferCandidate,
    build_offer_source,
)
from revenue_os.ecosystem.product_intent import ProductIntent
from revenue_os.ecosystem.wondershare_offer_source import (
    ConfigError,
    WondershareConfig,
    WondershareOfferSource,
)

#: the real values the user supplied - used only as test fixtures.
_PUBLISHER_ID = "3077697"
_TRACKING_URL = (
    "https://www.awin1.com/cread.php?awinmid=20202&awinaffid=3077697"
    "&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"
)
_VALID_ENV = {
    "WONDERSHARE_AWIN_PUBLISHER_ID": _PUBLISHER_ID,
    "WONDERSHARE_AWIN_TRACKING_URL": _TRACKING_URL,
}
_CFG = WondershareConfig(publisher_id=_PUBLISHER_ID, tracking_url=_TRACKING_URL)

#: env that authorises the feed-based AwinOfferSource - used to prove the
#: datafeed connector still takes precedence over the curated link.
_DATAFEED_ENV = {
    "AWIN_DATAFEED_API_KEY": "test-key-not-dialed",
    "AWIN_ADVERTISER_IDS": "20202",
}


def _intent(phrase: str) -> ProductIntent:
    return ProductIntent(category_phrase=phrase, intent="purchase_recommendation")


class ConfigTests(unittest.TestCase):
    def test_missing_env_raises(self):
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env({})

    def test_missing_url_only_raises(self):
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env({"WONDERSHARE_AWIN_PUBLISHER_ID": _PUBLISHER_ID})

    def test_valid_env_parses(self):
        cfg = WondershareConfig.from_env(_VALID_ENV)
        self.assertEqual(cfg.publisher_id, _PUBLISHER_ID)
        self.assertEqual(cfg.tracking_url, _TRACKING_URL)

    def test_non_https_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_TRACKING_URL=_TRACKING_URL.replace("https://", "http://"))
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)

    def test_wrong_host_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_TRACKING_URL=(
            "https://evil-redirector.example/cread.php?awinmid=20202"
            "&awinaffid=3077697&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"))
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)

    def test_wrong_path_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_TRACKING_URL=_TRACKING_URL.replace("/cread.php", "/redirect.php"))
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)

    def test_wrong_advertiser_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_TRACKING_URL=_TRACKING_URL.replace("awinmid=20202", "awinmid=99999"))
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)

    def test_mismatched_publisher_id_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_PUBLISHER_ID="0000000")
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)

    def test_non_wondershare_destination_fails_closed(self):
        env = dict(_VALID_ENV, WONDERSHARE_AWIN_TRACKING_URL=(
            "https://www.awin1.com/cread.php?awinmid=20202&awinaffid=3077697"
            "&ued=https%3A%2F%2Fpdf.example.com%2Fpdfelement.html"))
        with self.assertRaises(ConfigError):
            WondershareConfig.from_env(env)


class AuthorizedPropertyTests(unittest.TestCase):
    def test_authorized_true_with_explicit_config(self):
        self.assertTrue(WondershareOfferSource(config=_CFG).authorized)

    def test_authorized_false_without_env_or_config(self):
        self.assertFalse(WondershareOfferSource(environ={}).authorized)

    def test_authorized_true_with_valid_env(self):
        self.assertTrue(WondershareOfferSource(environ=_VALID_ENV).authorized)


class NoNetworkCallStructuralTests(unittest.TestCase):
    def test_module_imports_no_network_capable_library(self):
        import ast
        import inspect

        from revenue_os.ecosystem import wondershare_offer_source

        tree = ast.parse(inspect.getsource(wondershare_offer_source))
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
        self.assertEqual(WondershareOfferSource.discovery_mode, "curated")
        self.assertFalse(WondershareOfferSource.product_search_available)


class SearchGatingTests(unittest.TestCase):
    _RELEVANT = (
        "pdf editor", "edit pdf files", "pdf editing software", "pdf converter",
        "convert pdf to word", "fillable pdf form", "sign pdf", "pdf ocr",
        "adobe acrobat alternative", "alternative to adobe acrobat",
    )
    _IRRELEVANT = (
        "usb microphone", "bluetooth headphones", "mechanical keyboard",
        "vpn service", "sales funnel builder", "project management tool",
        "note taking app",
    )

    def test_relevant_categories_return_the_one_offer(self):
        src = WondershareOfferSource(config=_CFG)
        for phrase in self._RELEVANT:
            with self.subTest(phrase=phrase):
                out = src.search(_intent(phrase), 5)
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0].network, NETWORK_AWIN)

    def test_unrelated_categories_return_nothing(self):
        src = WondershareOfferSource(config=_CFG)
        for phrase in self._IRRELEVANT:
            with self.subTest(phrase=phrase):
                self.assertEqual(src.search(_intent(phrase), 5), [])

    def test_empty_category_phrase_returns_nothing(self):
        self.assertEqual(WondershareOfferSource(config=_CFG).search(ProductIntent(), 5), [])

    def test_zero_or_negative_limit_returns_nothing(self):
        src = WondershareOfferSource(config=_CFG)
        self.assertEqual(src.search(_intent("pdf editor"), 0), [])
        self.assertEqual(src.search(_intent("pdf editor"), -1), [])

    def test_missing_config_returns_nothing_even_if_relevant(self):
        src = WondershareOfferSource(environ={})
        self.assertEqual(src.search(_intent("pdf editor"), 5), [])


class CandidateNormalizationTests(unittest.TestCase):
    def _one(self) -> OfferCandidate:
        out = WondershareOfferSource(config=_CFG).search(_intent("pdf editor"), 5)
        self.assertEqual(len(out), 1)
        return out[0]

    def test_tracking_url_preserved_exactly(self):
        c = self._one()
        self.assertEqual(c.url, _TRACKING_URL)
        self.assertIn("awinmid=20202", c.url)
        self.assertIn(f"awinaffid={_PUBLISHER_ID}", c.url)

    def test_no_fabricated_price_or_currency(self):
        c = self._one()
        self.assertEqual(c.price, 0.0)
        self.assertEqual(c.currency, "")

    def test_no_fabricated_availability_or_product_id(self):
        c = self._one()
        self.assertEqual(c.availability, "")
        self.assertEqual(c.product_id, "")

    def test_provenance_and_observed_at_are_set(self):
        c = self._one()
        self.assertEqual(c.provenance, "awin:wondershare_curated_affiliate_link")
        self.assertTrue(c.observed_at)


class BuildOfferSourceWiringTests(unittest.TestCase):
    def test_no_config_falls_back_to_human_setup_required(self):
        src = build_offer_source("awin", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertEqual(src.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_curated_link_config_returns_wondershare_source(self):
        src = build_offer_source("awin", environ=_VALID_ENV)
        self.assertIsInstance(src, WondershareOfferSource)

    def test_datafeed_still_takes_precedence_over_curated_link(self):
        from revenue_os.ecosystem.awin_offer_source import AwinOfferSource

        src = build_offer_source("awin", environ={**_DATAFEED_ENV, **_VALID_ENV})
        self.assertIsInstance(src, AwinOfferSource)

    def test_other_networks_are_unaffected(self):
        src = build_offer_source("amazon_associates", environ={})
        self.assertIsInstance(src, HumanSetupRequiredOfferSource)
        self.assertNotIsInstance(src, WondershareOfferSource)


class OfferCandidateToPayloadBridgeTests(unittest.TestCase):
    def _candidate(self) -> OfferCandidate:
        return OfferCandidate(
            network=NETWORK_AWIN, title="Wondershare PDFelement", url=_TRACKING_URL,
            provenance="awin:wondershare_curated_affiliate_link", confidence=1.0)

    def test_candidate_alone_cannot_pass_validation(self):
        payload = offer_candidate_to_payload(self._candidate())
        self.assertNotIn("human_confirmed_joined", payload)
        self.assertNotIn("commission_kind", payload)
        with self.assertRaises(IngestionError):
            parse_offer_json(payload)

    def test_becomes_valid_only_once_a_human_supplies_commission_and_join(self):
        payload = offer_candidate_to_payload(self._candidate())
        payload.update({
            "program_name": "Wondershare DE (Awin advertiser 20202)",
            "commission_kind": "percent",
            "commission_rate": 0.20,
            "commission_evidence": ["SYNTHETIC TEST evidence - not a verified real rate card quote"],
            "human_confirmed_joined": True,
            "preserve_exact_url": True,
        })
        parsed = parse_offer_json(payload)   # must not raise
        self.assertEqual(parsed["product_url"], _TRACKING_URL)


# ---------------------------------------------------------------------------
# demand -> AffiliateOffer matching, via the EXISTING, unmodified
# affiliate_matching / offer_selection gates - no Wondershare-specific bypass.
# The commission rate below is clearly-labelled SYNTHETIC TEST data.
# ---------------------------------------------------------------------------

_WONDERSHARE_OFFER = AffiliateOffer(
    offer_id="aff-wondershare-test", network=NETWORK_AWIN,
    program_name="Wondershare DE (Awin advertiser 20202)",
    product_name="Wondershare PDFelement - PDF editor, converter & e-sign",
    product_url=_TRACKING_URL, product_price=0.0, currency="EUR",
    commission=CommissionModel(kind="percent", rate=0.20, currency="EUR", is_estimate=True,
                               evidence=("SYNTHETIC TEST commission - not a verified real rate",)),
    category="pdf-editor",
    keywords=("pdf", "pdf editor", "edit pdf", "pdf converter", "convert pdf",
              "pdf form", "sign pdf", "pdf ocr", "acrobat alternative"),
    status=model.POLICY_OK, preserve_exact_url=True, active=True,
)

_KEYBOARD_OFFER = AffiliateOffer(
    offer_id="aff-kbd-test", network="amazon_associates",
    program_name="Amazon.de PartnerNet", product_name="Keychron K8 mechanical keyboard",
    product_url="https://www.amazon.de/dp/B08TEST123", product_asin="B08TEST123",
    product_price=89.0, currency="EUR",
    commission=CommissionModel(kind="percent", rate=0.03, currency="EUR", is_estimate=False,
                               evidence=("Amazon PartnerNet standard fee schedule",)),
    category="mechanical-keyboard",
    keywords=("keyboard", "mechanical keyboard", "hot swap", "gaming"),
    status=model.POLICY_OK, active=True,
)


def _draft(title: str, category: str = "other") -> OpportunityDraft:
    return OpportunityDraft(title=title, description="", evidence=[], category=category)


class DemandMatchingTests(unittest.TestCase):
    def test_relevant_demand_matches_wondershare_offer(self):
        draft = _draft("which pdf editor should i buy to edit and sign contracts",
                       category="pdf-editor")
        matches = match_offers(draft, [_WONDERSHARE_OFFER])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].offer.offer_id, _WONDERSHARE_OFFER.offer_id)

    def test_unrelated_demand_does_not_match(self):
        draft = _draft("which mechanical keyboard would you recommend for coding",
                       category="mechanical-keyboard")
        self.assertEqual(match_offers(draft, [_WONDERSHARE_OFFER]), [])

    def test_relevant_offer_wins_over_unrelated_one(self):
        draft = _draft("looking for an adobe acrobat alternative to edit pdf forms",
                       category="pdf-editor")
        matches = match_offers(draft, [_WONDERSHARE_OFFER, _KEYBOARD_OFFER])
        selected = select_best_offer(matches)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.match.offer.offer_id, _WONDERSHARE_OFFER.offer_id)


if __name__ == "__main__":
    unittest.main()
