"""Post-payment customer intake for the Customer Launch Plan.

After paying, the buyer submits their business details through a static
form (built by build-checkout) that POSTs to an operator-configured form
provider. The operator exports those submissions as JSON and runs
`revenue_os intake-import`, which stores each one in data/intake.json
ONLY when its capture_id matches a real booked PayPal payment for the
candidate (see revenue.json / `paypal-sync`).

No server, no webhook, no secrets. Standard library only.

data/intake.json holds personal data. data/ is gitignored; do not commit
or share it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .revenue import RevenueLedger
from .store import now_iso

# form field name -> human label. Order is the render / display order.
INTAKE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Your name"),
    ("email", "Email"),
    ("business", "Business name / website"),
    ("sells", "What you sell"),
    ("current_price", "Current price"),
    ("target_audience", "Target audience"),
    ("customer_situation", "Current customer situation"),
    ("previous_attempts", "Previous customer-acquisition attempts"),
    ("biggest_problem", "Biggest customer-acquisition problem"),
)
FIELD_KEYS: tuple[str, ...] = tuple(k for k, _ in INTAKE_FIELDS)
_REQUIRED = ("name", "email", "sells")


class IntakeStore:
    """One JSON list, atomically written, keyed by order_id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_order: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "IntakeStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt intake store {store.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"intake store {store.path} must contain a JSON list")
        for entry in raw:
            store._by_order[str(entry["order_id"])] = dict(entry)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(self._by_order.values()), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, order_id: str) -> dict | None:
        return self._by_order.get(str(order_id))

    def all(self) -> list[dict]:
        return list(self._by_order.values())

    def add(self, order_id: str, candidate: str, fields: dict, *,
            capture_id: str = "", source: str = "import",
            submitted_at: str | None = None) -> dict:
        oid = str(order_id).strip()
        if not oid:
            raise ValueError("order_id is required")
        clean = {k: str(fields.get(k, "")).strip() for k in FIELD_KEYS}
        missing = [k for k in _REQUIRED if not clean[k]]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")
        entry = {
            "order_id": oid,
            "capture_id": str(capture_id).strip(),
            "candidate": str(candidate),
            "submitted_at": submitted_at or now_iso(),
            "imported_at": now_iso(),
            "status": "new",
            "source": source,
            "fields": clean,
        }
        self._by_order[oid] = entry
        return entry

    def mark_reviewed(self, order_id: str, *, actor: str) -> dict:
        entry = self._by_order.get(str(order_id))
        if entry is None:
            raise ValueError(f"no intake for order {order_id!r}")
        entry["status"] = "reviewed"
        entry["reviewed_by"] = actor
        entry["reviewed_at"] = now_iso()
        return entry

    def attach_plan(self, order_id: str, plan: dict) -> dict:
        """Store a drafted Customer Launch Plan (status 'draft'). The human
        owner still approves it before it is rendered for delivery."""
        entry = self._by_order.get(str(order_id))
        if entry is None:
            raise ValueError(f"no intake for order {order_id!r}")
        entry["plan"] = {**dict(plan), "status": "draft"}
        entry["plan_drafted_at"] = now_iso()
        return entry

    def approve_plan(self, order_id: str, *, actor: str) -> dict:
        entry = self._by_order.get(str(order_id))
        if entry is None:
            raise ValueError(f"no intake for order {order_id!r}")
        plan = entry.get("plan")
        if not isinstance(plan, dict):
            raise ValueError(f"order {order_id!r} has no drafted plan to approve")
        plan["status"] = "approved"
        plan["approved_by"] = actor
        plan["approved_at"] = now_iso()
        return entry


def _booked_paypal(ledger: RevenueLedger) -> dict[str, str]:
    """capture-id -> candidate_name, for every booked paypal:<id> payment."""
    out: dict[str, str] = {}
    for entry in ledger.entries():
        ref = str(entry.get("ref", ""))
        if ref.startswith("paypal:"):
            out[ref.split(":", 1)[1]] = entry["candidate_name"]
    return out


def _row_get(row: dict, *names: str) -> str:
    for n in names:
        if row.get(n):
            return str(row[n]).strip()
    return ""


def import_submissions(intake: IntakeStore, ledger: RevenueLedger,
                       rows: list[dict], *, candidate: str | None = None) -> dict:
    """Store form submissions that correspond to a real booked payment.

    Each row may be a flat dict of field names, or carry a nested
    `fields` dict. It must identify the payment via `capture_id`
    (alias `capture`) - matched against booked `paypal:<id>` refs.
    Returns {"stored": [...], "skipped": [{row, reason, ...}]}.
    """
    booked = _booked_paypal(ledger)
    stored: list[dict] = []
    skipped: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped.append({"row": i, "reason": "not an object"})
            continue
        cap = _row_get(row, "capture_id", "capture")
        oid = _row_get(row, "order_id", "order") or cap
        want = (_row_get(row, "candidate") or candidate or "").strip()
        if not cap:
            skipped.append({"row": i, "reason": "no capture_id", "order_id": oid})
            continue
        if cap not in booked:
            skipped.append({"row": i, "order_id": oid,
                            "reason": f"capture {cap!r} is not a booked payment"})
            continue
        owner = booked[cap]
        if want and want != owner:
            skipped.append({"row": i, "order_id": oid,
                            "reason": f"capture {cap!r} belongs to {owner!r}, "
                                      f"not {want!r}"})
            continue
        fields = row["fields"] if isinstance(row.get("fields"), dict) else row
        try:
            entry = intake.add(oid, owner, fields, capture_id=cap,
                               submitted_at=_row_get(row, "submitted_at") or None)
        except ValueError as exc:
            skipped.append({"row": i, "order_id": oid, "reason": str(exc)})
            continue
        stored.append(entry)
    if stored:
        intake.save()
    return {"stored": stored, "skipped": skipped}
