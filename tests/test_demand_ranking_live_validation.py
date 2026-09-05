"""Demand Ranking Layer - empirical validation (spec: Decision-/Ranking-
Design, live-signal validation step).

Two independent things live here:

1. `LiveValidationSmokeTests` - an OPT-IN, network-using smoke test over
   the four real, public, keyless demand sources (HN Algolia, Stack
   Exchange, Lobsters, Lemmy). Gated by `REVENUE_OS_NET_TESTS` (same
   convention as test_cli.py's `LiveHackerNewsTests` and
   test_llm_normalize.py/test_llm_offer.py) - normal `pytest` runs never
   touch the network. It asserts STRUCTURAL properties only (every field
   present, every score bounded, no crash across real, messy, arbitrary
   Unicode text from four different platforms) - never exact values,
   since live content changes constantly. This is the re-runnable half
   of the manual 1502-signal / 120-item-hand-labeled validation run
   documented in the accompanying report (see PR/commit description) -
   re-run it any time with:

       REVENUE_OS_NET_TESTS=1 python -m pytest tests/test_demand_ranking_live_validation.py -v

2. `KnownFailureModeRegressionTests` - fully OFFLINE, deterministic
   fixtures that reproduce, in miniature, the concrete failure modes the
   live validation run actually found (see the report). These exist so a
   future change to demand_signal.py/demand_ranking.py cannot silently
   make one of these DOCUMENTED, KNOWN limitations worse without a test
   failing - even though none of them are fixed in this step (spec:
   "keine Optimierung der Scores... nur dokumentieren"). Every fixture
   text below is SYNTHETIC (written for this test), not a copy of any
   real post - the failure mode reproduces on any topically-similar
   text, which is exactly the point (it is a topic-domain-blindness
   issue, not something specific to one real post's wording).
"""

from __future__ import annotations

import os
import unittest

from revenue_os.ecosystem import demand_ranking as dr
from revenue_os.ecosystem import demand_signal as ds


class KnownFailureModeRegressionTests(unittest.TestCase):
    """Spec: Decision-/Ranking-Design validation step, failure modes
    found on the live 1502-signal pull (2026-09-05). None of these are
    fixed here - see the module docstring above."""

    def test_political_news_text_can_trigger_explicit_purchase_intent(self):
        # FOUND ON: live Lemmy pull, 32/54 hand-labeled false positives in
        # the 120-item validation sample were political/news articles
        # from general-interest Lemmy communities; several were tagged
        # EXPLICIT_PURCHASE_INTENT via "would pay"/"willing to pay"
        # appearing in a policy/economics sentence, not a commercial
        # demand statement. classify_purchase_intent() is a pure
        # substring matcher with no topic-relevance check - this is a
        # known, documented limitation (a query/data-source suitability
        # issue for general-interest platforms), not a code defect
        # against its own contract (the module never claimed topic
        # awareness). Synthetic reproduction, not a copied real article.
        text = ("Voters said they would pay higher taxes to fund the new "
                "climate policy, according to the poll released Tuesday.")
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_EXPLICIT)
        self.assertEqual(quote, "would pay")

    def test_willing_to_pay_is_not_negation_safe(self):
        # FOUND ON: the same live pull - unlike the bare "would pay"/
        # "will pay for" markers (which demand_signal.py's own docstring
        # explicitly documents as negation-safe, since "would not pay"
        # does not contain "would pay" as a substring), "willing to pay"
        # carries NO such documented guarantee and does fire inside a
        # negated sentence. Not a violation of any documented contract
        # (negation-safety was only ever claimed for the two bare forms)
        # - documented here as a real, found nuance, not fixed.
        text = "Critics argue the government should not be willing to pay the ransom."
        level, quote = ds.classify_purchase_intent(text)
        self.assertEqual(level, ds.INTENT_EXPLICIT)
        self.assertEqual(quote, "willing to pay")

    def test_a_political_false_positive_still_gets_nonzero_buyer_confidence(self):
        # Consequence of the above at the ranking layer: a topically
        # irrelevant post with an accidental EXPLICIT match still gets
        # the full explicit_purchase_intent_base bonus - buyer_confidence
        # has no independent topic-relevance check either (it was never
        # designed to have one; it consumes demand_signal.py's evidence
        # as given, spec: "keine neue Marker-Extraktion" for this layer).
        text = ("Voters said they would pay higher taxes to fund the new "
                "climate policy, according to the poll released Tuesday.")
        ev = ds.build_demand_evidence(text)
        bc = dr.buyer_confidence(ev)
        self.assertTrue(bc.factors["explicit_purchase_intent_base"]["present"])
        self.assertGreater(bc.total, 0.0)

    def test_perspective_is_structurally_blind_on_non_hn_platforms(self):
        # FOUND ON: the live pull - of 1502 real signals, 390 came from
        # Lemmy and NONE of them had a detectable title_prefix_type other
        # than UNKNOWN (Lemmy has no Show/Ask/Launch/Tell HN convention).
        # A genuine supplier self-promotion post on Lemmy - a "looking for
        # testers for my product" pitch - therefore gets perspective=
        # UNKNOWN, not SUPPLIER, and the supplier_penalty never fires -
        # a real, found platform-coverage gap in the STRUCTURAL signal,
        # not something classify_post_perspective's own contract ever
        # promised to catch (it only recognizes HN's own conventions).
        title = "Looking for testers for our new game server host"
        text = ("Hey everyone, we're building the next-gen game server "
                "host where you pay by the hour instead of monthly. "
                "We're in public beta, come try it out!")
        ev = ds.build_demand_evidence(text, title=title)
        self.assertEqual(ev.perspective, ds.PERSPECTIVE_UNKNOWN)
        self.assertEqual(ev.title_prefix_type, ds.PREFIX_UNKNOWN)
        bc = dr.buyer_confidence(ev)
        self.assertFalse(bc.factors["supplier_penalty"]["present"])

    def test_problem_confidence_is_less_fooled_by_the_political_false_positive(self):
        # The one bright spot found: problem_confidence's top-50 in the
        # live pull had ZERO Lemmy political/news items (vs. 28/50 for
        # the existing demand_score and 12/50 for buyer_confidence) -
        # because political text essentially never matches the much
        # stronger PROBLEM_INTEREST+ASKER combo ("is there a tool
        # that...?"). It is NOT immune - the same EXPLICIT match still
        # earns the (weaker) `explicit_intent_base` factor by design
        # (see demand_ranking.py) - just far less than buyer_confidence
        # gets from the same text, and without the strong asker-combo
        # bonus that drives problem_confidence's real top ranks.
        text = ("Voters said they would pay higher taxes to fund the new "
                "climate policy, according to the poll released Tuesday.")
        ev = ds.build_demand_evidence(text)
        bc = dr.buyer_confidence(ev)
        pc = dr.problem_confidence(ev)
        self.assertFalse(pc.factors["problem_interest_asker_combo"]["present"])
        self.assertLess(pc.total, bc.total)


class LiveValidationSmokeTests(unittest.TestCase):
    """Opt-in (REVENUE_OS_NET_TESTS=1). Read-only public API calls only -
    no login, no credentials, no scraping of protected areas. Asserts
    structure/bounds only; prints an aggregate summary for manual
    inspection (not asserted, since live content is not deterministic)."""

    @unittest.skipUnless(
        os.environ.get("REVENUE_OS_NET_TESTS"), "network tests disabled")
    def test_all_four_sources_produce_structurally_valid_ranked_evidence(self):
        from revenue_os.acquisition_sources import (
            HNAlgoliaSource, LemmySource, LobstersSource, StackExchangeSource,
        )
        from revenue_os.ecosystem import demand_sources

        fetchers = {
            "hn-algolia": (HNAlgoliaSource(), 5),
            "stackexchange": (StackExchangeSource(), 5),
            "lobsters": (LobstersSource(), 5),
            "lemmy": (LemmySource(), 5),
        }
        # a SMALL query subset - this is a fast smoke test, not the full
        # deep validation run (that was done manually once; see the report)
        queries = ("is there a tool that", "i would pay for a tool")

        all_drafts = []
        sources_seen = set()
        for name, (fetcher, limit) in fetchers.items():
            errs = []
            drafts = demand_sources.discover_demand_signals(
                fetcher, queries=queries, limit=limit, errors=errs)
            all_drafts.extend(drafts)
            if drafts:
                sources_seen.add(name)

        self.assertGreater(len(all_drafts), 0, "no source returned anything")

        for d in all_drafts:
            bc = d.raw.get("buyer_confidence")
            pc = d.raw.get("problem_confidence")
            dq = d.raw.get("demand_quality")
            self.assertIsNotNone(bc)
            self.assertIsNotNone(pc)
            self.assertIsNotNone(dq)
            for score in (bc, pc, dq):
                self.assertGreaterEqual(score["total"], 0.0)
                self.assertLessEqual(score["total"], 1.0)
            ev = d.raw.get("demand_evidence")
            self.assertIn(ev["perspective"], ds.PERSPECTIVES)
            self.assertIn(ev["builder_signal"], ds.BUILDER_SIGNAL_STATES)
            self.assertIn(ev["title_prefix_type"], ds.TITLE_PREFIX_TYPES)

        print(f"\n[live validation smoke] {len(all_drafts)} real signals from "
              f"{len(sources_seen)}/4 sources ({sorted(sources_seen)}) - "
              f"all structurally valid.")

    @unittest.skipUnless(
        os.environ.get("REVENUE_OS_NET_TESTS"), "network tests disabled")
    def test_buy_recommendation_sources_produce_structurally_valid_ranked_evidence(self):
        """Demand Discovery expansion - the two new buy-recommendation
        sources ('demand-stackexchange-recs' / 'demand-lemmy-buying').
        Read-only GET only, a small query subset, no login/posting/
        account-creation. Reports how many raw records were found, how
        many survived IntentFilteredSource + dedup, and how many carry a
        meaningful buyer/problem signal - manual inspection only, since
        live content is not deterministic."""
        from revenue_os.ecosystem import demand_sources
        from revenue_os.ecosystem.sources import build_source

        sources = {
            "demand-stackexchange-recs": build_source("demand-stackexchange-recs"),
            "demand-lemmy-buying": build_source("demand-lemmy-buying"),
        }

        summary = {}
        all_drafts = []
        for name, src in sources.items():
            drafts = src.discover(10)
            all_drafts.extend(drafts)
            buyer_hits = sum(1 for d in drafts if d.raw["buyer_confidence"]["total"] > 0.3)
            problem_hits = sum(1 for d in drafts if d.raw["problem_confidence"]["total"] > 0.3)
            summary[name] = {
                "after_filter_and_dedup": len(drafts),
                "buyer_signal_count": buyer_hits,
                "problem_signal_count": problem_hits,
                "errors": list(src.last_errors),
            }

        for d in all_drafts:
            bc = d.raw.get("buyer_confidence")
            pc = d.raw.get("problem_confidence")
            self.assertIsNotNone(bc)
            self.assertIsNotNone(pc)
            for score in (bc, pc):
                self.assertGreaterEqual(score["total"], 0.0)
                self.assertLessEqual(score["total"], 1.0)
            level = d.raw["demand_evidence"]["intent_level"]
            # IntentFilteredSource guarantees only these two levels survive
            self.assertIn(level, (ds.INTENT_EXPLICIT, ds.INTENT_PROBLEM))

        print("\n[buy-recommendation live smoke]")
        for name, s in summary.items():
            print(f"  {name}: {s['after_filter_and_dedup']} signals after "
                  f"filter+dedup, {s['buyer_signal_count']} buyer / "
                  f"{s['problem_signal_count']} problem hits (>0.3), "
                  f"errors={s['errors']}")
        titles = [d.title for d in all_drafts]
        print(f"  sample titles: {titles[:5]}")


if __name__ == "__main__":
    unittest.main()
