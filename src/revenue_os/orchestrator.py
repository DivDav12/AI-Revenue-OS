"""CEO / Orchestrator agent.

Owns an in-memory task list and an agent registry. Dispatches one task
at a time to a capable agent, collects the structured Result, logs it,
and enqueues any follow-up tasks the agent declared - stamped with the
parent's id and depth+1. Bounded by max_depth and max_tasks so a
misbehaving agent cannot run away.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from .agent import Agent
from .messages import Result, Task
from .registry import AgentRegistry

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        registry: AgentRegistry | None = None,
        worker: Agent | None = None,
        *,
        max_depth: int = 3,
        sink=None,
    ) -> None:
        self.registry = registry or AgentRegistry()
        if worker is not None:
            self.registry.register(worker)
        self.max_depth = max_depth
        # optional callable (task, result) -> None, invoked after each dispatch
        self.sink = sink
        self._tasks: list[Task] = []
        self.results: list[Result] = []
        self.tasks_seen: list[Task] = []  # every dispatched task, for lineage

    def register(self, agent: Agent) -> None:
        self.registry.register(agent)

    def add_task(self, task: Task) -> None:
        self._tasks.append(task)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def _enqueue_follow_ups(self, parent: Task, result: Result) -> None:
        for follow_up in getattr(result, "follow_ups", ()) or ():
            child = replace(
                follow_up, parent_id=parent.id, depth=parent.depth + 1
            )
            if child.depth > self.max_depth:
                logger.warning(
                    "dropping follow-up %s: depth %d > max_depth %d",
                    child.id, child.depth, self.max_depth,
                )
                self.results.append(Result(
                    task_id=child.id, agent="orchestrator", status="error",
                    error="max task depth exceeded",
                ))
                continue
            self._tasks.append(child)

    def dispatch_next(self) -> Result | None:
        """Pull one task, route it to a capable agent, record the result,
        and enqueue any follow-ups."""
        if not self._tasks:
            logger.info("no pending tasks")
            return None

        task = self._tasks.pop(0)
        self.tasks_seen.append(task)
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
        if self.sink is not None:
            try:
                self.sink(task, result)
            except Exception:  # a logging sink must never break dispatch
                logger.warning("task sink failed for %s", task.id, exc_info=True)
        self._enqueue_follow_ups(task, result)
        return result

    def run_cycle(self, max_tasks: int = 200) -> list[Result]:
        """Drain all pending tasks (including follow-ups), bounded by
        max_tasks."""
        produced: list[Result] = []
        dispatched = 0
        while self._tasks:
            if dispatched >= max_tasks:
                logger.warning("run_cycle hit max_tasks=%d; %d task(s) dropped",
                               max_tasks, len(self._tasks))
                self._tasks.clear()
                stop = Result(
                    task_id="", agent="orchestrator", status="error",
                    error="max tasks per cycle exceeded",
                )
                self.results.append(stop)
                produced.append(stop)
                break
            result = self.dispatch_next()
            dispatched += 1
            if result is not None:
                produced.append(result)
        return produced

    # --- lineage ------------------------------------------------------

    def children_of(self, task_id: str) -> list[Task]:
        return [t for t in self.tasks_seen if t.parent_id == task_id]

    def descendants_of(self, task_id: str) -> list[Task]:
        out: list[Task] = []
        frontier = [task_id]
        while frontier:
            kids = [t for t in self.tasks_seen if t.parent_id in frontier]
            out.extend(kids)
            frontier = [t.id for t in kids]
        return out
