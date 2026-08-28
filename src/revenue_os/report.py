"""Read-only pipeline report and human action queue.

Pure computation over persisted state. No writes, no I/O of its own.
"""

from __future__ import annotations

from . import lifecycle
from .analytics import roi_summary
from .opportunity import CRITERIA
from .retro import outcome_retro
from .revenue import RevenueLedger
from .spend import SpendLedger
from .store import Candidate, CandidateStore

# midpoint of the 0-5 criterion scale; values below this are "weak"
NEUTRAL_SCORE = 2.5

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


def next_action(candidate: Candidate, spend_ledger: SpendLedger | None = None) -> str | None:
    if (
        candidate.status == "investigating"
        and candidate.plan.get("needs_human_budget")
        and spend_ledger is not None
        and spend_ledger.budget_for(candidate.name) == 0
    ):
        return "set a validation budget, then record outcome"
    return _NEXT_ACTION.get(candidate.status)


def pipeline_report(
    store: CandidateStore,
    revenue_ledger: RevenueLedger,
    spend_ledger: SpendLedger,
    discovery_log=None,
    llm_spend_log=None,
    llm_budget=None,
) -> dict:
    candidates = store.all()

    status_counts = {status: 0 for status in lifecycle.STATUSES}
    for cand in candidates:
        status_counts[cand.status] = status_counts.get(cand.status, 0) + 1

    action_queue = [
        {"name": cand.name, "status": cand.status, "next_action": action}
        for cand in candidates
        if (action := next_action(cand, spend_ledger)) is not None
    ]

    candidate_rows = [
        {
            "name": cand.name,
            "status": cand.status,
            "score": cand.total,
            "verdict": cand.verdict,
            "breakdown": dict(cand.breakdown),
            "estimate_source": cand.estimate_source,
            "rationale": cand.rationale,
            "plan_needs_budget": bool(cand.plan.get("needs_human_budget")),
            "plan_max_cost": cand.plan.get("max_cost", 0.0),
            "offer": dict(cand.offer),
        }
        for cand in candidates
    ]

    llm_spend = llm_spend_log.summary() if llm_spend_log is not None else None
    if llm_spend is not None and llm_budget is not None:
        llm_spend["cap_usd"] = llm_budget.cap
        llm_spend["remaining_usd"] = round(
            llm_budget.cap - llm_spend["total_cost_usd"], 4
        )

    grand_revenue = revenue_ledger.total()
    grand_spent = spend_ledger.total_spent()
    return {
        "status_counts": status_counts,
        "action_queue": action_queue,
        "candidates": candidate_rows,
        "last_discovery": discovery_log.latest() if discovery_log is not None else None,
        "llm_spend": llm_spend,
        "outcomes": outcome_retro(store),
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

    lines.append("")
    lines.append("LAST DISCOVERY")
    last = report.get("last_discovery")
    if not last:
        lines.append("  (no discovery run recorded)")
    else:
        lines.append(f"  {last['ts']}  source={last['source']} limit={last['limit']}")
        lines.append(
            f"  fetched={last['fetched']} filtered_out={last['filtered_out']} "
            f"dropped_below_score={last['dropped_below_score']} "
            f"evaluated={last['evaluated']} kept={last['kept']}"
        )
        lines.append(
            f"  new={last['new']} refreshed={last['refreshed']} "
            f"shortlisted={last['shortlisted']}"
        )
        if last.get("evaluator") == "llm":
            lines.append(
                f"  evaluator=llm est_cost=${last.get('est_cost_usd', 0)} "
                f"actual_cost=${last.get('actual_cost_usd', 0)} "
                f"cache_hits={last.get('eval_cache_hits', 0)} "
                f"cache_misses={last.get('eval_cache_misses', 0)}"
            )

    lines.append("")
    lines.append("LLM SPEND")
    spend = report.get("llm_spend")
    if not spend or spend["runs"] == 0:
        lines.append("  (no LLM runs recorded)")
    else:
        by = spend["by_activity"]
        lines.append(
            f"  total ${spend['total_cost_usd']} over {spend['runs']} run(s), "
            f"{spend['total_api_calls']} api call(s)"
        )
        lines.append(
            f"  evaluate ${by['evaluate']}  plan ${by['plan']}  offer ${by['offer']}"
        )
        if "cap_usd" in spend:
            lines.append(
                f"  cap ${spend['cap_usd']}  remaining ${spend['remaining_usd']}"
            )

    lines.append("")
    lines.append("OUTCOMES")
    retro = report.get("outcomes") or {}
    n_v = retro.get("counts", {}).get("validated", 0)
    n_r = retro.get("counts", {}).get("rejected", 0)
    if not retro.get("ready"):
        lines.append(f"  (need more recorded outcomes; have {n_v + n_r})")
    else:
        tot = retro["total"]
        lines.append(
            f"  validated {n_v} / rejected {n_r} - "
            f"avg score validated {tot['validated_avg']} vs "
            f"rejected {tot['rejected_avg']}"
        )
        preds = ", ".join(
            f"{name} {retro['by_criterion'][name]['gap']:+g}"
            for name in retro["most_predictive"]
        )
        lines.append(f"  most predictive: {preds}")

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


def render_candidate(candidate: Candidate) -> str:
    """Deterministic detail view of a single candidate."""
    lines: list[str] = [
        f"CANDIDATE {candidate.name}",
        f"  status         {candidate.status}",
        f"  description    {candidate.description}",
        f"  source         {candidate.source}",
        f"  raw_ref        {candidate.raw_ref}",
        f"  score          {candidate.total} ({candidate.verdict}) "
        f"[{candidate.estimate_source}]",
        f"  next action    {next_action(candidate) or '-'}",
        f"  first_seen     {candidate.first_seen}",
        f"  last_scored    {candidate.last_scored}",
    ]

    lines.append("  rationale")
    lines.append(f"    {candidate.rationale}" if candidate.rationale else "    (none)")

    lines.append("  score breakdown")
    if candidate.breakdown:
        for name in CRITERIA:
            value = candidate.breakdown.get(name)
            if value is None:
                continue
            mark = " <" if float(value) < NEUTRAL_SCORE else ""
            lines.append(f"    {name:<24} {value}{mark}")
    else:
        lines.append("    (none)")

    lines.append("  plan")
    if candidate.plan:
        for key in sorted(candidate.plan):
            lines.append(f"    {key}: {candidate.plan[key]}")
    else:
        lines.append("    (none)")

    lines.append("  offer")
    if candidate.offer:
        for key in sorted(candidate.offer):
            lines.append(f"    {key}: {candidate.offer[key]}")
    else:
        lines.append("    (none)")

    lines.append("  outcome")
    if candidate.outcome:
        for key in sorted(candidate.outcome):
            lines.append(f"    {key}: {candidate.outcome[key]}")
    else:
        lines.append("    (none)")

    lines.append("  history")
    if candidate.history:
        for entry in candidate.history:
            lines.append(
                f"    {entry.get('ts', '')}: {entry.get('from', '')} -> "
                f"{entry.get('to', '')} ({entry.get('actor', '')}) "
                f"{entry.get('note', '')}".rstrip()
            )
    else:
        lines.append("    (none)")

    return "\n".join(lines)
