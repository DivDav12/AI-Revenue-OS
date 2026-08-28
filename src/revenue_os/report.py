"""Read-only pipeline report and human action queue.

Pure computation over persisted state. No writes, no I/O of its own.
"""

from __future__ import annotations

from . import lifecycle
from .analytics import roi_summary
from .revenue import RevenueLedger
from .spend import SpendLedger
from .store import Candidate, CandidateStore

# lifecycle status -> the single next step a human must take (None = nothing)
_NEXT_ACTION = {
    "discovered": None,
    "shortlisted": "approve or reject",
    "approved": "run investigation",
    "investigating": "record validation outcome",
    "validated": "launch offer",
    "launched": "record first payment",
    "earning": "record further payments / spend",
    "rejected": None,
}


def next_action(candidate: Candidate) -> str | None:
    return _NEXT_ACTION.get(candidate.status)


def pipeline_report(
    store: CandidateStore,
    revenue_ledger: RevenueLedger,
    spend_ledger: SpendLedger,
) -> dict:
    candidates = store.all()

    status_counts = {status: 0 for status in lifecycle.STATUSES}
    for cand in candidates:
        status_counts[cand.status] = status_counts.get(cand.status, 0) + 1

    action_queue = [
        {"name": cand.name, "status": cand.status, "next_action": action}
        for cand in candidates
        if (action := next_action(cand)) is not None
    ]

    grand_revenue = revenue_ledger.total()
    grand_spent = spend_ledger.total_spent()
    return {
        "status_counts": status_counts,
        "action_queue": action_queue,
        "roi": roi_summary(store, revenue_ledger, spend_ledger),
        "totals": {
            "candidates": len(candidates),
            "grand_revenue": grand_revenue,
            "grand_spent": grand_spent,
            "grand_net": round(grand_revenue - grand_spent, 2),
        },
    }


def render_text(report: dict) -> str:
    lines: list[str] = []

    lines.append("PIPELINE STATUS")
    for status in lifecycle.STATUSES:
        lines.append(f"  {status:<14} {report['status_counts'].get(status, 0)}")

    lines.append("")
    lines.append("ACTION QUEUE")
    if not report["action_queue"]:
        lines.append("  (nothing awaiting a human)")
    else:
        for item in report["action_queue"]:
            lines.append(
                f"  {item['name']} [{item['status']}] -> {item['next_action']}"
            )

    totals = report["totals"]
    lines.append("")
    lines.append("ROI")
    lines.append(f"  candidates    {totals['candidates']}")
    lines.append(f"  revenue       {totals['grand_revenue']}")
    lines.append(f"  spent         {totals['grand_spent']}")
    lines.append(f"  net           {totals['grand_net']}")
    for name in sorted(report["roi"]["candidates"]):
        row = report["roi"]["candidates"][name]
        lines.append(
            f"  - {name} [{row['status']}]: "
            f"revenue={row['revenue']} spent={row['spent']} "
            f"net={row['net']} roi_ratio={row['roi_ratio']}"
        )

    return "\n".join(lines)
