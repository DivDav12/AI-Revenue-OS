import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revenue_os.llm_spend import LlmSpendLog
from revenue_os.llm_workers import (
    budget_gate,
    build_competitor_analyzer,
    build_evaluator,
    build_planner,
    build_proposer,
    build_researcher,
)
from revenue_os.opportunity import CRITERIA
from revenue_os.sources import RawSignal, StaticSource
from revenue_os.store import Candidate, CandidateStore

_PAYLOADS = {
    "record_scores": {**{c: 3.0 for c in CRITERIA}, "rationale": "ok"},
    "record_plan": {
        "hypothesis": "pay", "cheapest_test": "call 10", "success_metric": "3 yes",
        "effort": "low", "estimated_cost_usd": 0.0, "needs_human_budget": False,
    },
    "record_offer": {
        "what_is_sold": "thing", "price": 49.0, "currency": "USD",
        "delivery": "digital", "call_to_action": "buy", "positioning": "for X",
    },
}


class _Usage:
    input_tokens = 300
    output_tokens = 80
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"

    def __init__(self, name):
        self.name = name
        self.input = _PAYLOADS[name]


class _Resp:
    def __init__(self, name):
        self.content = [_Block(name)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(kwargs["tool_choice"]["name"])


class DeterministicModesTests(unittest.TestCase):
    def test_keyword_and_template_touch_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            n, name, est, cache = build_evaluator(
                mode="keyword", source=StaticSource([]), limit=10,
                model="claude-sonnet-5", max_cost_usd=0.5, refresh=False, data_dir=d,
            )
            self.assertEqual(name, "keyword")
            self.assertIsNone(cache)
            store = CandidateStore(Path(d) / "c.json")
            p, pc = build_planner(mode="template", store=store, model="m",
                                  max_cost_usd=0.5, refresh=False, data_dir=d)
            self.assertIsNone(pc)
            o, oc = build_proposer(mode="template", store=store, model="m",
                                   max_cost_usd=0.5, refresh=False, data_dir=d)
            self.assertIsNone(oc)


class LlmModeTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_build_evaluator_llm(self):
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            n, name, est, cache = build_evaluator(
                mode="llm", source=StaticSource([RawSignal(title="a paid saas tool")]),
                limit=10, model="claude-sonnet-5", max_cost_usd=0.5, refresh=False,
                data_dir=self.d,
            )
        self.assertEqual(name, "llm")
        self.assertIsNotNone(cache)
        self.assertGreater(est, 0.0)

    def test_per_action_ceiling_refuses(self):
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            with self.assertRaises(ValueError):
                build_evaluator(
                    mode="llm", source=StaticSource([RawSignal(title="x" * 200)]),
                    limit=10, model="claude-sonnet-5", max_cost_usd=1e-9,
                    refresh=False, data_dir=self.d,
                )

    def test_cumulative_cap_refuses(self):
        # a booked sale disables the EUR 3 pre-sale cap; test the llm_budget one
        from revenue_os.revenue import RevenueLedger
        led = RevenueLedger(Path(self.d) / "revenue.json")
        led.add({"candidate_name": "x", "amount": 29.9, "currency": "EUR",
                 "received_at": "2026-01-01T00:00:00+00:00", "actor": "t",
                 "ref": "paypal:seed"})
        led.save()
        log = LlmSpendLog(Path(self.d) / "llm_spend.json")
        log.add({"activity": "evaluate", "cost_usd": 5.0, "api_calls": 1})
        log.save()
        with self.assertRaisesRegex(ValueError, "cumulative cap"):
            budget_gate(self.d, 0.01, 0.5)

    def test_research_web_mode_sets_worker_mode_and_costs_more(self):
        store = CandidateStore(Path(self.d) / "c.json")
        store.put(Candidate(name="a", description="x" * 200, status="shortlisted"))
        with mock.patch("revenue_os.llm_normalize.build_client",
                        return_value=_FakeClient()):
            w_llm, _ = build_researcher(mode="llm", store=store,
                                       model="claude-sonnet-5", max_cost_usd=0.5,
                                       refresh=False, data_dir=self.d)
            w_web, _ = build_researcher(mode="web", store=store,
                                       model="claude-sonnet-5", max_cost_usd=0.5,
                                       refresh=False, data_dir=self.d)
        self.assertEqual(w_llm.mode, "llm")
        self.assertEqual(w_web.mode, "web")
        # a web run whose estimate blows the per-action ceiling is refused
        with mock.patch("revenue_os.llm_normalize.build_client",
                        return_value=_FakeClient()):
            with self.assertRaises(ValueError):
                build_competitor_analyzer(mode="web", store=store,
                                          model="claude-sonnet-5", max_cost_usd=1e-9,
                                          refresh=False, data_dir=self.d)

    def test_build_planner_llm(self):
        store = CandidateStore(Path(self.d) / "c.json")
        store.put(Candidate(name="a", description="a", status="approved"))
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            planner, cache = build_planner(
                mode="llm", store=store, model="claude-sonnet-5",
                max_cost_usd=0.5, refresh=False, data_dir=self.d,
            )
        self.assertIsNotNone(cache)
        plan = planner(store.get("a"))
        self.assertEqual(plan.candidate_name, "a")


if __name__ == "__main__":
    unittest.main()
