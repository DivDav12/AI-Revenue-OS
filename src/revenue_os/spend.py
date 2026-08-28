"""Cost control: per-candidate budgets and the human spend-authorization gate.

The system never spends money. Every authorization is capped by a
human-set per-candidate budget (default 0.0) and a ceiling (default 0.0)
that only a human can raise. record_spend() logs money the human has
already spent; it authorizes nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .store import now_iso

DEFAULT_CEILING = 0.0


@dataclass(frozen=True)
class SpendRequest:
    candidate_name: str
    purpose: str
    amount: float
    requested_by: str
    currency: str = "USD"
    created_at: str = ""


class SpendLedger:
    """Budgets and spend entries, persisted as one JSON object."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._budgets: dict[str, float] = {}
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "SpendLedger":
        ledger = cls(path)
        if not ledger.path.exists():
            return ledger
        try:
            raw = json.loads(ledger.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt spend ledger {ledger.path}: {exc}") from exc
        if not isinstance(raw, dict) or "budgets" not in raw or "entries" not in raw:
            raise ValueError(
                f"spend ledger {ledger.path} must be an object with "
                "'budgets' and 'entries'"
            )
        ledger._budgets = {k: float(v) for k, v in raw["budgets"].items()}
        ledger._entries = [dict(e) for e in raw["entries"]]
        return ledger

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"budgets": self._budgets, "entries": self._entries}, indent=2
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

    def entries(self) -> list[dict]:
        return list(self._entries)

    def _add(self, entry: dict) -> None:
        self._entries.append(entry)

    def _sum(self, name: str, kind: str) -> float:
        return round(
            sum(
                e["amount"]
                for e in self._entries
                if e["candidate_name"] == name and e["type"] == kind
            ),
            2,
        )

    def budget_for(self, name: str) -> float:
        return self._budgets.get(name, 0.0)

    def authorized_for(self, name: str) -> float:
        return self._sum(name, "authorized")

    def spent_for(self, name: str) -> float:
        return self._sum(name, "spent")

    def total_spent(self) -> float:
        return round(
            sum(e["amount"] for e in self._entries if e["type"] == "spent"), 2
        )


def set_budget(
    ledger: SpendLedger, name: str, amount: float, *, approver: str
) -> float:
    """Human raises (or sets) a candidate's spend cap. Only path that can."""
    if amount < 0:
        raise ValueError("budget must not be negative")
    ledger._budgets[name] = round(float(amount), 2)
    ledger._add(
        {
            "type": "budget_set",
            "candidate_name": name,
            "amount": round(float(amount), 2),
            "actor": approver,
            "ts": now_iso(),
        }
    )
    ledger.save()
    return ledger._budgets[name]


def authorize_spend(
    ledger: SpendLedger,
    request: SpendRequest,
    *,
    approver: str,
    ceiling: float = DEFAULT_CEILING,
) -> dict:
    if request.amount <= 0:
        raise ValueError("spend amount must be positive")
    if request.amount > ceiling:
        raise ValueError(
            f"amount {request.amount} exceeds ceiling {ceiling}; "
            "a human must raise the ceiling"
        )
    name = request.candidate_name
    if ledger.authorized_for(name) + request.amount > ledger.budget_for(name):
        raise ValueError(
            f"amount {request.amount} exceeds remaining budget for {name!r} "
            f"(budget={ledger.budget_for(name)}, "
            f"authorized={ledger.authorized_for(name)})"
        )
    entry = {
        "type": "authorized",
        "candidate_name": name,
        "amount": round(float(request.amount), 2),
        "purpose": request.purpose,
        "actor": approver,
        "ts": now_iso(),
    }
    ledger._add(entry)
    ledger.save()
    return entry


def deny_spend(
    ledger: SpendLedger, request: SpendRequest, *, approver: str, reason: str
) -> dict:
    entry = {
        "type": "denied",
        "candidate_name": request.candidate_name,
        "amount": round(float(request.amount), 2),
        "purpose": request.purpose,
        "reason": reason,
        "actor": approver,
        "ts": now_iso(),
    }
    ledger._add(entry)
    ledger.save()
    return entry


def record_spend(
    ledger: SpendLedger, name: str, amount: float, *, actor: str, note: str = ""
) -> dict:
    """Log money the human has already spent. Authorizes nothing."""
    if amount <= 0:
        raise ValueError("spend amount must be positive")
    if ledger.spent_for(name) + amount > ledger.authorized_for(name):
        raise ValueError(
            f"amount {amount} exceeds authorized spend for {name!r} "
            f"(authorized={ledger.authorized_for(name)}, "
            f"spent={ledger.spent_for(name)})"
        )
    entry = {
        "type": "spent",
        "candidate_name": name,
        "amount": round(float(amount), 2),
        "note": note,
        "actor": actor,
        "ts": now_iso(),
    }
    ledger._add(entry)
    ledger.save()
    return entry
