"""The MONEY / IDENTITY / LEGAL approval firewall.

When the autonomous loop meets an action classified as
MONEY_APPROVAL_REQUIRED / IDENTITY_APPROVAL_REQUIRED /
LEGAL_APPROVAL_REQUIRED it does NOT perform it. It files a structured
request here and keeps working on everything else.

One JSON file: <data-dir>/approvals.json - three lists (money, identity,
legal), each a list of request dicts. Atomic write. No I/O beyond disk.

A human resolves a request (approve / deny) from JARVIS or the CLI. An
*approved* request is a record only - it never itself moves money; the
human still performs the money/identity/legal act. `granted()` lets the
loop see which requests were approved so it can stop re-filing them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import now_iso

_KINDS = ("money", "identity", "legal")


def _rid(kind: str, key: str) -> str:
    import hashlib
    return kind[0] + hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:11]


@dataclass
class ApprovalRequest:
    id: str
    kind: str                      # money | identity | legal
    what: str                      # one line: the action
    why: str                       # why the loop wants it
    opportunity: str = ""          # opportunity id it belongs to
    status: str = "pending"        # pending | approved | denied
    created_at: str = ""
    decided_at: str = ""
    decided_by: str = ""
    decision_note: str = ""
    # money-only fields
    amount: float | None = None
    currency: str = "EUR"
    expected_benefit: str = ""
    downside: str = ""
    recommended_max_budget: float | None = None
    expected_roi: str = ""
    necessity: str = "optional"    # necessary | optional
    financial_effect: str = ""     # WHY this counts as a money action
    # identity/legal-only fields
    boundary: str = ""             # kyc | age | bank | paypal | signature | tax | ...
    what_happens_after: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


class ApprovalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_kind: dict[str, list[dict]] = {k: [] for k in _KINDS}

    @classmethod
    def load(cls, path: str | Path) -> "ApprovalStore":
        s = cls(path)
        if not s.path.exists():
            return s
        try:
            raw = json.loads(s.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return s
        if isinstance(raw, dict):
            for k in _KINDS:
                v = raw.get(k)
                if isinstance(v, list):
                    s._by_kind[k] = [x for x in v if isinstance(x, dict)]
        return s

    def save(self) -> None:
        payload = json.dumps(self._by_kind, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- filing ----------------------------------------------------
    def _upsert(self, kind: str, key: str, fields: dict) -> dict:
        rid = _rid(kind, key)
        lst = self._by_kind[kind]
        existing = next((r for r in lst if r.get("id") == rid), None)
        if existing is not None:
            # keep a human decision; refresh the rationale otherwise
            if existing.get("status") == "pending":
                existing.update({k: v for k, v in fields.items()
                                 if v not in (None, "")})
            return existing
        req = ApprovalRequest(id=rid, kind=kind, created_at=now_iso(), **fields)
        d = req.to_dict()
        lst.append(d)
        return d

    def request_money(self, *, key: str, what: str, why: str,
                      amount: float | None = None, opportunity: str = "",
                      currency: str = "EUR", expected_benefit: str = "",
                      downside: str = "", recommended_max_budget: float | None = None,
                      expected_roi: str = "", necessity: str = "optional",
                      what_happens_after: str = "",
                      fees: bool = False, recurring: bool = False,
                      creates_payment_obligation: bool = False,
                      future_commitment: bool = False,
                      requires_payment_method: bool = False) -> dict:
        """File a MONEY approval request - BUT only when the action actually
        has a financial effect. A nominal amount of 0 with no fees /
        subscription / obligation is NOT a money action; this returns a
        `{"status": "not_required"}` marker and files nothing, so the fleet
        proceeds autonomously."""
        from .action_class import has_financial_effect

        eff, reason = has_financial_effect({
            "amount": amount, "fees": fees, "recurring": recurring,
            "creates_payment_obligation": creates_payment_obligation,
            "future_commitment": future_commitment,
            "requires_payment_method": requires_payment_method,
        })
        if not eff:
            return {"status": "not_required", "key": key, "what": what,
                    "reason": "no real cost / fee / subscription / obligation - "
                              "this action is autonomous"}
        return self._upsert("money", key, dict(
            what=what, why=why,
            amount=(round(float(amount), 2) if amount not in (None, 0, 0.0) else None),
            opportunity=opportunity, currency=currency,
            expected_benefit=expected_benefit, downside=downside,
            recommended_max_budget=recommended_max_budget, expected_roi=expected_roi,
            necessity=necessity, financial_effect=reason,
            what_happens_after=what_happens_after
            or "the fleet resumes the money-gated step for this opportunity"))

    def request_identity(self, *, key: str, what: str, why: str, boundary: str,
                         opportunity: str = "", what_happens_after: str = "") -> dict:
        return self._upsert("identity", key, dict(
            what=what, why=why, boundary=boundary, opportunity=opportunity,
            what_happens_after=what_happens_after
            or "the fleet resumes the identity-gated step for this opportunity"))

    def request_legal(self, *, key: str, what: str, why: str, boundary: str,
                      opportunity: str = "", what_happens_after: str = "") -> dict:
        return self._upsert("legal", key, dict(
            what=what, why=why, boundary=boundary, opportunity=opportunity,
            what_happens_after=what_happens_after
            or "the fleet resumes the legal-gated step for this opportunity"))

    # --- resolving -----------------------------------------------
    def get(self, request_id: str) -> dict | None:
        for lst in self._by_kind.values():
            for r in lst:
                if r.get("id") == request_id:
                    return r
        return None

    def withdraw_for_opportunity(self, opportunity: str) -> int:
        """Drop still-pending requests tied to an abandoned opportunity."""
        n = 0
        for k in _KINDS:
            keep = []
            for r in self._by_kind[k]:
                if (r.get("opportunity") == opportunity
                        and r.get("status") == "pending"):
                    n += 1
                    continue
                keep.append(r)
            self._by_kind[k] = keep
        return n

    def decide(self, request_id: str, decision: str, *, by: str,
               note: str = "") -> dict:
        if decision not in ("approved", "denied"):
            raise ValueError("decision must be 'approved' or 'denied'")
        r = self.get(request_id)
        if r is None:
            raise ValueError(f"unknown approval request {request_id!r}")
        r["status"] = decision
        r["decided_at"] = now_iso()
        r["decided_by"] = str(by or "human")
        if note:
            r["decision_note"] = str(note)
        return r

    # --- views ---------------------------------------------------
    def pending(self, kind: str | None = None) -> list[dict]:
        kinds = (kind,) if kind else _KINDS
        return [r for k in kinds for r in self._by_kind[k]
                if r.get("status") == "pending"]

    def status_of(self, kind: str, key: str) -> str:
        """'pending' | 'approved' | 'denied' | 'none' for a loop-side key."""
        r = self.get(_rid(kind, key))
        return r.get("status", "none") if r else "none"

    def granted_ids(self, kind: str) -> set[str]:
        return {r["id"] for r in self._by_kind[kind]
                if r.get("status") == "approved"}

    def all(self, kind: str | None = None) -> list[dict]:
        kinds = (kind,) if kind else _KINDS
        return [r for k in kinds for r in self._by_kind[k]]

    def counts(self) -> dict:
        out = {}
        for k in _KINDS:
            lst = self._by_kind[k]
            out[k] = {
                "pending": sum(1 for r in lst if r.get("status") == "pending"),
                "approved": sum(1 for r in lst if r.get("status") == "approved"),
                "denied": sum(1 for r in lst if r.get("status") == "denied"),
            }
        return out


def request_id(kind: str, key: str) -> str:
    """The stable id the loop would file for (kind, key)."""
    return _rid(kind, key)


def load_approvals(data_dir: str | Path) -> ApprovalStore:
    return ApprovalStore.load(Path(data_dir) / "approvals.json")
