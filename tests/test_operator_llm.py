import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from revenue_os import cli
from revenue_os.agent_log import AgentLog
from revenue_os.approval import record_decision
from revenue_os.llm_spend import LlmSpendLog
from revenue_os.operator import Goal, OperatorAgent, load_goal
from revenue_os.opportunity import CRITERIA
from revenue_os.store import CandidateStore
from revenue_os.validation import record_validation_outcome

_PAYLOADS = {
    "record_scores": {**{c: 3.0 for c in CRITERIA}, "demand": 4.0, "rationale": "ok"},
    "record_plan": {
        "hypothesis": "pay", "cheapest_test": "call 10", "success_metric": "3 yes",
        "effort": "low", "estimated_cost_usd": 0.0, "needs_human_budget": False,
    },
    "record_offer": {
        "what_is_sold": "thing", "price": 49.0, "currency": "USD",
        "delivery": "digital", "call_to_action": "buy", "positioning": "for X",
    },
    "choose_action": {"action": "stop", "rationale": "funnel is full enough"},
    "record_research": {
        "competition": "crowded", "demand_evidence": "thin", "legal_flags": "none",
        "verdict": "caution", "rationale": "unproven",
    },
}


class _Usage:
    def __init__(self, i=300):
        self.input_tokens = i
        self.output_tokens = 80
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"

    def __init__(self, name):
        self.name = name
        self.input = _PAYLOADS[name]


class _Resp:
    def __init__(self, name, i):
        self.content = [_Block(name)]
        self.usage = _Usage(i)


class _FakeClient:
    def __init__(self, input_tokens=300):
        self.calls = 0
        self.input_tokens = input_tokens
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(kwargs["tool_choice"]["name"], self.input_tokens)


def _now():
    t = [datetime(2026, 9, 1, tzinfo=timezone.utc)]

    def f():
        t[0] = t[0].replace(second=(t[0].second + 1) % 60)
        return t[0]
    return f


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class GoalLlmFieldsTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        self.assertEqual(Goal().evaluator, "keyword")
        self.assertFalse(Goal().uses_llm)
        g = Goal(evaluator="llm", planner="llm", model="claude-opus-5")
        self.assertTrue(g.uses_llm)
        self.assertEqual(Goal.from_dict(g.to_dict()), g)


class OperatorLlmTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _spend(self):
        return LlmSpendLog.load(self.d / "llm_spend.json").entries()

    def test_deterministic_default_unchanged(self):
        OperatorAgent(self.d, Goal()).run()
        self.assertFalse((self.d / "llm_cache.json").exists())
        self.assertFalse((self.d / "llm_spend.json").exists())
        store = CandidateStore.load(self.d / "candidates.json")
        self.assertTrue(all(c.estimate_source == "keyword" for c in store.all()))

    def test_llm_evaluator_scores_and_records_spend(self):
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            OperatorAgent(self.d, Goal(evaluator="llm")).run()
        store = CandidateStore.load(self.d / "candidates.json")
        self.assertTrue(store.all())
        self.assertTrue(all(c.estimate_source == "llm" for c in store.all()))
        acts = [e["activity"] for e in self._spend()]
        self.assertIn("evaluate", acts)
        entry = next(
            e for e in AgentLog.load(self.d / "agent_log.json").entries()
            if e["action"] == "discover"
        )
        self.assertIn("llm_cost", entry["detail"])

    def test_llm_planner_and_proposer(self):
        goal = Goal(evaluator="llm", planner="llm", proposer="llm")
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            OperatorAgent(self.d, goal).run()
            store = CandidateStore.load(self.d / "candidates.json")
            name = next(c.name for c in store.all() if c.status == "shortlisted")
            record_decision(store, name, "approve", approver="h")
            OperatorAgent(self.d, goal).run()
            store = CandidateStore.load(self.d / "candidates.json")
            record_validation_outcome(store, name, "validated",
                                      metric_value="ok", actor="h")
            OperatorAgent(self.d, goal).run()
        c = CandidateStore.load(self.d / "candidates.json").get(name)
        self.assertEqual(c.status, "validated")
        self.assertEqual(c.offer["price"], 49.0)
        acts = {e["activity"] for e in self._spend()}
        self.assertEqual(acts, {"evaluate", "plan", "offer"})

    def test_cap_exhaustion_stops_the_agent(self):
        log = LlmSpendLog(self.d / "llm_spend.json")
        log.add({"activity": "evaluate", "cost_usd": 5.0, "api_calls": 1})
        log.save()
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            steps = OperatorAgent(self.d, Goal(evaluator="llm")).run()
        self.assertEqual(steps[-1].decision.action, "stop")
        self.assertIn("llm budget", steps[-1].decision.reason.lower())
        # nothing scored, no candidates persisted
        self.assertFalse((self.d / "candidates.json").exists())

    def test_gate_safety_with_llm(self):
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            OperatorAgent(self.d, Goal(evaluator="llm", planner="llm")).run()
        statuses = {c.status for c in CandidateStore.load(self.d / "candidates.json").all()}
        self.assertTrue(statuses <= {"discovered", "shortlisted"})

    def test_decision_policy_records_decide_spend(self):
        goal = Goal(decision_policy="llm", shortlist_n=3)
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            steps = OperatorAgent(self.d, goal).run()
        # discover (cold start, no policy call) then a policy-driven stop
        acts = [e["activity"] for e in self._spend()]
        self.assertIn("decide", acts)
        stop = steps[-1]
        self.assertEqual(stop.decision.action, "stop")
        self.assertIn("llm policy", stop.decision.reason)
        self.assertIn("decide_cost", stop.entry["detail"])

    def test_decision_policy_default_rules_no_decide_spend(self):
        OperatorAgent(self.d, Goal()).run()
        self.assertFalse((self.d / "llm_spend.json").exists())

    def test_research_agent_notes_shortlisted_and_records_spend(self):
        goal = Goal(research="llm")
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            steps = OperatorAgent(self.d, goal).run()
        store = CandidateStore.load(self.d / "candidates.json")
        shortlisted = [c for c in store.all() if c.status == "shortlisted"]
        self.assertTrue(shortlisted)
        self.assertTrue(all(c.research.get("verdict") == "caution" for c in shortlisted))
        self.assertIn("research", {e["activity"] for e in self._spend()})
        self.assertTrue(any(s.decision.action == "research" for s in steps))

    def test_research_default_off(self):
        OperatorAgent(self.d, Goal()).run()
        store = CandidateStore.load(self.d / "candidates.json")
        self.assertTrue(all(c.research == {} for c in store.all()))
        self.assertFalse((self.d / "llm_spend.json").exists())

    def test_research_cap_exhaustion_stops(self):
        log = LlmSpendLog(self.d / "llm_spend.json")
        log.add({"activity": "research", "cost_usd": 5.0, "api_calls": 1})
        log.save()
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            steps = OperatorAgent(self.d, Goal(research="llm")).run()
        self.assertEqual(steps[-1].decision.action, "stop")
        self.assertIn("llm budget", steps[-1].decision.reason.lower())

    def test_decision_policy_cap_exhaustion_falls_back_to_rules(self):
        log = LlmSpendLog(self.d / "llm_spend.json")
        log.add({"activity": "decide", "cost_usd": 5.0, "api_calls": 1})
        log.save()
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=_FakeClient()):
            steps = OperatorAgent(self.d, Goal(decision_policy="llm")).run()
        # deterministic rules still run: cold-start discover, then stop
        self.assertEqual(steps[0].decision.action, "discover")
        self.assertNotIn("llm policy", steps[-1].decision.reason)
        acts = [e["activity"] for e in self._spend()]
        self.assertNotIn("decide", acts[1:])  # no new decide entry recorded


class AgentLoopMaxSpendTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_loop_stops_on_max_spend(self):
        # high token count so one eval run costs well over the session limit
        client = _FakeClient(input_tokens=200_000)  # ~0.4 USD per call on sonnet
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=client):
            session = OperatorAgent(self.d, Goal(evaluator="llm")).run_continuous(
                1, max_spend_usd=0.10, max_ticks=5,
                sleep_fn=lambda s: None, clock_fn=lambda: 0.0, now_fn=_now(),
            )
        self.assertEqual(session.end_reason, "max-spend")


class AgentGoalCliTests(unittest.TestCase):
    def test_agent_goal_sets_llm_fields(self):
        with tempfile.TemporaryDirectory() as d:
            code, _ = _run([
                "agent-goal", "--evaluator", "llm", "--planner", "llm",
                "--model", "claude-opus-5", "--data-dir", d,
            ])
            self.assertEqual(code, 0)
            g = load_goal(d)
            self.assertEqual(g.evaluator, "llm")
            self.assertEqual(g.planner, "llm")
            self.assertEqual(g.model, "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
