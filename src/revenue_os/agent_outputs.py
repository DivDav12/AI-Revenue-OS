"""Persistence for the deterministic roster agents (Phase A onward).

One JSON object at <data-dir>/agent_outputs.json, atomically written,
keyed by capability. Each value is the most recent successful Result
output for that capability plus a timestamp and the objective it ran
for. Restart-safe: `run_agent` reloads it every call.

These agents hold no state of their own - this file IS their memory.
Standard library only. No database.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentOutputStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_cap: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "AgentOutputStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt agent output store {store.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"agent output store {store.path} must contain a JSON object")
        store._by_cap = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._by_cap, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def put(self, capability: str, output: dict, *, objective: str = "") -> None:
        self._by_cap[str(capability)] = {
            "output": output,
            "objective": objective,
            "ts": _now_iso(),
        }

    def get(self, capability: str) -> dict | None:
        entry = self._by_cap.get(str(capability))
        return dict(entry) if entry is not None else None

    def output(self, capability: str) -> dict | None:
        entry = self._by_cap.get(str(capability))
        return dict(entry["output"]) if entry is not None else None

    def all(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._by_cap.items()}

    def __len__(self) -> int:
        return len(self._by_cap)


def load_agent_outputs(data_dir: str | Path) -> AgentOutputStore:
    return AgentOutputStore.load(Path(data_dir) / "agent_outputs.json")
