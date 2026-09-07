"""Hard, absolute LLM/API spend ceiling for the affiliate/content pipeline.

Deliberately separate from the old `budget.py` (which implements a
different, retired concept: a pre-sale cap PLUS a locked "growth
capital" that unlocks on first sale - that distinction belonged to the
founder-outreach service business and does not apply here). This
pipeline never spends money on anything except its own optional LLM
calls, so there is exactly one constant and one guard.

Fail-closed on TWO conditions, not one:
  1. the cap is reached, or
  2. the caller cannot supply a real, verifiable cost estimate.

There is no code path that raises CAP_USD at runtime, no auto-reload,
and no override. Changing the cap means a human edits this constant and
redeploys - a visible, reviewed change, never silent creep.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: absolute ceiling, USD. Exactly $3.00 - not $3.20, no separate reserve.
CAP_USD = 3.00


class BudgetExhausted(RuntimeError):
    """Raised when a call would exceed CAP_USD, or its cost is unverifiable."""


@dataclass(frozen=True)
class SpendRecord:
    activity: str
    cost_usd: float
    ts: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "activity": self.activity,
            "cost_usd": round(float(self.cost_usd), 6),
            "ts": self.ts,
            "note": self.note,
        }


class SpendLedger:
    """Append-only, atomic-write JSON ledger - same pattern as the rest of
    this codebase's `_JsonListStore` family (tempfile + os.replace)."""

    _FILENAME = "budget_guard_spend.json"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._rows = [dict(r) for r in raw] if isinstance(raw, list) else []
            except json.JSONDecodeError:
                self._rows = []

    @classmethod
    def load(cls, data_dir) -> "SpendLedger":
        return cls(Path(data_dir) / cls._FILENAME)

    def rows(self) -> list[dict]:
        return list(self._rows)

    def total(self) -> float:
        return round(sum(float(r.get("cost_usd", 0.0)) for r in self._rows), 6)

    def record(self, rec: SpendRecord) -> None:
        self._rows.append(rec.to_dict())
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._rows, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def spent(data_dir) -> float:
    return SpendLedger.load(data_dir).total()


def remaining(data_dir) -> float:
    return round(CAP_USD - spent(data_dir), 6)


def guard(data_dir, *, estimated_cost_usd) -> None:
    """Call BEFORE any paid LLM/API call.

    Raises `BudgetExhausted` when:
      - `estimated_cost_usd` is missing, not a real number, NaN, or negative
        (fail-closed on UNCERTAINTY - a caller with no verifiable estimate
        must never guess "probably free"), OR
      - `spent + estimate > CAP_USD` (fail-closed on EXHAUSTION).

    A caller that cannot produce a real cost estimate should not call this
    at all with a guessed value - it should treat the action as unaffordable.
    """
    try:
        est = float(estimated_cost_usd)
    except (TypeError, ValueError):
        raise BudgetExhausted(
            f"no verifiable cost estimate ({estimated_cost_usd!r}) - refusing (fail-closed)")
    if math.isnan(est) or math.isinf(est) or est < 0:
        raise BudgetExhausted(
            f"invalid cost estimate {estimated_cost_usd!r} - refusing (fail-closed)")

    current = spent(data_dir)
    if current + est > CAP_USD:
        raise BudgetExhausted(
            f"would exceed the ${CAP_USD:.2f} hard cap "
            f"(spent ${current:.4f}, estimate ${est:.4f}) - "
            "no auto-reload, no override, no exception")


def record_spend(data_dir, *, activity: str, cost_usd: float, ts: str, note: str = "") -> None:
    """Call AFTER a successful paid call, with its real (or a documented
    upper-bound) cost. Never call this speculatively."""
    SpendLedger.load(data_dir).record(
        SpendRecord(activity=activity, cost_usd=float(cost_usd), ts=ts, note=note))
