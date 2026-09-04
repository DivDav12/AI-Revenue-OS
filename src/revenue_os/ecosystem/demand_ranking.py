"""Demand Ranking Layer (spec: Decision-/Ranking-Design step).

A SECOND, FULLY SEPARATE, ADVISORY-ONLY layer on top of the Demand
Quality Layer (`demand_signal.py`). It consumes an already-built
`DemandEvidence` and produces two EXPLAINABLE, INDEPENDENT confidence
scores:

    buyer_confidence(evidence)   -> BuyerConfidence    (0..1, factors, reasons)
    problem_confidence(evidence) -> ProblemConfidence   (0..1, factors, reasons)

Both mirror the exact same transparency pattern as
`demand_signal.score_demand_quality()`: every contributing factor is
named with its weight, whether it fired, and its sign - never a
black-box number.

Why two SEPARATE scores instead of one blended number (see the
Decision-Model analysis this implements): the 64-signal validation
showed TRUE_PURCHASE_INTENT and TRUE_PROBLEM_DEMAND have different,
sometimes opposite signal patterns (explicit intent dominates buyer
demand; `problem_interest + ASKER` dominates problem demand with much
higher precision). Blending them into one number would hide exactly the
distinction this module exists to surface.

===========================================================================
HARD SAFETY BOUNDARY - READ BEFORE WIRING THIS INTO ANYTHING
===========================================================================
This module is DELIBERATELY not wired into, and MUST NOT be used by:
    - verification.py (any gate)
    - discovery.py (acceptance/rejection of a discovered signal)
    - action_class.py / the MONEY/IDENTITY/LEGAL/SAFETY firewall
    - DEPLOY / ACCEPT / PLAN / EXECUTE approval flow
    - any auto-accept or auto-reject decision
    - automatic worker prioritization / queue ordering

`buyer_confidence`/`problem_confidence` are pure functions that never
raise, never block, and never return a value meant to gate anything -
they exist ONLY to be displayed to a human (JARVIS / a read-model / a
future dashboard column) alongside the existing, unmodified
`demand_score`. The existing `score_demand_quality()` total is completely
untouched by this module - it does not import from here, and nothing
here writes back into it.

Design rules enforced by construction (spec: "kein Faktor darf den Score
auf 0 multiplizieren", "keine harte AND-Bedingung", "keine harte
Ablehnung"):
    - every factor is PURELY ADDITIVE (a signed weight), never a
      multiplier - so no single missing/negative factor can zero out an
      otherwise-strong score the way an AND-gate or a multiplication
      would.
    - the only clamping is the final bound to [0.0, 1.0], exactly like
      `score_demand_quality()` already does.
    - no factor here is a precondition for another - each is evaluated
      independently against the same `DemandEvidence`.

No new marker, no new extraction, no new data source. Every predicate
below reads a field `demand_signal.py` already extracts (`intent_level`,
`perspective`, `builder_signal`, `intent_quote`, `builder_quote`,
`budget`, `repeat_signal_count`, `age_days`) - nothing here calls a
regex or touches raw text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .demand_signal import (
    BUILDER_YES,
    DemandEvidence,
    INTENT_EXPLICIT,
    INTENT_PROBLEM,
    PERSPECTIVE_ASKER,
    PERSPECTIVE_SUPPLIER,
)

# ---------------------------------------------------------------------------
# builder / intent independence - the ONLY piece of "logic" beyond a
# straight factor table. Reuses the two quotes demand_signal.py already
# extracted; does not parse or re-extract anything.
# ---------------------------------------------------------------------------


def builder_intent_independent(evidence: DemandEvidence) -> bool:
    """True only when BOTH an `intent_quote` and a `builder_quote` exist
    AND they come from different sentences (spec: "wenn beide Evidenzen
    vorhanden sind und aus unterschiedlichen Saetzen stammen"). This is
    the "I built a SaaS, but I would pay for tool X" case (e.g. the
    64-signal validation's #38/#54) - the builder-phrase and the
    purchase-intent statement are about two different things, so the
    builder penalty should be DAMPENED, not full-strength.

    NOTE on how "same sentence" is decided with ONLY these two fields:
    `intent_quote` is the short MARKER phrase classify_purchase_intent()
    matched (e.g. "would pay"), while `builder_quote` is the FULL
    SENTENCE classify_builder_signal() matched. They are almost never
    string-equal even when they co-occur in the same sentence, so
    equality is the wrong test. Instead: if the intent marker phrase
    appears INSIDE the builder's matched sentence, they are the same
    sentence (not independent); if it does not, they came from two
    different parts of the text (independent). Still exactly the two
    existing evidence fields - no new extraction, no new marker.

    Fails closed to False (= NOT proven independent = the stronger,
    full-strength penalty applies) whenever either quote is missing -
    same "safe failure mode" convention as every other provenance
    decision in `demand_signal.py`."""
    intent_quote = (evidence.intent_quote or "").strip().lower()
    builder_quote = (evidence.builder_quote or "").strip().lower()
    if not intent_quote or not builder_quote:
        return False
    return intent_quote not in builder_quote


# ---------------------------------------------------------------------------
# factor tables - (name, weight, predicate(evidence) -> bool, reason, sign).
# Identical shape to demand_signal.py's _POSITIVE_FACTORS/_NEGATIVE_FACTORS,
# reused here via the shared `_accumulate` loop below.
# ---------------------------------------------------------------------------

_BUYER_FACTORS: tuple = (
    ("explicit_purchase_intent_base", 0.60,
     lambda e: e.intent_level == INTENT_EXPLICIT,
     "explicit purchase intent is the primary buyer-confidence signal", "+"),
    ("supplier_penalty", 0.15,
     lambda e: e.perspective == PERSPECTIVE_SUPPLIER,
     "a Show/Launch/Tell HN title suggests self-promotion rather than a buyer statement", "-"),
    ("builder_penalty_full", 0.15,
     lambda e: e.builder_signal == BUILDER_YES and not builder_intent_independent(e),
     "a builder/provider claim overlaps the same statement (or no independent purchase-intent quote exists)", "-"),
    ("builder_penalty_dampened", 0.07,
     lambda e: e.builder_signal == BUILDER_YES and builder_intent_independent(e),
     "a builder/provider claim was found, but in an independent sentence from the purchase-intent quote", "-"),
    ("budget_bonus", 0.05,
     lambda e: e.budget.amount > 0 and not e.budget.is_estimate,
     "a concrete budget was stated (weak signal alone - suppliers state prices too)", "+"),
    ("repeat_signal_bonus", 0.03,
     lambda e: e.repeat_signal_count >= 1,
     "at least one other independent signal looks like this one", "+"),
    ("freshness_recent_bonus", 0.02,
     lambda e: e.age_days is not None and e.age_days <= 14,
     "the signal is recent (<=14 days)", "+"),
    ("freshness_stale_penalty", 0.02,
     lambda e: e.age_days is not None and e.age_days > 90,
     "the signal is stale (>90 days) - less actionable, not necessarily less real", "-"),
)

_PROBLEM_FACTORS: tuple = (
    ("problem_interest_base", 0.45,
     lambda e: e.intent_level == INTENT_PROBLEM,
     "a stated problem/need is the primary problem-confidence signal", "+"),
    ("explicit_intent_base", 0.25,
     lambda e: e.intent_level == INTENT_EXPLICIT,
     "explicit purchase intent also implies a real underlying problem, weighted lower here than on buyer_confidence", "+"),
    ("problem_interest_asker_combo", 0.25,
     lambda e: e.intent_level == INTENT_PROBLEM and e.perspective == PERSPECTIVE_ASKER,
     "problem-interest phrased as a genuine ask, not a pitch - empirically the strongest single "
     "combination found (64-signal validation: precision 0.79 / recall 0.79)", "+"),
    ("supplier_penalty", 0.05,
     lambda e: e.perspective == PERSPECTIVE_SUPPLIER,
     "a Show/Launch/Tell HN title is weak evidence against organic problem-demand", "-"),
    ("builder_penalty_full", 0.05,
     lambda e: e.builder_signal == BUILDER_YES and not builder_intent_independent(e),
     "a builder/provider claim overlaps the same statement", "-"),
    ("builder_penalty_dampened", 0.02,
     lambda e: e.builder_signal == BUILDER_YES and builder_intent_independent(e),
     "a builder/provider claim was found, but in an independent sentence", "-"),
    ("repeat_signal_bonus", 0.05,
     lambda e: e.repeat_signal_count >= 1,
     "at least one other independent signal looks like this one", "+"),
    ("freshness_recent_bonus", 0.02,
     lambda e: e.age_days is not None and e.age_days <= 14,
     "the signal is recent (<=14 days)", "+"),
    ("freshness_stale_penalty", 0.02,
     lambda e: e.age_days is not None and e.age_days > 90,
     "the signal is stale (>90 days)", "-"),
)


def _accumulate(evidence: DemandEvidence, specs: tuple) -> tuple[float, dict, list]:
    """Shared, purely-additive accumulation loop - identical mechanics to
    `demand_signal.score_demand_quality()`. No multiplication, no
    preconditions between factors, final clamp to [0, 1] only."""
    total = 0.0
    factors: dict = {}
    reasons: list = []
    for name, weight, predicate, reason, sign in specs:
        present = bool(predicate(evidence))
        factors[name] = {"weight": weight, "present": present, "sign": sign}
        if present:
            total += weight if sign == "+" else -weight
            reasons.append(f"{sign} {reason}")
    total = max(0.0, min(1.0, total))
    return total, factors, reasons


# ---------------------------------------------------------------------------
# results - same to_dict() shape/spirit as DemandQualityScore, so any
# existing caller that already knows how to render one knows how to
# render these too.
# ---------------------------------------------------------------------------


@dataclass
class BuyerConfidence:
    total: float
    evidence: DemandEvidence
    factors: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 3), "evidence": self.evidence.to_dict(),
                "factors": self.factors, "reasons": list(self.reasons)}


@dataclass
class ProblemConfidence:
    total: float
    evidence: DemandEvidence
    factors: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 3), "evidence": self.evidence.to_dict(),
                "factors": self.factors, "reasons": list(self.reasons)}


def buyer_confidence(evidence: DemandEvidence) -> BuyerConfidence:
    """How strongly this signal looks like GENUINE PURCHASE INTENT (demand
    category A). Advisory only - see the module's HARD SAFETY BOUNDARY
    docstring above. Never raises, never gates, never returns anything
    meant to auto-accept or auto-reject."""
    total, factors, reasons = _accumulate(evidence, _BUYER_FACTORS)
    return BuyerConfidence(total=total, evidence=evidence, factors=factors, reasons=reasons)


def problem_confidence(evidence: DemandEvidence) -> ProblemConfidence:
    """How strongly this signal looks like a GENUINE STATED PROBLEM
    without necessarily concrete purchase intent (demand category B).
    Advisory only - see the module's HARD SAFETY BOUNDARY docstring
    above. Never raises, never gates, never returns anything meant to
    auto-accept or auto-reject."""
    total, factors, reasons = _accumulate(evidence, _PROBLEM_FACTORS)
    return ProblemConfidence(total=total, evidence=evidence, factors=factors, reasons=reasons)
