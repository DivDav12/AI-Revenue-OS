"""Revenue-experiment ledger (Phase 3).

One row per prospect outreach *attempt*, keyed by `lead_id` (at most one
experiment per lead, ever). It records the offer under test and its
outcome so the continuous loop and the dashboard can answer "which
source / price actually converts?".

Lifecycle:
    drafted -> posted  -> intake -> sale
    drafted -> skipped
    posted  -> no_sale            (the sweep, after the follow-up window)

Everything here is deterministic and read-only w.r.t. the outside world:
it reads the candidate store, the outreach store, the intake store and
the revenue ledger - all local JSON. It NEVER calls PayPal, an LLM, the
network, or anything that posts / sends / contacts a person. It performs
no autonomous "act" - it only tracks what the rest of the system already
did.

Phase 2.6 (outreach feedback loop) adds, still deterministically and
without any outside call:
  - the lead's quality / type / age bucket / relevance at outreach time
    are captured on the experiment row (from the brief);
  - an optional human `reason` string on a status change;
  - `feedback()` - a read-only aggregation of SETTLED experiments
    (sale vs no_sale) by source / quality / type. It only REPORTS; it
    computes no weights and changes no lead score, query or source. It
    stays "not ready" until >=8 settled outcomes with both classes are
    present (the same gate calibration.py uses) and even then nothing
    consumes it automatically;
  - `sync_lead_backrefs()` - best-effort annotation of the acquisition
    lead (local JSON) with a compact `outreach_outcome` breadcrumb. It
    never touches `human_review_status`, the score, or the acquisition
    queue's own dedup.

State: data/experiments.json (atomic write, restart-safe).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import now_iso

STATUSES = ("drafted", "posted", "skipped", "intake", "sale", "no_sale")
_OPEN = ("drafted", "posted", "intake")
_CLOSED = ("skipped", "sale", "no_sale")
_SETTLED = ("sale", "no_sale")       # a posted attempt whose outcome resolved
_MIN_SETTLED = 8                     # feedback gate - mirrors calibration.py

# allowed transitions (a closed experiment is terminal)
_NEXT: dict[str, tuple[str, ...]] = {
    "drafted": ("posted", "skipped", "sale"),
    "posted": ("intake", "skipped", "no_sale", "sale"),
    "intake": ("sale", "no_sale"),
}

_DISCOVERY_COOLDOWN_H = 6.0          # documented default, overridable
_FOLLOWUP_DAYS = 14.0               # posted -> no_sale after this, <=0 disables


# --- helpers -----------------------------------------------------------

def _load_list(path: Path) -> list:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


def _parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _age_days(created: str, *, now: datetime | None = None) -> int | None:
    dt = _parse_iso(created)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - dt).days)


# --- the store -------------------------------------------------------

class ExperimentStore:
    """One JSON list, atomically written, keyed by lead_id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_lead: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "ExperimentStore":
        store = cls(path)
        if not store.path.exists():
            return store
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"experiment store {store.path} must be a JSON list")
        for e in raw:
            store._by_lead[str(e["lead_id"])] = dict(e)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.ranked(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, lead_id: str) -> dict | None:
        return self._by_lead.get(str(lead_id))

    def all(self) -> list[dict]:
        return list(self._by_lead.values())

    def ranked(self) -> list[dict]:
        order = {s: i for i, s in enumerate(
            ("sale", "intake", "posted", "drafted", "no_sale", "skipped"))}
        return sorted(self._by_lead.values(),
                      key=lambda e: (order.get(e.get("status"), 9),
                                     e.get("created_at", "")))

    def open(self, lead_id: str, *, candidate: str, offer_price,
             currency: str, source: str, platform: str,
             checkout_url: str = "", prospect_quality: str = "",
             prospect_type: str = "", age_bucket: str = "",
             relevance_score=None, now: str | None = None) -> str:
        """Insert a `drafted` experiment. No-op if the lead already has one
        (open OR closed - one experiment per lead, ever). Returns
        'opened' | 'exists'.

        `prospect_quality` / `prospect_type` / `age_bucket` /
        `relevance_score` are the lead's state at outreach time (copied
        from the brief); they are optional and default to empty so older
        callers and older stored rows keep working unchanged."""
        lid = str(lead_id or "").strip()
        if not lid:
            raise ValueError("open() needs a lead_id")
        if lid in self._by_lead:
            return "exists"
        ts = now or now_iso()
        self._by_lead[lid] = {
            "lead_id": lid,
            "candidate": str(candidate or ""),
            "offer_price": offer_price,
            "currency": str(currency or "EUR"),
            "source": str(source or ""),
            "platform": str(platform or ""),
            "checkout_url": str(checkout_url or ""),
            "prospect_quality": str(prospect_quality or ""),
            "prospect_type": str(prospect_type or ""),
            "age_bucket": str(age_bucket or ""),
            "relevance_score": relevance_score,
            "status": "drafted",
            "created_at": ts,
            "posted_at": None,
            "outcome_at": None,
            "revenue_ref": "",
            "note": "",
        }
        return "opened"

    def advance(self, lead_id: str, status: str, *, note: str = "",
                reason: str = "", revenue_ref: str = "",
                now: str | None = None) -> dict:
        entry = self._by_lead.get(str(lead_id))
        if entry is None:
            raise ValueError(f"no experiment for lead {lead_id!r}")
        return self._advance_entry(entry, status, note=note, reason=reason,
                                   revenue_ref=revenue_ref, now=now)

    def _advance_entry(self, entry: dict, status: str, *, note: str = "",
                       reason: str = "", revenue_ref: str = "",
                       now: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        cur = entry["status"]
        if cur == status:
            return entry
        if cur in _CLOSED:
            raise ValueError(
                f"experiment for {entry['lead_id']} is closed ({cur}); "
                f"cannot move to {status}")
        if status not in _NEXT.get(cur, ()):
            raise ValueError(f"invalid transition {cur} -> {status}")
        ts = now or now_iso()
        entry["status"] = status
        if status == "posted":
            entry["posted_at"] = ts
        if status in _CLOSED:
            entry["outcome_at"] = ts
        if revenue_ref:
            entry["revenue_ref"] = revenue_ref
        if note:
            entry["note"] = note
        if reason:
            entry["reason"] = str(reason)
        return entry


# --- the five module operations -------------------------------------

def _launched_candidate(data_dir: Path):
    from .store import CandidateStore
    store = CandidateStore.load(data_dir / "candidates.json")
    for c in store.all():
        if c.status in ("launched", "earning") and c.offer:
            return c
    return None


def open_from_briefs(data_dir, *, now: str | None = None) -> dict:
    """Open a `drafted` experiment for every outreach brief that does not
    have one yet. Offer / price / candidate come from the launched
    candidate; source / platform from the brief. Deterministic; no network.
    """
    data_dir = Path(data_dir)
    from .outreach import OutreachStore

    briefs = OutreachStore.load(data_dir / "outreach.json").all()
    if not briefs:
        return {"opened": 0, "existing": 0}

    cand = _launched_candidate(data_dir)
    cand_name = cand.name if cand else ""
    price = cand.offer.get("price") if cand else None
    currency = (cand.offer.get("currency", "EUR") if cand else "EUR")

    exp = ExperimentStore.load(data_dir / "experiments.json")
    opened = existing = 0
    for b in briefs:
        brief = b.get("brief") or {}
        lid = str(b.get("lead_id") or brief.get("lead_id") or "").strip()
        if not lid:
            continue
        outcome = exp.open(
            lid, candidate=cand_name, offer_price=price, currency=currency,
            source=str(brief.get("source") or ""),
            platform=str(brief.get("platform") or ""),
            checkout_url=str(brief.get("checkout_link") or ""),
            prospect_quality=str(brief.get("prospect_quality") or ""),
            prospect_type=str(brief.get("prospect_type") or ""),
            age_bucket=str(brief.get("age_bucket") or ""),
            relevance_score=brief.get("relevance_score"),
            now=now)
        opened += outcome == "opened"
        existing += outcome == "exists"
    if opened:
        exp.save()
    return {"opened": opened, "existing": existing}


def advance(data_dir, lead_id: str, status: str, *, note: str = "",
            reason: str = "", revenue_ref: str = "") -> dict:
    """Move one experiment forward (used by `outreach-status` and
    `experiment-close`). Invalid transitions raise ValueError. `reason` is
    an optional human note stored alongside the system `note`."""
    data_dir = Path(data_dir)
    exp = ExperimentStore.load(data_dir / "experiments.json")
    entry = exp.advance(lead_id, status, note=note, reason=reason,
                        revenue_ref=revenue_ref)
    exp.save()
    if entry.get("status") in _CLOSED:
        sync_lead_backrefs(data_dir)
    return entry


def correlate_sale(data_dir) -> dict:
    """Read-only join. An intake row only exists once its capture_id matched
    a BOOKED `paypal:<id>` payment, and it carries the acquisition `lead_id`.
    So: for each intake whose experiment is still open, mark `intake`, and
    mark `sale` (with revenue_ref) once the booked ledger entry is found.
    Never calls PayPal.
    """
    data_dir = Path(data_dir)
    from .intake import IntakeStore
    from .revenue import RevenueLedger

    intakes = IntakeStore.load(data_dir / "intake.json").all()
    if not intakes:
        return {"sale": 0, "intake": 0}

    booked: dict[str, dict] = {}
    for e in RevenueLedger.load(data_dir / "revenue.json").entries():
        ref = str(e.get("ref") or "")
        if ref.startswith("paypal:"):
            booked[ref.split("paypal:", 1)[1]] = e

    exp = ExperimentStore.load(data_dir / "experiments.json")
    sales = intakes_marked = 0
    changed = False
    for it in intakes:
        lid = str(it.get("lead_id") or "").strip()
        if not lid:
            continue
        entry = exp.get(lid)
        if entry is None or entry["status"] in _CLOSED:
            continue
        cap = str(it.get("capture_id") or "").strip()
        if cap and cap in booked:
            rev = booked[cap]
            if entry["status"] == "posted":
                exp._advance_entry(entry, "intake",
                                   note=f"intake {it.get('order_id')}")
            exp._advance_entry(
                entry, "sale", revenue_ref=f"paypal:{cap}",
                note=f"order {it.get('order_id')} "
                     f"{rev.get('amount')} {rev.get('currency', 'EUR')}")
            sales += 1
            changed = True
        elif entry["status"] == "posted":
            exp._advance_entry(entry, "intake",
                               note=f"intake {it.get('order_id')} (payment pending)")
            intakes_marked += 1
            changed = True
    if changed:
        exp.save()
        sync_lead_backrefs(data_dir)
    return {"sale": sales, "intake": intakes_marked}


def sweep(data_dir, *, now: datetime | None = None,
          followup_days: float = _FOLLOWUP_DAYS) -> dict:
    """Close `posted` experiments older than `followup_days` (measured from
    posted_at) that have no linked intake -> `no_sale`. `followup_days <= 0`
    disables the sweep entirely (a no-op)."""
    data_dir = Path(data_dir)
    if followup_days is not None and followup_days <= 0:
        return {"closed": 0, "disabled": True}

    from .intake import IntakeStore
    linked = {str(it.get("lead_id") or "").strip()
              for it in IntakeStore.load(data_dir / "intake.json").all()
              if it.get("lead_id")}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=float(followup_days))
    exp = ExperimentStore.load(data_dir / "experiments.json")
    closed = 0
    for entry in exp.all():
        if entry["status"] != "posted" or entry["lead_id"] in linked:
            continue
        posted = _parse_iso(entry.get("posted_at"))
        if posted is not None and posted < cutoff:
            exp._advance_entry(entry, "no_sale",
                               note=f"no sale within {int(followup_days)}d of posting",
                               now=now_iso())
            closed += 1
    if closed:
        exp.save()
        sync_lead_backrefs(data_dir)
    return {"closed": closed, "disabled": False}


def rollup(data_dir, *, now: datetime | None = None) -> dict:
    """Per-source and overall counts by status - for the dashboard / CLI."""
    data_dir = Path(data_dir)
    rows = ExperimentStore.load(data_dir / "experiments.json").ranked()
    overall = {s: 0 for s in STATUSES}
    by_source: dict[str, dict] = {}
    for e in rows:
        st = e.get("status", "drafted")
        overall[st] = overall.get(st, 0) + 1
        src = e.get("source") or "unknown"
        bucket = by_source.setdefault(src, {s: 0 for s in STATUSES})
        bucket[st] = bucket.get(st, 0) + 1
    return {
        "total": len(rows),
        "overall": overall,
        "by_source": by_source,
        "open": sum(overall[s] for s in _OPEN),
        "closed": sum(overall[s] for s in _CLOSED),
        "rows": [
            {"source": e.get("source") or "unknown",
             "platform": e.get("platform") or "",
             "offer_price": e.get("offer_price"),
             "currency": e.get("currency") or "EUR",
             "status": e.get("status"),
             "age_days": _age_days(e.get("created_at"), now=now),
             "revenue_ref": e.get("revenue_ref") or ""}
            for e in rows
        ],
    }


# --- Phase 2.6: the outreach feedback view --------------------------

def feedback(data_dir, *, min_settled: int = _MIN_SETTLED) -> dict:
    """Deterministic, read-only aggregation of SETTLED outreach
    experiments (sale vs no_sale) by source / prospect quality / prospect
    type.

    It REPORTS conversion counts only. It computes no weights and changes
    no lead score, query, or source ordering. `ready` stays False until at
    least `min_settled` settled experiments exist with BOTH outcomes
    present - the same ">=8 with both classes" gate calibration.py uses -
    and even then nothing consumes the result automatically. A human reads
    it and decides.
    """
    data_dir = Path(data_dir)
    rows = ExperimentStore.load(data_dir / "experiments.json").all()
    settled = [e for e in rows if e.get("status") in _SETTLED]
    sales = sum(1 for e in settled if e.get("status") == "sale")
    no_sales = len(settled) - sales

    def _dim(key: str, default: str = "unknown") -> dict:
        out: dict[str, dict] = {}
        for e in settled:
            k = str(e.get(key) or default)
            b = out.setdefault(k, {"settled": 0, "sale": 0, "no_sale": 0})
            b["settled"] += 1
            b[e["status"]] += 1
        for b in out.values():
            b["sale_rate"] = (round(b["sale"] / b["settled"], 3)
                              if b["settled"] else 0.0)
        return dict(sorted(out.items()))

    ready = len(settled) >= int(min_settled) and sales >= 1 and no_sales >= 1
    note = ("enough settled outcomes - but this is advisory only; no "
            "weighting, query or source change is applied automatically"
            if ready else
            f"not enough settled outcomes yet ({len(settled)}/{int(min_settled)}, "
            "both a sale and a no_sale required); lead scoring, queries and "
            "sources stay unchanged")
    return {
        "settled": len(settled),
        "needed": int(min_settled),
        "sale": sales,
        "no_sale": no_sales,
        "ready": ready,
        "advisory_only": True,
        "note": note,
        "by_source": _dim("source"),
        "by_quality": _dim("prospect_quality"),
        "by_type": _dim("prospect_type"),
    }


# --- Phase 2.6: acquisition-lead back-reference (additive breadcrumb) ---

def sync_lead_backrefs(data_dir) -> dict:
    """Best-effort: annotate each acquisition lead whose experiment has
    CLOSED with a compact `outreach_outcome` reference.

    Purely additive. It never changes `human_review_status`, the score,
    the age, or the acquisition queue's own dedup logic - it only leaves a
    breadcrumb a human and the dashboard can read. Local JSON only; no
    network, PayPal, or LLM. A failure here is swallowed: the experiment
    ledger is the source of truth, this is a convenience mirror.
    """
    data_dir = Path(data_dir)
    try:
        from .acquisition import AcquisitionStore
        store = AcquisitionStore.load(data_dir / "acquisition.json")
        if not store.all():
            return {"annotated": 0}
        exp = ExperimentStore.load(data_dir / "experiments.json")
        annotated = 0
        for e in exp.all():
            if e.get("status") not in _CLOSED:
                continue
            lid = str(e.get("lead_id") or "")
            lead = store.by_id(lid)
            if lead is None:
                continue
            ref = {
                "status": e["status"],
                "source": e.get("source", ""),
                "prospect_quality": e.get("prospect_quality", ""),
                "closed_at": e.get("outcome_at") or "",
                "revenue_ref": e.get("revenue_ref", ""),
            }
            if lead.get("outreach_outcome") != ref:
                store.record_outreach_outcome(lid, ref)
                annotated += 1
        if annotated:
            store.save()
        return {"annotated": annotated}
    except Exception:  # advisory mirror - never break the caller
        return {"annotated": 0}
