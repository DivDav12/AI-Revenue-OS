"""Structured revenue-opportunity concept and deterministic scoring.

An Opportunity carries eight estimates, each on a 0-5 "favourability"
scale (higher is always better, so cost/competition/time are supplied
as affordability / headroom / speed). Estimates are inputs: a human
provides them now, a discovery agent may provide them later.

score_opportunity() is a pure, deterministic function. No LLM, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Each criterion is scored 0-5, higher is better. Equal weight (simplest).
CRITERIA: tuple[str, ...] = (
    "startup_affordability",
    "automation_potential",
    "demand",
    "competition_headroom",
    "legal_feasibility",
    "speed_to_first_revenue",
    "profit_potential",
    "scalability",
)

SCORE_MIN = 0.0
SCORE_MAX = 5.0

# Verdict thresholds on the weighted total (0-5).
PURSUE_AT = 3.5
HOLD_AT = 2.5


@dataclass(frozen=True)
class Opportunity:
    name: str
    description: str = ""
    startup_affordability: float = 0.0
    automation_potential: float = 0.0
    demand: float = 0.0
    competition_headroom: float = 0.0
    legal_feasibility: float = 0.0
    speed_to_first_revenue: float = 0.0
    profit_potential: float = 0.0
    scalability: float = 0.0

    def estimates(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in CRITERIA}


@dataclass(frozen=True)
class OpportunityScore:
    opportunity_name: str
    total: float
    verdict: str
    breakdown: dict[str, float] = field(default_factory=dict)


def _verdict(total: float) -> str:
    if total >= PURSUE_AT:
        return "pursue"
    if total >= HOLD_AT:
        return "hold"
    return "reject"


def score_opportunity(opp: Opportunity) -> OpportunityScore:
    """Deterministically score an Opportunity. Raises ValueError on bad input."""
    breakdown = opp.estimates()
    for name, value in breakdown.items():
        if not SCORE_MIN <= value <= SCORE_MAX:
            raise ValueError(
                f"estimate {name}={value} out of range [{SCORE_MIN}, {SCORE_MAX}]"
            )
    total = round(sum(breakdown.values()) / len(breakdown), 2)
    return OpportunityScore(
        opportunity_name=opp.name,
        total=total,
        verdict=_verdict(total),
        breakdown=breakdown,
    )
