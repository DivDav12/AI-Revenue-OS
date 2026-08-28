"""Outcome-derived criterion weights (opt-in).

Turns the M25 retrospective into weights for score_opportunity: criteria
that ran higher on validated candidates count for more, noise criteria
keep weight ~1. Transparent (a clamped-linear function of the gap),
deterministic (a pure function of the store), and recomputed on every
run so it self-corrects as outcomes accumulate.

A no-op until there are enough outcomes with both classes present -
then calibration_weights returns None and scoring stays equal-weight.

Feedback-loop note: calibrated scoring changes which candidates get
shortlisted, so the outcome set is somewhat self-selecting. The human
still approves every candidate and the `outcomes` data stays visible.
"""

from __future__ import annotations

from .opportunity import CRITERIA
from .retro import outcome_retro

_K = 0.3
_MIN_W, _MAX_W = 0.5, 2.0


def _clamp(x: float) -> float:
    return max(_MIN_W, min(_MAX_W, x))


def calibration_weights(store, *, min_outcomes: int = 8) -> dict[str, float] | None:
    """Return per-criterion weights averaging 1.0, or None for equal weight."""
    retro = outcome_retro(store, min_outcomes=min_outcomes)
    counts = retro["counts"]
    if not retro["ready"] or counts["validated"] < 1 or counts["rejected"] < 1:
        return None

    raw = {c: _clamp(1.0 + retro["by_criterion"][c]["gap"] * _K) for c in CRITERIA}
    mean = sum(raw.values()) / len(raw)
    return {c: round(raw[c] / mean, 4) for c in CRITERIA}
