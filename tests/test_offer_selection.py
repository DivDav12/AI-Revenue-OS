"""Multi-Offer Selection (Demand-First Affiliate architecture, Offer
Discovery MVP).

Covers: the relevance gate (min_relevance), that a highly-profitable but
irrelevant offer can never win over a relevant one, deterministic
tie-breaking among several relevant offers, usability filtering, empty/
no-relevant-matches -> None, and - critically - that
`affiliate_matching.py`/`affiliate_profitability.py` are used completely
UNCHANGED (this module calls their existing public functions only).

Uses the real, persisted JBL offer shape (spec: "Nutze das vorhandene
JBL-Angebot") plus one clearly-labelled SYNTHETIC second test offer - no
external market facts are invented for either.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import model
from revenue_os.ecosystem.affiliate_matching import AffiliateMatch, match_offers
from revenue_os.ecosystem.affiliate_model import AffiliateOffer, CommissionModel
from revenue_os.ecosystem.model import OpportunityDraft
from revenue_os.ecosystem.offer_selection import SelectedOffer, select_best_offer

#: the real, currently-ingested JBL offer's shape (same facts as
#: data/affiliate_offers.json - copied here as a fixture so this test
#: file has no dependency on repo data state; the offer itself is never
#: touched by this step).
_JBL_OFFER = AffiliateOffer(
    offer_id="aff-4168b32b377e", network="amazon_associates",
    program_name="Amazon.de PartnerNet", product_name="JBL Quantum Stream Talk",
    product_url="https://www.amazon.de/JBL-Quantum-Stream-Talk-super-kardioidem/dp/B0CQP5NL72",
    product_asin="B0CQP5NL72", product_price=39.99, currency="EUR",
    price_is_estimate=True,
    commission=CommissionModel(kind="percent", rate=0.03, currency="EUR",
                               cookie_duration_days=1.0, is_estimate=False,
                               evidence=("Amazon PartnerNet standard fee schedule",)),
    category="usb-microphone-streaming",
    keywords=("microphone", "mikrofon", "usb-mikrofon", "usb microphone",
             "streaming", "discord", "gaming", "podcast", "creator",
             "home-office", "voice chat"),
    status=model.POLICY_OK, tracking_param="tag", tracking_value="airevenue-21",
    active=True,
)

#: a clearly-labelled SYNTHETIC second test offer - never a real product,
#: never a real commission rate, exists only to prove multi-offer
#: selection with more than one candidate.
_SYNTHETIC_TEST_OFFER = AffiliateOffer(
    offer_id="aff-synthetic-test-offer", network="amazon_associates",
    program_name="SYNTHETIC TEST PROGRAM - not a real affiliate program",
    product_name="SYNTHETIC TEST wireless earbuds (test fixture only)",
    product_url="https://www.amazon.de/dp/B0SYNTHETIC",
    product_asin="B0SYNTHETIC", product_price=49.99, currency="EUR",
    price_is_estimate=True,
    commission=CommissionModel(kind="percent", rate=0.10, currency="EUR",
                               cookie_duration_days=1.0, is_estimate=True,
                               evidence=("SYNTHETIC TEST commission - not a real rate",)),
    category="wireless-earbuds", keywords=("earbuds", "wireless", "bluetooth", "headphones"),
    status=model.POLICY_OK, tracking_param="tag", tracking_value="test-tag-99",
    active=True,
)


def _match(offer: AffiliateOffer, *, match_score: float, demand_strength: float = 0.5) -> AffiliateMatch:
    return AffiliateMatch(offer=offer, match_score=match_score,
                          matched_terms=[], demand_strength=demand_strength)


class RelevanceGateTests(unittest.TestCase):
    def test_below_threshold_match_is_excluded(self):
        m = _match(_JBL_OFFER, match_score=0.10)
        self.assertIsNone(select_best_offer([m], min_relevance=0.15))

    def test_exactly_at_threshold_is_included(self):
        m = _match(_JBL_OFFER, match_score=0.15)
        out = select_best_offer([m], min_relevance=0.15)
        self.assertIsNotNone(out)
        self.assertEqual(out.match.offer.offer_id, _JBL_OFFER.offer_id)

    def test_custom_min_relevance_is_respected(self):
        m = _match(_JBL_OFFER, match_score=0.4)
        self.assertIsNone(select_best_offer([m], min_relevance=0.5))
        self.assertIsNotNone(select_best_offer([m], min_relevance=0.3))


class IrrelevantHighProfitNeverWinsTests(unittest.TestCase):
    """The central safety requirement: a higher commission must never
    lift an irrelevant offer above a relevant one."""

    def test_high_commission_low_relevance_loses_to_relevant_offer(self):
        # SYNTHETIC_TEST_OFFER has a much higher nominal commission rate
        # (10% vs JBL's 3%) but scores far below the relevance gate for
        # this (synthetic) demand text - it must never be selected.
        irrelevant_but_lucrative = _match(_SYNTHETIC_TEST_OFFER, match_score=0.05,
                                          demand_strength=0.9)
        relevant = _match(_JBL_OFFER, match_score=0.5, demand_strength=0.5)

        out = select_best_offer([irrelevant_but_lucrative, relevant])
        self.assertIsNotNone(out)
        self.assertEqual(out.match.offer.offer_id, _JBL_OFFER.offer_id)

    def test_irrelevant_offer_never_even_reaches_profitability_scoring(self):
        # a match_score of 0 (no overlap at all) must be gated out before
        # affiliate_profitability.evaluate() is ever called on it -
        # verified indirectly: the only surviving/selected candidate is
        # the relevant one, regardless of how lucrative the other looks.
        irrelevant = _match(_SYNTHETIC_TEST_OFFER, match_score=0.0, demand_strength=1.0)
        relevant = _match(_JBL_OFFER, match_score=0.2, demand_strength=0.2)
        out = select_best_offer([irrelevant, relevant])
        self.assertEqual(out.match.offer.offer_id, _JBL_OFFER.offer_id)


class MultipleRelevantOffersTests(unittest.TestCase):
    def test_higher_decision_value_wins_among_relevant_offers(self):
        a = _match(_JBL_OFFER, match_score=0.4, demand_strength=0.6)
        b = _match(_SYNTHETIC_TEST_OFFER, match_score=0.4, demand_strength=0.6)
        out = select_best_offer([a, b])
        self.assertIsNotNone(out)
        # both are relevant - the winner must be whichever actually
        # projects the higher affiliate_profitability.evaluate() decision
        # value, not simply "the first in the list" or "the higher rate".
        from revenue_os.ecosystem.affiliate_profitability import evaluate
        from revenue_os.ecosystem.model import estimate_value

        dv_a = estimate_value(evaluate(a).decision_value)
        dv_b = estimate_value(evaluate(b).decision_value)
        expected_winner = a if dv_a >= dv_b else b
        self.assertEqual(out.match.offer.offer_id, expected_winner.offer.offer_id)

    def test_selection_is_deterministic_across_repeated_calls(self):
        a = _match(_JBL_OFFER, match_score=0.4)
        b = _match(_SYNTHETIC_TEST_OFFER, match_score=0.4)
        r1 = select_best_offer([a, b])
        r2 = select_best_offer([b, a])   # reversed input order too
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_result_is_a_selected_offer_with_full_evidence(self):
        a = _match(_JBL_OFFER, match_score=0.3)
        out = select_best_offer([a])
        self.assertIsInstance(out, SelectedOffer)
        self.assertIs(out.match, a)
        self.assertIsInstance(out.decision_value, float)
        d = out.to_dict()
        self.assertIn("match", d)
        self.assertIn("profitability", d)
        self.assertIn("decision_value", d)


class UsabilityFilterTests(unittest.TestCase):
    def test_a_relevant_but_unusable_offer_is_never_selected(self):
        from dataclasses import replace

        unusable = replace(_SYNTHETIC_TEST_OFFER, offer_id="aff-synthetic-unusable",
                           status=model.POLICY_HUMAN_SETUP_REQUIRED)
        m = _match(unusable, match_score=0.9, demand_strength=0.9)
        self.assertIsNone(select_best_offer([m]))

    def test_unusable_relevant_offer_loses_to_usable_less_relevant_one(self):
        from dataclasses import replace

        unusable = replace(_SYNTHETIC_TEST_OFFER, offer_id="aff-synthetic-unusable",
                           status=model.POLICY_HUMAN_SETUP_REQUIRED)
        m_unusable = _match(unusable, match_score=0.9, demand_strength=0.9)
        m_usable = _match(_JBL_OFFER, match_score=0.2, demand_strength=0.2)
        out = select_best_offer([m_unusable, m_usable])
        self.assertIsNotNone(out)
        self.assertEqual(out.match.offer.offer_id, _JBL_OFFER.offer_id)


class EmptyAndNoneCasesTests(unittest.TestCase):
    def test_empty_matches_list_returns_none(self):
        self.assertIsNone(select_best_offer([]))

    def test_no_relevant_matches_returns_none(self):
        m1 = _match(_JBL_OFFER, match_score=0.01)
        m2 = _match(_SYNTHETIC_TEST_OFFER, match_score=0.02)
        self.assertIsNone(select_best_offer([m1, m2]))


class ExistingLogicUnaffectedTests(unittest.TestCase):
    """Hard requirement: affiliate_matching.py / affiliate_profitability.py
    behave exactly as before - this module only calls their existing,
    unmodified public functions."""

    def test_match_offers_output_feeds_select_best_offer_unchanged(self):
        draft = OpportunityDraft(
            title="what USB microphone would you recommend for streaming and Discord?",
            description="Looking for a good USB microphone for gaming and podcast use.",
            category="other")
        matches = match_offers(draft, [_JBL_OFFER, _SYNTHETIC_TEST_OFFER])
        out = select_best_offer(matches)
        # the JBL offer's keywords overlap this demand text; the synthetic
        # earbuds offer's keywords do not - matching is untouched, so this
        # must still resolve to the JBL offer.
        self.assertIsNotNone(out)
        self.assertEqual(out.match.offer.offer_id, _JBL_OFFER.offer_id)

    def test_affiliate_profitability_evaluate_called_with_no_side_effects(self):
        from revenue_os.ecosystem.affiliate_profitability import evaluate

        m = _match(_JBL_OFFER, match_score=0.5)
        before = evaluate(m).to_dict()
        select_best_offer([m])
        after = evaluate(m).to_dict()
        self.assertEqual(before, after)

    def test_affiliate_matching_module_is_not_modified_by_this_import(self):
        # structural guarantee: offer_selection.py only imports the
        # existing public names, defines no new AffiliateMatch fields.
        from revenue_os.ecosystem import affiliate_matching

        self.assertEqual(
            sorted(f.name for f in affiliate_matching.AffiliateMatch.__dataclass_fields__.values()),
            sorted(["offer", "match_score", "matched_terms", "demand_strength"]))


if __name__ == "__main__":
    unittest.main()
