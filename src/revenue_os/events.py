"""The execution event log.

An append-only, monotonically-sequenced record of what the task pipeline
actually did: a task was created, became ready, started, succeeded, failed,
was scheduled for retry, was blocked on an approval, was cancelled; an
opportunity transitioned as a result.

One JSON file (`<data-dir>/execution_events.json`, atomic write), capped at
`_CAP` entries, newest last. Each event carries a `seq` so a consumer
(JARVIS) can process everything after the last seq it handled - exactly
once, deterministically, without re-reacting to old events.

This log is an OUTPUT stream. The worker writes to it; nothing in the
worker reads it back to decide what to run. That is the guardrail against
a recursive execution loop: reactions go JARVIS -> state machine ->
TaskQueue -> worker, never event -> agent.

Standard library only. No network, no money, no LLM.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso

_CAP = 2000

EVENT_TYPES: tuple[str, ...] = (
    "TASK_CREATED",
    "TASK_READY",
    "TASK_STARTED",
    "TASK_SUCCEEDED",
    "TASK_FAILED",
    "TASK_RETRY_SCHEDULED",
    "TASK_BLOCKED",
    "TASK_CANCELLED",
    "OPPORTUNITY_TRANSITIONED",
)


class EventLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._events: list[dict] = []
        self._seq = 0

    @classmethod
    def load(cls, path: str | Path) -> "EventLog":
        log = cls(path)
        if not log.path.exists():
            return log
        try:
            raw = json.loads(log.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return log                     # a corrupt log must not break the worker
        if isinstance(raw, list):
            log._events = [e for e in raw if isinstance(e, dict)]
            log._seq = max((int(e.get("seq", 0)) for e in log._events), default=0)
        return log

    def save(self) -> None:
        self._events = self._events[-_CAP:]
        payload = json.dumps(self._events, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- write ---------------------------------------------------
    def emit(self, event_type: str, *, task_id: str = "", opportunity_id: str = "",
             task_type: str = "", actor: str = "worker", **data) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
        self._seq += 1
        entry = {
            "seq": self._seq,
            "ts": now_iso(),
            "type": event_type,
            "task_id": str(task_id or ""),
            "opportunity_id": str(opportunity_id or ""),
            "task_type": str(task_type or ""),
            "actor": str(actor or "worker"),
            "data": {k: v for k, v in data.items()},
        }
        self._events.append(entry)
        return entry

    # --- read ---------------------------------------------------
    def all(self) -> list[dict]:
        return list(self._events)

    def recent(self, n: int = 50) -> list[dict]:
        return list(self._events[-n:][::-1])

    def since(self, seq: int) -> list[dict]:
        """Every event with seq strictly greater than `seq`, oldest first.
        The deterministic 'what is new for me' query."""
        return [e for e in self._events if int(e.get("seq", 0)) > int(seq)]

    def by_type(self, *types: str) -> list[dict]:
        return [e for e in self._events if e.get("type") in types]

    def by_task(self, task_id: str) -> list[dict]:
        return [e for e in self._events if e.get("task_id") == task_id]

    def last_seq(self) -> int:
        return self._seq

    def __len__(self) -> int:
        return len(self._events)


def load_events(data_dir: str | Path) -> EventLog:
    return EventLog.load(Path(data_dir) / "execution_events.json")
