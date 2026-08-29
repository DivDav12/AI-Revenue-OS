import tempfile
import unittest
from pathlib import Path

from revenue_os.deliverable import render_launch_plan_md
from revenue_os.intake import IntakeStore
from revenue_os.launch_plan import (
    LaunchPlanAgent,
    LaunchPlanWorker,
    draft_plan_llm,
    draft_plan_web,
    qc_plan,
)
from revenue_os.llm_cache import LlmCache
from revenue_os.messages import Task
from revenue_os.revenue import RevenueLedger, record_payment
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import draft_launch_plan

_FIELDS = {
    "name": "Dana", "email": "dana@example.com", "business": "acme.example",
    "sells": "a booking tool for tattoo studios", "current_price": "19 EUR/mo",
    "target_audience": "independent tattoo artists in the EU",
    "customer_situation": "4 unpaid beta users", "previous_attempts": "instagram, cold DMs",
    "biggest_problem": "no repeatable acquisition channel",
}


def _valid_plan_payload():
    return {
        "business_analysis": {
            "what_sold": "a booking tool", "problem_solved": "no-show chaos",
            "value_proposition": "fewer no-shows, less admin",
        },
        "ideal_customer": {
            "profile": "solo tattoo artist", "characteristics": "books via DMs",
            "where_to_reach": "artist communities and conventions",
        },
        "acquisition_opportunities": [
            {"name": f"Opp {i}", "channel": f"channel {i}",
             "why_relevant": "artists gather there", "first_step": "post an intro"}
            for i in range(1, 7)
        ],
        "prioritized_strategy": {
            "ranking": [f"Opp {i}" for i in range(1, 7)],
            "start_with": "Opp 1", "reasoning": "highest intent, lowest effort",
        },
        "action_plan_14_day": [
            {"day": d, "focus": f"focus {d}", "actions": f"do thing {d}"}
            for d in range(1, 15)
        ],
        "outreach_templates": [
            {"name": "Cold DM", "context": "to an artist", "body": "Hi {name}, ..."},
            {"name": "Community post", "context": "in a forum", "body": "I built ..."},
        ],
        "next_steps": ["Pick 2 channels", "Write the DM", "Send 10 messages"],
    }


class _Usage:
    input_tokens = 1200
    output_tokens = 900
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _ToolBlock:
    type = "tool_use"
    name = "record_launch_plan"

    def __init__(self, payload):
        self.input = payload


class _SearchResult:
    type = "web_search_tool_result"
    content = [{"title": "t", "url": "https://example.com/a"}]  # list = success


class _Resp:
    def __init__(self, payload, *, with_search=False):
        self.content = ([_SearchResult()] if with_search else []) + [_ToolBlock(payload)]
        self.usage = _Usage()
        self.stop_reason = "tool_use"


class _FakeClient:
    def __init__(self, payload=None, *, with_search=False):
        self.payload = payload or _valid_plan_payload()
        self.with_search = with_search
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.payload, with_search=self.with_search)


class QcTests(unittest.TestCase):
    def test_valid_plan_passes_and_is_stamped(self):
        plan = draft_plan_llm(_FIELDS, client=_FakeClient())
        self.assertTrue(plan["qc"]["passed"])
        self.assertEqual(len(plan["action_plan_14_day"]), 14)
        self.assertEqual(plan["basis"], "model knowledge, no web")

    def test_rejects_short_action_plan(self):
        bad = _valid_plan_payload()
        bad["action_plan_14_day"] = bad["action_plan_14_day"][:10]
        with self.assertRaisesRegex(ValueError, "days 1..14"):
            draft_plan_llm(_FIELDS, client=_FakeClient(bad))

    def test_rejects_too_few_opportunities(self):
        bad = _valid_plan_payload()
        bad["acquisition_opportunities"] = bad["acquisition_opportunities"][:3]
        with self.assertRaisesRegex(ValueError, "acquisition opportunities"):
            draft_plan_llm(_FIELDS, client=_FakeClient(bad))

    def test_rejects_guarantee_language(self):
        bad = _valid_plan_payload()
        bad["prioritized_strategy"]["reasoning"] = "this guarantees new customers"
        with self.assertRaisesRegex(ValueError, "promise language"):
            draft_plan_llm(_FIELDS, client=_FakeClient(bad))

    def test_web_requires_sources(self):
        # _FakeClient with_search=False -> grounded_tool_call sees 0 searches
        with self.assertRaisesRegex(ValueError, "sources"):
            draft_plan_web(_FIELDS, client=_FakeClient())

    def test_web_with_sources_ok(self):
        payload = _valid_plan_payload()
        payload["sources"] = [{"url": "https://example.com/x", "title": "X"}]
        plan = draft_plan_web(_FIELDS, client=_FakeClient(payload, with_search=True))
        self.assertTrue(plan["basis"].startswith("web search"))
        self.assertEqual(plan["sources"][0]["title"], "X")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_second_call_is_a_cache_hit(self):
        c = _FakeClient()
        w = LaunchPlanWorker(client=c, mode="llm", cache=self.cache)
        w("ORD1", _FIELDS)
        w("ORD1", _FIELDS)
        self.assertEqual(c.calls, 1)
        self.assertEqual(w.cache_hits, 1)

    def test_ceiling_blocks_a_call(self):
        w = LaunchPlanWorker(client=_FakeClient(), mode="llm", max_cost_usd=0.0)
        w.meter.input_tokens = 1_000_000
        with self.assertRaises(Exception):
            w("ORD1", _FIELDS)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")
        self.store.put(Candidate(name="cand", status="launched"))
        self.store.save()
        self.ledger = RevenueLedger(self.d / "revenue.json")
        record_payment(self.store, self.ledger, "cand", 29.9, actor="paypal",
                       currency="EUR", ref="paypal:CAP1")
        self.intake = IntakeStore(self.d / "intake.json")
        self.intake.add("ORD1", "cand", _FIELDS, capture_id="CAP1")
        self.intake.save()

    def tearDown(self):
        self._dir.cleanup()

    def _worker(self):
        return LaunchPlanWorker(client=_FakeClient(), mode="llm")

    def test_requires_reviewed_status(self):
        with self.assertRaisesRegex(ValueError, "intake-review"):
            draft_launch_plan(self.intake, self.ledger, self._worker(), "ORD1")

    def test_requires_a_booked_capture(self):
        self.intake.mark_reviewed("ORD1", actor="me")
        self.intake._by_order["ORD1"]["capture_id"] = "GHOST"
        with self.assertRaisesRegex(ValueError, "not a booked payment"):
            draft_launch_plan(self.intake, self.ledger, self._worker(), "ORD1")

    def test_happy_path_attaches_draft_and_is_idempotent(self):
        self.intake.mark_reviewed("ORD1", actor="me")
        out = draft_launch_plan(self.intake, self.ledger, self._worker(), "ORD1")
        self.assertEqual(out["plan"]["status"], "draft")
        reloaded = IntakeStore.load(self.d / "intake.json")
        self.assertTrue(reloaded.get("ORD1")["plan"]["qc"]["passed"])
        with self.assertRaisesRegex(ValueError, "already has a plan"):
            draft_launch_plan(reloaded, self.ledger, self._worker(), "ORD1")

    def test_approve_gate_and_render(self):
        self.intake.mark_reviewed("ORD1", actor="me")
        draft_launch_plan(self.intake, self.ledger, self._worker(), "ORD1")
        self.assertEqual(self.intake.get("ORD1")["plan"]["status"], "draft")
        self.intake.approve_plan("ORD1", actor="owner")
        self.assertEqual(self.intake.get("ORD1")["plan"]["status"], "approved")
        md = render_launch_plan_md(self.intake.get("ORD1"))
        self.assertIn("# Customer Launch Plan", md)
        self.assertIn("14-day action plan", md)
        self.assertIn("Day 14", md)
        # the only "guarantee" is the disclaimer's negation
        self.assertIn("not a guarantee of customers", md)

    def test_approve_requires_a_draft(self):
        self.intake.mark_reviewed("ORD1", actor="me")
        with self.assertRaisesRegex(ValueError, "no drafted plan"):
            self.intake.approve_plan("ORD1", actor="owner")


class AgentTests(unittest.TestCase):
    def test_agent_returns_plan(self):
        agent = LaunchPlanAgent(name="fulfillment_writer")
        entry = {"order_id": "ORD1", "candidate": "cand", "fields": _FIELDS}
        r = agent.run(Task(objective="p", capability="draft_launch_plan",
                           payload={"intake": entry,
                                    "worker": LaunchPlanWorker(client=_FakeClient(),
                                                               mode="llm")}))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["order_id"], "ORD1")
        self.assertTrue(r.output["plan"]["qc"]["passed"])

    def test_agent_errors_without_worker(self):
        agent = LaunchPlanAgent(name="fulfillment_writer")
        r = agent.run(Task(objective="p", capability="draft_launch_plan",
                           payload={"intake": {"order_id": "x", "fields": {}}}))
        self.assertEqual(r.status, "error")


if __name__ == "__main__":
    unittest.main()
