"""Wire the Orchestrator with a registry of agents and run demo cycles."""

from __future__ import annotations

import logging

from .agent import DiscoveryAgent, EvaluatorAgent, ReverseAgent, WorkerAgent
from .messages import Result, Task
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .sources import RawSignal, StaticSource
from .workflow import discover_evaluate_select

_SAMPLE_SIGNALS = [
    RawSignal(
        title="Show HN: an open-source no-code automation platform",
        text="We built a self-serve tool to automate repetitive API workflows.",
        source="sample",
        external_id="s1",
    ),
    RawSignal(
        title="Ask HN: how do you find your first paying customers?",
        text="Bootstrapped founder looking for revenue and pricing advice.",
        source="sample",
        external_id="s2",
    ),
    RawSignal(
        title="Launch: a marketplace for reusable document templates",
        text="MVP is live, free tier plus paid plans.",
        source="sample",
        external_id="s3",
    ),
    RawSignal(
        title="A weekend project with no obvious business model",
        text="Just something I made for fun.",
        source="sample",
        external_id="s4",
    ),
]


def build_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    registry.register(EvaluatorAgent(name="evaluator"))
    registry.register(
        DiscoveryAgent(StaticSource(_SAMPLE_SIGNALS), name="discovery")
    )
    return Orchestrator(registry=registry)


def run_once(tasks: list[Task] | None = None) -> list[Result]:
    """Run a single execution cycle over the given (or a default) task."""
    orchestrator = build_orchestrator()
    default = [Task(objective="M4 smoke test: confirm the runtime cycle works")]
    for task in tasks if tasks is not None else default:
        orchestrator.add_task(task)
    return orchestrator.run_cycle()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    candidates = discover_evaluate_select(
        StaticSource(_SAMPLE_SIGNALS), limit=10, top_n=3
    )
    print("Candidate opportunities for further investigation:")
    for rank, score in enumerate(candidates, start=1):
        print(
            f"  {rank}. {score.opportunity_name}: "
            f"{score.total} ({score.verdict})"
        )
