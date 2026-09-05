"""Affiliate commission lifecycle -> the real revenue ledger (spec 11 + 12).

PENDING -> CONFIRMED -> (booked into the SAME ledger record_task_outcome
already uses) -> PAID, or PENDING -> REVERSED (never booked).

Booking happens exactly once, on the CONFIRMED transition, via the
existing, unmodified `revenue.record_opportunity_payment()` - the same
idempotent-by-ref function the TASK strategy already uses for a
human-confirmed real payment. This module adds NO new ledger, no new
money-movement code path: it is a thin, affiliate-specific caller of
code that is already tested and already gated (see
`revenue.record_payment`'s `guard_no_money_in_autonomy` - note that
`record_opportunity_payment` itself, like the TASK path, records an
ALREADY-RECEIVED payment; it does not move money).

Known limitation (documented, not silently handled): a commission
REVERSED *after* it was already CONFIRMED and booked is recorded here
(status flips to REVERSED, a REVERSED-outcome learning row is written)
but the ledger entry itself is not clawed back - `RevenueLedger` has no
negative-amount / reversal primitive. A human reconciling books should
treat a post-confirmation reversal as requiring manual correction.

Every settled commission - win or loss - feeds `learning.record_outcome`
(the SAME generic learning loop the TASK strategy uses), keyed by
`strategy="AFFILIATE"` and `category=offer.category` so
`priority_weights()` can already rank affiliate categories/sources
against every other strategy with zero new learning code (spec 12: reuse,
don't rebuild).
"""

from __future__ import annotations

from . import learning, model
from .affiliate_model import (
    COMMISSION_CONFIRMED,
    COMMISSION_PAID,
    COMMISSION_PENDING,
    COMMISSION_REVERSED,
    COMMISSION_STATUSES,
    CommissionRecord,
    CommissionStore,
    new_id,
)


class AffiliateRevenueError(ValueError):
    pass


def record_pending_commission(data_dir, *, link_id: str, opportunity_id: str,
                              offer_id: str, amount: float, currency: str = "EUR",
                              is_estimate: bool = True, note: str = "",
                              now_iso: str = "") -> CommissionRecord:
    """A projected/reported-but-unconfirmed commission (e.g. the network's
    own dashboard shows it as 'pending'). Never booked to the ledger -
    `expected_revenue` in JARVIS/CLI output, not `revenue`."""
    store = CommissionStore.load(data_dir)
    rec = CommissionRecord(commission_id=new_id("comm"), link_id=link_id,
                           opportunity_id=opportunity_id, offer_id=offer_id,
                           status=COMMISSION_PENDING, amount=round(max(0.0, amount), 2),
                           currency=currency, is_estimate=is_estimate, note=note,
                           recorded_at=now_iso)
    store.upsert(rec)
    store.save()
    return rec


def confirm_commission(data_dir, commission_id: str, *, ref: str, amount: float | None = None,
                       actor: str = "human", now_iso: str = "") -> dict:
    """The network has confirmed this commission really happened (a
    settled, real fact - not a projection). Books it into the real
    revenue ledger, idempotently by `ref`, then feeds a WIN into the
    learning loop. `amount` overrides the pending estimate only if the
    confirmed amount differs (networks sometimes adjust it)."""
    from ..revenue import RevenueLedger, record_opportunity_payment
    from pathlib import Path

    store = CommissionStore.load(data_dir)
    rec = store.get(commission_id)
    if rec is None:
        raise AffiliateRevenueError(f"unknown commission {commission_id!r}")
    if rec.status not in (COMMISSION_PENDING,):
        raise AffiliateRevenueError(
            f"commission {commission_id!r} is {rec.status!r}, can only confirm from PENDING")
    if not ref:
        raise AffiliateRevenueError("a stable provider reference (--ref) is required")

    final_amount = float(amount) if amount is not None else rec.amount
    if final_amount <= 0:
        raise AffiliateRevenueError("confirmed amount must be positive")

    ledger = RevenueLedger.load(Path(data_dir) / "revenue.json")
    if ledger.has_ref(ref):
        booked_outcome = "already_booked"
    else:
        booked = record_opportunity_payment(
            ledger, opportunity_id=rec.opportunity_id, amount=final_amount, ref=ref,
            currency=rec.currency, note=f"affiliate commission ({rec.offer_id})", actor=actor)
        booked_outcome = booked["outcome"]

    rec.status = COMMISSION_CONFIRMED
    rec.amount = round(final_amount, 2)
    rec.is_estimate = False
    rec.ref = ref
    rec.recorded_at = now_iso
    store.upsert(rec)
    store.save()

    from .affiliate_model import AffiliateLinkStore
    link_store = AffiliateLinkStore.load(data_dir)
    link = link_store.get(rec.link_id)
    if link is not None:
        link.conversion_count += 1
        link.commission_eur += final_amount
        link.revenue_eur += final_amount
        link_store.upsert(link)
        link_store.save()

    offer_category = ""
    from .affiliate_model import AffiliateOfferStore
    offer = AffiliateOfferStore.load(data_dir).get(rec.offer_id)
    if offer is not None:
        offer_category = offer.category

    learning.record_outcome(data_dir, learning.Outcome(
        opportunity_id=rec.opportunity_id, strategy=model.STRAT_AFFILIATE,
        source="affiliate", category=offer_category or "other",
        opportunity_type=model.TYPE_AFFILIATE, distribution_channel=link.source if link else "",
        cost_eur=0.0, revenue_eur=final_amount, success=True, settled=True))

    return {"commission_id": rec.commission_id, "status": rec.status,
           "amount": rec.amount, "ref": ref, "ledger_outcome": booked_outcome}


def reverse_commission(data_dir, commission_id: str, *, note: str = "",
                       now_iso: str = "") -> dict:
    """The network reversed this commission (return, fraud check, chargeback
    upstream) - records a settled LOSS in learning. See the module
    docstring's documented limitation re: post-confirmation reversal."""
    store = CommissionStore.load(data_dir)
    rec = store.get(commission_id)
    if rec is None:
        raise AffiliateRevenueError(f"unknown commission {commission_id!r}")
    if rec.status == COMMISSION_REVERSED:
        return {"commission_id": rec.commission_id, "status": rec.status, "outcome": "already_reversed"}

    was_confirmed = rec.status in (COMMISSION_CONFIRMED, COMMISSION_PAID)
    rec.status = COMMISSION_REVERSED
    rec.note = note or rec.note
    rec.recorded_at = now_iso
    store.upsert(rec)
    store.save()

    from .affiliate_model import AffiliateOfferStore
    offer = AffiliateOfferStore.load(data_dir).get(rec.offer_id)

    learning.record_outcome(data_dir, learning.Outcome(
        opportunity_id=rec.opportunity_id, strategy=model.STRAT_AFFILIATE,
        source="affiliate", category=(offer.category if offer else "other"),
        opportunity_type=model.TYPE_AFFILIATE, cost_eur=0.0, revenue_eur=0.0,
        success=False, failure_reason=note or "commission reversed", settled=True))

    return {"commission_id": rec.commission_id, "status": rec.status,
           "outcome": "reversed", "was_previously_confirmed": was_confirmed,
           "ledger_note": ("this commission was already booked to the ledger; "
                           "no automatic claw-back was performed - a human must "
                           "reconcile") if was_confirmed else ""}


def mark_paid(data_dir, commission_id: str, *, now_iso: str = "") -> dict:
    """The network actually transferred the money (distinct from
    CONFIRMED, which only means the sale/commission itself is settled -
    payout timing is often separate, e.g. monthly)."""
    store = CommissionStore.load(data_dir)
    rec = store.get(commission_id)
    if rec is None:
        raise AffiliateRevenueError(f"unknown commission {commission_id!r}")
    if rec.status != COMMISSION_CONFIRMED:
        raise AffiliateRevenueError(
            f"commission {commission_id!r} is {rec.status!r}, can only mark PAID from CONFIRMED")
    rec.status = COMMISSION_PAID
    rec.recorded_at = now_iso
    store.upsert(rec)
    store.save()
    return {"commission_id": rec.commission_id, "status": rec.status}


def opportunity_commission_summary(data_dir, opportunity_id: str) -> dict:
    """Read-only rollup, split by settled-vs-not so a caller can never
    confuse a hopeful PENDING number with real revenue (spec 11)."""
    from .affiliate_model import SETTLED_COMMISSION_STATUSES

    recs = CommissionStore.load(data_dir).by_opportunity(opportunity_id)
    pending = [r for r in recs if r.status == COMMISSION_PENDING]
    settled = [r for r in recs if r.status in SETTLED_COMMISSION_STATUSES]
    reversed_ = [r for r in recs if r.status == COMMISSION_REVERSED]
    return {
        "opportunity_id": opportunity_id,
        "pending_estimated_eur": round(sum(r.amount for r in pending), 2),
        "confirmed_or_paid_eur": round(sum(r.amount for r in settled), 2),
        "reversed_count": len(reversed_),
        "commissions": [r.to_dict() for r in recs],
    }
