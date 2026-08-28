"""Agent base class and two deterministic stub workers.

No LLM or network calls at this stage: workers produce deterministic
results so the runtime plumbing can be tested.
"""

from __future__ import annotations

from .messages import Result, Task


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
