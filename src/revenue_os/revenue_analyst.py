"""Revenue Analyst - deterministic portfolio ROI read.

Pure aggregation of what pipeline_report already computed: the roi
summary (analytics.roi_summary), the outcome retro (retro.outcome_retro)
and the candidate rows. No LLM, no I/O, no money - it authorises
nothing and records no payment. Also stands in for Sales Tracker and
Profit Master until revenue volume justifies splitting them out.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_EARNING = ("launched", "earning")


def _round(x: float) -> float:
    return round(float(x), 2)


def build_revenue_analysis(roi: dict, outcomes: dict,
                           candidates: list[dict]) -> dict:
    roi = roi or {}
    per_roi = roi.get("candidates", {}) or {}
    status_by_name = {c["name"]: c["status"] for c in (candidates or [])}

    rows = []
    for name, r in per_roi.items():
        rows.append({
            "name": name,
            "status": r.get("status") or status_by_name.get(name, ""),
            "revenue": _round(r.get("revenue", 0.0)),
            "spent": _round(r.get("spent", 0.0)),
            "net": _round(r.get("net", 0.0)),
            "roi_ratio": r.get("roi_ratio"),
        })
    rows.sort(key=lambda x: x["net"], reverse=True)

    revenue = _round(roi.get("grand_revenue", 0.0))
    spent = _round(roi.get("grand_spent", 0.0))
    net = _round(roi.get("grand_net", revenue - spent))
    launched = sum(1 for s in status_by_name.values() if s == "launched")
    earning = sum(1 for s in status_by_name.values() if s == "earning")

    best = ({"name": rows[0]["name"], "net": rows[0]["net"]}
            if rows and rows[0]["net"] > 0 else None)
    spenders = [x for x in rows if x["spent"] > 0]
    worst = ({"name": spenders[-1]["name"], "net": spenders[-1]["net"]}
             if spenders else None)
    efficiency = _round(net / spent) if spent > 0 else None

    outcomes = outcomes or {}
    if outcomes.get("ready"):
        signal = ", ".join(outcomes.get("most_predictive", [])[:2]) or "no clear signal"
    else:
        signal = "not enough outcomes yet"

    if revenue > 0:
        readout = (
            f"Portfolio: ${revenue} revenue, ${spent} spent, ${net} net "
            f"across {launched + earning} launched/earning candidate(s)."
        )
        if best:
            readout += f" Best: {best['name']} at ${best['net']} net."
    elif spent > 0:
        readout = (
            f"No revenue recorded yet - {len(spenders)} candidate(s) have "
            f"spent ${spent} in validation."
        )
    else:
        readout = "No revenue or spend recorded yet."

    return {
        "portfolio": {
            "revenue": revenue, "spent": spent, "net": net,
            "roi_ratio": efficiency, "launched": launched, "earning": earning,
        },
        "per_candidate": rows,
        "best": best,
        "worst": worst,
        "spend_efficiency": efficiency,
        "outcome_signal": signal,
        "readout": readout,
    }


class RevenueAnalystAgent(Agent):
    role = "revenue_analyst"
    objective = "Aggregate the ledgers into a portfolio ROI read."
    capabilities = ("analyze_revenue",)

    def run(self, task: Task) -> Result:
        roi = task.payload.get("roi")
        if not isinstance(roi, dict):
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['roi'] must be the roi summary dict",
            )
        analysis = build_revenue_analysis(
            roi, task.payload.get("outcomes") or {},
            task.payload.get("candidates") or [],
        )
        return Result(
            task_id=task.id, agent=self.name, status="ok", output=analysis
        )
