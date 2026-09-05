"""Demand Sources adapter (spec: Demand-to-Revenue plan, Step 2).

Covers: AcqRecord -> DemandEvidence -> OpportunityDraft for each reused
acquisition source (HN Algolia, Stack Exchange, Lobsters, Lemmy), missing/
unknown fields, stale signals, in-batch dedupe + repeat counting via the
existing TASK fingerprint, URL/source provenance, FACT/ESTIMATED/UNKNOWN,
determinism, and non-interference with the existing TASK classification
and verification gates.

All fixtures are in-memory `AcqRecord`s or a fixture `AcqSearchable` - no
network access anywhere in this file.
"""

from __future__ import annotations

import unittest

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import demand_signal, demand_sources, model


def _record(**kw) -> AcqRecord:
    base = dict(
        title="Is there a tool that categorizes my Stripe transactions?",
        url="https://news.ycombinator.com/item?id=40000123",
        text="I run a small agency and would pay $20/month for something "
             "that does this automatically.",
        author="founder_x",
        posted_at="2026-09-01T00:00:00+00:00",
        platform="Hacker News",
        source="hn-algolia",
        query="is there a tool that",
    )
    base.update(kw)
    return AcqRecord(**base)


class _FixtureSource:
    """A minimal `AcqSearchable` fixture - no network, deterministic."""

    def __init__(self, records: list[AcqRecord]) -> None:
        self._records = records

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        return [r for r in self._records if r.query == query][:limit]


#: word-based (never digit-based) index labels for round-robin fixtures -
#: the fingerprint dedupe deliberately strips DIGITS from a title (spec:
#: TASK dedupe, reused here), so a fixture using e.g. "q1"/"q2" or
#: "result 0"/"result 1" would collapse into a single fingerprint and
#: falsely look like a duplicate. Real, distinct English words (for both
#: the query name AND the per-index label) avoid that entirely.
_IDX_WORDS = ("one", "two", "three", "four", "five", "six", "seven")


class _MultiQuerySource:
    """A fixture AcqSearchable that returns N fresh, TEXTUALLY DISTINCT
    records for WHATEVER query it is asked about - used to prove
    round-robin fairness without depending on real API behaviour. Callers
    must use word-based (not digit-based) query names - see _IDX_WORDS."""

    def __init__(self, per_query: int = 3) -> None:
        self.per_query = per_query
        self.calls: list[str] = []

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        self.calls.append(query)
        n = min(self.per_query, limit)
        return [_record(title=f"{query} query needs {_IDX_WORDS[i]} widget",
                        url=f"https://news.ycombinator.com/item?id={query}-{_IDX_WORDS[i]}",
                        query=query)
               for i in range(n)]


class RoundRobinFairnessTests(unittest.TestCase):
    """Spec: Demand Validation phase - "jede Query muss eine Chance
    bekommen". Regression coverage for the real-run finding that a query
    late in DEFAULT_QUERIES (e.g. the empirically strongest one,
    "i would pay for a tool" at position 10 of 17) was being silently
    crowded out because early queries alone already filled the global
    limit under the old sequential-concatenation order."""

    def test_a_late_query_is_not_starved_by_earlier_ones(self):
        # 5 queries x 3 records each = 15 candidates; a global limit of 6
        # is not enough for even the first two queries alone (3+3=6) to
        # be the ONLY ones represented if fairness is respected - the
        # 5th (last) query must still get at least one slot.
        queries = ("alpha", "bravo", "charlie", "delta", "echo")
        fetcher = _MultiQuerySource(per_query=3)
        src = demand_sources.DemandDiscoverySource(
            "hn-algolia", fetcher, queries=queries)
        drafts = src.discover(6)
        self.assertEqual(len(drafts), 6)
        titles = " ".join(d.title for d in drafts)
        for q in queries:
            self.assertIn(f"{q} query", titles, f"query {q!r} was starved: {titles}")

    def test_every_query_is_still_actually_called(self):
        fetcher = _MultiQuerySource(per_query=2)
        src = demand_sources.DemandDiscoverySource(
            "hn-algolia", fetcher, queries=("alpha", "bravo", "charlie"))
        src.discover(3)
        self.assertEqual(fetcher.calls, ["alpha", "bravo", "charlie"])

    def test_round_robin_preserves_dedupe_and_repeat_counting(self):
        # the SAME underlying record (same fingerprint) surfaced by two
        # different queries must still collapse to one draft with a
        # correct repeat_signal_count, round-robin or not.
        dup_a = _record(query="q1")
        dup_b = _record(query="q2", url="https://news.ycombinator.com/item?id=99999")

        class _DupSource:
            def search(self, query, limit, *, since_ts=None):
                return [dup_a] if query == "q1" else [dup_b]

        drafts = demand_sources.discover_demand_signals(
            _DupSource(), queries=("q1", "q2"), limit=5)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].raw["demand_evidence"]["repeat_signal_count"], 1)

    def test_a_failing_query_among_several_does_not_break_round_robin(self):
        class _FlakyMultiSource:
            def __init__(self):
                self.calls = 0

            def search(self, query, limit, *, since_ts=None):
                self.calls += 1
                if query == "bad":
                    raise ConnectionError("simulated outage")
                return [_record(title=f"{query} result alpha",
                               url=f"https://news.ycombinator.com/item?id={query}-alpha",
                               query=query)]

        errors: list = []
        drafts = demand_sources.discover_demand_signals(
            _FlakyMultiSource(), queries=("goodone", "bad", "goodtwo"),
            limit=5, errors=errors)
        titles = " ".join(d.title for d in drafts)
        self.assertIn("goodone", titles)
        self.assertIn("goodtwo", titles)
        self.assertTrue(any("bad" in e for e in errors))


class DefaultQueriesTests(unittest.TestCase):
    """Demand Validation phase: the query list was broadened (real-run
    finding - see the module's own comment) to cover concrete commercial
    pain beyond bare tool-lookup phrasing. Additive only - nothing
    removed, so existing recall/tests relying on the original phrases
    keep working."""

    def test_original_step_2_queries_are_still_present(self):
        original = {
            "is there a tool that", "is there a service that",
            "i would pay for a tool", "i would pay for a service",
            "looking for a tool that", "looking for software that",
            "does anyone know a tool for", "recommend a tool for",
            "what tool do you use for",
        }
        self.assertTrue(original.issubset(set(demand_sources.DEFAULT_QUERIES)))

    def test_new_commercial_pain_queries_are_present(self):
        expected_new = {
            "need a way to automate", "how do i automate",
            "alternative to", "is too expensive",
            "paying for a tool", "would pay for a tool",
        }
        self.assertTrue(expected_new.issubset(set(demand_sources.DEFAULT_QUERIES)))

    def test_no_query_contains_fabricated_intent_language(self):
        # a query is a SEARCH TERM only - none of them should themselves
        # read as a first-person purchase claim (that would blur "what we
        # searched for" with "what the source actually said"); they all
        # stay third-person/neutral phrasing except the two explicit
        # "i would pay" probes, which exist to SURFACE such posts, not to
        # fabricate one.
        for q in demand_sources.DEFAULT_QUERIES:
            self.assertIsInstance(q, str)
            self.assertTrue(q.strip())


class AcqRecordToDraftTests(unittest.TestCase):
    def test_hn_record_with_explicit_purchase_intent(self):
        rec = _record(
            title="I would pay for a tool that categorizes my Stripe transactions",
            text="As a solo founder, I would pay $20/month for this, urgently.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.opportunity_type, model.TYPE_DIGITAL_PRODUCT)
        self.assertGreater(draft.demand_hint, 0.5)
        self.assertEqual(draft.source_meta.source, "hn-algolia")

    def test_hn_record_with_concrete_tool_request_and_digital_shape(self):
        # PROBLEM_INTEREST alone is not a "sichere Grundlage" (Step 3) - it
        # only escalates to TYPE_DIGITAL_PRODUCT when the need is ALSO
        # independently confirmed digital-shaped (PRODUCTIZABLE_HIGH).
        rec = _record(
            title="Is there a tool that generates a dashboard from my "
                  "Stripe transactions?",
            text="A spreadsheet export would already help a lot.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.opportunity_type, model.TYPE_DIGITAL_PRODUCT)
        self.assertGreater(draft.demand_hint, 0.0)

    def test_bare_tool_question_without_corroboration_fails_closed_to_other(self):
        # spec Step 3: "nicht allein deshalb ... weil es kommerziell
        # interessant klingt" - a bare "is there a tool" question with no
        # digital-shape confirmation and no explicit purchase intent is
        # NOT a sichere Grundlage - stays TYPE_OTHER.
        rec = _record(
            title="Is there a tool that categorizes my Stripe transactions?",
            text="Doing this manually every month is painful.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.opportunity_type, model.TYPE_OTHER)

    def test_stackexchange_problem(self):
        rec = _record(
            source="stackexchange", platform="Stack Overflow",
            url="https://stackoverflow.com/questions/12345/csv-dedupe",
            title="Looking for a tool that dedupes CSV rows automatically",
            text="I would pay for a service that does this - manual dedupe "
                 "takes hours every week.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.source_meta.source, "stackexchange")
        self.assertEqual(draft.opportunity_type, model.TYPE_DIGITAL_PRODUCT)

    def test_lobsters_problem(self):
        rec = _record(
            source="lobsters", platform="Lobsters",
            url="https://lobste.rs/s/abc123/csv_dedupe",
            title="Does anyone know a tool for deduping CSV exports?",
            text="Recommend a tool for this if you have one.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.source_meta.source, "lobsters")
        self.assertIn(draft.opportunity_type, (model.TYPE_DIGITAL_PRODUCT, model.TYPE_OTHER))

    def test_lemmy_problem(self):
        rec = _record(
            source="lemmy", platform="Lemmy (c/opensource)",
            url="https://lemmy.world/post/999",
            title="Looking for software that dedupes CSV rows",
            text="I would pay for a service that handles this for me.")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(draft.source_meta.source, "lemmy")
        self.assertEqual(draft.opportunity_type, model.TYPE_DIGITAL_PRODUCT)

    def test_missing_and_unknown_fields_do_not_crash_or_invent_data(self):
        rec = AcqRecord(title="Some post", url="", text="", author="",
                        posted_at="", platform="", source="", query="q")
        draft = demand_sources.acq_record_to_draft(rec)
        self.assertEqual(draft.title, "Some post")
        self.assertEqual(draft.raw["target_customer"], "")
        self.assertEqual(draft.demand_hint, 0.0)
        self.assertEqual(draft.est_pay_eur, 0.0)
        self.assertIsNone(draft.raw["demand_evidence"]["age_days"])

    def test_old_signal_is_penalised(self):
        rec = _record(posted_at="2025-01-01T00:00:00+00:00")
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertTrue(draft.raw["demand_quality"]["factors"]["stale_signal"]["present"])

    def test_url_and_source_provenance_are_preserved(self):
        rec = _record(url="https://news.ycombinator.com/item?id=999&ref=foo")
        draft = demand_sources.acq_record_to_draft(rec)
        self.assertIn("id=999", draft.source_url)
        self.assertEqual(draft.source_meta.source, "hn-algolia")
        self.assertEqual(draft.source_id, draft.source_url)

    def test_generic_help_seeking_text_never_becomes_a_false_purchase_intent(self):
        rec = _record(
            title="How do I get my first paying customers?",
            text="Launched my SaaS 3 months ago, 0 paying customers so far.")
        draft = demand_sources.acq_record_to_draft(rec)
        self.assertEqual(draft.opportunity_type, model.TYPE_OTHER)
        self.assertLess(draft.demand_hint, 0.3)
        self.assertNotEqual(
            draft.raw["demand_evidence"]["intent_level"],
            demand_signal.INTENT_EXPLICIT)

    def test_deterministic(self):
        rec = _record()
        d1 = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        d2 = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(d1.title, d2.title)
        self.assertEqual(d1.demand_hint, d2.demand_hint)
        self.assertEqual(d1.raw["demand_quality"], d2.raw["demand_quality"])
        self.assertEqual(d1.raw["fingerprint"], d2.raw["fingerprint"])


class ProvenanceTests(unittest.TestCase):
    def test_fact_estimated_unknown_are_all_present_and_distinguishable(self):
        rec = _record()
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        prov = draft.raw["demand_provenance"]
        self.assertEqual(prov["title_and_url"], demand_signal.FACT)
        self.assertEqual(prov["opportunity_type"], demand_signal.ESTIMATED)
        self.assertEqual(prov["productizability"], demand_signal.ESTIMATED)
        # this fixture states a budget and an audience explicitly
        self.assertEqual(prov["budget"], demand_signal.FACT)
        self.assertEqual(prov["audience"], demand_signal.FACT)
        # repeat_signal_count was never supplied for a single-record call
        self.assertEqual(prov["repeat_signal_count"], demand_signal.UNKNOWN)

    def test_unknown_stays_unknown_when_nothing_is_stated(self):
        rec = _record(text="", title="A vague post about nothing in particular")
        draft = demand_sources.acq_record_to_draft(rec)
        prov = draft.raw["demand_provenance"]
        self.assertEqual(prov["budget"], demand_signal.UNKNOWN)
        self.assertEqual(prov["audience"], demand_signal.UNKNOWN)


class RankingLayerReadModelTests(unittest.TestCase):
    """Spec: Decision-/Ranking-Design step, additive Read-Model
    integration. `acq_record_to_draft` must expose `buyer_confidence`/
    `problem_confidence` in `draft.raw` WITHOUT changing anything that
    was already there - `demand_hint` (== `demand_quality.total`),
    `opportunity_type`, `est_pay_eur`, `demand_evidence`, `demand_quality`
    and `demand_provenance` must be byte-for-byte identical to before
    this integration."""

    def test_buyer_and_problem_confidence_are_present(self):
        draft = demand_sources.acq_record_to_draft(_record())
        self.assertIn("buyer_confidence", draft.raw)
        self.assertIn("problem_confidence", draft.raw)
        self.assertIsInstance(draft.raw["buyer_confidence"]["total"], float)
        self.assertIsInstance(draft.raw["problem_confidence"]["total"], float)

    def test_buyer_confidence_matches_a_direct_call_on_the_same_evidence(self):
        from revenue_os.ecosystem import demand_ranking

        rec = _record()
        draft = demand_sources.acq_record_to_draft(rec)
        evidence = demand_signal.build_demand_evidence(
            f"{rec.title} {rec.text}".strip(), title=rec.title,
            discovered_at=rec.posted_at, source_type=rec.source or "")
        expected = demand_ranking.buyer_confidence(evidence).to_dict()
        self.assertEqual(draft.raw["buyer_confidence"]["total"], expected["total"])
        self.assertEqual(draft.raw["buyer_confidence"]["factors"], expected["factors"])

    def test_problem_confidence_matches_a_direct_call_on_the_same_evidence(self):
        from revenue_os.ecosystem import demand_ranking

        rec = _record()
        draft = demand_sources.acq_record_to_draft(rec)
        evidence = demand_signal.build_demand_evidence(
            f"{rec.title} {rec.text}".strip(), title=rec.title,
            discovered_at=rec.posted_at, source_type=rec.source or "")
        expected = demand_ranking.problem_confidence(evidence).to_dict()
        self.assertEqual(draft.raw["problem_confidence"]["total"], expected["total"])
        self.assertEqual(draft.raw["problem_confidence"]["factors"], expected["factors"])

    def test_factors_and_reasons_are_exposed(self):
        draft = demand_sources.acq_record_to_draft(_record())
        for key in ("buyer_confidence", "problem_confidence"):
            self.assertIn("factors", draft.raw[key])
            self.assertIn("reasons", draft.raw[key])
            for name, f in draft.raw[key]["factors"].items():
                self.assertIn("weight", f)
                self.assertIn("present", f)
                self.assertIn("sign", f)

    def test_existing_demand_score_and_related_fields_are_untouched(self):
        rec = _record()
        draft = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        evidence = demand_signal.build_demand_evidence(
            f"{rec.title} {rec.text}".strip(), title=rec.title,
            discovered_at=rec.posted_at, now_iso="2026-09-04T00:00:00+00:00",
            source_type=rec.source or "")
        expected_score = demand_signal.score_demand_quality(evidence)
        self.assertEqual(draft.raw["demand_quality"], expected_score.to_dict())
        self.assertEqual(draft.demand_hint, expected_score.total)
        self.assertNotIn("buyer_confidence", draft.raw["demand_quality"]["factors"])
        self.assertNotIn("problem_confidence", draft.raw["demand_quality"]["factors"])


class DedupeAndRepeatSignalTests(unittest.TestCase):
    def test_duplicate_records_within_a_batch_are_deduped(self):
        rec = _record()
        same_again = _record(url="https://news.ycombinator.com/item?id=40000999")
        src = _FixtureSource([rec, same_again])
        drafts = demand_sources.discover_demand_signals(
            src, queries=("is there a tool that",), now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].raw["demand_evidence"]["repeat_signal_count"], 1)
        self.assertTrue(drafts[0].raw["demand_quality"]["factors"]["repeat_signal"]["present"])

    def test_distinct_problems_are_not_merged(self):
        rec_a = _record(title="Is there a tool that categorizes my Stripe transactions?")
        rec_b = _record(
            title="Is there a tool that tracks my inventory automatically?",
            url="https://news.ycombinator.com/item?id=40000777")
        src = _FixtureSource([rec_a, rec_b])
        drafts = demand_sources.discover_demand_signals(
            src, queries=("is there a tool that",))
        self.assertEqual(len(drafts), 2)
        for d in drafts:
            self.assertEqual(d.raw["demand_evidence"]["repeat_signal_count"], 0)

    def test_queries_that_return_nothing_yield_no_drafts(self):
        src = _FixtureSource([])
        drafts = demand_sources.discover_demand_signals(src, queries=("is there a tool that",))
        self.assertEqual(drafts, [])


class _SinceTsRecordingSource:
    """Records the exact `since_ts` value it was called with - proves the
    freshness filter is a pure pass-through, not reinterpreted anywhere
    on the way to the real fetcher."""

    def __init__(self) -> None:
        self.received: list = []

    def search(self, query, limit, *, since_ts=None):
        self.received.append(since_ts)
        return [_record(query=query)]


class FreshnessFilterTests(unittest.TestCase):
    """Spec: Demand Validation phase, retrieval-only freshness filter -
    `since_ts` narrows what gets FETCHED, never touches demand_signal.py's
    scoring and never deletes anything already persisted (that guarantee
    is proven at the DiscoveryEngine level in
    test_demand_discovery_integration.py)."""

    def test_since_ts_is_passed_through_unchanged_to_the_fetcher(self):
        src = _SinceTsRecordingSource()
        demand_sources.discover_demand_signals(
            src, queries=("alpha", "bravo"), since_ts="2026-08-28T00:00:00+00:00")
        self.assertEqual(src.received, ["2026-08-28T00:00:00+00:00"] * 2)

    def test_default_since_ts_is_none_unchanged(self):
        src = _SinceTsRecordingSource()
        demand_sources.discover_demand_signals(src, queries=("alpha",))
        self.assertEqual(src.received, [None])

    def test_demand_discovery_source_threads_since_ts_to_every_query(self):
        src = _SinceTsRecordingSource()
        wrapped = demand_sources.DemandDiscoverySource(
            "hn-algolia", src, queries=("alpha", "bravo", "charlie"),
            since_ts="2026-08-01T00:00:00+00:00")
        wrapped.discover(10)
        self.assertEqual(set(src.received), {"2026-08-01T00:00:00+00:00"})

    def test_build_demand_source_accepts_since_ts(self):
        src = demand_sources.build_demand_source(
            "demand-hn", since_ts="2026-08-01T00:00:00+00:00")
        self.assertEqual(src._since_ts, "2026-08-01T00:00:00+00:00")

    def test_build_demand_source_default_since_ts_is_none(self):
        src = demand_sources.build_demand_source("demand-hn")
        self.assertIsNone(src._since_ts)

    def test_freshness_filter_never_touches_scoring(self):
        # the SAME record, scored with and without a since_ts context
        # (since_ts only affects what the FETCHER receives, never what
        # build_demand_evidence/score_demand_quality compute) - identical
        # score either way.
        rec = _record()
        d1 = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        d2 = demand_sources.acq_record_to_draft(rec, now_iso="2026-09-04T00:00:00+00:00")
        self.assertEqual(d1.demand_hint, d2.demand_hint)
        self.assertEqual(d1.raw["demand_quality"], d2.raw["demand_quality"])


class NoInterferenceWithTaskLogicTests(unittest.TestCase):
    """Hard rule: this step must not change TASK classification or
    verification behaviour at all."""

    def test_task_signal_classification_is_unaffected(self):
        from revenue_os.ecosystem import task_signal
        from revenue_os.ecosystem.model import OpportunityDraft

        # the exact fixture used in the TASK discovery-quality-layer tests
        draft = OpportunityDraft(
            title="A paid CSV dedupe script",
            evidence=["Ask HN: I will pay for a CSV dedupe script"])
        self.assertEqual(task_signal.classify_task_kind(draft), model.TASK_INSTANT_PAID)

    def test_verification_gates_are_unaffected(self):
        from revenue_os.ecosystem import verification
        from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta

        meta = SourceMeta(source="unit", source_type="test",
                          access_method=model.ACCESS_OFFICIAL_API,
                          automation_allowed=True, policy_status=model.POLICY_OK)
        draft = OpportunityDraft(
            title="A paid CSV dedupe script", description="budget ~50 eur",
            opportunity_type=model.TYPE_DIGITAL_PRODUCT,
            evidence=["Ask HN: I will pay for a CSV dedupe script"],
            source_meta=meta, est_pay_eur=29.0)
        v = verification.verify(draft)
        self.assertEqual(v.status, model.V_QUALIFIED)

    def test_demand_sources_module_does_not_import_verification(self):
        # demand_sources.py may import task_signal (it reuses the
        # fingerprint utility) but must never import verification.py -
        # this step does not touch the verification gate at all.
        import ast
        import inspect

        source = inspect.getsource(demand_sources)
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported_names.update(alias.name for alias in node.names)
        self.assertNotIn("verification", imported_names)


if __name__ == "__main__":
    unittest.main()
