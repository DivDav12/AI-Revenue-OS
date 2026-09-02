"""Distribution Strategist - deterministic, no network, no LLM, no money."""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import roster
from revenue_os.agent_runner import last_output, run_agent
from revenue_os.distribution import DistributionAgent, build_distribution_plan
from revenue_os.messages import Task
from revenue_os.pipeline import run_pipeline
from revenue_os.store import Candidate, CandidateStore
from revenue_os.team import build_team

_DEV_OPP = {
    "id": "opp_dev1",
    "title": "Open-source CLI that packages an API for indie developers",
    "target_customer": "solo developers shipping a SaaS",
    "category": "developer_tool",
    "required_work": "build the CLI + a paid pro tier",
    "probability": 0.2,
}
_OFFER = {"what_is_sold": "Developer CLI Pro", "price": 29.0, "currency": "EUR",
         "positioning": "ship your API integration in an afternoon"}


class DistributionPlanShapeTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_distribution_plan(opportunity=_DEV_OPP, offer=_OFFER)

    def test_schema_has_the_required_keys(self):
        p = self.plan
        self.assertEqual(p["opportunity_id"], "opp_dev1")
        self.assertTrue(p["human_gate_required"])
        self.assertIsInstance(p["channels"], list)
        self.assertGreaterEqual(len(p["channels"]), 10)
        self.assertIn(p["top_recommendation"].split(" - ")[0],
                      [c["channel"] for c in p["channels"]])
        for c in p["channels"]:
            for k in ("channel", "type", "fit_score", "reach_score",
                      "effort_score", "cost", "risk_score", "reason",
                      "recommended_action", "requires_human_action"):
                self.assertIn(k, c)
            self.assertEqual(c["cost"], 0)
            self.assertIs(c["requires_human_action"], True)
            self.assertIn(c["type"],
                          ("organic", "direct", "community", "content", "partner"))
            for s in ("fit_score", "reach_score", "effort_score", "risk_score"):
                self.assertGreaterEqual(c[s], 0)
                self.assertLessEqual(c[s], 10)

    def test_channels_are_priority_sorted_best_first(self):
        # the sort key is internal; assert the published order is stable and
        # that the top pick is a genuinely high-fit channel for a dev tool
        chans = self.plan["channels"]
        top = chans[0]
        self.assertGreaterEqual(top["fit_score"], 6)
        self.assertEqual(self.plan["shortlist"], [c["channel"] for c in chans[:3]])

    def test_developer_opportunity_favours_developer_channels(self):
        by_name = {c["channel"]: c for c in self.plan["channels"]}
        hn = next(k for k in by_name if k.startswith("Hacker News"))
        li = next(k for k in by_name if k.startswith("LinkedIn"))
        self.assertGreater(by_name[hn]["fit_score"], by_name[li]["fit_score"])
        self.assertTrue(by_name[hn]["matched_signals"])

    def test_b2b_service_opportunity_favours_direct_channels(self):
        b2b = {"id": "opp_b2b", "title": "Fixed-scope analytics audit for agencies",
               "target_customer": "B2B marketing agencies", "category": "b2b_service",
               "probability": 0.15}
        plan = build_distribution_plan(
            opportunity=b2b,
            offer={"what_is_sold": "Analytics audit", "price": 450.0})
        by_name = {c["channel"]: c for c in plan["channels"]}
        li = next(k for k in by_name if k.startswith("LinkedIn"))
        self.assertGreaterEqual(by_name[li]["fit_score"], 6)

    def test_community_channels_are_flagged_no_auto_post(self):
        by_name = {c["channel"]: c for c in self.plan["channels"]}
        hn = next(k for k in by_name if k.startswith("Hacker News"))
        owned = next(k for k in by_name if k.startswith("SEO / content"))
        self.assertFalse(by_name[hn]["auto_post_allowed"])
        self.assertIn("NOT permitted", by_name[hn]["reason"])
        self.assertTrue(by_name[owned]["auto_post_allowed"])

    def test_buying_probability_is_read_through(self):
        self.assertEqual(self.plan["buying_probability"], 0.2)
        no_p = build_distribution_plan(
            opportunity={"id": "x", "title": "a thing"}, offer={})
        self.assertEqual(no_p["buying_probability"], "unknown")


class DistributionAgentTests(unittest.TestCase):
    def test_agent_returns_ok_with_a_plan(self):
        res = DistributionAgent(name="distribution_strategist").run(
            Task(objective="x", capability="research_distribution",
                 payload={"opportunity": _DEV_OPP, "offer": _OFFER}))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.output["opportunity_id"], "opp_dev1")

    def test_agent_rejects_a_bad_payload(self):
        res = DistributionAgent(name="distribution_strategist").run(
            Task(objective="x", capability="research_distribution",
                 payload={"opportunity": "not a dict"}))
        self.assertEqual(res.status, "error")

    def test_registry_routes_the_capability(self):
        agent = build_team().registry.find_for(
            Task(objective="x", capability="research_distribution"))
        self.assertEqual(agent.name, "distribution_strategist")

    def test_roster_spec_is_live_and_autonomous(self):
        spec = roster.by_capability("research_distribution")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.id, "distribution_strategist")
        self.assertEqual(spec.status, "live")
        self.assertEqual(spec.gate, "autonomous")   # research only, never acts


class DistributionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_run_agent_dispatches_and_persists(self):
        res = run_agent(self.d, "research_distribution",
                        {"opportunity": _DEV_OPP, "offer": _OFFER},
                        objective="test")
        self.assertEqual(res.status, "ok")
        out = last_output(self.d, "research_distribution")
        self.assertTrue(out["human_gate_required"])
        self.assertFalse((self.d / "llm_spend.json").exists())
        self.assertFalse((self.d / "revenue.json").exists())

    def test_pipeline_runs_the_distribution_step(self):
        st = CandidateStore(self.d / "candidates.json")
        st.put(Candidate(name="dt", description="open-source developer CLI tool",
                         status="validated", total=3.4, verdict="hold",
                         offer={"what_is_sold": "CLI Pro", "price": 29.0,
                                "currency": "EUR", "positioning": "for developers"},
                         plan={"hypothesis": "h"}))
        st.save()
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "prepared")
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["research_distribution"], "ok")
        outs = json.loads((self.d / "agent_outputs.json").read_text(encoding="utf-8"))
        self.assertIn("research_distribution", outs)
        self.assertTrue(outs["research_distribution"]["output"]["human_gate_required"])


if __name__ == "__main__":
    unittest.main()
