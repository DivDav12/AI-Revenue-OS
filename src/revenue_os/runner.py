"""Wire the Orchestrator and a WorkerAgent together and run one cycle."""

from __future__ import annotations

import logging

from .agent import WorkerAgent
from .messages import Result, Task
from .orchestrator import Orchestrator


def build_orchestrator() -> Orchestrator:
    return Orchestrator(worker=WorkerAgent(name="worker-1"))


def run_once(tasks: list[Task] | None = None) -> list[Result]:
    """Run a single execution cycle over the given (or a default) task."""
    orchestrator = build_orchestrator()
    for task in tasks or [Task(objective="M1 smoke test: confirm the runtime cycle works")]:
        orchestrator.add_task(task)
    return orchestrator.run_cycle()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = run_once()
    for result in results:
        print(f"[{result.status}] {result.agent} -> {result.output or result.error}")
