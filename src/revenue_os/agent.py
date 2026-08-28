"""Agent base class and two deterministic stub workers.

Workers are deterministic by default. DiscoveryAgent accepts an
injectable normalizer so an opt-in LLM path can replace the keyword
nudger without changing the runtime plumbing.
"""

from __future__ import annotations

import logging

from .messages import Result, Task
from .normalize import to_opportunity
from .opportunity import Opportunity, score_opportunity

logger = logging.getLogger(__name__)


class Agent:
    """Base agent: a role, an objective, capabilities, and run(task) -> Result."""

    role: str = "agent"
    objective: str = ""
    capabilities: tuple[str, ...] = ()

    def __init__(
        self, name: str | None = None, capabilities: tuple[str, ...] | None = None
    ) -> None:
        self.name = name or self.role
        if capabilities is not None:
            self.capabilities = tuple(capabilities)

    def can_handle(self, task: Task) -> bool:
        if task.capability is None:
            return True
        return task.capability in self.capabilities

    def run(self, task: Task) -> Result:
        raise NotImplementedError


class WorkerAgent(Agent):
    """Stub worker. Echoes the task objective back as a completed result."""

    role = "worker"
    objective = "Execute a single assigned task and return a structured result."
    capabilities = ("echo",)

    def run(self, task: Task) -> Result:
        try:
            output = {
                "handled_objective": task.objective,
                "received_payload": dict(task.payload),
                "note": "stub worker: no real work performed",
            }
            return Result(
                task_id=task.id,
                agent=self.name,
                status="ok",
                output=output,
            )
        except Exception as exc:  # defensive: keep the cycle alive
            return Result(
                task_id=task.id,
                agent=self.name,
                status="error",
                error=str(exc),
            )


class EvaluatorAgent(Agent):
    """Scores a revenue Opportunity carried in task.payload['opportunity']."""

    role = "evaluator"
    objective = "Score a structured revenue opportunity deterministically."
    capabilities = ("evaluate",)

    def run(self, task: Task) -> Result:
        opp = task.payload.get("opportunity")
        if not isinstance(opp, Opportunity):
            return Result(
                task_id=task.id,
                agent=self.name,
                status="error",
                error="payload['opportunity'] must be an Opportunity",
            )
        try:
            score = score_opportunity(opp)
        except ValueError as exc:
            return Result(
                task_id=task.id,
                agent=self.name,
                status="error",
                error=str(exc),
            )
        return Result(
            task_id=task.id,
            agent=self.name,
            status="ok",
            output={
                "opportunity_name": score.opportunity_name,
                "total": score.total,
                "verdict": score.verdict,
                "breakdown": score.breakdown,
            },
        )


class DiscoveryAgent(Agent):
    """Fetches raw signals from a Source and normalizes them to Opportunities.

    limit is read from task.payload['limit'] (default 10).
    """

    role = "discovery"
    objective = "Collect external signals and normalize them into opportunities."
    capabilities = ("discover",)

    def __init__(self, source, name: str | None = None, normalizer=to_opportunity) -> None:
        super().__init__(name=name)
        self.source = source
        self.normalizer = normalizer

    def run(self, task: Task) -> Result:
        limit = int(task.payload.get("limit", 10))
        try:
            signals = self.source.fetch(limit)
        except Exception as exc:  # source failure must not kill the cycle
            return Result(
                task_id=task.id,
                agent=self.name,
                status="error",
                error=f"source fetch failed: {exc}",
            )
        opportunities = []
        for signal in signals:
            try:
                opportunities.append(self.normalizer(signal))
            except Exception as exc:  # one bad normalization must not kill the cycle
                logger.warning("skipped signal %r: %s", signal.title, exc)
        return Result(
            task_id=task.id,
            agent=self.name,
            status="ok",
            output={"opportunities": opportunities, "count": len(opportunities)},
        )


class ReverseAgent(Agent):
    """Stub worker. Returns the task objective reversed."""

    role = "reverser"
    objective = "Reverse the objective string of a task."
    capabilities = ("reverse",)

    def run(self, task: Task) -> Result:
        try:
            output = {
                "handled_objective": task.objective,
                "reversed": task.objective[::-1],
                "note": "stub worker: no real work performed",
            }
            return Result(
                task_id=task.id,
                agent=self.name,
                status="ok",
                output=output,
            )
        except Exception as exc:  # defensive: keep the cycle alive
            return Result(
                task_id=task.id,
                agent=self.name,
                status="error",
                error=str(exc),
            )
