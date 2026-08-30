"""Profit Master (#18, revenue cluster) - deterministic financial read.

Pure arithmetic over values the caller supplies from the real ledgers
(booked revenue, actual costs, LLM spend, marketing spend, refunds).
Missing components are recorded in `missing_inputs` and treated as 0 -
never invented. PayPal stays read-only elsewhere; this agent has no
spending authority and touches no ledger.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_COMPONENTS = ("booked_revenue", "actual_costs", "llm_spend", "marketing_spend", "refunds")


def _num(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def build_profit_read(values: dict) -> dict:
    values = values or {}
    missing = [c for c in _COMPONENTS if c not in values or values[c] is None]

    revenue = _num(values.get("booked_revenue"))
    direct_cost = _num(values.get("actual_costs"))
    llm = _num(values.get("llm_spend"))
    marketing = _num(values.get("marketing_spend"))
    refunds = _num(values.get("refunds"))

    total_cost = round(direct_cost + llm + marketing, 2)
    net_revenue = round(revenue - refunds, 2)
    gross_profit = round(net_revenue - direct_cost, 2)
    net_profit = round(net_revenue - total_cost, 2)
    margin = round(net_profit / net_revenue, 4) if net_revenue else None
    roi = round(net_profit / total_cost, 4) if total_cost else None

    return {
        "revenue": revenue,
        "net_revenue_after_refunds": net_revenue,
        "cost": total_cost,
        "cost_breakdown": {"actual_costs": direct_cost, "llm_spend": llm,
                           "marketing_spend": marketing},
        "refunds": refunds,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "margin": margin,
        "ROI": roi,
        "missing_inputs": missing,
        "note": "actual supplied values only - missing components counted as 0, "
                "not estimated; no ledger was modified",
    }


class ProfitMasterAgent(Agent):
    role = "profit_master"
    objective = "Calculate financial performance from supplied ledger values."
    capabilities = ("manage_profit",)

    def run(self, task: Task) -> Result:
        values = task.payload.get("values")
        if values is None:
            values = {k: task.payload[k] for k in _COMPONENTS if k in task.payload}
        if not isinstance(values, dict) or not values:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs 'values' dict or at least one cost/revenue key")
        bad = [k for k, v in values.items()
               if v is not None and not isinstance(v, (int, float))]
        if bad:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error=f"non-numeric values: {bad}")
        out = build_profit_read(values)
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
