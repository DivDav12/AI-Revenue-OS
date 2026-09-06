"""Content Opportunity Engine (SEO/content strategy layer).

Separates three things this codebase keeps structurally distinct:

    A) DEMAND DISCOVERY   - already real (demand_sources.py /
                            acquisition_sources.py) - real posts from real
                            public APIs.
    B) CONTENT OPPORTUNITY - THIS module - decides which of those real,
                            already-discovered/verified signals are worth
                            writing an honest evergreen guide about.
    C) TRAFFIC ACQUISITION - already real (distribution.py) - deciding
                            WHERE/HOW to tell people about a page, always
                            human-gated for anything off our own site.

Finding a real post is never, by itself, permission to publish something
targeting it, and never permission to post/advertise anywhere - it is
only evidence a topic gets asked about. This module turns that evidence
into a scored, fail-closed recommendation using the EXISTING, unmodified
relevance/demand machinery - no new matching logic:

    `affiliate_matching.match_offers()`   - relevance_to_offer (0..1)
    `affiliate_matching.demand_strength()` - buyer_intent (0..1)

`competition_estimate` has NO real data source in this codebase - it is
always `None` with an explicit note, never a guessed number (spec: "Do
not fabricate search volume"). `content_opportunity_score` is a plain,
documented formula over the two REAL inputs above - never inflated by
the (absent) competition figure.

Pure, deterministic, no network call - operates only on ALREADY-PERSISTED
opportunity records (`opportunity_store`) and an already-ingested
`AffiliateOffer`. `recommend_page` is fail-closed: only a real, evidenced,
sufficiently relevant signal clears it - an opportunity with no real
evidence can never reach here (`verification.verify()` already rejects
those, spec section 3, before a record is even persisted as QUALIFIED).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import model
from .affiliate_matching import demand_strength as _demand_strength
from .affiliate_matching import match_offers
from .affiliate_model import AffiliateOffer

#: same relevance floor `affiliate_matching.match_offers()`/
#: `offer_selection.select_best_offer()` already use elsewhere - a single,
#: consistent bar across the whole affiliate pipeline, not a new one.
_MIN_RELEVANCE = 0.15

#: source_type values that are NOT independently-arising real demand -
#: an editorial pick or synthetic/test record is never itself "evidence"
#: that people are asking about a topic (spec: separate demand discovery
#: from editorial judgement - see ecosystem.editorial's own module
#: docstring for why that path exists and is kept distinct).
_EXCLUDED_SOURCE_TYPES = frozenset({"editorial_pick"})

#: a LOCAL safeguard, scoped to this module only - never changes
#: `affiliate_matching.match_offers()` itself (that engine stays
#: unmodified; it is also used for the already-tested, revenue-affecting
#: offer-selection path and must not be touched here). A live run found
#: that engine's `product_name`-tokenization can surface individual,
#: near-zero-specificity English words (a marketing tagline like
#: "...all-in-one...online business platform..." yields "all"/"one"/
#: "business"/"platform") that coincidentally overlap totally unrelated
#: real demand. Recommending a whole new evergreen PAGE is a materially
#: higher-stakes decision than a routine offer match, so this module adds
#: its own, stricter bar on top: a match resting ENTIRELY on these bare
#: generic words is never enough evidence by itself.
_GENERIC_FILLER_WORDS = frozenset({
    "all", "one", "online", "business", "platform", "website", "service",
    "services", "tool", "tools", "app", "apps", "software", "solution",
    "solutions", "system", "systems", "product", "products", "the", "and",
})


@dataclass(frozen=True)
class ContentOpportunity:
    opportunity_id: str
    title: str
    source: str
    demand_evidence: tuple                  # verbatim quotes from the real post
    buyer_intent: float                     # 0..1, from real demand-quality scoring
    relevance_to_offer: float               # 0..1, keyword/category overlap vs the offer
    matched_terms: tuple
    expected_conversion_relevance: float    # same basis as relevance_to_offer - no separate model exists
    competition_estimate: None = None       # no real data source - never guessed
    competition_note: str = "no real competition/search-volume data source is " \
                            "wired in this codebase - never estimated or guessed"
    content_opportunity_score: float = 0.0
    recommend_page: bool = False
    already_has_a_page: bool = False   # kept last but defaulted (dataclass field-order rule)

    def to_dict(self) -> dict:
        return {
            "opportunity_id": self.opportunity_id, "title": self.title,
            "source": self.source, "demand_evidence": list(self.demand_evidence),
            "buyer_intent": round(self.buyer_intent, 3),
            "relevance_to_offer": round(self.relevance_to_offer, 3),
            "matched_terms": list(self.matched_terms),
            "expected_conversion_relevance": round(self.expected_conversion_relevance, 3),
            "competition_estimate": self.competition_estimate,
            "competition_note": self.competition_note,
            "content_opportunity_score": round(self.content_opportunity_score, 3),
            "recommend_page": self.recommend_page,
            "already_has_a_page": self.already_has_a_page,
        }


def score_content_opportunity(*, opportunity_id: str, title: str, source: str,
                              evidence: tuple, offer: AffiliateOffer,
                              draft, already_has_a_page: bool) -> ContentOpportunity:
    """Pure scoring step - `draft` is an already-built `OpportunityDraft`
    (the caller decides how to obtain it; see `find_content_opportunities()`
    for the real, persisted-record path)."""
    matches = match_offers(draft, [offer], min_score=0.0)
    match = matches[0] if matches else None
    relevance = match.match_score if match else 0.0
    matched_terms = tuple(match.matched_terms) if match else ()
    intent = _demand_strength(draft)

    # never fully zeroed by a weak (but real) buyer-intent score - a
    # topically relevant, real question is still worth writing about even
    # when demand_signal.py scored its purchase-intent strength low (same
    # "(0.5 + 0.5*x)" damping convention affiliate_profitability.py already
    # uses for automation_level).
    score = round(relevance * (0.5 + 0.5 * intent), 3)
    has_specific_match = bool(set(matched_terms) - _GENERIC_FILLER_WORDS)
    recommend = (relevance >= _MIN_RELEVANCE and bool(evidence) and not already_has_a_page
                and has_specific_match)

    return ContentOpportunity(
        opportunity_id=opportunity_id, title=title, source=source,
        demand_evidence=tuple(evidence), buyer_intent=intent,
        relevance_to_offer=relevance, matched_terms=matched_terms,
        expected_conversion_relevance=relevance,
        content_opportunity_score=score, recommend_page=recommend,
        already_has_a_page=already_has_a_page)


def find_content_opportunities(data_dir, *, offer_id: str, limit: int = 200) -> list[ContentOpportunity]:
    """Scan every REAL, already-discovered/verified opportunity on file
    (never synthetic, never an editorial pick - see `_EXCLUDED_SOURCE_TYPES`)
    for topical relevance to one already-ingested `AffiliateOffer`. Ranked
    highest content_opportunity_score first. `already_has_a_page=True`
    entries are still returned (visibility) but `recommend_page` is always
    False for them (duplicate prevention - never propose a second page for
    a topic we already cover)."""
    from .affiliate_model import AffiliateAssetStore, AffiliateOfferStore
    from .pipeline import draft_from_record
    from ..opportunity_store import load_opportunities

    offer = AffiliateOfferStore.load(data_dir).get(offer_id)
    if offer is None:
        return []

    covered_opportunity_ids = {a.opportunity_id for a in AffiliateAssetStore.load(data_dir).all()}

    out: list[ContentOpportunity] = []
    for rec in load_opportunities(data_dir).all()[:limit]:
        d = rec.get("discovery") or {}
        if rec.get("origin") != model.ORIGIN_REAL:
            continue
        if d.get("source_type") in _EXCLUDED_SOURCE_TYPES:
            continue
        evidence = tuple(e for e in (d.get("evidence") or []) if str(e).strip())
        if not evidence:
            continue
        draft = draft_from_record(rec)
        out.append(score_content_opportunity(
            opportunity_id=rec["id"], title=rec.get("title", ""),
            source=d.get("source", ""), evidence=evidence, offer=offer, draft=draft,
            already_has_a_page=rec["id"] in covered_opportunity_ids))

    out.sort(key=lambda c: (-c.content_opportunity_score, c.opportunity_id))
    return out


def content_opportunity_report(data_dir, *, offer_id: str) -> dict:
    """Read-model wrapper for CLI/JARVIS - a plain list would work too, but
    this makes the "nothing cleared the bar right now" case explicit and
    countable rather than an ambiguous empty list."""
    opportunities = find_content_opportunities(data_dir, offer_id=offer_id)
    recommended = [c for c in opportunities if c.recommend_page]
    return {
        "offer_id": offer_id,
        "scanned": len(opportunities),
        "recommended_count": len(recommended),
        "recommended": [c.to_dict() for c in recommended],
        "all_candidates": [c.to_dict() for c in opportunities],
        "note": ("no real, independently-arising demand signal currently clears "
                "the relevance bar for a new page - this is a report of the "
                "CURRENT real data, not a permanent verdict; re-run after more "
                "discovery" if not recommended else ""),
    }
