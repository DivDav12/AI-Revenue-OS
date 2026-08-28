import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.agent import DiscoveryAgent, WorkerAgent
from revenue_os.messages import Task
from revenue_os.normalize import NEUTRAL, to_opportunity
from revenue_os.opportunity import CRITERIA, Opportunity, score_opportunity
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry
from revenue_os.sources import (
    HackerNewsSource,
    LocalFileSource,
    RawSignal,
    StaticSource,
    _map_hn_item,
)
from revenue_os.workflow import discover_evaluate_select


class _FailingSource:
    def fetch(self, limit):
        raise RuntimeError("boom")


class LocalFileSourceTests(unittest.TestCase):
    def test_reads_signals_from_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "signals.json"
            path.write_text(
                json.dumps([{"title": "A", "url": "http://x"}, {"title": "B"}]),
                encoding="utf-8",
            )
            signals = LocalFileSource(path).fetch(10)
        self.assertEqual([s.title for s in signals], ["A", "B"])
        self.assertEqual(signals[0].url, "http://x")
        self.assertEqual(signals[0].source, "local-file")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            LocalFileSource("does-not-exist.json").fetch(10)

    def test_malformed_file_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                LocalFileSource(path).fetch(10)


class HackerNewsMappingTests(unittest.TestCase):
    def test_map_hn_item_is_pure(self):
        item = {"id": 42, "title": " Show HN: thing ", "url": "http://h", "text": "t"}
        signal = _map_hn_item(item)
        self.assertEqual(signal.title, "Show HN: thing")
        self.assertEqual(signal.url, "http://h")
        self.assertEqual(signal.external_id, "42")
        self.assertEqual(signal.source, "hacker-news")

    def test_map_hn_item_normalizes_unicode_punctuation(self):
        item = {"id": 1, "title": "Boop – tiny “push”…", "url": "", "text": ""}
        signal = _map_hn_item(item)
        self.assertEqual(signal.title, 'Boop - tiny "push"...')

    def test_fetch_zero_limit_makes_no_request(self):
        # capped to 0 -> returns immediately without network
        self.assertEqual(HackerNewsSource().fetch(0), [])


class NormalizeTests(unittest.TestCase):
    def test_neutral_baseline_and_provenance(self):
        signal = RawSignal(
            title="A quiet plain report", url="http://u", source="src", external_id="9"
        )
        opp = to_opportunity(signal)
        self.assertEqual(opp.estimates(), {c: NEUTRAL for c in CRITERIA})
        self.assertEqual(opp.source, "src")
        self.assertEqual(opp.raw_ref, "http://u")
        self.assertEqual(opp.description, "A quiet plain report")

    def test_keywords_nudge_and_clamp(self):
        signal = RawSignal(title="automation automate no-code api marketplace")
        opp = to_opportunity(signal)
        self.assertEqual(opp.automation_potential, 5.0)  # 2.5 + 4 nudges, clamped
        self.assertEqual(opp.scalability, NEUTRAL + 1.0)

    def test_normalization_is_deterministic(self):
        signal = RawSignal(title="Launch: a SaaS pricing tool")
        self.assertEqual(to_opportunity(signal), to_opportunity(signal))


class DiscoveryAgentTests(unittest.TestCase):
    def test_agent_discovers_and_normalizes(self):
        registry = AgentRegistry()
        source = StaticSource([RawSignal(title="one"), RawSignal(title="two")])
        registry.register(DiscoveryAgent(source, name="discovery"))
        orch = Orchestrator(registry=registry)
        orch.add_task(Task(objective="discover", capability="discover"))

        result = orch.dispatch_next()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["count"], 2)
        self.assertTrue(all(isinstance(o, Opportunity) for o in result.output["opportunities"]))

    def test_source_failure_produces_error_and_cycle_survives(self):
        registry = AgentRegistry()
        registry.register(DiscoveryAgent(_FailingSource(), name="discovery"))
        registry.register(WorkerAgent(name="echo-worker"))
        orch = Orchestrator(registry=registry)
        orch.add_task(Task(objective="discover", capability="discover"))
        orch.add_task(Task(objective="echo", capability="echo"))

        results = orch.run_cycle()

        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[1].status, "ok")


class DiscoverEvaluateSelectTests(unittest.TestCase):
    def test_end_to_end_returns_ranked_top_n(self):
        signals = [
            RawSignal(title="plain note"),
            RawSignal(title="automation automate no-code api marketplace saas revenue"),
            RawSignal(title="a SaaS platform"),
        ]
        ranked = discover_evaluate_select(StaticSource(signals), limit=10, top_n=2)
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(ranked[0].total, ranked[1].total)


class RegressionTests(unittest.TestCase):
    def test_opportunity_new_fields_default_empty_and_scoring_unchanged(self):
        opp = Opportunity(
            name="x",
            startup_affordability=3,
            automation_potential=3,
            demand=3,
            competition_headroom=3,
            legal_feasibility=3,
            speed_to_first_revenue=3,
            profit_potential=3,
            scalability=3,
        )
        self.assertEqual(opp.source, "")
        self.assertEqual(opp.raw_ref, "")
        score = score_opportunity(opp)
        self.assertEqual(score.total, 3.0)
        self.assertEqual(score.verdict, "hold")


if __name__ == "__main__":
    unittest.main()
