"""Append-only log of agent tasks dispatched inside the team.

One JSON list at <data-dir>/task_log.json, atomically written, in the
shape of the other ledgers. One entry per Task the Orchestrator ran:
the capability, the agent that handled it, its lineage (parent_id /
depth), and a compact summary built only from the real Result.output -
no timing, no progress, nothing invented.

Capped at the most recent MAX_ENTRIES so a long-running loop cannot
grow the file without bound. Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 500

# keys copied verbatim from Result.output into the entry summary when present
_SUMMARY_KEYS = (
    "count", "opportunity_name", "total", "verdict", "candidate_name",
    "kept", "shortlist", "dropped", "new", "refreshed", "shortlisted",
    "keywords", "sources", "runs", "researched",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize(output: dict) -> dict:
    out: dict = {}
    for key in _SUMMARY_KEYS:
        if key in output:
            value = output[key]
            if isinstance(value, list):
                out[key] = len(value)
            else:
                out[key] = value
    if "opportunities" in output and "count" not in out:
        out["count"] = len(output["opportunities"])
    return out


class TaskLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "TaskLog":
        log = cls(path)
        if not log.path.exists():
            return log
        try:
            raw = json.loads(log.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt task log {log.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"task log {log.path} must contain a JSON list")
        log._entries = [dict(e) for e in raw]
        return log

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries[-MAX_ENTRIES:], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def entries(self) -> list[dict]:
        return list(self._entries)

    def add(self, entry: dict) -> None:
        self._entries.append(dict(entry))

    def record(self, task, result) -> None:
        """Orchestrator sink: append one entry for a dispatched task."""
        self.add({
            "ts": _now_iso(),
            "task_id": task.id,
            "parent_id": task.parent_id,
            "depth": task.depth,
            "capability": task.capability,
            "agent": result.agent,
            "objective": task.objective,
            "status": result.status,
            "summary": summarize(result.output) if result.status == "ok" else {},
            "error": result.error,
        })

    def __len__(self) -> int:
        return len(self._entries)


def load_task_log(data_dir: str | Path) -> TaskLog:
    return TaskLog.load(Path(data_dir) / "task_log.json")
