import tempfile
import unittest
from pathlib import Path

from revenue_os.copywriter import (
    CopywriterAgent,
    CopywriterWorker,
    write_copy_llm,
)
from revenue_os.llm_cache import LlmCache
from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import write_copy_for_validated

_OFFER = {
    "what_is_sold": "a done-for-you onboarding audit",
    "price": 250.0, "currency": "USD", "delivery": "manual",
    "call_to_action": "Book a paid pilot this week.", "positioning": "for seed SaaS",
}

_DRAFT = {
    "headline": "Fix onboarding drop-off in two weeks",
    "subheadline": "For seed SaaS teams losing trials before activation",
    "body": "Most trials never reach the aha moment.\n\nYou get a full audit and a "
            "prioritised fix list.\n\nRun by one operator who has done this before.",
    "primary_cta": "Book a paid pilot this week.",
    "faq": [
        {"question": "How much?", "answer": "$250 for the pilot audit."},
        {"question": "How fast?", "answer": "Delivered within two weeks."},
        {"question": "What if it doesn't help?", "answer": "Full refund, no questions."},
    ],
}


class _Usage:
    input_tokens = 600
    output_tokens = 700
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"
    name = "record_launch_copy"

    def __init__(self, payload):
        self.input = payload


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(payload)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = _DRAFT if payload is None else payload
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.payload)


def _cand(name="alpha", status="validated", **kw):
    return Candidate(name=name, description=f"{name} opportunity", status=status,
                     offer=dict(_OFFER), **kw)


class WriteCopyLlmTests(unittest.TestCase):
    def test_draft_fields_and_basis(self):
        d = write_copy_llm(_cand(), _OFFER, client=_FakeClient())
        self.assertEqual(d["basis"], "model draft, not published")
        self.assertEqual(len(d["faq"]), 3)
        self.assertIn("drafted_at", d)

    def test_wrong_faq_count_raises(self):
        bad = {**_DRAFT, "faq": _DRAFT["faq"][:2]}
        with self.assertRaises(ValueError):
            write_copy_llm(_cand(), _OFFER, client=_FakeClient(bad))

    def test_empty_headline_raises(self):
        with self.assertRaises(ValueError):
            write_copy_llm(_cand(), _OFFER, client=_FakeClient({**_DRAFT, "headline": " "}))


class CopywriterWorkerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "llm_copy_cache.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_second_call_cached(self):
        client = _FakeClient()
        w = CopywriterWorker(client=client, model="claude-sonnet-5", cache=self.cache)
        w(_cand(), _OFFER)
        w(_cand(), _OFFER)
        self.assertEqual(client.calls, 1)
        self.assertEqual(w.cache_hits, 1)

    def test_ceiling_stops(self):
        from revenue_os.llm_normalize import CostCeilingExceeded
        w = CopywriterWorker(client=_FakeClient(), model="claude-sonnet-5",
                             max_cost_usd=0.0)
        with self.assertRaises(CostCeilingExceeded):
            w(_cand(), _OFFER)
        self.assertTrue(w.ceiling_hit)


class CopywriterAgentTests(unittest.TestCase):
    def _orch(self):
        reg = AgentRegistry()
        reg.register(CopywriterAgent(name="copywriter"))
        return Orchestrator(registry=reg)

    def test_agent_returns_draft(self):
        orch = self._orch()
        orch.add_task(Task(
            capability="write_copy", objective="c",
            payload={"candidate": _cand(), "offer": dict(_OFFER),
                     "worker": CopywriterWorker(client=_FakeClient())},
        ))
        r = orch.dispatch_next()
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["launch_draft"]["headline"], _DRAFT["headline"])

    def test_missing_offer_errors(self):
        orch = self._orch()
        orch.add_task(Task(capability="write_copy", objective="c",
                           payload={"candidate": _cand(),
                                    "worker": CopywriterWorker(client=_FakeClient())}))
        self.assertEqual(orch.dispatch_next().status, "error")


class WriteCopyForValidatedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_drafts_only_validated_with_offer_and_is_idempotent(self):
        self.store.put(_cand("has_offer"))
        self.store.put(Candidate(name="no_offer", status="validated"))
        self.store.put(_cand("shortlisted_one", status="shortlisted"))
        worker = CopywriterWorker(client=_FakeClient())
        seen = []
        out = write_copy_for_validated(
            self.store, worker, sink=lambda t, r: seen.append(r.agent)
        )
        self.assertEqual([c.name for c in out], ["has_offer"])
        self.assertEqual(seen, ["copywriter"])
        self.assertEqual(self.store.get("has_offer").launch_draft["headline"],
                         _DRAFT["headline"])
        self.assertFalse(self.store.get("no_offer").launch_draft)
        # re-run does nothing
        self.assertEqual(write_copy_for_validated(self.store, worker), [])

    def test_one_failure_does_not_strand_the_rest(self):
        self.store.put(_cand("good"))
        self.store.put(_cand("bad"))

        def _worker(cand, offer):
            if cand.name == "bad":
                raise RuntimeError("boom")
            return dict(_DRAFT)

        out = write_copy_for_validated(self.store, _worker)
        self.assertEqual([c.name for c in out], ["good"])

    def test_draft_survives_a_rescore(self):
        self.store.put(_cand("a"))
        write_copy_for_validated(self.store, CopywriterWorker(client=_FakeClient()))
        self.store.upsert(Candidate(name="a", description="a opportunity", total=9.9))
        self.assertEqual(self.store.get("a").launch_draft["headline"], _DRAFT["headline"])


if __name__ == "__main__":
    unittest.main()
