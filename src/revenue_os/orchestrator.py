"""CEO / Orchestrator agent.

Owns an in-memory task list, dispatches one task at a time to a
worker, collects the structured Result, and logs it.
"""

from __future__ import annotations

import logging

from .agent import Agent
from .messages import Result, Task

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, worker: Agent) -> None:
        self.worker = worker
        self._tasks: list[Task] = []
        self.results: list[Result] = []

    def add_task(self, task: Task) -> None:
        self._tasks.append(task)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def dispatch_next(self) -> Result | None:
        """Pull one task, run it through the worker, record the result."""
        if not self._tasks:
            logger.info("no pending tasks")
            return None

        task = self._tasks.pop(0)
        logger.info("dispatching task %s: %s", task.id, task.objective)
        result = self.worker.run(task)
        self.results.append(result)
        logger.info(
            "result %s for task %s: status=%s", result.id, result.task_id, result.status
        )
        return result

    def run_cycle(self) -> list[Result]:
        """Drain all pending tasks once."""
        produced: list[Result] = []
        while self._tasks:
            result = self.dispatch_next()
            if result is not None:
                produced.append(result)
        return produced
