"""Append-only log of operator-agent decisions.

One JSON list at <data-dir>/agent_log.json, atomically written, in the
shape of the other ledgers. One entry per agent step: what it decided,
why, what changed, and the human digest afterwards. Standard library
only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class AgentLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "AgentLog":
        log = cls(path)
        if not log.path.exists():
            return log
        try:
            raw = json.loads(log.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt agent log {log.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"agent log {log.path} must contain a JSON list")
        log._entries = [dict(e) for e in raw]
        return log

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

    def entries(self) -> list[dict]:
        return list(self._entries)

    def add(self, entry: dict) -> None:
        self._entries.append(dict(entry))

    def latest(self) -> dict | None:
        return dict(self._entries[-1]) if self._entries else None

    def __len__(self) -> int:
        return len(self._entries)
