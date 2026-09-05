"""Demand <-> Affiliate Offer matching (spec section 2).

Pure, deterministic. Never invents an offer, never guesses a category the
evidence does not support - a demand signal with no matching offer simply
produces no match (the empty list), which the caller then reports as
HUMAN_SETUP_REQUIRED ("no usable offer covers this demand yet").

Reuses the existing demand evidence text (`OpportunityDraft.title` /
`.description` / `.evidence`) and the demand-ranking scores already
computed for it (`draft.raw['buyer_confidence']` /
`draft.raw['problem_confidence']`, when the draft came through
`demand_sources.py`) - no new keyword extraction beyond a simple, visible
token-overlap count against each offer's own human-supplied `keywords` +
`category` + `product_name`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .affiliate_model import AffiliateOffer
from .model import OpportunityDraft

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "you", "your", "are",
    "have", "has", "does", "how", "what", "when", "where", "there", "not",
})


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP}


def _demand_text(draft: OpportunityDraft) -> str:
    parts = [draft.title, draft.description, draft.category]
    parts.extend(str(e) for e in (draft.evidence or []))
    return " ".join(parts)


def demand_strength(draft: OpportunityDraft) -> float:
    """0..1 - prefers the demand-ranking layer's buyer/problem confidence
    (advisory-only elsewhere, but a perfectly legitimate INPUT to a new,
    downstream affiliate-matching score - this module is not one of the
    places demand_ranking.py's docstring forbids wiring it into) over the
    coarser `demand_hint`, when available."""
    raw = draft.raw or {}
    bc = (raw.get("buyer_confidence") or {}).get("total")
    pc = (raw.get("problem_confidence") or {}).get("total")
    if isinstance(bc, (int, float)) or isinstance(pc, (int, float)):
        return max(float(bc or 0.0), float(pc or 0.0))
    return max(0.0, min(1.0, float(draft.demand_hint or 0.0)))


@dataclass
class AffiliateMatch:
    offer: AffiliateOffer
    match_score: float                # 0..1 - keyword/category overlap strength
    matched_terms: list = field(default_factory=list)
    demand_strength: float = 0.0

    def to_dict(self) -> dict:
        return {"offer_id": self.offer.offer_id, "program_name": self.offer.program_name,
                "product_name": self.offer.product_name, "match_score": round(self.match_score, 3),
                "matched_terms": list(self.matched_terms),
                "demand_strength": round(self.demand_strength, 3),
                "usable": self.offer.usable, "offer_status": self.offer.status}


def match_offers(draft: OpportunityDraft, offers: list, *,
                 min_score: float = 0.15) -> list[AffiliateMatch]:
    """Rank every offer (usable or not - the caller decides what to do with
    an unusable-but-well-matched offer, e.g. surface it as a setup
    priority) by overlap with this one demand signal. Returns best match
    first; offers scoring below `min_score` are dropped as noise."""
    hay_tokens = _tokens(_demand_text(draft))
    strength = demand_strength(draft)
    out: list[AffiliateMatch] = []
    for offer in offers:
        candidate_terms = set(offer.keywords) | {offer.category} | _tokens(offer.product_name)
        candidate_terms = {t.lower() for t in candidate_terms if t}
        hits = sorted(candidate_terms & hay_tokens)
        # category exact-match is a stronger, separate signal than a loose
        # keyword hit - counted once, weighted higher.
        category_hit = offer.category and offer.category.lower() in hay_tokens
        score = min(1.0, 0.25 * len(hits) + (0.35 if category_hit else 0.0))
        if score < min_score:
            continue
        out.append(AffiliateMatch(offer=offer, match_score=score,
                                  matched_terms=hits, demand_strength=strength))
    out.sort(key=lambda m: (-m.match_score, -m.demand_strength))
    return out


def best_usable_match(draft: OpportunityDraft, offers: list, *,
                      min_score: float = 0.15) -> AffiliateMatch | None:
    """The single best match whose offer is actually usable (POLICY_OK,
    active) right now - what plan()/autonomy actually acts on. A
    higher-scoring but unusable offer never silently wins here; see
    `match_offers()` for the full ranked list including those, used only
    for reporting/setup-priority."""
    for m in match_offers(draft, offers, min_score=min_score):
        if m.offer.usable:
            return m
    return None
