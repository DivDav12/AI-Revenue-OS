"""Evaluation workflow: score a set of opportunities and rank them.

Dispatches one 'evaluate' Task per Opportunity through the Orchestrator,
collects the Results, and returns the successful scores ranked by total
(highest first). No LLM, no I/O.
"""

from __future__ import annotations

from .agent import EvaluatorAgent
from .messages import Task
from .opportunity import Opportunity, OpportunityScore
from .orchestrator import Orchestrator
from .registry import AgentRegistry


def _default_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(EvaluatorAgent(name="evaluator"))
    return Orchestrator(registry=registry)


def run_evaluation(
    opportunities: list[Opportunity], orchestrator: Orchestrator | None = None
) -> list[OpportunityScore]:
    orchestrator = orchestrator or _default_orchestrator()
    for opp in opportunities:
        orchestrator.add_task(
            Task(
                objective=f"evaluate opportunity: {opp.name}",
                capability="evaluate",
                payload={"opportunity": opp},
            )
        )
    scores: list[OpportunityScore] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            continue
        scores.append(
            OpportunityScore(
                opportunity_name=result.output["opportunity_name"],
                total=result.output["total"],
                verdict=result.output["verdict"],
                breakdown=result.output["breakdown"],
            )
        )
    scores.sort(key=lambda s: s.total, reverse=True)
    return scores
