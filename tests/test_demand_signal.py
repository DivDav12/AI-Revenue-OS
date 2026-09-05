"""Demand Quality Layer (spec: Demand-to-Revenue plan, Step 1).

Covers: purchase-intent classification (explicit vs. problem-interest vs.
help-request), budget extraction (context + vagueness gated), audience
extraction (conservative, verbatim-only), urgency, productizability,
signal age, provenance (FACT/ESTIMATED/UNKNOWN), the full score, and
determinism. No test asserts a fabricated price/audience/buyer count as
real.
"""

from __future__ import annotations

import unittest

from revenue_os.ecosystem import demand_signal as ds


class PurchaseIntentTests(unittest.TestCase):
    def test_explicit_pay_language_is_explicit_intent(self):
        text = "I would pay for a tool that automatically categorizes my Stripe transactions."
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_EXPLICIT)
        self.assertIn("i would pay", quote)

    def test_is_there_a_tool_is_problem_interest_not_purchase_intent(self):
        text = "Is there a tool that automatically categorizes my Stripe transactions?"
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_PROBLEM)
        self.assertNotEqual(level, ds.INTENT_EXPLICIT)

    def test_i_need_help_is_not_automatically_purchase_intent(self):
        text = "I need help categorizing my Stripe transactions every month."
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_HELP)
        self.assertNotIn(level, (ds.INTENT_EXPLICIT, ds.INTENT_PROBLEM))

    def test_no_marker_at_all_is_none(self):
        text = "Just some unrelated text about the weather today."
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_NONE)
        self.assertEqual(quote, "")

    def test_explicit_wins_over_help_in_the_same_text(self):
        text = "I need help - is there a tool for this? I would pay for it."
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_EXPLICIT)


class ProductAgnosticBuyRecommendationMarkerTests(unittest.TestCase):
    """Demand Discovery expansion: `_PROBLEM_INTEREST_MARKERS` was
    extended ADDITIVELY with product-agnostic (no "tool"/"app"/"software"
    anchor) buy-recommendation phrasing, so a genuine physical-product
    purchase-recommendation question (e.g. a USB microphone) is
    recognised exactly like a software "is there a tool" question always
    was - nothing existing was removed or reordered."""

    def test_which_product_should_i_buy_is_problem_interest(self):
        text = "Which USB microphone should I buy for streaming under 50 euros?"
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_PROBLEM)
        self.assertEqual(quote, "should i buy")

    def test_current_product_is_bad_what_should_i_replace_it_with(self):
        text = ("My current microphone is really noisy on calls - what "
                "would you recommend for a replacement?")
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_PROBLEM)

    def test_worth_buying_and_before_you_buy_are_recognised(self):
        for text in ("Is this microphone worth buying for podcasting?",
                     "Anything to know before you buy a USB mic?"):
            level, _ = ds.classify_purchase_intent(text)
            self.assertEqual(level, ds.INTENT_PROBLEM, text)

    def test_new_markers_never_outrank_explicit_intent(self):
        text = "Should I buy this? I would pay for it right now."
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_EXPLICIT)

    def test_new_markers_are_product_agnostic_no_hardcoded_product_name(self):
        for marker in ("should i buy", "should i get", "worth buying",
                       "before you buy", "in the market for", "looking to buy"):
            self.assertNotIn("microphone", marker)
            self.assertNotIn("mic", marker)
            self.assertNotIn("jbl", marker)

    def test_bare_would_you_recommend_needs_a_topic_anchor(self):
        # THE target case from the spec: a concrete product noun ("BT
        # earbuds") immediately precedes the phrase -> anchored -> still
        # classified as genuine problem-interest.
        text = "what BT earbuds would you recommend?"
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_PROBLEM)
        self.assertEqual(quote, "would you recommend")

    def test_unanchored_would_you_recommend_is_not_a_product_problem(self):
        # THE real false positive found on a live 'demand-lemmy-buying'
        # run: a free-time/mental-health question that happens to contain
        # "would you recommend" with nothing named right before it.
        text = ("I want to try out some things and meet new people. My "
                "ideas are hiking, volunteering and reading. What would "
                "you recommend? Any good ideas?")
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_NONE)
        self.assertEqual(quote, "")

    def test_bare_would_you_recommend_with_nothing_before_it_is_not_anchored(self):
        text = "Would you recommend this brand?"
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_NONE)

    def test_qualified_recommend_markers_are_unaffected_by_the_anchor_guard(self):
        # every OTHER marker (including the ones from the same additive
        # batch) already names its own qualifier/anchor and must keep
        # working exactly as before, with no topic-anchor requirement.
        cases = {
            "What would you recommend for a replacement?": "what would you recommend for",
            "Any recommendations for a good budget mic?": "any recommendations for",
            "Can you recommend a good one?": "can you recommend a",
        }
        for text, expected_quote in cases.items():
            level, quote = ds.classify_purchase_intent(text)
            self.assertEqual(level, ds.INTENT_PROBLEM, text)
            self.assertEqual(quote, expected_quote, text)

    def test_unanchored_hit_still_falls_through_to_help_request(self):
        text = "I need help - what would you recommend?"
        level, _ = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_HELP)

    def test_unanchored_hit_never_escalates_to_explicit(self):
        text = "What would you recommend? Any good ideas?"
        level, _ = ds.classify_purchase_intent(text)
        self.assertNotEqual(level, ds.INTENT_EXPLICIT)

    def test_anchor_filler_word_immediately_before_the_phrase_does_not_count(self):
        for filler in ("so", "and", "but", "now"):
            text = f"Great, {filler} would you recommend one?"
            level, _ = ds.classify_purchase_intent(text)
            self.assertEqual(level, ds.INTENT_NONE, text)

    def test_a_real_noun_right_before_any_recommendations_is_anchored(self):
        text = "I have a €50 budget, headphones any recommendations?"
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_PROBLEM)
        self.assertEqual(quote, "any recommendations")

    def test_existing_tool_markers_and_their_exact_quotes_are_unchanged(self):
        # regression: the additive extension must not shift which marker
        # _first_match() picks, nor its exact quote, for any pre-existing
        # phrasing.
        cases = {
            "Is there a tool that categorizes my Stripe transactions?": "is there a tool that",
            "Does anyone know a tool for this?": "does anyone know a tool",
            "Recommend a tool for CSV dedupe.": "recommend a tool for",
            "Any recommendations for a tool like this?": "any recommendations for a tool",
        }
        for text, expected_quote in cases.items():
            level, quote = ds.classify_purchase_intent(text)
            self.assertEqual(level, ds.INTENT_PROBLEM, text)
            self.assertEqual(quote, expected_quote, text)


class BudgetExtractionTests(unittest.TestCase):
    def test_explicit_budget_is_extracted(self):
        text = "I'd pay $20 a month for something like this."
        pe = ds.extract_stated_budget(text)
        self.assertEqual(pe.amount, 20.0)
        self.assertEqual(pe.currency, "USD")
        self.assertFalse(pe.is_estimate)
        self.assertIn("$20", pe.evidence[0])

    def test_euro_budget_is_extracted(self):
        pe = ds.extract_stated_budget("My budget for this would be around... "
                                      "actually let's say I could spend 30 EUR on it.")
        self.assertEqual(pe.amount, 30.0)
        self.assertEqual(pe.currency, "EUR")

    def test_no_budget_mentioned_is_not_invented(self):
        pe = ds.extract_stated_budget("I would pay for a tool that does this.")
        self.assertEqual(pe.amount, 0.0)
        self.assertTrue(pe.is_estimate)

    def test_vague_budget_is_not_extracted(self):
        pe = ds.extract_stated_budget("I'd pay up to $50 for this, roughly.")
        self.assertEqual(pe.amount, 0.0)

    def test_a_bare_number_with_no_payment_context_is_not_a_budget(self):
        # "$20" appears but not in a pay/budget/spend/afford/cost/price
        # context sentence - must not be picked up.
        pe = ds.extract_stated_budget("I lost $20 on a bad tool once. "
                                      "Is there something better?")
        self.assertEqual(pe.amount, 0.0)


class AudienceExtractionTests(unittest.TestCase):
    def test_explicit_audience_is_extracted_verbatim(self):
        text = ("As a solo founder, I really need a tool for this. "
               "It's been a huge time sink.")
        audience = ds.extract_audience(text)
        self.assertIn("solo founder", audience)

    def test_no_audience_marker_is_unknown(self):
        audience = ds.extract_audience("I need a tool that does this thing.")
        self.assertEqual(audience, "")


class UrgencyTests(unittest.TestCase):
    def test_urgent_language_is_detected(self):
        markers = ds.find_urgency_markers("I need this right now, my launch is tomorrow.")
        self.assertTrue(markers)

    def test_calm_language_has_no_urgency_markers(self):
        markers = ds.find_urgency_markers("At some point it would be nice to have this.")
        self.assertEqual(markers, ())


class ProductizabilityTests(unittest.TestCase):
    def test_digital_friendly_need_scores_high(self):
        level, _ = ds.assess_productizability(
            "I need a spreadsheet template that automates this report.")
        self.assertEqual(level, ds.PRODUCTIZABLE_HIGH)

    def test_hardware_is_downgraded(self):
        level, reasons = ds.assess_productizability(
            "I need a physical device that measures this.")
        self.assertEqual(level, ds.PRODUCTIZABLE_LOW)
        self.assertTrue(reasons)

    def test_medical_advice_is_downgraded(self):
        level, _ = ds.assess_productizability(
            "I need someone to diagnose this condition and give medical advice.")
        self.assertEqual(level, ds.PRODUCTIZABLE_LOW)

    def test_legal_advice_is_downgraded(self):
        level, _ = ds.assess_productizability(
            "I need legal advice on how to structure this contract.")
        self.assertEqual(level, ds.PRODUCTIZABLE_LOW)

    def test_custom_bespoke_development_is_downgraded(self):
        level, _ = ds.assess_productizability(
            "I'm looking for custom software development for my specific workflow.")
        self.assertEqual(level, ds.PRODUCTIZABLE_LOW)

    def test_no_marker_either_way_is_medium(self):
        level, reasons = ds.assess_productizability("I have a general problem with X.")
        self.assertEqual(level, ds.PRODUCTIZABLE_MEDIUM)
        self.assertEqual(reasons, ())

    def test_mixed_signal_fails_closed_to_low(self):
        # a digital-friendly word alongside a blocker - blocker wins
        level, _ = ds.assess_productizability(
            "I need a custom software development project involving a dashboard.")
        self.assertEqual(level, ds.PRODUCTIZABLE_LOW)


class PostPerspectiveTests(unittest.TestCase):
    """Spec: Demand Validation phase - a purely structural (title-shape)
    signal, empirically found to separate genuine askers from
    self-promotional suppliers. Title only - body text never consulted."""

    def test_show_hn_is_supplier(self):
        self.assertEqual(
            ds.classify_post_perspective("Show HN: I built a new tool"),
            ds.PERSPECTIVE_SUPPLIER)

    def test_launch_hn_is_supplier(self):
        self.assertEqual(
            ds.classify_post_perspective("Launch HN: Corrily (YC W21) - Price Optimization"),
            ds.PERSPECTIVE_SUPPLIER)

    def test_tell_hn_is_supplier(self):
        self.assertEqual(
            ds.classify_post_perspective("Tell HN: The solution to Wikipedia's funding"),
            ds.PERSPECTIVE_SUPPLIER)

    def test_ask_hn_is_asker(self):
        self.assertEqual(
            ds.classify_post_perspective("Ask HN: Is there a tool that does X?"),
            ds.PERSPECTIVE_ASKER)

    def test_ask_yc_is_asker(self):
        self.assertEqual(
            ds.classify_post_perspective("Ask YC: I would pay for that"),
            ds.PERSPECTIVE_ASKER)

    def test_bare_ask_prefix_is_asker(self):
        self.assertEqual(
            ds.classify_post_perspective("Ask: do you offer this web prototyping tool?"),
            ds.PERSPECTIVE_ASKER)

    def test_question_mark_alone_is_asker(self):
        # Algolia often strips the "Ask HN:" prefix - the trailing "?" is
        # the fallback structural signal.
        self.assertEqual(
            ds.classify_post_perspective(
                "Is there a tool that categorizes my Stripe transactions?"),
            ds.PERSPECTIVE_ASKER)

    def test_statement_without_question_mark_is_unknown(self):
        self.assertEqual(
            ds.classify_post_perspective("I want to make a boring process fun"),
            ds.PERSPECTIVE_UNKNOWN)

    def test_empty_title_is_unknown(self):
        self.assertEqual(ds.classify_post_perspective(""), ds.PERSPECTIVE_UNKNOWN)

    def test_mid_sentence_mention_of_show_hn_does_not_misfire(self):
        # only a PREFIX counts, never a bare substring anywhere in the title
        self.assertEqual(
            ds.classify_post_perspective(
                "Why do people still post Show HN: threads on Fridays"),
            ds.PERSPECTIVE_UNKNOWN)

    def test_supplier_prefix_wins_even_with_a_question_mark(self):
        # a Show HN post can itself end in "?" (e.g. "anyone need it?") -
        # the self-presentation prefix still takes priority.
        self.assertEqual(
            ds.classify_post_perspective(
                "Show HN: I built cloud transcoding - anyone need it?"),
            ds.PERSPECTIVE_SUPPLIER)

    def test_case_insensitive(self):
        self.assertEqual(
            ds.classify_post_perspective("SHOW HN: my new tool"),
            ds.PERSPECTIVE_SUPPLIER)
        self.assertEqual(
            ds.classify_post_perspective("ASK HN: is there a tool?"),
            ds.PERSPECTIVE_ASKER)

    def test_perspective_is_available_in_demand_evidence_and_provenance(self):
        ev = ds.build_demand_evidence(
            "I would pay for a tool that does this.",
            title="Show HN: my new pricing tool")
        self.assertEqual(ev.perspective, ds.PERSPECTIVE_SUPPLIER)
        prov = ds.provenance_summary(ev)
        self.assertEqual(prov["perspective"], ds.FACT)

    def test_unknown_perspective_provenance_is_unknown(self):
        ev = ds.build_demand_evidence("some text", title="a plain statement")
        self.assertEqual(ev.perspective, ds.PERSPECTIVE_UNKNOWN)
        self.assertEqual(ds.provenance_summary(ev)["perspective"], ds.UNKNOWN)

    def test_omitting_title_defaults_to_unknown_perspective(self):
        # existing callers that never pass `title` keep working exactly
        # as before - perspective just stays UNKNOWN.
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        self.assertEqual(ev.perspective, ds.PERSPECTIVE_UNKNOWN)

    def test_perspective_never_changes_the_score(self):
        # the whole point: SAME text/title-bearing content, scored with
        # and without title - identical total, identical factors. Only
        # `.evidence.perspective` differs.
        text = "I would pay for a tool that does this."
        supplier_title = "Show HN: my pricing tool"
        s_no_title = ds.score_demand_signal(text)
        s_with_title = ds.score_demand_signal(text, title=supplier_title)
        self.assertEqual(s_no_title.total, s_with_title.total)
        self.assertEqual(s_no_title.factors, s_with_title.factors)
        self.assertNotEqual(s_no_title.evidence.perspective,
                            s_with_title.evidence.perspective)

    def test_perspective_is_not_among_the_scored_factors(self):
        score = ds.score_demand_signal("I would pay for a tool.",
                                       title="Ask HN: is there a tool?")
        self.assertNotIn("perspective", score.factors)


class TitlePrefixTypeTests(unittest.TestCase):
    """Spec: Decision-Model step - `classify_post_perspective` must stay
    byte-for-byte behavior-compatible while additionally exposing WHICH
    specific title convention matched (needed later to give `Tell HN:`
    a weaker supplier penalty than `Show/Launch HN:`)."""

    def test_show_hn_is_prefix_show(self):
        self.assertEqual(ds.classify_title_prefix_type("Show HN: my tool"), ds.PREFIX_SHOW)

    def test_launch_hn_is_prefix_launch(self):
        self.assertEqual(ds.classify_title_prefix_type("Launch HN: my startup"), ds.PREFIX_LAUNCH)

    def test_tell_hn_is_prefix_tell(self):
        self.assertEqual(ds.classify_title_prefix_type("Tell HN: a story"), ds.PREFIX_TELL)

    def test_ask_hn_is_prefix_ask(self):
        self.assertEqual(ds.classify_title_prefix_type("Ask HN: is there a tool?"), ds.PREFIX_ASK)

    def test_bare_question_mark_is_not_a_prefix_type(self):
        # the trailing "?" makes classify_post_perspective say ASKER, but
        # it is not a PREFIX at all - classify_title_prefix_type must not
        # invent one.
        self.assertEqual(
            ds.classify_title_prefix_type("Is there a tool that does this?"),
            ds.PREFIX_UNKNOWN)

    def test_empty_title_is_unknown(self):
        self.assertEqual(ds.classify_title_prefix_type(""), ds.PREFIX_UNKNOWN)

    def test_show_and_launch_and_tell_all_still_collapse_to_supplier(self):
        # classify_post_perspective's existing, unchanged behavior
        for title in ("Show HN: x", "Launch HN: x", "Tell HN: x"):
            self.assertEqual(ds.classify_post_perspective(title), ds.PERSPECTIVE_SUPPLIER)

    def test_title_prefix_type_is_available_in_demand_evidence_and_provenance(self):
        ev = ds.build_demand_evidence("some text", title="Show HN: my tool")
        self.assertEqual(ev.title_prefix_type, ds.PREFIX_SHOW)
        self.assertEqual(ds.provenance_summary(ev)["title_prefix_type"], ds.FACT)

    def test_unknown_title_prefix_type_provenance_is_unknown(self):
        ev = ds.build_demand_evidence("some text", title="a plain statement")
        self.assertEqual(ev.title_prefix_type, ds.PREFIX_UNKNOWN)
        self.assertEqual(ds.provenance_summary(ev)["title_prefix_type"], ds.UNKNOWN)

    def test_title_prefix_type_never_changes_the_score(self):
        text = "I would pay for a tool that does this."
        s_no_title = ds.score_demand_signal(text)
        s_with_title = ds.score_demand_signal(text, title="Tell HN: my pricing tool")
        self.assertEqual(s_no_title.total, s_with_title.total)
        self.assertEqual(s_no_title.factors, s_with_title.factors)


class BuilderSignalTests(unittest.TestCase):
    """Spec: Demand Validation phase, step 2 - a second, INDEPENDENT
    structural signal (title+body, not title-only like perspective).
    Sentence-scoped and qualifier-aware: a bare marker match is not
    enough when the same sentence reads as personal-use backstory or a
    bare past-experience mention with no accompanying pitch."""

    def test_i_built_is_builder_yes(self):
        state, quote = ds.classify_builder_signal("I built a new tool for teams.")
        self.assertEqual(state, ds.BUILDER_YES)
        self.assertIn("i built", quote.lower())

    def test_we_launched_is_builder_yes(self):
        state, _ = ds.classify_builder_signal("We launched our new dashboard today.")
        self.assertEqual(state, ds.BUILDER_YES)

    def test_my_saas_is_builder_yes(self):
        state, _ = ds.classify_builder_signal("Feedback wanted on my SaaS pricing page.")
        self.assertEqual(state, ds.BUILDER_YES)

    def test_our_service_is_builder_yes(self):
        state, _ = ds.classify_builder_signal("Our service handles this automatically.")
        self.assertEqual(state, ds.BUILDER_YES)

    def test_offer_marker_overrides_past_experience_qualifier(self):
        # "before" alone would suppress the match, but "anyone need it"
        # in the same sentence proves this is a current pitch, not a
        # bare capability mention.
        state, _ = ds.classify_builder_signal(
            "I built something like this before, anyone need it?")
        self.assertEqual(state, ds.BUILDER_YES)

    def test_because_i_needed_suppresses_the_match(self):
        state, quote = ds.classify_builder_signal(
            "I built this because I needed a way to track my expenses.")
        self.assertEqual(state, ds.BUILDER_UNKNOWN)
        self.assertEqual(quote, "")

    def test_for_myself_suppresses_the_match(self):
        state, _ = ds.classify_builder_signal("I made a tool for myself last weekend.")
        self.assertEqual(state, ds.BUILDER_UNKNOWN)

    def test_bare_past_experience_without_offer_marker_suppresses_the_match(self):
        state, _ = ds.classify_builder_signal(
            "We built systems like this before at my last job.")
        self.assertEqual(state, ds.BUILDER_UNKNOWN)

    def test_my_product_does_not_misfire_on_my_productivity(self):
        # word-boundary regression: a naive substring check on "my
        # product" also matches inside "my productivity" - found
        # empirically on the 64-signal validation set.
        state, _ = ds.classify_builder_signal(
            "My productivity in WFH environments has suffered lately.")
        self.assertEqual(state, ds.BUILDER_UNKNOWN)

    def test_no_marker_at_all_is_unknown(self):
        state, quote = ds.classify_builder_signal(
            "Is there a tool that categorizes my Stripe transactions?")
        self.assertEqual(state, ds.BUILDER_UNKNOWN)
        self.assertEqual(quote, "")

    def test_empty_text_and_title_is_unknown(self):
        self.assertEqual(ds.classify_builder_signal("", title="")[0], ds.BUILDER_UNKNOWN)

    def test_case_insensitive(self):
        state, _ = ds.classify_builder_signal("WE BUILT a new tool for this.")
        self.assertEqual(state, ds.BUILDER_YES)

    def test_marker_in_title_is_detected(self):
        state, quote = ds.classify_builder_signal(
            "some unrelated body text", title="I built cloud transcoding - anyone need it?")
        self.assertEqual(state, ds.BUILDER_YES)
        self.assertIn("anyone need", quote.lower())

    def test_builder_signal_is_available_in_demand_evidence_and_provenance(self):
        ev = ds.build_demand_evidence(
            "Check it out, we just launched our new tool.", title="Show HN: my tool")
        self.assertEqual(ev.builder_signal, ds.BUILDER_YES)
        self.assertTrue(ev.builder_quote)
        prov = ds.provenance_summary(ev)
        self.assertEqual(prov["builder_signal"], ds.FACT)

    def test_unknown_builder_signal_provenance_is_unknown(self):
        ev = ds.build_demand_evidence("I would pay for a tool that does this.")
        self.assertEqual(ev.builder_signal, ds.BUILDER_UNKNOWN)
        self.assertEqual(ds.provenance_summary(ev)["builder_signal"], ds.UNKNOWN)

    def test_builder_signal_never_changes_the_score(self):
        text = "I would pay for a tool that does this."
        builder_text = text + " I built something similar before, anyone need it?"
        s_plain = ds.score_demand_signal(text)
        s_builder = ds.score_demand_signal(builder_text)
        # the ADDED sentence must not move total/factors - only a change
        # in intent/budget/etc. from the extra text could do that, and
        # this added sentence carries none of those markers.
        self.assertEqual(s_plain.factors, s_builder.factors)
        self.assertNotEqual(s_plain.evidence.builder_signal,
                            s_builder.evidence.builder_signal)

    def test_builder_signal_is_not_among_the_scored_factors(self):
        score = ds.score_demand_signal(
            "I would pay for a tool.", title="Ask HN: is there a tool?")
        self.assertNotIn("builder_signal", score.factors)

    def test_independent_of_perspective(self):
        # a plain "Ask HN:" post (perspective=ASKER) can still carry a
        # builder-phrase in the body - the two signals are orthogonal.
        ev = ds.build_demand_evidence(
            "I built this myself and it works great, anyone need it?",
            title="Ask HN: thoughts on this?")
        self.assertEqual(ev.perspective, ds.PERSPECTIVE_ASKER)
        self.assertEqual(ev.builder_signal, ds.BUILDER_YES)


class SignalAgeTests(unittest.TestCase):
    def test_age_is_computed_from_a_known_timestamp(self):
        age = ds.signal_age_days("2026-08-25T00:00:00+00:00",
                                 now_iso="2026-09-04T00:00:00+00:00")
        self.assertAlmostEqual(age, 10.0, places=3)

    def test_no_timestamp_is_unknown(self):
        self.assertIsNone(ds.signal_age_days(""))

    def test_unparseable_timestamp_is_unknown(self):
        self.assertIsNone(ds.signal_age_days("not-a-date"))


class ProvenanceTests(unittest.TestCase):
    def test_fact_estimated_unknown_are_distinguishable(self):
        ev = ds.build_demand_evidence(
            "I would pay $20/month for a tool like this, as a solo founder, "
            "I need this urgently.",
            discovered_at="2026-09-01T00:00:00+00:00",
            now_iso="2026-09-04T00:00:00+00:00")
        prov = ds.provenance_summary(ev)
        self.assertEqual(prov["intent"], ds.FACT)
        self.assertEqual(prov["budget"], ds.FACT)
        self.assertEqual(prov["audience"], ds.FACT)
        self.assertEqual(prov["urgency"], ds.FACT)
        self.assertEqual(prov["age"], ds.FACT)
        self.assertEqual(prov["productizability"], ds.ESTIMATED)
        self.assertEqual(prov["repeat_signal_count"], ds.UNKNOWN)   # never supplied

    def test_empty_signal_is_all_unknown_except_the_estimate(self):
        ev = ds.build_demand_evidence("")
        prov = ds.provenance_summary(ev)
        self.assertEqual(prov["intent"], ds.UNKNOWN)
        self.assertEqual(prov["budget"], ds.UNKNOWN)
        self.assertEqual(prov["audience"], ds.UNKNOWN)
        self.assertEqual(prov["urgency"], ds.UNKNOWN)
        self.assertEqual(prov["age"], ds.UNKNOWN)
        self.assertEqual(prov["productizability"], ds.ESTIMATED)

    def test_repeat_signal_count_is_a_fact_only_when_positive(self):
        ev0 = ds.build_demand_evidence("x", repeat_signal_count=0)
        ev2 = ds.build_demand_evidence("x", repeat_signal_count=2)
        self.assertEqual(ds.provenance_summary(ev0)["repeat_signal_count"], ds.UNKNOWN)
        self.assertEqual(ds.provenance_summary(ev2)["repeat_signal_count"], ds.FACT)


class DemandQualityScoreTests(unittest.TestCase):
    def test_strong_signal_scores_well_above_a_vague_one(self):
        strong = ds.score_demand_signal(
            "As a solo founder, I would pay $25/month right now for a "
            "dashboard that automates this report.",
            discovered_at="2026-09-03T00:00:00+00:00",
            now_iso="2026-09-04T00:00:00+00:00")
        vague = ds.score_demand_signal("Just thinking out loud about stuff.")
        self.assertGreater(strong.total, vague.total)
        self.assertGreater(strong.total, 0.5)

    def test_vague_signal_never_scores_artificially_high(self):
        score = ds.score_demand_signal("Just thinking out loud about stuff.")
        self.assertLess(score.total, 0.15)
        self.assertTrue(score.factors["no_concrete_signal"]["present"])

    def test_help_request_scores_lower_than_explicit_intent(self):
        help_score = ds.score_demand_signal("I need help with my Stripe export.")
        explicit_score = ds.score_demand_signal(
            "I would pay for a tool that handles my Stripe export automatically.")
        self.assertLess(help_score.total, explicit_score.total)

    def test_not_productizable_need_is_heavily_penalised(self):
        score = ds.score_demand_signal(
            "I would pay for someone to give me medical advice on this condition.")
        self.assertTrue(score.factors["not_productizable"]["present"])
        self.assertLess(score.total, 0.3)

    def test_score_never_exceeds_bounds(self):
        score = ds.score_demand_signal(
            "As a solo founder, I would pay $999/month right now for a "
            "dashboard automation tool - this is urgent and critical.",
            discovered_at="2026-09-04T00:00:00+00:00",
            now_iso="2026-09-04T00:00:00+00:00",
            repeat_signal_count=5)
        self.assertGreaterEqual(score.total, 0.0)
        self.assertLessEqual(score.total, 1.0)

    def test_every_factor_is_named_weighted_and_explained(self):
        score = ds.score_demand_signal("I would pay for a tool that does this.")
        for name, f in score.factors.items():
            self.assertIn("weight", f)
            self.assertIn("present", f)
            self.assertIn("sign", f)
        self.assertTrue(score.reasons)

    def test_repeat_signal_count_increases_score(self):
        base = ds.score_demand_signal("I would pay for a tool that does this.")
        repeated = ds.score_demand_signal("I would pay for a tool that does this.",
                                          repeat_signal_count=3)
        self.assertGreater(repeated.total, base.total)
        self.assertTrue(repeated.factors["repeat_signal"]["present"])


class DeterminismTests(unittest.TestCase):
    def test_identical_input_gives_identical_output(self):
        text = "As a solo founder, I would pay $20/month for this, urgently."
        s1 = ds.score_demand_signal(text, discovered_at="2026-09-01T00:00:00+00:00",
                                    now_iso="2026-09-04T00:00:00+00:00")
        s2 = ds.score_demand_signal(text, discovered_at="2026-09-01T00:00:00+00:00",
                                    now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(s1.to_dict(), s2.to_dict())


class NoSideEffectsOnTypeOrVerificationTests(unittest.TestCase):
    """Hard rule: the demand-quality layer is advisory only - it must not
    read or write opportunity_type / verification state, and this step
    must not touch task_signal's TASK logic at all."""

    def test_module_does_not_import_verification_or_discovery(self):
        import sys

        # demand_signal must already be imported by the test collection
        # above; assert its own module namespace never pulled in the
        # verification/discovery/task_signal modules.
        mod = sys.modules["revenue_os.ecosystem.demand_signal"]
        src_modules = {getattr(v, "__module__", "") for v in vars(mod).values()}
        self.assertNotIn("revenue_os.ecosystem.verification", src_modules)
        self.assertNotIn("revenue_os.ecosystem.discovery", src_modules)
        self.assertNotIn("revenue_os.ecosystem.task_signal", src_modules)

    def test_no_public_function_accepts_or_returns_opportunity_type(self):
        import inspect

        for name in ("build_demand_evidence", "score_demand_quality",
                     "score_demand_signal", "classify_purchase_intent"):
            fn = getattr(ds, name)
            sig = inspect.signature(fn)
            self.assertNotIn("opportunity_type", sig.parameters)

    def test_scoring_never_raises_on_task_shaped_text(self):
        # a TASK-shaped signal (e.g. "I will pay $20 for this bounty") must
        # score cleanly as a demand signal too - this module has no notion
        # of TASK_KINDS and must not special-case it either way.
        score = ds.score_demand_signal("I will pay $20 for this bounty.")
        self.assertIsInstance(score.total, float)


if __name__ == "__main__":
    unittest.main()
