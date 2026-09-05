"""Multi-Offer Selection (Demand-First Affiliate architecture, Offer
Discovery MVP).

The missing layer identified in the architecture analysis: `affiliate_
matching.match_offers()` ranks by RELEVANCE only (match_score, then
demand_strength); `affiliate_profitability.evaluate()` projects economics
for exactly ONE already-chosen match. Neither combines both across
SEVERAL candidate offers for the same demand signal - with only one
offer ever in the catalog so far, that gap was invisible.

`select_best_offer()` closes it with a strict TWO-STAGE process, so a
higher commission can never buy its way past irrelevance:

    Stage 1 (GATE, relevance only):  keep matches with match_score >= min_relevance
    Stage 2 (RANK, profitability):   affiliate_profitability.evaluate() each
                                      survivor, pick the highest decision_value

A candidate that fails Stage 1 is never even profitability-scored - a
highly-profitable but irrelevant offer literally cannot reach Stage 2,
regardless of its commission.

This module does not change `affiliate_matching.py` or
`affiliate_profitability.py` in any way - it only calls their existing,
unmodified public functions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .affiliate_matching import AffiliateMatch
from .affiliate_profitability import AffiliateProfitability, evaluate
from .model import estimate_value


@dataclass(frozen=True)
class SelectedOffer:
    """The single winning candidate, plus the full evidence for why - the
    relevance match AND the profitability projection it was ranked on,
    never just a bare offer id."""

    match: AffiliateMatch
    profitability: AffiliateProfitability
    decision_value: float

    def to_dict(self) -> dict:
        return {
            "match": self.match.to_dict(),
            "profitability": self.profitability.to_dict(),
            "decision_value": round(self.decision_value, 3),
        }


def select_best_offer(matches: list[AffiliateMatch], *,
                      min_relevance: float = 0.15) -> SelectedOffer | None:
    """Pick the single best offer among several already-computed matches
    (the output of `affiliate_matching.match_offers()`).

    Fail-closed, deterministic:
      - an empty `matches` list, or one where nothing clears
        `min_relevance`, returns None (never a guessed "best available").
      - only USABLE offers (`match.offer.usable`) are ever selected - the
        same bar `affiliate_matching.best_usable_match()` already applies
        elsewhere; an unusable (HUMAN_SETUP_REQUIRED/inactive) offer is
        never "the best", it is simply not actionable yet.
      - ties are broken deterministically (match_score, then offer_id) -
        the same input always produces the same winner."""
    relevant = [m for m in matches if m.match_score >= min_relevance and m.offer.usable]
    if not relevant:
        return None

    scored: list[tuple[float, AffiliateMatch, AffiliateProfitability]] = []
    for m in relevant:
        prof = evaluate(m)
        dv = estimate_value(prof.decision_value)
        scored.append((dv, m, prof))

    scored.sort(key=lambda t: (-t[0], -t[1].match_score, t[1].offer.offer_id))
    dv, m, prof = scored[0]
    return SelectedOffer(match=m, profitability=prof, decision_value=dv)
