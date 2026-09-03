"""Strategy engine (spec section 10 + 28 + 29).

One opportunity can be monetised several ways. `score_strategies()` scores
each viable strategy on the same axes; `select_strategy()` picks the
highest that clears a floor.

Deliberate biases from the spec:
  * section 28 - for the first EUR of revenue, prefer low-capital, fast,
    highly-automatable, low-platform-risk, already-demanded, repeatable
    opportunities. A EUR 8 task that ships today can outrank a theoretical
    EUR 1000 service.
  * section 29 - SERVICE is never the default. A service that needs lots of
    human labour, scales badly, or has high CAC is penalised. It stays a
    legal, available option - just not the one the system reaches for.

Pure + deterministic. No LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import model
from .model import OpportunityDraft, estimate_value
from .profitability import Profitability

# which strategies each opportunity type can plausibly support
_VIABLE: dict[str, tuple[str, ...]] = {
    model.TYPE_TASK: (model.STRAT_TASK, model.STRAT_SERVICE),
    model.TYPE_DIGITAL_PRODUCT: (model.STRAT_PRODUCT, model.STRAT_AFFILIATE),
    model.TYPE_CONTENT: (model.STRAT_PRODUCT, model.STRAT_AFFILIATE, model.STRAT_OTHER),
    model.TYPE_SOFTWARE_TOOL: (model.STRAT_PRODUCT, model.STRAT_SERVICE),
    model.TYPE_AFFILIATE: (model.STRAT_AFFILIATE, model.STRAT_PRODUCT),
    model.TYPE_ECOMMERCE: (model.STRAT_ECOMMERCE,),
    model.TYPE_DROPSHIPPING: (model.STRAT_ECOMMERCE,),
    model.TYPE_SERVICE: (model.STRAT_SERVICE, model.STRAT_PRODUCT),
    model.TYPE_OTHER: (model.STRAT_OTHER, model.STRAT_PRODUCT, model.STRAT_AFFILIATE),
}

# per-strategy profile (0..1 each, higher = better for us right now)
#   capital_light : needs ~no cash to start
#   speed         : time to a first measurable result
#   automatable   : share the current stack does without a human
#   low_platform_risk : unlikely to trip a platform rule
#   repeatable    : can run again cheaply once it works
#   scalable      : upside if it works
_PROFILE: dict[str, dict[str, float]] = {
    model.STRAT_TASK:      dict(capital_light=1.00, speed=0.95, automatable=0.65,
                                low_platform_risk=0.75, repeatable=0.85, scalable=0.35),
    model.STRAT_PRODUCT:   dict(capital_light=1.00, speed=0.55, automatable=0.95,
                                low_platform_risk=0.90, repeatable=0.95, scalable=0.90),
    model.STRAT_AFFILIATE: dict(capital_light=1.00, speed=0.45, automatable=0.70,
                                low_platform_risk=0.55, repeatable=0.80, scalable=0.75),
    model.STRAT_ECOMMERCE: dict(capital_light=0.35, speed=0.30, automatable=0.40,
                                low_platform_risk=0.55, repeatable=0.70, scalable=0.80),
    model.STRAT_SERVICE:   dict(capital_light=0.90, speed=0.60, automatable=0.45,
                                low_platform_risk=0.80, repeatable=0.40, scalable=0.25),
    model.STRAT_OTHER:     dict(capital_light=0.70, speed=0.50, automatable=0.50,
                                low_platform_risk=0.60, repeatable=0.55, scalable=0.55),
}

# section-28 weighting: the axes that matter most for the first revenue
_AXIS_WEIGHT = dict(capital_light=1.4, speed=1.3, automatable=1.3,
                    low_platform_risk=1.1, repeatable=1.0, scalable=0.6)

# section-29: SERVICE is down-weighted. Applied AFTER scoring so a truly
# outstanding service can still win, just with a handicap.
_SERVICE_HANDICAP = 0.80

_FLOOR = 0.20                    # below this, no strategy is recommended


@dataclass
class StrategyScore:
    strategy: str
    score: float
    profile: dict
    expected_profit: float
    profit_per_hour: float
    decision_value: float
    notes: str = ""

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "score": round(self.score, 3),
                "profile": self.profile, "expected_profit": self.expected_profit,
                "profit_per_hour": self.profit_per_hour,
                "decision_value": self.decision_value, "notes": self.notes}


@dataclass
class StrategySelection:
    recommended: str
    options: list = field(default_factory=list)     # list[StrategyScore]
    reason: str = ""
    demand_backed: bool = False

    def to_dict(self) -> dict:
        return {"recommended": self.recommended,
                "options": [o.to_dict() for o in self.options],
                "reason": self.reason, "demand_backed": self.demand_backed,
                "service_is_not_default": True}


def score_strategies(draft: OpportunityDraft, prof: Profitability, *,
                     priority_weights: dict | None = None) -> list[StrategyScore]:
    pw = priority_weights or {}
    profit = estimate_value(prof.expected_profit)
    pph = estimate_value(prof.profit_per_hour)
    dv = estimate_value(prof.decision_value)
    demand = max(0.0, min(1.0, float(draft.demand_hint or 0.0)))

    viable = _VIABLE.get(draft.opportunity_type, (model.STRAT_OTHER,))
    out: list[StrategyScore] = []
    for strat in viable:
        p = _PROFILE[strat]
        axis = sum(p[a] * _AXIS_WEIGHT[a] for a in _AXIS_WEIGHT) / sum(_AXIS_WEIGHT.values())
        # economic term: normalise decision_value into ~0..1
        econ = max(0.0, min(1.0, dv / 20.0))
        # demand term: a real demand signal lifts everything, most for
        # strategies that convert existing demand directly (task, affiliate)
        demand_lift = demand * (0.20 if strat in (model.STRAT_TASK, model.STRAT_AFFILIATE)
                                else 0.10)
        raw = 0.55 * axis + 0.35 * econ + demand_lift
        raw *= float(pw.get(strat.lower(), 1.0))
        if strat == model.STRAT_SERVICE:
            raw *= _SERVICE_HANDICAP
        notes = ""
        if strat == model.STRAT_SERVICE:
            notes = "service is down-weighted (spec 29): human-labour heavy, low repeatability"
        out.append(StrategyScore(
            strategy=strat, score=round(raw, 3), profile=dict(p),
            expected_profit=profit, profit_per_hour=pph, decision_value=dv,
            notes=notes))
    out.sort(key=lambda s: -s.score)
    return out


def select_strategy(draft: OpportunityDraft, prof: Profitability, *,
                    priority_weights: dict | None = None) -> StrategySelection:
    options = score_strategies(draft, prof, priority_weights=priority_weights)
    # hard economic gate: negative projected profit / decision value -> nothing
    if estimate_value(prof.expected_profit) <= 0 or estimate_value(prof.decision_value) <= 0:
        return StrategySelection(
            recommended="", options=options,
            reason="projected profit / decision value is not positive - "
                   "not worth pursuing",
            demand_backed=bool(draft.demand_hint))
    if not options or options[0].score < _FLOOR:
        return StrategySelection(
            recommended="", options=options,
            reason=f"no strategy clears the floor ({_FLOOR}) - not worth pursuing",
            demand_backed=bool(draft.demand_hint))
    best = options[0]
    reason = (f"{best.strategy} scores {best.score} - "
              + ("demand-backed, " if draft.demand_hint else "")
              + f"expected profit EUR {best.expected_profit:.2f} "
              f"at EUR {best.profit_per_hour:.2f}/h")
    if best.strategy == model.STRAT_SERVICE:
        reason += " (won despite the service handicap)"
    return StrategySelection(recommended=best.strategy, options=options,
                             reason=reason, demand_backed=bool(draft.demand_hint))
