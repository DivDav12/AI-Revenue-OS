import tempfile
import unittest
from pathlib import Path

from revenue_os.competition import (
    CompetitionWorker,
    CompetitorAnalyzerAgent,
    analyze_competition_llm,
)
from revenue_os.llm_cache import LlmCache
from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import analyze_competition_shortlisted

_NOTE = {
    "named_competitors": "Zapier, Make, n8n",
    "pricing_landscape": "$0-50/mo tiered by task volume",
    "differentiation_angle": "vertical templates for a niche",
    "saturation": "several well-funded incumbents",
    "verdict": "crowded",
    "rationale": "mature category with strong incumbents",
}


class _Usage:
    input_tokens = 500
    output_tokens = 180
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"
    name = "record_competition_analysis"

    def __init__(self, payload):
        self.input = payload


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(payload)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = _NOTE if payload is None else payload
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.payload)


def _cand(name="alpha", status="shortlisted", **kw):
    return Candidate(name=name, description=f"{name} opportunity", status=status, **kw)


class AnalyzeCompetitionLlmTests(unittest.TestCase):
    def test_note_fields_and_basis(self):
        note = analyze_competition_llm(_cand(), client=_FakeClient())
        self.assertEqual(note["verdict"], "crowded")
        self.assertEqual(note["basis"], "model knowledge, no web")
        self.assertIn("analyzed_at", note)

    def test_bad_verdict_raises(self):
        with self.assertRaises(ValueError):
            analyze_competition_llm(
                _cand(), client=_FakeClient({**_NOTE, "verdict": "meh"})
            )

    def test_empty_field_raises(self):
        with self.assertRaises(ValueError):
            analyze_competition_llm(
                _cand(), client=_FakeClient({**_NOTE, "pricing_landscape": " "})
            )


class CompetitionWorkerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "llm_competition_cache.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_second_call_cached(self):
        client = _FakeClient()
        w = CompetitionWorker(client=client, model="claude-sonnet-5", cache=self.cache)
        w(_cand())
        w(_cand())
        self.assertEqual(client.calls, 1)
        self.assertEqual(w.cache_hits, 1)

    def test_ceiling_stops(self):
        from revenue_os.llm_normalize import CostCeilingExceeded

        w = CompetitionWorker(client=_FakeClient(), model="claude-sonnet-5",
                              max_cost_usd=0.0)
        with self.assertRaises(CostCeilingExceeded):
            w(_cand())
        self.assertTrue(w.ceiling_hit)


class CompetitorAnalyzerAgentTests(unittest.TestCase):
    def _orch(self):
        reg = AgentRegistry()
        reg.register(CompetitorAnalyzerAgent(name="competitor_analyzer"))
        return Orchestrator(registry=reg)

    def test_agent_returns_note(self):
        orch = self._orch()
        orch.add_task(Task(
            capability="analyze_competition", objective="c",
            payload={"candidate": _cand(),
                     "worker": CompetitionWorker(client=_FakeClient())},
        ))
        r = orch.dispatch_next()
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["competition"]["verdict"], "crowded")

    def test_bad_payload_errors(self):
        orch = self._orch()
        orch.add_task(Task(capability="analyze_competition", objective="c", payload={}))
        self.assertEqual(orch.dispatch_next().status, "error")


class AnalyzeCompetitionShortlistedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_attaches_notes_and_is_idempotent(self):
        self.store.put(_cand("a"))
        self.store.put(_cand("b"))
        worker = CompetitionWorker(client=_FakeClient())
        seen = []
        out = analyze_competition_shortlisted(
            self.store, worker, sink=lambda t, r: seen.append(r.agent)
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(seen, ["competitor_analyzer", "competitor_analyzer"])
        self.assertEqual(self.store.get("a").competition["verdict"], "crowded")
        # re-run does nothing
        self.assertEqual(analyze_competition_shortlisted(self.store, worker), [])

    def test_one_failure_does_not_strand_the_rest(self):
        self.store.put(_cand("good"))
        self.store.put(_cand("bad"))

        def _worker(cand):
            if cand.name == "bad":
                raise RuntimeError("boom")
            return dict(_NOTE)

        out = analyze_competition_shortlisted(self.store, _worker)
        self.assertEqual([c.name for c in out], ["good"])
        self.assertFalse(self.store.get("bad").competition)

    def test_survives_a_rescore(self):
        from revenue_os.store import Candidate as C
        self.store.put(_cand("a"))
        analyze_competition_shortlisted(self.store, CompetitionWorker(client=_FakeClient()))
        self.store.upsert(C(name="a", description="a opportunity", total=9.9))
        self.assertEqual(self.store.get("a").competition["verdict"], "crowded")


if __name__ == "__main__":
    unittest.main()
