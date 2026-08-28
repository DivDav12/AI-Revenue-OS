"""Append-only log of discovery-cycle outcomes.

One JSON list, atomically written, in the shape of the other ledgers.
Each entry records what a single run of run_discovery_cycle did:

  fetched              opportunities that entered evaluation
  filtered_out         signals removed by the relevance filter beforehand
                       (raw source output = fetched + filtered_out)
  dropped_below_score  scored candidates below --min-score
  evaluated            candidates successfully scored
  kept                 candidates persisted this run
  new / refreshed      of those, how many were first-seen vs. re-scored
  shortlisted          candidates auto-shortlisted this run
  calibrated           --calibrated was requested
  weights_applied      calibration actually had enough data to reweight

Read-only consumers surface the latest entry. Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class DiscoveryLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "DiscoveryLog":
        log = cls(path)
        if not log.path.exists():
            return log
        try:
            raw = json.loads(log.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt discovery log {log.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"discovery log {log.path} must contain a JSON list")
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
