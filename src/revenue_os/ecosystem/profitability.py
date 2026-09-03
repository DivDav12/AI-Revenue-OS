"""Profitability engine (spec section 9).

Pure, deterministic. Turns coarse inputs into a comparable economic
picture. EVERY output number carries `is_estimate: true` (spec section 7 +
20) - this module never observes anything, it projects.

The headline comparator is `profit_per_hour` (spec's own example: a small
fast high-probability opportunity can beat a big slow uncertain one).
`decision_value` folds in success probability and risk so the strategy /
ranking layer has one number to sort on.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import model
from .model import OpportunityDraft, estimate

# fleet time is not cash, but it is scarce - price it so a 4-hour job is not
# "free". Deliberately low (the fleet is cheap) but non-zero.
_FLEET_HOUR_EUR = 2.0

_RISK_BY_TYPE = {
    model.TYPE_TASK: 0.15,
    model.TYPE_DIGITAL_PRODUCT: 0.30,
    model.TYPE_CONTENT: 0.35,
    model.TYPE_SOFTWARE_TOOL: 0.45,
    model.TYPE_AFFILIATE: 0.40,
    model.TYPE_ECOMMERCE: 0.55,
    model.TYPE_DROPSHIPPING: 0.65,
    model.TYPE_SERVICE: 0.35,
    model.TYPE_OTHER: 0.50,
}

# base first-outcome probability by type, nudged by the source's demand hint
_BASE_PROB_BY_TYPE = {
    model.TYPE_TASK: 0.55,
    model.TYPE_DIGITAL_PRODUCT: 0.12,
    model.TYPE_CONTENT: 0.10,
    model.TYPE_SOFTWARE_TOOL: 0.10,
    model.TYPE_AFFILIATE: 0.14,
    model.TYPE_ECOMMERCE: 0.10,
    model.TYPE_DROPSHIPPING: 0.08,
    model.TYPE_SERVICE: 0.18,
    model.TYPE_OTHER: 0.12,
}

# how much of the end-to-end work the current stack can do without a human
_AUTOMATION_BY_TYPE = {
    model.TYPE_TASK: 0.70,
    model.TYPE_DIGITAL_PRODUCT: 0.95,
    model.TYPE_CONTENT: 0.90,
    model.TYPE_SOFTWARE_TOOL: 0.60,
    model.TYPE_AFFILIATE: 0.75,
    model.TYPE_ECOMMERCE: 0.45,
    model.TYPE_DROPSHIPPING: 0.35,
    model.TYPE_SERVICE: 0.55,
    model.TYPE_OTHER: 0.50,
}


@dataclass
class Profitability:
    expected_revenue: dict
    expected_cost: dict
    expected_profit: dict
    expected_time_hours: dict
    profit_per_hour: dict
    success_probability: dict
    automation_pct: dict
    risk: dict
    roi: dict
    decision_value: dict          # the single comparator (>=0 better)
    is_estimate: bool = True

    def to_dict(self) -> dict:
        return {
            "expected_revenue": self.expected_revenue,
            "expected_cost": self.expected_cost,
            "expected_profit": self.expected_profit,
            "expected_time_hours": self.expected_time_hours,
            "profit_per_hour": self.profit_per_hour,
            "success_probability": self.success_probability,
            "automation_pct": self.automation_pct,
            "risk": self.risk,
            "roi": self.roi,
            "decision_value": self.decision_value,
            "is_estimate": True,
        }


def _revenue_guess(draft: OpportunityDraft) -> float:
    if draft.est_pay_eur and draft.est_pay_eur > 0:
        return float(draft.est_pay_eur)
    # no stated pay -> a conservative per-type default (still an estimate)
    return {
        model.TYPE_TASK: 12.0,
        model.TYPE_DIGITAL_PRODUCT: 29.0,
        model.TYPE_CONTENT: 15.0,
        model.TYPE_SOFTWARE_TOOL: 49.0,
        model.TYPE_AFFILIATE: 20.0,
        model.TYPE_ECOMMERCE: 25.0,
        model.TYPE_DROPSHIPPING: 22.0,
        model.TYPE_SERVICE: 80.0,
        model.TYPE_OTHER: 20.0,
    }.get(draft.opportunity_type, 20.0)


def _time_hours_guess(draft: OpportunityDraft) -> float:
    if draft.est_time_minutes and draft.est_time_minutes > 0:
        return max(0.05, float(draft.est_time_minutes) / 60.0)
    return {
        model.TYPE_TASK: 0.5,
        model.TYPE_DIGITAL_PRODUCT: 3.0,
        model.TYPE_CONTENT: 2.0,
        model.TYPE_SOFTWARE_TOOL: 12.0,
        model.TYPE_AFFILIATE: 4.0,
        model.TYPE_ECOMMERCE: 8.0,
        model.TYPE_DROPSHIPPING: 10.0,
        model.TYPE_SERVICE: 6.0,
        model.TYPE_OTHER: 4.0,
    }.get(draft.opportunity_type, 4.0)


def _cash_cost_guess(draft: OpportunityDraft) -> float:
    # V1: the fleet spends EUR 0 on everything (hosting is free GitHub Pages,
    # delivery is a free SMTP tier). Real cash cost only appears for
    # e-commerce/dropshipping/ads, which are all HUMAN_APPROVAL_REQUIRED and
    # priced by the human at that gate - so 0 here, plus a token risk buffer.
    return {
        model.TYPE_ECOMMERCE: 0.0,
        model.TYPE_DROPSHIPPING: 0.0,
    }.get(draft.opportunity_type, 0.0)


def evaluate(draft: OpportunityDraft, *, weights: dict | None = None) -> Profitability:
    """Deterministic economic projection for one opportunity."""
    otype = draft.opportunity_type
    w = weights or {}

    revenue = round(_revenue_guess(draft), 2)
    time_h = round(_time_hours_guess(draft), 3)
    cash = round(_cash_cost_guess(draft), 2)
    fleet_cost = round(time_h * _FLEET_HOUR_EUR, 2)
    cost = round(cash + fleet_cost, 2)
    profit = round(revenue - cost, 2)

    base_p = _BASE_PROB_BY_TYPE.get(otype, 0.12)
    demand = max(0.0, min(1.0, float(draft.demand_hint or 0.0)))
    prob = round(max(0.02, min(0.95, base_p * (0.6 + 0.8 * demand)
                               * float(w.get("prob_mult", 1.0)))), 3)

    automation = round(_AUTOMATION_BY_TYPE.get(otype, 0.5), 3)
    risk = round(min(0.95, _RISK_BY_TYPE.get(otype, 0.5)
                     * float(w.get("risk_mult", 1.0))), 3)

    pph = round(profit / time_h, 2) if time_h > 0 else 0.0
    roi = round(profit / cost, 2) if cost > 0 else round(profit, 2)

    # one comparator: expected profit per hour, discounted by uncertainty and
    # risk, rewarded for automation. Sorts opportunities directly.
    dv = round(pph * prob * (1.0 - 0.5 * risk) * (0.5 + 0.5 * automation), 3)

    return Profitability(
        expected_revenue=estimate(revenue, "stated pay or per-type default"),
        expected_cost=estimate(cost, f"cash {cash} + fleet {fleet_cost} @ EUR {_FLEET_HOUR_EUR}/h"),
        expected_profit=estimate(profit, "revenue - cost"),
        expected_time_hours=estimate(time_h, "stated time or per-type default"),
        profit_per_hour=estimate(pph, "profit / time"),
        success_probability=estimate(prob, f"base {base_p} x demand {demand}"),
        automation_pct=estimate(automation, "share of work the stack does without a human"),
        risk=estimate(risk, "per-type platform/execution risk"),
        roi=estimate(roi, "profit / cost"),
        decision_value=estimate(dv, "pph x prob x (1-0.5*risk) x (0.5+0.5*automation)"),
    )
