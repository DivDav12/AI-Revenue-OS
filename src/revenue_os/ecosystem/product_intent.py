"""Product Intent extraction (Demand-First Affiliate architecture, Step 1).

The first bridge in:

    DemandSignal -> DemandEvidence -> ProductIntent -> Opportunity

`extract_product_intent()` turns a real demand signal's TITLE into a
structured guess at WHAT PRODUCT CATEGORY the poster is trying to buy or
replace - e.g. "what BT earbuds would you recommend?" -> category_phrase
"bt earbuds", intent "purchase_recommendation". This is deliberately the
ONLY thing this module does. It does NOT search for offers, does NOT call
an LLM, does NOT touch the network, and does NOT read or write
`demand_signal.py`'s scoring in any way - `classify_purchase_intent()`,
`score_demand_quality()`, `buyer_confidence()`/`problem_confidence()` are
completely unaffected by this module's existence (verified by the
regression tests in test_product_intent.py and the existing
test_demand_sources.py suite).

Same marker-based, structurally-anchored TECHNIQUE as
`demand_signal.py`'s `_has_topic_anchor()` (a concrete word must actually
be present in the text - never a filler/pronoun alone), but a SEPARATE,
independent rule table: `demand_signal.py`'s markers classify INTENT
STRENGTH (EXPLICIT/PROBLEM/HELP/NONE) and were never designed to capture
WHICH noun phrase follows them. This module adds capture groups around
its own small set of recommendation/replacement patterns - it does not
read from, or write into, `_EXPLICIT_INTENT_MARKERS`/
`_PROBLEM_INTEREST_MARKERS`.

Fails closed by construction (spec: "keine erfundenen Kategorien"):
  - no pattern match at all               -> category_phrase="", intent=""
  - a pattern matches but the captured
    phrase is empty, a filler/pronoun, or
    a non-product word ("advice", "help",
    "someone", ...)                        -> category_phrase="", intent=""
  - a captured phrase longer than 6 words  -> rejected (a regex that grabbed
                                              a whole clause instead of a
                                              noun phrase, not a real category)
  - `provenance` is ESTIMATED for a real extraction, UNKNOWN when nothing
    was derived - NEVER FACT (a category guess is always an
    interpretation of the text, never a verbatim fact the poster stated
    as such).

`constraints` reuses `DemandEvidence.budget` - the ALREADY-extracted,
provenance-tagged budget from `demand_signal.py` - if (and only if) it is
a real, non-estimated, positive amount. No new budget extraction is
introduced here.

Pure, deterministic, offline. No I/O, no LLM, no network, no randomness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .demand_signal import ESTIMATED, UNKNOWN, DemandEvidence

# ---------------------------------------------------------------------------
# intent vocabulary
# ---------------------------------------------------------------------------

INTENT_PURCHASE_RECOMMENDATION = "purchase_recommendation"
INTENT_REPLACEMENT = "replacement"
INTENT_NONE = ""
PRODUCT_INTENTS = (INTENT_PURCHASE_RECOMMENDATION, INTENT_REPLACEMENT, INTENT_NONE)


@dataclass(frozen=True)
class ProductIntent:
    """A structured, fail-closed guess at what product a demand signal is
    about. Every field defaults to the "we could not safely tell" state -
    never a guess dressed up as a fact (same convention as
    `demand_signal.PaymentEvidence`)."""

    category_phrase: str = ""
    intent: str = INTENT_NONE
    constraints: tuple = ()
    provenance: str = UNKNOWN

    def to_dict(self) -> dict:
        return {
            "category_phrase": self.category_phrase,
            "intent": self.intent,
            "constraints": list(self.constraints),
            "provenance": self.provenance,
        }


# ---------------------------------------------------------------------------
# category-phrase cleanup - strip leading determiners/adjectives, reject
# non-product fillers. Conservative on purpose: a false NEGATIVE (missing a
# real category) is the safe failure mode here, exactly like every other
# extraction in demand_signal.py - a false POSITIVE (inventing a category)
# is what this module must never do.
# ---------------------------------------------------------------------------

_LEADING_FILLERS: tuple[str, ...] = (
    "a ", "an ", "some ", "good ", "great ", "decent ", "cheap ",
    "affordable ", "new ", "nice ", "solid ", "the ", "my ", "our ",
)

#: words a capture group can technically match but that are never a
#: product category on their own - generic nouns/pronouns a "looking
#: for X" / "any recommendations for X" pattern can otherwise latch onto
#: (spec: "Text, aus dem keine sichere Produktkategorie hervorgeht").
_NON_PRODUCT_WORDS = frozenset({
    "advice", "help", "feedback", "someone", "somebody", "people", "anyone",
    "opinions", "thoughts", "recommendations", "suggestions", "ideas",
    "answers", "guidance", "input", "tips", "info", "information",
    "it", "this", "that", "one", "something", "anything", "everything",
    "nothing", "options", "alternatives",
})

#: a captured phrase this long is almost certainly a whole clause the
#: regex over-matched, not a product noun phrase - reject rather than
#: return something misleading.
_MAX_PHRASE_WORDS = 6


def _clean_phrase(raw: str) -> str:
    p = raw.strip().strip(".,!?;:").strip().lower()
    changed = True
    while changed:
        changed = False
        for filler in _LEADING_FILLERS:
            if p.startswith(filler):
                p = p[len(filler):]
                changed = True
    p = p.strip()
    if not p:
        return ""
    words = p.split()
    # reject both a bare non-product word AND a phrase that merely STARTS
    # with one ("someone in this situation") - a trailing qualifier never
    # turns a pronoun/filler into a product category.
    if p in _NON_PRODUCT_WORDS or words[0] in _NON_PRODUCT_WORDS:
        return ""
    if len(words) > _MAX_PHRASE_WORDS:
        return ""
    return p


# ---------------------------------------------------------------------------
# rule table - (intent, compiled regex with exactly one capture group).
# Tried in order; the first rule that both matches AND survives
# `_clean_phrase()` wins (mirrors demand_signal.py's `_first_match`
# priority-order convention). Runs ONLY against the signal's TITLE - the
# one field this module's signature actually receives as raw text; a
# separate PaymentEvidence-style extension to also scan body text is a
# later, explicit decision, not implied by this step.
# ---------------------------------------------------------------------------

_WORD = r"[a-z][a-z0-9 \-]{1,60}?"

_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (INTENT_REPLACEMENT,
     re.compile(r"\bneed(?:s|ed)?\s+(?:(?:a|an)\b)?\s*replacement\s+(?:for\s+)?"
                r"(?:(?:a|an|my|the|our)\b)?\s*(" + _WORD + r")\s*[.?!]*$")),
    (INTENT_REPLACEMENT,
     re.compile(r"\bmy current\s+(" + _WORD + r")\s+(?:is|has been|keeps)\s+"
                r"(?:really\s+|very\s+|quite\s+|so\s+)?"
                r"(?:bad|broken|noisy|dying|failing|worn out|acting up|not working)\b")),
    (INTENT_PURCHASE_RECOMMENDATION,
     re.compile(r"\b(?:what|which)\s+(" + _WORD + r")\s+would you recommend\b")),
    (INTENT_PURCHASE_RECOMMENDATION,
     re.compile(r"\b(?:which|what)\s+(" + _WORD + r")\s+should i (?:buy|get)\b")),
    (INTENT_PURCHASE_RECOMMENDATION,
     re.compile(r"\bany recommendations for\s+(?:(?:a|an)\b)?\s*(" + _WORD + r")\s*[.?!]*$")),
    (INTENT_PURCHASE_RECOMMENDATION,
     re.compile(r"\blooking for\s+(?:(?:a|an|some)\b)?\s*(" + _WORD + r")\s*[.?!]*$")),
    (INTENT_PURCHASE_RECOMMENDATION,
     re.compile(r"\b(?:recommend a good|can you recommend a|can anyone recommend a)\s+"
                r"(" + _WORD + r")\s*[.?!]*$")),
)


def _extract_category_and_intent(title: str) -> tuple[str, str]:
    blob = (title or "").strip().lower()
    if not blob:
        return "", INTENT_NONE
    for intent, rx in _RULES:
        m = rx.search(blob)
        if not m:
            continue
        phrase = _clean_phrase(m.group(1))
        if phrase:
            return phrase, intent
    return "", INTENT_NONE


# ---------------------------------------------------------------------------
# constraints - reuses the ALREADY-extracted DemandEvidence.budget, no new
# extraction. Only a real, source-stated, non-estimated amount counts
# (same FACT bar `demand_signal.provenance_summary()` already applies).
# ---------------------------------------------------------------------------

def _extract_constraints(evidence: DemandEvidence) -> tuple:
    b = evidence.budget
    if b.amount > 0 and not b.is_estimate:
        amount = f"{b.amount:g}" if b.amount != int(b.amount) else str(int(b.amount))
        return (f"budget:{amount}{b.currency}",)
    return ()


def extract_product_intent(evidence: DemandEvidence, *, title: str) -> ProductIntent:
    """The single entry point. Pure text (title) + already-extracted
    evidence (budget only) in, a fail-closed `ProductIntent` out. Never
    raises, never guesses a category the title does not structurally
    support."""
    category, intent = _extract_category_and_intent(title)
    if not category:
        return ProductIntent()
    return ProductIntent(
        category_phrase=category, intent=intent,
        constraints=_extract_constraints(evidence), provenance=ESTIMATED)
