"""Revenue ledger and the human-driven launch / payment record points.

The system never moves money. mark_launched() records that the human
put an offer live; record_payment() logs a payment the human has
already received elsewhere. Both are human input points.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import lifecycle
from .store import Candidate, CandidateStore, now_iso


class RevenueLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "RevenueLedger":
        ledger = cls(path)
        if not ledger.path.exists():
            return ledger
        try:
            raw = json.loads(ledger.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt revenue ledger {ledger.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"revenue ledger {ledger.path} must contain a JSON list")
        ledger._entries = [dict(e) for e in raw]
        return ledger

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
        self._entries.append(entry)

    def total_for(self, name: str) -> float:
        return round(
            sum(e["amount"] for e in self._entries if e["candidate_name"] == name), 2
        )

    def total(self) -> float:
        return round(sum(e["amount"] for e in self._entries), 2)

    def first_payment_at(self, name: str) -> str | None:
        for entry in self._entries:
            if entry["candidate_name"] == name:
                return entry["received_at"]
        return None


def mark_launched(
    store: CandidateStore, name: str, *, actor: str, note: str = ""
) -> Candidate:
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    updated = lifecycle.advance(candidate, "launched", note=note, actor=actor)
    store.put(updated)
    store.save()
    return updated


def record_payment(
    store: CandidateStore,
    ledger: RevenueLedger,
    name: str,
    amount: float,
    *,
    actor: str,
    currency: str = "USD",
    note: str = "",
    received_at: str | None = None,
) -> Candidate:
    if amount <= 0:
        raise ValueError("amount must be positive")
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    if candidate.status not in ("launched", "earning"):
        raise ValueError(
            f"candidate {name!r} is {candidate.status!r}; must be launched or earning"
        )

    ledger.add(
        {
            "candidate_name": name,
            "amount": round(float(amount), 2),
            "currency": currency,
            "received_at": received_at or now_iso(),
            "note": note,
            "actor": actor,
        }
    )
    ledger.save()

    if candidate.status == "launched":
        candidate = lifecycle.advance(
            candidate, "earning", note="first payment recorded", actor=actor
        )
        store.put(candidate)
        store.save()
    return candidate


def revenue_summary(store: CandidateStore, ledger: RevenueLedger) -> dict:
    per_candidate = {}
    for cand in store.all():
        total = ledger.total_for(cand.name)
        if total == 0.0 and cand.status not in ("launched", "earning"):
            continue
        per_candidate[cand.name] = {
            "status": cand.status,
            "total": total,
            "first_revenue": total > 0.0,
        }
    return {"grand_total": ledger.total(), "candidates": per_candidate}
