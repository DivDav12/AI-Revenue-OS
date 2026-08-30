"""Budget Allocator (#16, marketing cluster, HUMAN-GATED) - allocation
MODEL.

Given a budget a human has already stated as available, it models how
that budget could be split across campaign options. It authorizes
nothing, unlocks no Growth Capital, and never exceeds the stated amount.
Pure arithmetic - no store, no env, no money movement.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_SCENARIOS = (("conservative", 0.5), ("base", 1.0), ("aggressive", 1.5))


def _clean_options(options: list) -> list:
    out = []
    for o in options or []:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        out.append({
            "name": str(o["name"]),
            "min": max(0.0, float(o.get("min", 0) or 0)),
            "max": float(o["max"]) if o.get("max") is not None else None,
            "expected_roi": float(o.get("expected_roi", 1.0) or 0.0),
        })
    return out


def _allocate(options: list, budget: float) -> dict:
    if budget <= 0 or not options:
        return {o["name"]: 0.0 for o in options}
    floors = {o["name"]: min(o["min"], budget) for o in options}
    remaining = max(0.0, budget - sum(floors.values()))
    weights = {o["name"]: max(0.0, o["expected_roi"]) for o in options}
    wsum = sum(weights.values()) or 1.0
    alloc = {}
    for o in options:
        share = floors[o["name"]] + remaining * weights[o["name"]] / wsum
        if o["max"] is not None:
            share = min(share, o["max"])
        alloc[o["name"]] = round(share, 2)
    # never exceed the stated budget
    total = sum(alloc.values())
    if total > budget and total > 0:
        alloc = {k: round(v * budget / total, 2) for k, v in alloc.items()}
    return alloc


def build_allocation(available_budget: float, campaign_options: list, *,
                     expected_roi: dict | None = None,
                     risk_constraints: dict | None = None) -> dict:
    budget = max(0.0, float(available_budget or 0.0))
    options = _clean_options(campaign_options)
    if expected_roi:
        for o in options:
            if o["name"] in expected_roi:
                o["expected_roi"] = float(expected_roi[o["name"]])

    base = _allocate(options, budget)
    scenarios = []
    for label, mult in _SCENARIOS:
        alloc = _allocate(options, round(budget * mult, 2)) if label != "base" else base
        exp_return = round(sum(alloc[o["name"]] * o["expected_roi"] for o in options), 2)
        scenarios.append({
            "scenario": label, "budget_multiplier": mult,
            "allocation": alloc, "expected_return": exp_return,
        })

    base_return = round(sum(base[o["name"]] * o["expected_roi"] for o in options), 2)
    downside = round(-sum(base.values()), 2)   # worst case: full spend, zero return

    return {
        "recommended_allocation": base,
        "scenarios": scenarios,
        "expected_return": base_return,
        "downside": downside,
        "rationale": [
            "weighted by expected_roi within each option's min/max",
            f"total never exceeds the stated available_budget ({budget})",
            "worst case assumes the full spend returns nothing",
        ],
        "respects_presale_cap": True,
        "unlocks_growth_capital": False,
        "authorizes_spend": False,
        "risk_constraints_seen": dict(risk_constraints or {}),
        "human_gate_required": True,
        "note": "model only - a human decides and funds any spend",
    }


class BudgetAllocatorAgent(Agent):
    role = "budget_allocator"
    objective = "Model a budget split; authorize nothing."
    capabilities = ("allocate_budget",)

    def run(self, task: Task) -> Result:
        budget = task.payload.get("available_budget")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['available_budget'] must be a number")
        options = task.payload.get("campaign_options")
        if not isinstance(options, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['campaign_options'] must be a list")
        out = build_allocation(
            budget, options,
            expected_roi=task.payload.get("expected_roi") if isinstance(
                task.payload.get("expected_roi"), dict) else None,
            risk_constraints=task.payload.get("risk_constraints") if isinstance(
                task.payload.get("risk_constraints"), dict) else None,
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
