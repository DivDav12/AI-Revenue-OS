"""Validation-outcome retrospective.

Pure aggregation over candidates that actually ran a validation test
(record_validation_outcome). Shows how validated vs rejected candidates
scored, per criterion, so the human can see which signals predicted
paying demand. Read-only; nothing here feeds back into scoring.
"""

from __future__ import annotations

from .opportunity import CRITERIA
from .store import Candidate, CandidateStore

# an outcome dict was recorded and it settled the test one way or the other
_SETTLED = ("validated", "rejected")


def _is_validated(cand: Candidate) -> bool:
    return cand.outcome.get("outcome") == "validated"


def _qualifying(store: CandidateStore) -> list[Candidate]:
    return [c for c in store.all() if c.outcome.get("outcome") in _SETTLED]


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def outcome_retro(store: CandidateStore, *, min_outcomes: int = 3) -> dict:
    settled = _qualifying(store)
    validated = [c for c in settled if _is_validated(c)]
    rejected = [c for c in settled if not _is_validated(c)]

    by_criterion: dict[str, dict] = {}
    for name in CRITERIA:
        v = [float(c.breakdown[name]) for c in validated if name in c.breakdown]
        r = [float(c.breakdown[name]) for c in rejected if name in c.breakdown]
        v_avg, r_avg = _avg(v), _avg(r)
        by_criterion[name] = {
            "validated_avg": v_avg,
            "rejected_avg": r_avg,
            "gap": round(v_avg - r_avg, 2),
        }

    most_predictive = sorted(
        by_criterion, key=lambda n: abs(by_criterion[n]["gap"]), reverse=True
    )[:3]

    return {
        "counts": {"validated": len(validated), "rejected": len(rejected)},
        "ready": len(settled) >= min_outcomes,
        "total": {
            "validated_avg": _avg([c.total for c in validated]),
            "rejected_avg": _avg([c.total for c in rejected]),
        },
        "by_criterion": by_criterion,
        "most_predictive": most_predictive,
        "outcomes": [
            {
                "name": c.name,
                "outcome": c.outcome.get("outcome", ""),
                "score": c.total,
                "metric_value": c.outcome.get("metric_value", ""),
            }
            for c in settled
        ],
    }
