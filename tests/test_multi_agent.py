import unittest

from revenue_os.agent import ReverseAgent, WorkerAgent
from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry


def _orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    return Orchestrator(registry=registry)


class MultiAgentTests(unittest.TestCase):
    def test_task_routed_to_agent_by_capability(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="abc", capability="reverse"))

        result = orch.dispatch_next()

        self.assertEqual(result.agent, "reverse-worker")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["reversed"], "cba")

    def test_task_without_capability_falls_back_to_first_agent(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="no capability given"))

        result = orch.dispatch_next()

        self.assertEqual(result.agent, "echo-worker")
        self.assertEqual(result.status, "ok")

    def test_unknown_capability_produces_error_result(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="x", capability="does-not-exist"))

        result = orch.dispatch_next()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.agent, "orchestrator")

    def test_registry_rejects_duplicate_agent_name(self):
        registry = AgentRegistry()
        registry.register(WorkerAgent(name="dup"))
        with self.assertRaises(ValueError):
            registry.register(WorkerAgent(name="dup"))


class AcquisitionAgentRoutingTests(unittest.TestCase):
    """Phase 2.3: the named acquisition agents route by capability, and the
    scout -> scorer chain runs through one Orchestrator cycle."""

    def test_named_acquisition_agents_route_by_capability(self):
        from revenue_os.team import build_team
        reg = build_team().registry
        self.assertEqual(
            reg.find_for(Task(objective="x", capability="score_prospects")).name,
            "opportunity_scorer")
        self.assertEqual(
            reg.find_for(Task(objective="x", capability="draft_outreach")).name,
            "outreach_drafter")

    def test_scout_emits_a_scorer_follow_up_and_the_chain_runs(self):
        from revenue_os.acquisition import AcquisitionAgent, ProspectScoutAgent
        from revenue_os.acquisition_sources import AcqRecord

        class _Src:
            name = "fake"

            def search(self, query, limit, *, since_ts=None):
                return [AcqRecord(
                    title="how do I get my first customers for my SaaS",
                    url="https://news.ycombinator.com/item?id=1",
                    text="0 paying customers, just launched", source="fake",
                    posted_at="2026-08-28T00:00:00+00:00", query=query)]

        reg = AgentRegistry()
        reg.register(ProspectScoutAgent(_Src(), name="prospect_scout"))
        reg.register(AcquisitionAgent(name="opportunity_scorer"))
        orch = Orchestrator(registry=reg)
        orch.add_task(Task(objective="scout", capability="scout_prospects",
                           payload={"queries": ["q"], "limit": 5, "then": "score"}))
        results = orch.run_cycle()

        self.assertEqual([r.agent for r in results],
                         ["prospect_scout", "opportunity_scorer"])
        self.assertEqual(results[0].output["count"], 1)
        leads = results[1].output["leads"]
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["url"], "https://news.ycombinator.com/item?id=1")


if __name__ == "__main__":
    unittest.main()
