"""Wire the Orchestrator with a registry of worker agents and run one cycle."""

from __future__ import annotations

import logging

from .agent import ReverseAgent, WorkerAgent
from .messages import Result, Task
from .orchestrator import Orchestrator
from .registry import AgentRegistry


def build_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    return Orchestrator(registry=registry)


def run_once(tasks: list[Task] | None = None) -> list[Result]:
    """Run a single execution cycle over the given (or a default) task."""
    orchestrator = build_orchestrator()
    default = [Task(objective="M2 smoke test: confirm the runtime cycle works")]
    for task in tasks if tasks is not None else default:
        orchestrator.add_task(task)
    return orchestrator.run_cycle()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = run_once(
        [
            Task(objective="route me to the echo worker", capability="echo"),
            Task(objective="route me to the reverse worker", capability="reverse"),
        ]
    )
    for result in results:
        print(f"[{result.status}] {result.agent} -> {result.output or result.error}")
