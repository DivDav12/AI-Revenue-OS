"""Human-maintained register of operational blockers.

One JSON list at <data-dir>/blockers.json, atomically written, in the
shape of the other ledgers. Every entry is recorded by a person - this
module never detects, infers, or invents a blocker, and the dashboard
renders exactly what is in the file (an absent/empty file renders an
honest note, not "all clear").

It exists because some real blockers leave no trace on disk: a payment
account restriction, missing API credits, a provider outage. Those are
facts the operator knows and the dashboard must not hide.

Standard library only. No network, no money.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")
_OPEN = "open"
_RESOLVED = "resolved"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BlockerStore:
    """A list of {id, area, title, detail, severity, status, opened_at,
    resolved_at, owner} dicts. `owner` is who has to act: 'human' or
    'system'."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "BlockerStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt blocker store {store.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"blocker store {store.path} must contain a JSON list")
        store._entries = [dict(e) for e in raw if isinstance(e, dict)]
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def all(self) -> list[dict]:
        return [dict(e) for e in self._entries]

    def open(self) -> list[dict]:
        """Open blockers, most severe first, then oldest first."""
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(
            (dict(e) for e in self._entries if e.get("status", _OPEN) == _OPEN),
            key=lambda e: (rank.get(e.get("severity", "warning"), 9),
                           str(e.get("opened_at", ""))),
        )

    def get(self, blocker_id: str) -> dict | None:
        for e in self._entries:
            if e.get("id") == blocker_id:
                return dict(e)
        return None

    def add(self, blocker_id: str, title: str, *, area: str = "",
            detail: str = "", severity: str = "warning",
            owner: str = "human") -> dict:
        """Record a blocker a person has observed. Re-adding a known id
        updates its text and reopens it."""
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")
        entry = {
            "id": str(blocker_id),
            "area": str(area),
            "title": str(title),
            "detail": str(detail),
            "severity": severity,
            "status": _OPEN,
            "owner": str(owner),
            "opened_at": _now_iso(),
            "resolved_at": None,
        }
        for i, existing in enumerate(self._entries):
            if existing.get("id") == entry["id"]:
                entry["opened_at"] = existing.get("opened_at") or entry["opened_at"]
                self._entries[i] = entry
                return dict(entry)
        self._entries.append(entry)
        return dict(entry)

    def resolve(self, blocker_id: str) -> dict:
        for i, existing in enumerate(self._entries):
            if existing.get("id") == blocker_id:
                updated = dict(existing)
                updated["status"] = _RESOLVED
                updated["resolved_at"] = _now_iso()
                self._entries[i] = updated
                return dict(updated)
        raise ValueError(f"no blocker with id {blocker_id!r}")

    def __len__(self) -> int:
        return len(self._entries)


def load_blockers(data_dir: str | Path) -> BlockerStore:
    return BlockerStore.load(Path(data_dir) / "blockers.json")
