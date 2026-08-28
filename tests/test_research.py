import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revenue_os.llm_cache import LlmCache
from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry
from revenue_os.research import (
    ResearchAgent,
    ResearchWorker,
    research_candidate_llm,
)
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import research_shortlisted

_NOTE = {
    "competition": "several open-source tools already do this",
    "demand_evidence": "steady HN interest but few buyers named",
    "legal_flags": "none apparent",
    "verdict": "caution",
    "rationale": "crowded space, unproven willingness to pay",
}


class _Usage:
    input_tokens = 500
    output_tokens = 160
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"
    name = "record_research"

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


class ResearchCandidateLlmTests(unittest.TestCase):
    def test_note_fields_and_basis(self):
        note = research_candidate_llm(_cand(), client=_FakeClient())
        self.assertEqual(note["verdict"], "caution")
        self.assertEqual(note["basis"], "model knowledge, no web")
        self.assertIn("researched_at", note)
        self.assertIn("model", note)

    def test_bad_verdict_raises(self):
        with self.assertRaises(ValueError):
            research_candidate_llm(
                _cand(), client=_FakeClient({**_NOTE, "verdict": "maybe"})
            )

    def test_empty_field_raises(self):
        with self.assertRaises(ValueError):
            research_candidate_llm(
                _cand(), client=_FakeClient({**_NOTE, "competition": "  "})
            )


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "llm_research_cache.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_second_call_cached(self):
        client = _FakeClient()
        w = ResearchWorker(client=client, model="claude-sonnet-5", cache=self.cache)
        a = w(_cand())
        b = w(_cand())
        self.assertEqual(client.calls, 1)
        self.assertEqual(w.cache_hits, 1)
        self.assertEqual(a["verdict"], b["verdict"])

    def test_ceiling_stops(self):
        from revenue_os.llm_normalize import CostCeilingExceeded

        w = ResearchWorker(client=_FakeClient(), model="claude-sonnet-5", max_cost_usd=0.0)
        with self.assertRaises(CostCeilingExceeded):
            w(_cand())
        self.assertTrue(w.ceiling_hit)


class ResearchAgentTests(unittest.TestCase):
    def _orch(self):
        reg = AgentRegistry()
        reg.register(ResearchAgent(name="researcher"))
        return Orchestrator(registry=reg)

    def test_agent_returns_note(self):
        orch = self._orch()
        orch.add_task(Task(
            capability="research", objective="r",
            payload={"candidate": _cand(), "worker": ResearchWorker(client=_FakeClient())},
        ))
        result = orch.dispatch_next()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["research"]["verdict"], "caution")

    def test_bad_payload_errors(self):
        orch = self._orch()
        orch.add_task(Task(capability="research", objective="r", payload={}))
        self.assertEqual(orch.dispatch_next().status, "error")

    def test_failing_worker_errors_but_cycle_survives(self):
        def boom(_c):
            raise RuntimeError("nope")

        orch = self._orch()
        orch.add_task(Task(capability="research", objective="a",
                           payload={"candidate": _cand("a"), "worker": boom}))
        orch.add_task(Task(capability="research", objective="b",
                           payload={"candidate": _cand("b"),
                                    "worker": ResearchWorker(client=_FakeClient())}))
        results = orch.run_cycle()
        self.assertEqual({r.status for r in results}, {"error", "ok"})


class ResearchShortlistedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_attaches_notes_to_shortlisted_only(self):
        self.store.put(_cand("a", "shortlisted"))
        self.store.put(_cand("b", "shortlisted"))
        self.store.put(_cand("c", "discovered"))
        worker = ResearchWorker(client=_FakeClient(), model="claude-sonnet-5")
        noted = research_shortlisted(self.store, worker)
        self.assertEqual({c.name for c in noted}, {"a", "b"})
        self.assertEqual(self.store.get("c").research, {})
        self.assertEqual(self.store.get("a").research["verdict"], "caution")

    def test_idempotent(self):
        self.store.put(_cand("a", "shortlisted"))
        client = _FakeClient()
        worker = ResearchWorker(client=client, model="claude-sonnet-5")
        research_shortlisted(self.store, worker)
        research_shortlisted(self.store, worker)  # 'a' already noted -> no-op
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
