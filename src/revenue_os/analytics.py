"""Read-only ROI view across candidates, revenue, and spend.

Pure computation over the persisted ledgers. No I/O of its own.
"""

from __future__ import annotations

from .revenue import RevenueLedger
from .spend import SpendLedger
from .store import CandidateStore

_REVENUE_STATES = ("launched", "earning")


def roi_summary(
    store: CandidateStore, revenue_ledger: RevenueLedger, spend_ledger: SpendLedger
) -> dict:
    candidates: dict[str, dict] = {}
    for cand in store.all():
        revenue = revenue_ledger.total_for(cand.name)
        spent = spend_ledger.spent_for(cand.name)
        if revenue == 0.0 and spent == 0.0 and cand.status not in _REVENUE_STATES:
            continue
        net = round(revenue - spent, 2)
        candidates[cand.name] = {
            "status": cand.status,
            "revenue": revenue,
            "budget": spend_ledger.budget_for(cand.name),
            "authorized": spend_ledger.authorized_for(cand.name),
            "spent": spent,
            "net": net,
            "roi_ratio": round(net / spent, 2) if spent > 0.0 else None,
        }

    grand_revenue = revenue_ledger.total()
    grand_spent = spend_ledger.total_spent()
    return {
        "grand_revenue": grand_revenue,
        "grand_spent": grand_spent,
        "grand_net": round(grand_revenue - grand_spent, 2),
        "candidates": candidates,
    }
