"""Append-only ledger of AI operating spend.

One JSON list at <data-dir>/llm_spend.json, atomically written, in the
shape of the other ledgers. One entry per LLM-using command invocation
(discovery evaluation, validation planning, offer proposal), so the
human owner can see total AI spend at a glance.

Records only what already happened; it never gates a run. Standard
library only; never touched on the deterministic paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso

_ACTIVITIES = ("evaluate", "plan", "offer", "decide")


def entry_from(activity: str, worker) -> dict:
    """Build a spend entry from any LlmNormalizer / LlmPlanner /
    LlmOfferProposer after a run."""
    if activity not in _ACTIVITIES:
        raise ValueError(f"activity must be one of {_ACTIVITIES}")
    meter = getattr(worker, "meter", None)
    return {
        "ts": now_iso(),
        "activity": activity,
        "model": getattr(worker, "model", ""),
        "api_calls": int(getattr(worker, "cache_misses", 0)),
        "input_tokens": int(getattr(meter, "input_tokens", 0)),
        "output_tokens": int(getattr(meter, "output_tokens", 0)),
        "cost_usd": round(getattr(meter, "cost_usd", 0.0), 4),
        "cache_hits": int(getattr(worker, "cache_hits", 0)),
        "cache_misses": int(getattr(worker, "cache_misses", 0)),
        "ceiling_hit": bool(getattr(worker, "ceiling_hit", False)),
    }


class LlmSpendLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "LlmSpendLog":
        log = cls(path)
        if not log.path.exists():
            return log
        try:
            raw = json.loads(log.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt llm spend log {log.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"llm spend log {log.path} must contain a JSON list")
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

    def summary(self) -> dict:
        by_activity = {a: 0.0 for a in _ACTIVITIES}
        total_cost = total_calls = 0.0
        for e in self._entries:
            cost = float(e.get("cost_usd", 0.0))
            total_cost += cost
            total_calls += int(e.get("api_calls", 0))
            act = e.get("activity")
            if act in by_activity:
                by_activity[act] = round(by_activity[act] + cost, 4)
        return {
            "runs": len(self._entries),
            "total_cost_usd": round(total_cost, 4),
            "total_api_calls": int(total_calls),
            "by_activity": by_activity,
        }
