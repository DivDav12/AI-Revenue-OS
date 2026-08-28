"""Wire the Orchestrator with a registry of worker agents and run one cycle."""

from __future__ import annotations

import logging

from .agent import EvaluatorAgent, ReverseAgent, WorkerAgent
from .messages import Result, Task
from .opportunity import Opportunity
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .workflow import run_evaluation


def build_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    registry.register(EvaluatorAgent(name="evaluator"))
    return Orchestrator(registry=registry)


def run_once(tasks: list[Task] | None = None) -> list[Result]:
    """Run a single execution cycle over the given (or a default) task."""
    orchestrator = build_orchestrator()
    default = [Task(objective="M3 smoke test: confirm the runtime cycle works")]
    for task in tasks if tasks is not None else default:
        orchestrator.add_task(task)
    return orchestrator.run_cycle()


_SAMPLE_OPPORTUNITIES = [
    Opportunity(
        name="niche-newsletter-automation",
        description="Curated B2B newsletter assembled from public sources",
        startup_affordability=5,
        automation_potential=4,
        demand=3,
        competition_headroom=3,
        legal_feasibility=5,
        speed_to_first_revenue=3,
        profit_potential=3,
        scalability=4,
    ),
    Opportunity(
        name="bespoke-consulting",
        description="One-to-one paid consulting engagements",
        startup_affordability=4,
        automation_potential=1,
        demand=3,
        competition_headroom=2,
        legal_feasibility=5,
        speed_to_first_revenue=4,
        profit_potential=4,
        scalability=1,
    ),
    Opportunity(
        name="template-marketplace",
        description="Self-serve marketplace for reusable document templates",
        startup_affordability=4,
        automation_potential=5,
        demand=4,
        competition_headroom=2,
        legal_feasibility=4,
        speed_to_first_revenue=2,
        profit_potential=4,
        scalability=5,
    ),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ranked = run_evaluation(_SAMPLE_OPPORTUNITIES, orchestrator=build_orchestrator())
    print("Ranked opportunities:")
    for rank, score in enumerate(ranked, start=1):
        print(f"  {rank}. {score.opportunity_name}: {score.total} ({score.verdict})")
