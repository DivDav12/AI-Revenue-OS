"""Structured messages passed between agents.

Agents communicate only through these objects, never free-form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class Task:
    """A unit of work handed to an agent."""

    objective: str
    payload: dict = field(default_factory=dict)
    capability: str | None = None  # optional routing hint for the registry
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)
    parent_id: str | None = None  # the task that spawned this one (None = root)
    depth: int = 0


@dataclass(frozen=True)
class Result:
    """The outcome an agent returns for a Task."""

    task_id: str
    agent: str
    status: str  # "ok" | "error"
    output: dict = field(default_factory=dict)
    error: str | None = None
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)
    follow_ups: tuple = ()  # Tasks the agent wants spawned; the orchestrator enqueues them
