"""Product Intent extraction (Demand-First Affiliate architecture, Step 1).

Covers: positive purchase-recommendation/replacement extraction, direct
product mention, missing category, missing purchase intent, UNKNOWN/empty
defaults, no fabricated categories, provenance (never FACT), determinism,
and regression - `demand_signal.py`'s `classify_purchase_intent()`/
`score_demand_quality()` and `demand_ranking.py`'s `buyer_confidence()`/
`problem_confidence()` must be byte-for-byte unaffected by this module's
existence.

All fixtures are plain strings - no network, no LLM, fully offline.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import demand_signal as ds
from revenue_os.ecosystem import product_intent as pi


def _evidence(text: str, **kw):
    return ds.build_demand_evidence(text, title=text, **kw)


class RequiredExampleTests(unittest.TestCase):
    """The exact case from the spec."""

    def test_bt_earbuds_recommendation(self):
        text = "what BT earbuds would you recommend?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "bt earbuds")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)
        self.assertEqual(out.provenance, ds.ESTIMATED)


class PositivePurchaseRecommendationTests(unittest.TestCase):
    def test_wireless_earbuds(self):
        text = "what wireless earbuds would you recommend?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "wireless earbuds")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)

    def test_which_product_should_i_buy(self):
        text = "which mechanical keyboard should I buy?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "mechanical keyboard")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)

    def test_direct_product_mention_looking_for(self):
        text = "looking for a good USB microphone"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "usb microphone")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)

    def test_any_recommendations_for(self):
        text = "any recommendations for a standing desk?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "standing desk")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)

    def test_can_you_recommend_a(self):
        text = "can you recommend a good office chair"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "office chair")
        self.assertEqual(out.intent, pi.INTENT_PURCHASE_RECOMMENDATION)


class ReplacementIntentTests(unittest.TestCase):
    def test_need_a_replacement(self):
        text = "need a replacement laptop charger"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "laptop charger")
        self.assertEqual(out.intent, pi.INTENT_REPLACEMENT)

    def test_current_product_is_bad(self):
        text = "My current microphone is really noisy on calls"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "microphone")
        self.assertEqual(out.intent, pi.INTENT_REPLACEMENT)


class MissingCategoryOrIntentTests(unittest.TestCase):
    """Negative cases - the system must not guess."""

    def test_technical_discussion_without_purchase_intent(self):
        text = ("I've been working on the perfect UI for cron and "
                "supervisor for several months now. Tell me what you think.")
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")
        self.assertEqual(out.provenance, ds.UNKNOWN)

    def test_news(self):
        text = "Election results announced tonight"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_politics(self):
        text = ("Voters said they would pay higher taxes to fund the new "
                "climate policy, according to the poll released Tuesday.")
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_pure_problem_description_without_product_reference(self):
        text = "How can I improve client communication workflows?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_generic_help_request_never_becomes_a_fabricated_category(self):
        text = "I feel stuck lately, any advice?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_the_real_false_positive_title_from_the_lemmy_live_run(self):
        # the actual title of the real mental-health/activities post found
        # on a live 'demand-lemmy-buying' run - must never yield a product.
        text = ("What are some activities which are free or inexpensive "
                "that I can do to entertain myself / keep good mental "
                "health while searching for a better paid job?")
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_no_marker_at_all_is_unknown_not_empty_guess(self):
        text = "The weather has been nice lately."
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out, pi.ProductIntent())

    def test_empty_title(self):
        out = pi.extract_product_intent(_evidence(""), title="")
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")
        self.assertEqual(out.provenance, ds.UNKNOWN)


class NoFabricatedCategoryTests(unittest.TestCase):
    """A pattern can technically match but capture a non-product filler
    word - this must still fail closed, never return a fake category."""

    def test_looking_for_advice_is_not_a_product(self):
        text = "looking for advice"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_any_recommendations_for_someone_is_not_a_product(self):
        text = "any recommendations for someone in this situation?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")

    def test_bare_would_you_recommend_with_nothing_named_is_not_a_product(self):
        text = "What would you recommend? Any good ideas?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")
        self.assertEqual(out.intent, "")

    def test_an_overlong_capture_is_rejected_not_returned_as_is(self):
        text = ("looking for a tool that automatically categorizes my "
                "stripe transactions every single month without fail")
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.category_phrase, "")


class ProvenanceTests(unittest.TestCase):
    def test_provenance_is_estimated_for_a_real_extraction(self):
        text = "what BT earbuds would you recommend?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.provenance, ds.ESTIMATED)
        self.assertNotEqual(out.provenance, ds.FACT)

    def test_provenance_is_unknown_when_nothing_derived(self):
        text = "Election results announced tonight"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.provenance, ds.UNKNOWN)
        self.assertNotEqual(out.provenance, ds.FACT)

    def test_provenance_is_never_fact_across_all_fixtures(self):
        fixtures = (
            "what BT earbuds would you recommend?",
            "need a replacement laptop charger",
            "Election results announced tonight",
            "",
        )
        for text in fixtures:
            out = pi.extract_product_intent(_evidence(text), title=text)
            self.assertNotEqual(out.provenance, ds.FACT, text)


class ConstraintsTests(unittest.TestCase):
    def test_constraints_empty_when_no_real_budget_stated(self):
        text = "what BT earbuds would you recommend?"
        out = pi.extract_product_intent(_evidence(text), title=text)
        self.assertEqual(out.constraints, ())

    def test_constraints_reuse_the_already_extracted_budget(self):
        title = "What USB microphone would you recommend?"
        body = "I would pay $50 for one, budget is tight."
        blob = f"{title} {body}"
        ev = ds.build_demand_evidence(blob, title=title)
        out = pi.extract_product_intent(ev, title=title)
        self.assertEqual(out.category_phrase, "usb microphone")
        self.assertEqual(out.constraints, ("budget:50USD",))

    def test_constraints_ignore_a_vague_estimated_budget(self):
        title = "What USB microphone would you recommend?"
        body = "Maybe around $50, not sure."
        blob = f"{title} {body}"
        ev = ds.build_demand_evidence(blob, title=title)
        out = pi.extract_product_intent(ev, title=title)
        self.assertEqual(out.constraints, ())


class DeterminismTests(unittest.TestCase):
    def test_same_input_yields_identical_output(self):
        text = "what BT earbuds would you recommend?"
        ev = _evidence(text)
        a = pi.extract_product_intent(ev, title=text)
        b = pi.extract_product_intent(ev, title=text)
        self.assertEqual(a, b)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_never_raises_on_arbitrary_text(self):
        for text in ("", " ", "?!?!", "a" * 500, "\n\t weird \x00 bytes"):
            try:
                pi.extract_product_intent(_evidence(text), title=text)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"raised on {text!r}: {exc!r}")


class RegressionTests(unittest.TestCase):
    """Hard requirement: this module must not change demand_signal.py's or
    demand_ranking.py's behaviour at all - verified by direct comparison
    against a call made with product_intent never imported/used."""

    def test_classify_purchase_intent_is_unaffected(self):
        cases = [
            "Is there a tool that categorizes my Stripe transactions?",
            "I would pay for a tool that does this.",
            "what BT earbuds would you recommend?",
            "I need help with my Stripe integration.",
            "Just some unrelated text about the weather.",
        ]
        for text in cases:
            level, quote = ds.classify_purchase_intent(text)
            # calling extract_product_intent afterwards must not change
            # a subsequent (or prior) call to classify_purchase_intent
            ev = ds.build_demand_evidence(text, title=text)
            pi.extract_product_intent(ev, title=text)
            level2, quote2 = ds.classify_purchase_intent(text)
            self.assertEqual(level, level2, text)
            self.assertEqual(quote, quote2, text)

    def test_score_demand_quality_is_byte_identical(self):
        text = "what BT earbuds would you recommend?"
        ev = ds.build_demand_evidence(text, title=text, now_iso="2026-09-06T00:00:00+00:00")
        before = ds.score_demand_quality(ev).to_dict()
        pi.extract_product_intent(ev, title=text)
        after = ds.score_demand_quality(ev).to_dict()
        self.assertEqual(before, after)

    def test_buyer_and_problem_confidence_are_byte_identical(self):
        from revenue_os.ecosystem import demand_ranking as dr

        text = "I would pay for a good USB microphone right now."
        ev = ds.build_demand_evidence(text, title=text)
        bc_before = dr.buyer_confidence(ev).to_dict()
        pc_before = dr.problem_confidence(ev).to_dict()
        pi.extract_product_intent(ev, title=text)
        bc_after = dr.buyer_confidence(ev).to_dict()
        pc_after = dr.problem_confidence(ev).to_dict()
        self.assertEqual(bc_before, bc_after)
        self.assertEqual(pc_before, pc_after)

    def test_product_intent_module_does_not_import_or_modify_marker_tables(self):
        # structural guarantee: this module must never write to
        # demand_signal.py's marker tuples (only read demand_signal for
        # ESTIMATED/UNKNOWN/DemandEvidence).
        import ast
        import inspect

        source = inspect.getsource(pi)
        tree = ast.parse(source)
        assigned_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                assigned_names.add(node.attr)
        self.assertNotIn("_PROBLEM_INTEREST_MARKERS", assigned_names)
        self.assertNotIn("_EXPLICIT_INTENT_MARKERS", assigned_names)


if __name__ == "__main__":
    unittest.main()
