"""Agent base class and a single stub worker.

No LLM or network calls at this stage: WorkerAgent produces a
deterministic result so the runtime plumbing can be tested.
"""

from __future__ import annotations

from .messages import Result, Task


class Agent:
    """Base agent: a role, an objective, and a run(task) -> Result method."""

    role: str = "agent"
    objective: str = ""

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.role

    def run(self, task: Task) -> Result:
        raise NotImplementedError


class WorkerAgent(Agent):
    """Stub worker. Echoes the task objective back as a completed result."""

    role = "worker"
    objective = "Execute a single assigned task and return a structured result."

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
