"""CEO / Orchestrator agent.

Owns an in-memory task list and an agent registry. Dispatches one task
at a time to a capable agent, collects the structured Result, logs it.
"""

from __future__ import annotations

import logging

from .agent import Agent
from .messages import Result, Task
from .registry import AgentRegistry

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self, registry: AgentRegistry | None = None, worker: Agent | None = None
    ) -> None:
        self.registry = registry or AgentRegistry()
        if worker is not None:
            self.registry.register(worker)
        self._tasks: list[Task] = []
        self.results: list[Result] = []

    def register(self, agent: Agent) -> None:
        self.registry.register(agent)

    def add_task(self, task: Task) -> None:
        self._tasks.append(task)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def dispatch_next(self) -> Result | None:
        """Pull one task, route it to a capable agent, record the result."""
        if not self._tasks:
            logger.info("no pending tasks")
            return None

        task = self._tasks.pop(0)
        agent = self.registry.find_for(task)
        if agent is None:
            logger.warning(
                "no agent for task %s (capability=%s)", task.id, task.capability
            )
            result = Result(
                task_id=task.id,
                agent="orchestrator",
                status="error",
                error=f"no capable agent for capability={task.capability!r}",
            )
            self.results.append(result)
            return result

        logger.info(
            "dispatching task %s to %s: %s", task.id, agent.name, task.objective
        )
        result = agent.run(task)
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
