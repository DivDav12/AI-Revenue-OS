"""Demand Ranking Layer tests (spec: Decision-/Ranking-Design step).

Covers: buyer_confidence/problem_confidence primary signals, the
builder-independence dampening rule, soft-penalty behavior (never zero),
bounded/explainable output, and - critically - that the EXISTING
`demand_score` (score_demand_quality) is completely unaffected by this
module's existence.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import demand_ranking as dr
from revenue_os.ecosystem import demand_signal as ds


class BuyerConfidenceTests(unittest.TestCase):
    def test_explicit_intent_gives_high_buyer_confidence(self):
        ev = ds.build_demand_evidence("I would pay $20 a month for a tool like this.")
        bc = dr.buyer_confidence(ev)
        self.assertTrue(bc.factors["explicit_purchase_intent_base"]["present"])
        self.assertGreaterEqual(bc.total, 0.6)

    def test_no_explicit_intent_is_not_artificially_high(self):
        ev = ds.build_demand_evidence("Is there a tool that automatically does this?")
        bc = dr.buyer_confidence(ev)
        self.assertFalse(bc.factors["explicit_purchase_intent_base"]["present"])
        self.assertEqual(bc.total, 0.0)

    def test_vague_text_gives_zero_buyer_confidence(self):
        ev = ds.build_demand_evidence("Just thinking out loud about stuff.")
        bc = dr.buyer_confidence(ev)
        self.assertEqual(bc.total, 0.0)

    def test_supplier_perspective_is_only_a_soft_penalty(self):
        ev = ds.build_demand_evidence(
            "I would pay $50/month for exactly this kind of automation tool.",
            title="Show HN: my automation tool")
        bc = dr.buyer_confidence(ev)
        self.assertTrue(bc.factors["supplier_penalty"]["present"])
        self.assertGreater(bc.total, 0.0)     # never zeroed out
        self.assertLess(bc.total, 0.60)       # but visibly reduced vs. base alone

    def test_builder_yes_is_only_a_soft_penalty(self):
        ev = ds.build_demand_evidence(
            "I built this and I would pay for a much better one right now.")
        self.assertEqual(ev.builder_signal, ds.BUILDER_YES)   # sanity on the fixture
        bc = dr.buyer_confidence(ev)
        self.assertTrue(bc.factors["builder_penalty_full"]["present"]
                        or bc.factors["builder_penalty_dampened"]["present"])
        self.assertGreater(bc.total, 0.0)
        self.assertLess(bc.total, 0.60)

    def test_independent_builder_and_intent_quotes_dampen_the_penalty(self):
        same_sentence = ds.build_demand_evidence(
            "I built this and I would pay for a much better one right now.")
        different_sentences = ds.build_demand_evidence(
            "I built this tool last year. I would pay for a much better one right now.")
        self.assertEqual(same_sentence.builder_signal, ds.BUILDER_YES)
        self.assertEqual(different_sentences.builder_signal, ds.BUILDER_YES)

        bc_same = dr.buyer_confidence(same_sentence)
        bc_diff = dr.buyer_confidence(different_sentences)

        self.assertFalse(dr.builder_intent_independent(same_sentence))
        self.assertTrue(dr.builder_intent_independent(different_sentences))

        self.assertTrue(bc_same.factors["builder_penalty_full"]["present"])
        self.assertFalse(bc_same.factors["builder_penalty_dampened"]["present"])
        self.assertFalse(bc_diff.factors["builder_penalty_full"]["present"])
        self.assertTrue(bc_diff.factors["builder_penalty_dampened"]["present"])

        # the dampened (independent) case must hurt less than the full penalty
        self.assertGreater(bc_diff.total, bc_same.total)

    def test_budget_present_gives_a_small_bonus_never_a_requirement(self):
        with_budget = ds.build_demand_evidence("I would pay $20 a month for a tool like this.")
        without_budget = ds.build_demand_evidence("I would pay for a tool like this.")
        bc_with = dr.buyer_confidence(with_budget)
        bc_without = dr.buyer_confidence(without_budget)

        self.assertTrue(bc_with.factors["budget_bonus"]["present"])
        self.assertFalse(bc_without.factors["budget_bonus"]["present"])
        self.assertGreater(bc_with.total, bc_without.total)
        # "small" bonus and never a requirement to clear the base
        self.assertLessEqual(bc_with.factors["budget_bonus"]["weight"], 0.10)
        self.assertGreaterEqual(bc_without.total, 0.5)

    def test_repeat_signal_gives_a_small_bonus(self):
        text = "I would pay for a tool that does this."
        bc0 = dr.buyer_confidence(ds.build_demand_evidence(text, repeat_signal_count=0))
        bc2 = dr.buyer_confidence(ds.build_demand_evidence(text, repeat_signal_count=2))
        self.assertTrue(bc2.factors["repeat_signal_bonus"]["present"])
        self.assertGreater(bc2.total, bc0.total)
        self.assertLessEqual(bc2.factors["repeat_signal_bonus"]["weight"], 0.10)

    def test_missing_evidence_gives_no_artificial_positive_signal(self):
        bc = dr.buyer_confidence(ds.DemandEvidence())
        self.assertEqual(bc.total, 0.0)
        self.assertEqual(bc.reasons, [])


class ProblemConfidenceTests(unittest.TestCase):
    def test_problem_interest_with_asker_gives_high_problem_confidence(self):
        ev = ds.build_demand_evidence(
            "Is there a tool that automatically categorizes my expenses?",
            title="Ask HN: is there a tool that does this?")
        pc = dr.problem_confidence(ev)
        self.assertTrue(pc.factors["problem_interest_asker_combo"]["present"])
        self.assertGreaterEqual(pc.total, 0.6)

    def test_problem_interest_without_asker_is_lower_but_not_zero(self):
        ev = ds.build_demand_evidence(
            "Is there a tool that automatically categorizes my expenses?")
        pc = dr.problem_confidence(ev)
        self.assertFalse(pc.factors["problem_interest_asker_combo"]["present"])
        self.assertGreaterEqual(pc.total, 0.4)
        self.assertLess(pc.total, 0.6)

    def test_no_problem_interest_is_not_artificially_high(self):
        ev = ds.build_demand_evidence("Just thinking out loud about stuff.")
        pc = dr.problem_confidence(ev)
        self.assertEqual(pc.total, 0.0)

    def test_explicit_intent_contributes_less_than_problem_interest(self):
        explicit_ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        problem_ev = ds.build_demand_evidence("Is there a tool that does this?")
        pc_explicit = dr.problem_confidence(explicit_ev)
        pc_problem = dr.problem_confidence(problem_ev)
        self.assertTrue(pc_explicit.factors["explicit_intent_base"]["present"])
        self.assertTrue(pc_problem.factors["problem_interest_base"]["present"])
        self.assertLess(pc_explicit.total, pc_problem.total)

    def test_supplier_and_builder_are_only_weak_modifiers_here(self):
        ev = ds.build_demand_evidence(
            "Is there a tool that automatically categorizes my expenses?",
            title="Ask HN: is there a tool that does this?")
        pc = dr.problem_confidence(ev)
        supplier_weight = pc.factors["supplier_penalty"]["weight"]
        builder_weight = pc.factors["builder_penalty_full"]["weight"]
        self.assertLessEqual(supplier_weight, 0.10)
        self.assertLessEqual(builder_weight, 0.10)

    def test_missing_evidence_gives_no_artificial_positive_signal(self):
        pc = dr.problem_confidence(ds.DemandEvidence())
        self.assertEqual(pc.total, 0.0)
        self.assertEqual(pc.reasons, [])


class BoundsAndExplainabilityTests(unittest.TestCase):
    def test_scores_stay_within_zero_one_with_many_factors_firing(self):
        ev = ds.build_demand_evidence(
            "I built this before, anyone need it? I would pay $999/month for a much "
            "better one right now, this is urgent.",
            title="Show HN: my crazy expensive tool",
            discovered_at="2020-01-01T00:00:00+00:00",
            now_iso="2026-09-04T00:00:00+00:00",
            repeat_signal_count=5)
        bc = dr.buyer_confidence(ev)
        pc = dr.problem_confidence(ev)
        self.assertGreaterEqual(bc.total, 0.0)
        self.assertLessEqual(bc.total, 1.0)
        self.assertGreaterEqual(pc.total, 0.0)
        self.assertLessEqual(pc.total, 1.0)

    def test_every_factor_is_named_weighted_and_explained(self):
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        for score in (dr.buyer_confidence(ev), dr.problem_confidence(ev)):
            for name, f in score.factors.items():
                self.assertIn("weight", f)
                self.assertIn("present", f)
                self.assertIn("sign", f)
            self.assertTrue(score.reasons)

    def test_to_dict_round_trips_cleanly(self):
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        bc_dict = dr.buyer_confidence(ev).to_dict()
        pc_dict = dr.problem_confidence(ev).to_dict()
        for d in (bc_dict, pc_dict):
            self.assertIn("total", d)
            self.assertIn("factors", d)
            self.assertIn("reasons", d)
            self.assertIn("evidence", d)
            self.assertIsInstance(d["total"], float)


class NoImpactOnExistingDemandScoreTests(unittest.TestCase):
    """Hard rule: this module is purely additive - it must never change
    the existing, unmodified score_demand_quality() output, its factor
    tables, or its weights."""

    def test_score_demand_quality_output_identical_regardless_of_ranking_calls(self):
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        before = ds.score_demand_quality(ev).to_dict()
        dr.buyer_confidence(ev)
        dr.problem_confidence(ev)
        after = ds.score_demand_quality(ev).to_dict()
        self.assertEqual(before, after)

    def test_new_factor_names_do_not_leak_into_the_existing_score(self):
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        score = ds.score_demand_quality(ev)
        self.assertNotIn("buyer_confidence", score.factors)
        self.assertNotIn("problem_confidence", score.factors)
        self.assertNotIn("explicit_purchase_intent_base", score.factors)
        self.assertNotIn("problem_interest_asker_combo", score.factors)

    def test_existing_positive_factor_weights_are_exactly_unchanged(self):
        self.assertEqual(
            {name: weight for name, weight, *_ in ds._POSITIVE_FACTORS},
            {"explicit_purchase_intent": 0.20, "problem_interest": 0.08,
             "stated_budget": 0.15, "audience_named": 0.10, "urgency": 0.08,
             "digital_friendly": 0.10, "recent_signal": 0.07, "repeat_signal": 0.12})

    def test_existing_negative_factor_weights_are_exactly_unchanged(self):
        self.assertEqual(
            {name: weight for name, weight, *_ in ds._NEGATIVE_FACTORS},
            {"no_concrete_signal": 0.12, "help_request_only": 0.08,
             "not_productizable": 0.25, "stale_signal": 0.10, "unknown_age": 0.03})


if __name__ == "__main__":
    unittest.main()
