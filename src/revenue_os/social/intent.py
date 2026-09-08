"""The "should we even respond?" gate (marketing logic).

Before any content is produced the agent must be able to answer YES to:
"Does this thread / person actually have a problem or a buying intent
that this product category fits?"

Deterministic, offline. Reuses the EXISTING demand-quality extraction
(`ecosystem/demand_signal.py`) and the EXISTING product-category
extraction (`ecosystem/product_intent.py`) - it adds no new heuristic and
never reads a numeric score. Same idea as the WIP
`ecosystem/browser_demand.classify_browser_demand`, rebuilt here as a
small clean function so this subpackage does not depend on unfinished
work.

RELEVANT  - explicit purchase intent OR a concrete product problem, AND a
            concrete product category was extracted, AND the poster is the
            one asking (not a seller promoting their own thing).
REJECT    - everything else (general chat, news, a review with no buying
            intent, a supplier post, a help request with no product named).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ecosystem import demand_signal, product_intent

RELEVANT = "RELEVANT"
REJECT = "REJECT"

# how confident the category signal is - purely for the human-facing draft,
# never gates anything on its own.
STRENGTH_HIGH = "HIGH"       # explicit purchase intent + category
STRENGTH_MEDIUM = "MEDIUM"   # problem / replacement need + category
STRENGTH_NONE = "NONE"


@dataclass(frozen=True)
class DemandAssessment:
    decision: str                 # RELEVANT | REJECT
    strength: str
    reason: str
    intent_level: str = demand_signal.INTENT_NONE
    intent_marker: str = ""
    perspective: str = demand_signal.PERSPECTIVE_UNKNOWN
    category_phrase: str = ""
    product_intent_kind: str = product_intent.INTENT_NONE

    @property
    def relevant(self) -> bool:
        return self.decision == RELEVANT

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "strength": self.strength,
            "reason": self.reason, "intent_level": self.intent_level,
            "intent_marker": self.intent_marker, "perspective": self.perspective,
            "category_phrase": self.category_phrase,
            "product_intent_kind": self.product_intent_kind,
        }


def assess_text(*, title: str, body: str = "", discovered_at: str = "") -> DemandAssessment:
    """Classify one raw post (title + body) for buying relevance."""
    blob = f"{title}\n{body}".strip()
    intent_level, marker = demand_signal.classify_purchase_intent(blob)
    evidence = demand_signal.build_demand_evidence(
        blob, title=title, discovered_at=discovered_at)
    pin = product_intent.extract_product_intent(evidence, title=title)
    perspective = evidence.perspective
    cat = pin.category_phrase

    base = dict(intent_level=intent_level, intent_marker=marker,
                perspective=perspective, category_phrase=cat,
                product_intent_kind=pin.intent)

    if perspective == demand_signal.PERSPECTIVE_SUPPLIER:
        return DemandAssessment(
            REJECT, STRENGTH_NONE,
            "poster is presenting / promoting their own product, not asking "
            "to buy one", **base)

    if intent_level == demand_signal.INTENT_EXPLICIT and cat:
        return DemandAssessment(
            RELEVANT, STRENGTH_HIGH,
            f"explicit purchase intent ({marker!r}) for a concrete product "
            f"category ({cat!r})", **base)

    if intent_level == demand_signal.INTENT_PROBLEM and cat:
        return DemandAssessment(
            RELEVANT, STRENGTH_MEDIUM,
            f"a concrete product problem / replacement need ({marker!r}) for "
            f"{cat!r}", **base)

    if intent_level in (demand_signal.INTENT_EXPLICIT, demand_signal.INTENT_PROBLEM):
        return DemandAssessment(
            REJECT, STRENGTH_NONE,
            f"buying-shaped wording ({marker!r}) but no concrete product "
            "category could be extracted", **base)

    return DemandAssessment(
        REJECT, STRENGTH_NONE,
        "no purchase intent - general discussion / help request / news / "
        "review without buying intent", **base)


def assess_draft(draft) -> DemandAssessment:
    """Same, from an `ecosystem.model.OpportunityDraft` (e.g. one produced
    by `demand_sources.acq_record_to_draft`)."""
    return assess_text(
        title=draft.title or "",
        body=(draft.description or ""),
        discovered_at=getattr(draft, "discovered_at", "") or "")
