import unittest

from revenue_os.agent import EvaluatorAgent
from revenue_os.messages import Task
from revenue_os.opportunity import Opportunity, score_opportunity
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry
from revenue_os.workflow import run_evaluation


def _uniform(value: float, name: str = "opp") -> Opportunity:
    return Opportunity(
        name=name,
        startup_affordability=value,
        automation_potential=value,
        demand=value,
        competition_headroom=value,
        legal_feasibility=value,
        speed_to_first_revenue=value,
        profit_potential=value,
        scalability=value,
    )


class OpportunityScoringTests(unittest.TestCase):
    def test_score_is_deterministic_and_expected(self):
        opp = Opportunity(
            name="known",
            startup_affordability=5,
            automation_potential=4,
            demand=3,
            competition_headroom=3,
            legal_feasibility=5,
            speed_to_first_revenue=3,
            profit_potential=3,
            scalability=4,
        )
        score = score_opportunity(opp)
        self.assertEqual(score.total, 3.75)
        self.assertEqual(score.opportunity_name, "known")
        self.assertEqual(score_opportunity(opp).total, score.total)

    def test_verdict_thresholds(self):
        self.assertEqual(score_opportunity(_uniform(5)).verdict, "pursue")
        self.assertEqual(score_opportunity(_uniform(3)).verdict, "hold")
        self.assertEqual(score_opportunity(_uniform(1)).verdict, "reject")

    def test_out_of_range_estimate_raises(self):
        with self.assertRaises(ValueError):
            score_opportunity(_uniform(6))


class EvaluatorAgentTests(unittest.TestCase):
    def _orchestrator(self) -> Orchestrator:
        registry = AgentRegistry()
        registry.register(EvaluatorAgent(name="evaluator"))
        return Orchestrator(registry=registry)

    def test_agent_routes_by_capability_and_returns_score(self):
        orch = self._orchestrator()
        orch.add_task(
            Task(
                objective="evaluate",
                capability="evaluate",
                payload={"opportunity": _uniform(4)},
            )
        )
        result = orch.dispatch_next()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.agent, "evaluator")
        self.assertEqual(result.output["total"], 4.0)
        self.assertEqual(result.output["verdict"], "pursue")

    def test_bad_payload_produces_error_and_cycle_survives(self):
        orch = self._orchestrator()
        orch.add_task(Task(objective="evaluate", capability="evaluate", payload={}))
        orch.add_task(
            Task(
                objective="evaluate",
                capability="evaluate",
                payload={"opportunity": _uniform(3)},
            )
        )
        results = orch.run_cycle()
        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[1].status, "ok")


class EvaluationWorkflowTests(unittest.TestCase):
    def test_run_evaluation_ranks_by_total_desc(self):
        opps = [_uniform(1, "low"), _uniform(5, "high"), _uniform(3, "mid")]
        ranked = run_evaluation(opps)
        self.assertEqual(
            [s.opportunity_name for s in ranked], ["high", "mid", "low"]
        )
        self.assertEqual([s.total for s in ranked], [5.0, 3.0, 1.0])


if __name__ == "__main__":
    unittest.main()
