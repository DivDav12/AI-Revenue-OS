"""Cumulative cap on AI operating spend.

A per-data-dir ceiling on total LLM spend, stored at
<data-dir>/llm_budget.json. The system refuses to start an LLM run when
recorded spend plus the run's estimate would exceed it; only the human
raises it (cli: `llm-budget <amount>`), mirroring spend.set_budget.

Standard library only; never read on the deterministic paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso

DEFAULT_CAP_USD = 5.00


class LlmBudget:
    def __init__(self, path: str | Path, cap_usd: float = DEFAULT_CAP_USD) -> None:
        self.path = Path(path)
        self._cap = float(cap_usd)
        self._history: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "LlmBudget":
        budget = cls(path)
        if not budget.path.exists():
            return budget
        try:
            raw = json.loads(budget.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt llm budget {budget.path}: {exc}") from exc
        if not isinstance(raw, dict) or "cap_usd" not in raw:
            raise ValueError(
                f"llm budget {budget.path} must be an object with 'cap_usd'"
            )
        budget._cap = float(raw["cap_usd"])
        budget._history = [dict(h) for h in raw.get("history", [])]
        return budget

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"cap_usd": self._cap, "history": self._history}, indent=2
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @property
    def cap(self) -> float:
        return round(self._cap, 4)

    def history(self) -> list[dict]:
        return list(self._history)

    def set_cap(self, amount: float, *, actor: str) -> float:
        if amount < 0:
            raise ValueError("llm budget cap must not be negative")
        self._cap = round(float(amount), 4)
        self._history.append(
            {"ts": now_iso(), "actor": actor, "cap_usd": self._cap}
        )
        self.save()
        return self._cap
