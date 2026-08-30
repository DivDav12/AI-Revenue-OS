"""Sales Tracker (#17, revenue cluster) - deterministic funnel state.

Counts the funnel from data the caller supplies. Payment truth is NOT
sourced here: `paid_count` is taken verbatim from `payment_events`
(what the existing PayPal/RevenueLedger integration already booked). It
calls no payment API, books nothing, and never fabricates a sale - a
missing payment feed yields `paid_count = 0` plus a note.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_QUALIFIED_STATUSES = ("reviewed", "approved", "qualified", "shortlisted")


def _rate(n, d) -> float:
    return round(n / d, 4) if d else 0.0


def build_funnel_state(*, leads=None, reviewed_opportunities=None, offers=None,
                       payment_events=None) -> dict:
    leads = leads or []
    reviewed = reviewed_opportunities or []
    offers = offers or []
    payments = payment_events

    lead_count = len(leads)
    qualified_count = sum(
        1 for o in reviewed
        if isinstance(o, dict) and (
            o.get("human_review_status") == "reviewed"
            or o.get("status") in _QUALIFIED_STATUSES
            or o.get("qualified") is True)
    ) or len(reviewed)
    offer_count = len([o for o in offers if o])

    if payments is None:
        paid_count = 0
        payment_note = "no payment feed supplied - paid_count is 0, not inferred"
    else:
        paid_count = len([p for p in payments if p])
        payment_note = "paid_count taken from the supplied payment feed (PayPal/RevenueLedger)"

    return {
        "funnel_state": {
            "leads": lead_count,
            "qualified": qualified_count,
            "offers": offer_count,
            "paid": paid_count,
        },
        "lead_count": lead_count,
        "qualified_count": qualified_count,
        "offer_count": offer_count,
        "paid_count": paid_count,
        "conversion_metrics": {
            "lead_to_qualified": _rate(qualified_count, lead_count),
            "qualified_to_offer": _rate(offer_count, qualified_count),
            "offer_to_paid": _rate(paid_count, offer_count),
            "lead_to_paid": _rate(paid_count, lead_count),
        },
        "payment_source_note": payment_note,
        "fabricated_sales": 0,
    }


class SalesTrackerAgent(Agent):
    role = "sales_tracker"
    objective = "Maintain the sales funnel state from supplied data."
    capabilities = ("track_sales",)

    def run(self, task: Task) -> Result:
        p = task.payload
        for key in ("leads", "reviewed_opportunities", "offers"):
            if key in p and not isinstance(p[key], list):
                return Result(task_id=task.id, agent=self.name, status="error",
                              error=f"payload['{key}'] must be a list when given")
        if "payment_events" in p and p["payment_events"] is not None \
                and not isinstance(p["payment_events"], list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['payment_events'] must be a list or null")
        if not any(k in p for k in ("leads", "reviewed_opportunities", "offers",
                                    "payment_events")):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs at least one funnel input")
        out = build_funnel_state(
            leads=p.get("leads"),
            reviewed_opportunities=p.get("reviewed_opportunities"),
            offers=p.get("offers"),
            payment_events=p.get("payment_events"),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
