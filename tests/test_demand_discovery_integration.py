"""Demand Discovery integration with the real DiscoveryEngine (spec:
Demand-to-Revenue plan, Step 3).

Covers: a real DiscoveryEngine.run() over a DemandDiscoverySource wrapping
a fixture AcqSearchable - persistence, provenance, canonical URL,
dedupe, multiple sources, fail-closed on invalid records, no change to
TASK classification or verification gates, no automatic acceptance/
build/execution, and determinism.

No network in this file: DemandDiscoverySource always wraps a fixture
`AcqSearchable`, never the real acquisition_sources.py fetchers. The
build_source('demand-*') factory wiring is checked WITHOUT calling
.discover() (so it never hits the network either).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import demand_signal, demand_sources, model, sources
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.opportunity_store import load_opportunities


def _record(**kw) -> AcqRecord:
    base = dict(
        title="I would pay for a tool that categorizes my Stripe transactions",
        url="https://news.ycombinator.com/item?id=40000123",
        text="As a solo founder, I would pay $20/month for this, urgently.",
        author="founder_x", posted_at="2026-09-01T00:00:00+00:00",
        platform="Hacker News", source="hn-algolia", query="q")
    base.update(kw)
    return AcqRecord(**base)


class _FixtureSource:
    """A minimal AcqSearchable fixture, matching test_demand_sources.py's
    pattern - no network."""

    def __init__(self, records: list[AcqRecord]) -> None:
        self._records = records

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        return list(self._records)[:limit]


class _FlakyFixtureSource:
    """Fails on the first call, succeeds afterwards - proves one bad
    query does not kill the whole run."""

    def __init__(self, records: list[AcqRecord]) -> None:
        self._records = records
        self.calls = 0

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("simulated transient outage")
        return list(self._records)[:limit]


class DiscoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _demand_source(self, records, **kw) -> demand_sources.DemandDiscoverySource:
        return demand_sources.DemandDiscoverySource(
            "hn-algolia", _FixtureSource(records),
            queries=("is there a tool that",), **kw)

    def test_acquisition_record_is_correctly_integrated_into_discovery(self):
        src = self._demand_source([_record()])
        rep = DiscoveryEngine(self.d, sources=[src]).run()
        self.assertEqual(rep.new, 1)
        store = load_opportunities(self.d)
        recs = store.all()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["discovery"]["source"], "hn-algolia")
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_DIGITAL_PRODUCT)
        self.assertEqual(rec["origin"], "real")   # official_api access -> real

    def test_demand_evidence_survives_persistence(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        ev = rec["discovery"].get("demand_evidence")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["intent_level"], demand_signal.INTENT_EXPLICIT)
        self.assertEqual(ev["budget"]["amount"], 20.0)
        self.assertEqual(ev["budget"]["currency"], "USD")

    def test_provenance_survives_persistence(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        prov = rec["discovery"].get("demand_provenance")
        self.assertIsNotNone(prov)
        self.assertEqual(prov["intent"], demand_signal.FACT)
        self.assertEqual(prov["opportunity_type"], demand_signal.ESTIMATED)
        self.assertEqual(prov["repeat_signal_count"], demand_signal.UNKNOWN)

    def test_buyer_confidence_survives_persistence(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        bc = rec["discovery"].get("buyer_confidence")
        self.assertIsNotNone(bc)
        self.assertIsInstance(bc["total"], float)
        self.assertIn("factors", bc)
        self.assertIn("reasons", bc)

    def test_problem_confidence_survives_persistence(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        pc = rec["discovery"].get("problem_confidence")
        self.assertIsNotNone(pc)
        self.assertIsInstance(pc["total"], float)
        self.assertIn("factors", pc)
        self.assertIn("reasons", pc)

    def test_ranking_fields_do_not_change_existing_discovery_fields(self):
        # same fixture/assertions as test_demand_evidence_survives_persistence
        # and test_acquisition_record_is_correctly_integrated_into_discovery -
        # re-asserted here to prove the ranking-layer addition changed
        # nothing about them.
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_DIGITAL_PRODUCT)
        self.assertEqual(rec["discovery"]["verification"]["status"], model.V_QUALIFIED)
        ev = rec["discovery"]["demand_evidence"]
        self.assertEqual(ev["intent_level"], demand_signal.INTENT_EXPLICIT)
        self.assertEqual(ev["budget"]["amount"], 20.0)

    def test_missing_ranking_data_on_an_old_style_draft_does_not_crash(self):
        # simulates a draft built before buyer_confidence/problem_confidence
        # existed - `.get()` must return None, never raise.
        from revenue_os.ecosystem import demand_sources, verification
        from revenue_os.ecosystem.discovery import _draft_to_opportunity

        draft = demand_sources.acq_record_to_draft(_record())
        del draft.raw["buyer_confidence"]
        del draft.raw["problem_confidence"]
        verdict = verification.verify(draft)
        opp = _draft_to_opportunity(draft, verdict)   # must not raise
        self.assertIsNone(opp.discovery["buyer_confidence"])
        self.assertIsNone(opp.discovery["problem_confidence"])
        # everything else about the (unaffected) demand_evidence still works
        self.assertIn("demand_evidence", opp.discovery)

    def test_non_demand_draft_gets_no_ranking_keys_at_all(self):
        # a draft from any OTHER source (no demand_evidence in raw at all)
        # must not gain buyer_confidence/problem_confidence keys either -
        # full backward compatibility for every non-demand source.
        from revenue_os.ecosystem import verification
        from revenue_os.ecosystem.discovery import _draft_to_opportunity
        from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta

        meta = SourceMeta(source="unit", source_type="test",
                          access_method=model.ACCESS_OFFICIAL_API,
                          automation_allowed=True, policy_status=model.POLICY_OK)
        draft = OpportunityDraft(
            title="A paid CSV dedupe script", description="budget ~50 eur",
            opportunity_type=model.TYPE_DIGITAL_PRODUCT,
            evidence=["Ask HN: I will pay for a CSV dedupe script"],
            source_meta=meta, est_pay_eur=29.0)
        verdict = verification.verify(draft)
        opp = _draft_to_opportunity(draft, verdict)
        self.assertNotIn("buyer_confidence", opp.discovery)
        self.assertNotIn("problem_confidence", opp.discovery)
        self.assertNotIn("demand_evidence", opp.discovery)

    def test_canonical_url_survives_persistence(self):
        src = self._demand_source(
            [_record(url="https://news.ycombinator.com/item?id=40000123&ref=x")])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        self.assertEqual(rec["discovery"]["source_url"],
                         "https://news.ycombinator.com/item?id=40000123")

    def test_demand_hint_and_est_pay_eur_are_persisted(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        self.assertGreater(rec["discovery"]["demand_hint"], 0.5)
        # budget was USD, not EUR - never converted (spec 7)
        self.assertEqual(rec["discovery"]["est_pay_eur"], 0.0)
        self.assertEqual(rec["discovery"]["payment_evidence"]["currency"], "USD")

    def test_dedupe_across_two_runs(self):
        rec1 = _record()
        src1 = self._demand_source([rec1])
        DiscoveryEngine(self.d, sources=[src1]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 1)

        # a re-scrape under a fresh URL/id but the same normalised title
        rec2 = _record(url="https://news.ycombinator.com/item?id=40099999")
        src2 = self._demand_source([rec2])
        rep2 = DiscoveryEngine(self.d, sources=[src2]).run()
        self.assertEqual(rep2.new, 0)
        self.assertEqual(rep2.refreshed, 1)
        self.assertEqual(len(load_opportunities(self.d).all()), 1)

    def test_distinct_problems_are_not_merged_across_runs(self):
        DiscoveryEngine(self.d, sources=[self._demand_source([_record()])]).run()
        other = _record(title="I would pay for a tool that tracks my inventory",
                        url="https://news.ycombinator.com/item?id=40077777")
        DiscoveryEngine(self.d, sources=[self._demand_source([other])]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 2)

    def test_multiple_demand_sources_all_work(self):
        recs = {
            "hn-algolia": _record(source="hn-algolia"),
            "stackexchange": _record(
                source="stackexchange",
                url="https://stackoverflow.com/questions/1/csv-dedupe",
                title="I would pay for a tool that dedupes CSV rows"),
            "lobsters": _record(
                source="lobsters", url="https://lobste.rs/s/abc/csv",
                title="I would pay for a service that dedupes CSV rows"),
            "lemmy": _record(
                source="lemmy", url="https://lemmy.world/post/1",
                title="I would pay for software that dedupes CSV rows"),
        }
        srcs = [demand_sources.DemandDiscoverySource(
                    name, _FixtureSource([rec]), queries=("q",))
                for name, rec in recs.items()]
        rep = DiscoveryEngine(self.d, sources=srcs).run()
        self.assertEqual(rep.new, 4)
        by_source = {r["discovery"]["source"] for r in load_opportunities(self.d).all()}
        self.assertEqual(by_source, set(recs))

    def test_invalid_or_missing_fields_fail_closed_not_crash(self):
        blank = AcqRecord(title="", url="", text="", author="", posted_at="",
                          platform="", source="hn-algolia", query="q")
        src = self._demand_source([blank])
        rep = DiscoveryEngine(self.d, sources=[src]).run()
        # a title-less record is rejected by the existing, UNCHANGED
        # verification gate ("no title") - not a crash, not silently
        # dropped either: it is persisted as REJECTED, auditable.
        self.assertEqual(rep.raw, 1)
        rec = load_opportunities(self.d).all()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["discovery"]["verification"]["status"],
                         model.V_REJECTED)

    def test_one_failing_query_does_not_kill_the_source(self):
        flaky = _FlakyFixtureSource([_record()])
        src = demand_sources.DemandDiscoverySource(
            "hn-algolia", flaky, queries=("is there a tool that", "second query"))
        rep = DiscoveryEngine(self.d, sources=[src]).run()
        self.assertEqual(rep.new, 1)
        self.assertTrue(any("simulated transient outage" in e for e in rep.errors))

    def test_no_automatic_acceptance(self):
        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        rec = load_opportunities(self.d).all()[0]
        self.assertFalse((rec.get("execution") or {}).get("accepted"))

    def test_no_automatic_task_or_build_chain(self):
        from revenue_os.execution import load_tasks

        src = self._demand_source([_record()])
        DiscoveryEngine(self.d, sources=[src]).run()
        self.assertEqual(load_tasks(self.d).all(), [])

    def test_deterministic_across_two_fresh_stores(self):
        def run_once():
            with tempfile.TemporaryDirectory() as td:
                src = self._demand_source([_record()], now_iso="2026-09-04T00:00:00+00:00")
                DiscoveryEngine(td, sources=[src]).run()
                rec = load_opportunities(td).all()[0]
                d = dict(rec["discovery"])
                d.pop("discovered_at", None)
                return d

        self.assertEqual(run_once(), run_once())


class ExistingLogicUnaffectedTests(unittest.TestCase):
    def test_task_classification_is_unaffected(self):
        from revenue_os.ecosystem import task_signal
        from revenue_os.ecosystem.model import OpportunityDraft

        draft = OpportunityDraft(
            title="A paid CSV dedupe script",
            evidence=["Ask HN: I will pay for a CSV dedupe script"])
        self.assertEqual(task_signal.classify_task_kind(draft), model.TASK_INSTANT_PAID)

    def test_verification_gates_are_unaffected_for_non_demand_drafts(self):
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
        self.assertEqual(verification.verify(draft).status, model.V_QUALIFIED)

    def test_money_identity_legal_safety_gates_are_unaffected(self):
        from revenue_os import action_class as ac

        # a sanity check that discovering demand signals never touched
        # the shared firewall - representative kinds still classify
        # exactly as before.
        self.assertIs(ac.classify("spend_money").action_class,
                      ac.ActionClass.MONEY_APPROVAL_REQUIRED)
        self.assertIs(ac.classify("kyc").action_class,
                      ac.ActionClass.IDENTITY_APPROVAL_REQUIRED)
        self.assertIs(ac.classify("solve_captcha").action_class,
                      ac.ActionClass.SAFETY_BLOCKED)


class BuildSourceFactoryTests(unittest.TestCase):
    """Checks the sources.build_source('demand-*') wiring WITHOUT ever
    calling .discover() - so this never touches the network."""

    def test_all_four_demand_names_resolve_to_a_demand_discovery_source(self):
        for name, expected_label in (
                ("demand-hn", "hn-algolia"),
                ("demand-stackexchange", "stackexchange"),
                ("demand-lobsters", "lobsters"),
                ("demand-lemmy", "lemmy")):
            src = sources.build_source(name)
            self.assertIsInstance(src, demand_sources.DemandDiscoverySource)
            self.assertEqual(src.meta.source, expected_label)
            self.assertEqual(src.meta.source_type, "demand_signal")

    def test_unknown_demand_name_raises(self):
        with self.assertRaises(ValueError):
            sources.build_source("demand-nonsense")


class HistoricalDataPreservationTests(unittest.TestCase):
    """Spec: Demand Validation phase - "historische Daten dürfen nicht
    gelöscht werden". A freshness-filtered run only affects what gets
    FETCHED this run; DiscoveryEngine never deletes a record another run
    (or another source) already persisted."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_a_freshness_filtered_run_returning_nothing_keeps_old_records(self):
        # run 1: no filter, persists one old signal
        old_rec = _record(posted_at="2020-01-01T00:00:00+00:00")
        DiscoveryEngine(self.d, sources=[demand_sources.DemandDiscoverySource(
            "hn-algolia", _FixtureSource([old_rec]),
            queries=("is there a tool that",))]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 1)

        # run 2: a freshness-filtered source that (correctly, given the
        # filter) finds nothing new
        class _EmptyWhenFiltered:
            def search(self, query, limit, *, since_ts=None):
                return [] if since_ts else [old_rec]

        DiscoveryEngine(self.d, sources=[demand_sources.DemandDiscoverySource(
            "hn-algolia", _EmptyWhenFiltered(), queries=("is there a tool that",),
            since_ts="2026-08-28T00:00:00+00:00")]).run()

        # the record from run 1 is still there, untouched
        recs = load_opportunities(self.d).all()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], old_rec.title[:200])


class MaxAgeDaysCliTests(unittest.TestCase):
    """Spec: Demand Validation phase - `--max-age-days` is explicitly
    steerable per run and defaults to the unchanged (no filter)
    behaviour. Verified at the CLI argument/threading layer with a
    patched `build_source` - no network, no real discovery run."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _run(self, argv):
        import io
        from contextlib import redirect_stdout

        from revenue_os import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_max_age_days_computes_and_threads_since_ts(self):
        from unittest.mock import patch

        captured = {}

        def _fake_build_source(name, **kw):
            captured["kw"] = kw
            return sources.SyntheticSource(seed=0)

        with patch("revenue_os.ecosystem.sources.build_source", _fake_build_source):
            code, _ = self._run([
                "discover", "--data-dir", str(self.d), "--source", "synthetic",
                "--limit", "1", "--max-age-days", "7"])
        self.assertEqual(code, 0)
        self.assertIn("since_ts", captured["kw"])
        since_ts = captured["kw"]["since_ts"]
        # roughly 7 days ago, ISO-8601, parseable
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(since_ts)
        age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
        self.assertAlmostEqual(age_days, 7.0, delta=0.1)

    def test_omitting_max_age_days_leaves_retrieval_unchanged(self):
        from unittest.mock import patch

        captured = {}

        def _fake_build_source(name, **kw):
            captured["kw"] = kw
            return sources.SyntheticSource(seed=0)

        with patch("revenue_os.ecosystem.sources.build_source", _fake_build_source):
            code, _ = self._run([
                "discover", "--data-dir", str(self.d), "--source", "synthetic",
                "--limit", "1"])
        self.assertEqual(code, 0)
        # exactly the old call shape - no since_ts key introduced at all
        self.assertNotIn("since_ts", captured["kw"])

    def test_non_positive_max_age_days_is_rejected(self):
        code, out = self._run([
            "discover", "--data-dir", str(self.d), "--source", "synthetic",
            "--max-age-days", "0"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
