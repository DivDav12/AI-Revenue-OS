"""Cheapest-test validation planning and outcome recording.

plan_validation() is pure and deterministic (template-driven, no LLM).
Every template costs nothing; one that would require spend sets
needs_human_budget so the human owner decides.

record_validation_outcome() is a human input point: it records the
observed result and moves the candidate to its terminal status.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import lifecycle
from .store import Candidate, CandidateStore, now_iso

_LOW_SCALABILITY = 2.0
_HIGH_AUTOMATION = 3.5
_VALID_OUTCOMES = ("validated", "rejected")


@dataclass(frozen=True)
class ValidationPlan:
    candidate_name: str
    hypothesis: str
    cheapest_test: str
    success_metric: str
    effort: str  # "low" | "medium"
    max_cost: float = 0.0
    needs_human_budget: bool = False
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_name": self.candidate_name,
            "hypothesis": self.hypothesis,
            "cheapest_test": self.cheapest_test,
            "success_metric": self.success_metric,
            "effort": self.effort,
            "max_cost": self.max_cost,
            "needs_human_budget": self.needs_human_budget,
            "created_at": self.created_at,
        }


def plan_validation(candidate: Candidate) -> ValidationPlan:
    """Pick the single cheapest test that would confirm real demand."""
    desc = candidate.description.lower()
    automation = float(candidate.breakdown.get("automation_potential", 0.0))
    scalability = float(candidate.breakdown.get("scalability", 0.0))

    hypothesis = f"People will pay for: {candidate.description or candidate.name}"

    if "marketplace" in desc:
        test = "Concierge test: manually match one buyer with one seller by hand."
        metric = "1 completed transaction within 2 weeks"
        effort = "medium"
    elif scalability <= _LOW_SCALABILITY:
        test = "Direct outreach to 10 named prospects offering the service."
        metric = "3 prospects agree to a paid pilot"
        effort = "low"
    elif automation >= _HIGH_AUTOMATION or "saas" in desc or "platform" in desc:
        test = "Publish a one-page landing site with a waitlist signup."
        metric = "25 waitlist signups within 2 weeks"
        effort = "low"
    else:
        test = "Run 5 problem-interview conversations with target users."
        metric = "3 of 5 confirm the problem is worth paying to solve"
        effort = "low"

    return ValidationPlan(
        candidate_name=candidate.name,
        hypothesis=hypothesis,
        cheapest_test=test,
        success_metric=metric,
        effort=effort,
        max_cost=0.0,
        needs_human_budget=False,
        created_at=now_iso(),
    )


def record_validation_outcome(
    store: CandidateStore,
    name: str,
    outcome: str,
    *,
    metric_value: str,
    actor: str,
    note: str = "",
) -> Candidate:
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {list(_VALID_OUTCOMES)}")
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    advanced = lifecycle.advance(candidate, outcome, note=note, actor=actor)
    recorded = replace(
        advanced,
        outcome={
            "ts": now_iso(),
            "outcome": outcome,
            "metric_value": metric_value,
            "note": note,
            "actor": actor,
        },
    )
    store.put(recorded)
    store.save()
    return recorded
