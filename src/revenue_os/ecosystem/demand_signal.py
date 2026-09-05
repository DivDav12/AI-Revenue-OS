"""Demand Quality Layer (spec: Demand-to-Revenue plan, Step 1).

Deterministic, evidence-based scoring for DIGITAL_PRODUCT-shaped demand
signals - distinguishes a genuine "I would pay for X" from a generic
help-request or a vague mention, WITHOUT ever inventing a budget, an
audience, or a fact that is not in the source text.

Deliberately scoped narrow for this step: this module only extracts
evidence from raw text and scores it. It does NOT touch
`opportunity_type`, `verification.py`, `discovery.py`, or any TASK-signal
logic - those integrations are a later step. Nothing here is a hard gate;
the score is advisory only, exactly like `task_signal.TaskQualityScore`.

    build_demand_evidence(text, ...) -> DemandEvidence   (pure extraction)
    score_demand_quality(evidence)   -> DemandQualityScore (pure scoring)
    score_demand_signal(text, ...)   -> DemandQualityScore (convenience: both)

Every derived fact is one of three provenance states, always distinguishable
(see `provenance_summary`):

    FACT       - a verbatim quote, a parsed count/date, or an unambiguous
                 structural match. Never a guess.
    ESTIMATED  - a deterministic judgement call (e.g. productizability)
                 that is not itself a fact quoted from the source.
    UNKNOWN    - nothing usable was found. Never filled with a default
                 that could be mistaken for a real observation.

Pure, deterministic. No I/O, no LLM, no network, no randomness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import model
from .model import PaymentEvidence

# ---------------------------------------------------------------------------
# provenance vocabulary
# ---------------------------------------------------------------------------

FACT = "FACT"
ESTIMATED = "ESTIMATED"
UNKNOWN = "UNKNOWN"
PROVENANCE_STATES = (FACT, ESTIMATED, UNKNOWN)


# ---------------------------------------------------------------------------
# purchase-intent strength - evidence-only, priority-ordered, never guessed
# from tone/sentiment. A help-request alone NEVER escalates to purchase
# intent (spec: "I need help" must not become buying intent just because
# it is a request for help).
# ---------------------------------------------------------------------------

INTENT_EXPLICIT = "EXPLICIT_PURCHASE_INTENT"
INTENT_PROBLEM = "PROBLEM_INTEREST"
INTENT_HELP = "HELP_REQUEST"
INTENT_NONE = "NONE"
INTENT_LEVELS = (INTENT_EXPLICIT, INTENT_PROBLEM, INTENT_HELP, INTENT_NONE)

_EXPLICIT_INTENT_MARKERS = (
    "i would pay", "i'd pay", "id pay", "we would pay", "we'd pay",
    "willing to pay", "happy to pay", "glad to pay", "take my money",
    "shut up and take my money", "i'll pay for", "i will pay for",
    "we'll pay for", "we will pay for", "would gladly pay",
    "would happily pay", "ready to pay", "id happily pay",
    # bare (no pronoun anchor) - deliberately safe against negation:
    # "would not pay" / "wouldn't pay" do NOT contain "would pay" as a
    # contiguous substring, so this does not misfire on a refusal.
    "would pay", "will pay for",
)
_PROBLEM_INTEREST_MARKERS = (
    "is there a tool that", "is there a service that", "is there software that",
    "is there an app that", "does anyone know a tool", "does anyone know a service",
    "does anyone know of a tool", "looking for a tool that",
    "looking for a service that", "looking for software that",
    "looking for an app that", "recommend a tool for", "recommend a tool that",
    "any tool for", "any tool that", "any recommendations for a tool",
    "what tool do you use for", "what's the best tool for",
    "whats the best tool for", "anyone know a good tool",
    # Additive extension (Demand Discovery expansion - real, physical-
    # product buy-recommendation demand, e.g. "which USB mic should I buy
    # under EUR 50?"): every marker above is scoped to
    # tool/service/software/app wording, so it never fires on a genuine
    # product-recommendation question that names no digital category at
    # all. These markers are deliberately PRODUCT-AGNOSTIC (no "tool"/
    # "app"/"software" anchor) so they work for hardware/consumer-product
    # demand exactly like the software case, without touching a single
    # existing marker/order/quote (kept ADDITIVE, nothing removed - same
    # rule as DEFAULT_QUERIES's own expansion in demand_sources.py).
    "should i buy", "should i get", "what should i buy",
    "which one should i buy", "which one should i get", "worth buying",
    "before you buy", "any recommendations for", "in the market for",
    "looking to buy", "can you recommend a", "can anyone recommend",
    "what would you recommend for", "recommend a good",
    # found on a real 'demand-lemmy-buying' live-validation run (2026-09):
    # a genuine buy-recommendation question ("what BT earbuds would you
    # recommend?") carries NEITHER a "for" clause nor a tool/software
    # anchor - bare "would you recommend"/"any recommendations" (no
    # trailing qualifier) close that gap. UNLIKE every other marker in
    # this tuple, these two name no product/category of their own, so
    # they additionally require a topic anchor - see _UNANCHORED_MARKERS
    # / _has_topic_anchor() below - before classify_purchase_intent()
    # accepts them. Kept HERE, unmodified, not removed: the anchor
    # requirement is an extra guard applied at classification time, not a
    # change to this marker list.
    "would you recommend", "any recommendations",
)
_HELP_REQUEST_MARKERS = (
    "i need help", "we need help", "can someone help", "could someone help",
    "how do i", "how can i", "how would i", "need advice", "need some advice",
    "any advice", "please help",
)

#: the two _PROBLEM_INTEREST_MARKERS entries with no product/category
#: anchor of their own (every other marker either names "tool"/"service"/
#: "app"/"software", or a commerce verb like "buy"/"buying"). A live
#: 'demand-lemmy-buying' validation run (2026-09) found a bare "would you
#: recommend" firing on a topically unrelated free-time/mental-health
#: question ("I want to try some things... What would you recommend? Any
#: good ideas?") purely because the phrase appears somewhere in the text -
#: the same class of topic-blindness already documented for
#: classify_purchase_intent() elsewhere (see
#: test_demand_ranking_live_validation.py's KnownFailureModeRegressionTests),
#: just newly observable because these two markers have no qualifier to
#: fall back on. classify_purchase_intent() only accepts a hit on one of
#: these two when _has_topic_anchor() finds a concrete word immediately
#: before the phrase (e.g. "BT earbuds would you recommend") - every other
#: marker is completely unaffected by this check.
_UNANCHORED_MARKERS = frozenset({"would you recommend", "any recommendations"})

#: question/filler words that do NOT count as a topic anchor - a bare
#: "What would you recommend?" (nothing named) must still fail the check.
#: Small, generic, closed-class function words - not a topic/category
#: classifier and not specific to any one product.
_ANCHOR_FILLER_WORDS = frozenset({
    "what", "so", "and", "or", "but", "then", "now", "just", "really",
    "also", "still", "please", "you", "i", "we", "there", "else",
})
#: the single word immediately before an unanchored marker, if any -
#: deliberately just ONE adjacent word (not a full noun-phrase parse):
#: enough to tell "BT earbuds would you recommend" (anchored - "earbuds")
#: apart from "What would you recommend" (not anchored - "what"), without
#: any new NLP/topic machinery.
_ANCHOR_WORD_RE = re.compile(
    r"([a-z][a-z\-']{2,})\s+(?:would you recommend|any recommendations)\b")


def _has_topic_anchor(blob: str) -> bool:
    m = _ANCHOR_WORD_RE.search(blob)
    return bool(m) and m.group(1) not in _ANCHOR_FILLER_WORDS


def _first_match(blob: str, markers: tuple[str, ...]) -> str:
    return next((m for m in markers if m in blob), "")


def classify_purchase_intent(text: str) -> tuple[str, str]:
    """(level, matched_marker). Priority EXPLICIT > PROBLEM > HELP > NONE -
    a stronger signal, if present, always wins; weaker markers elsewhere in
    the same text do not dilute it.

    One extra, narrowly-scoped guard: a PROBLEM match on one of the two
    _UNANCHORED_MARKERS (see there) is only accepted when
    _has_topic_anchor() confirms a concrete word precedes it in the same
    text - otherwise classification falls through to HELP/NONE exactly as
    if that marker had not matched at all. Every other marker in
    _PROBLEM_INTEREST_MARKERS is completely unaffected."""
    blob = (text or "").lower()
    hit = _first_match(blob, _EXPLICIT_INTENT_MARKERS)
    if hit:
        return INTENT_EXPLICIT, hit
    hit = _first_match(blob, _PROBLEM_INTEREST_MARKERS)
    if hit and (hit not in _UNANCHORED_MARKERS or _has_topic_anchor(blob)):
        return INTENT_PROBLEM, hit
    hit = _first_match(blob, _HELP_REQUEST_MARKERS)
    if hit:
        return INTENT_HELP, hit
    return INTENT_NONE, ""


# ---------------------------------------------------------------------------
# post perspective - a purely STRUCTURAL signal (title-prefix / question
# shape), deliberately kept OUT of score_demand_quality()'s weighted sum
# (spec: Demand Validation phase - "kein Score-Threshold, keine Änderung
# der bestehenden Gewichte"). Empirical finding this responds to: many
# high-scoring false positives are SUPPLIER-side posts (a founder pitching
# their OWN product/pricing) that happen to contain the same "would pay" /
# "is there a service" phrasing our intent markers look for - neither
# `score` nor `intent_level` distinguishes "I would pay for X" (asker) from
# "here's what my service costs" (supplier). This field makes that
# distinction available for a LATER, separate filtering decision - it is
# advisory data only here, never consulted by the score.
# ---------------------------------------------------------------------------

PERSPECTIVE_ASKER = "ASKER"          # the poster is seeking/asking for something
PERSPECTIVE_SUPPLIER = "SUPPLIER"    # the poster is presenting/promoting their own thing
PERSPECTIVE_UNKNOWN = "UNKNOWN"      # no structural marker either way
PERSPECTIVES = (PERSPECTIVE_ASKER, PERSPECTIVE_SUPPLIER, PERSPECTIVE_UNKNOWN)

#: "Ask HN"/"Ask YC"/a bare "Ask:" are HN's own request convention; a
#: trailing "?" is a cheap, general, deterministic structural marker for
#: "the poster is asking something" that also catches titles Algolia
#: returns with the "Ask HN:" prefix stripped.
_ASKER_TITLE_PREFIXES = ("ask hn:", "ask yc:", "ask:")

#: HN's own posting conventions (real, documented community norms, not a
#: guess): "Show HN" = show what you built, "Launch HN" = launching a
#: product, "Tell HN" = sharing a story/lesson - all self-presentation,
#: never a request. Matched only as a TITLE PREFIX (case-insensitive),
#: never a bare substring, so a title that merely mentions these words
#: mid-sentence does not misfire. Keyed by the SPECIFIC prefix type
#: (spec: Decision-Model step - "Supplier-Penalty soll spaeter zwischen
#: Show/Launch und Tell unterscheiden koennen") - `Tell HN:` posts are
#: empirically NOT reliably self-promotional (two known real
#: purchase-intent signals used it), while `Show HN:`/`Launch HN:` were
#: never wrong in the 64-signal validation. No new marker: same three
#: strings as before, just no longer collapsed into one bucket.
PREFIX_SHOW = "SHOW"
PREFIX_LAUNCH = "LAUNCH"
PREFIX_TELL = "TELL"
PREFIX_ASK = "ASK"
PREFIX_UNKNOWN = "UNKNOWN"
TITLE_PREFIX_TYPES = (PREFIX_SHOW, PREFIX_LAUNCH, PREFIX_TELL, PREFIX_ASK, PREFIX_UNKNOWN)

_SUPPLIER_PREFIX_TYPES: dict[str, str] = {
    "show hn:": PREFIX_SHOW,
    "launch hn:": PREFIX_LAUNCH,
    "tell hn:": PREFIX_TELL,
}
#: kept for readability/back-compat with the perspective docstring below -
#: derived from the same dict above, not a second independent list.
_SUPPLIER_TITLE_PREFIXES = tuple(_SUPPLIER_PREFIX_TYPES)


def classify_title_prefix_type(title: str) -> str:
    """Which SPECIFIC title convention matched - SHOW/LAUNCH/TELL/ASK, or
    UNKNOWN. A pure refinement of `classify_post_perspective`: exactly
    the same markers, just reporting which one fired instead of
    collapsing Show/Launch/Tell into one SUPPLIER bucket. This step only
    returns the evidence - nothing yet CONSUMES the distinction (see
    `demand_ranking.py`'s module docstring for why the supplier penalty
    there is still flat for now)."""
    t = (title or "").strip().lower()
    if not t:
        return PREFIX_UNKNOWN
    for prefix, ptype in _SUPPLIER_PREFIX_TYPES.items():
        if t.startswith(prefix):
            return ptype
    if any(t.startswith(p) for p in _ASKER_TITLE_PREFIXES):
        return PREFIX_ASK
    return PREFIX_UNKNOWN


def classify_post_perspective(title: str) -> str:
    """Evidence-based, title-structure-only classification - never reads
    the body text, never affects intent/budget/productizability/score.
    Unchanged behavior/signature - now implemented on top of
    `classify_title_prefix_type` so the two never drift apart."""
    t = (title or "").strip().lower()
    if not t:
        return PERSPECTIVE_UNKNOWN
    prefix = classify_title_prefix_type(title)
    if prefix in (PREFIX_SHOW, PREFIX_LAUNCH, PREFIX_TELL):
        return PERSPECTIVE_SUPPLIER
    if prefix == PREFIX_ASK or t.endswith("?"):
        return PERSPECTIVE_ASKER
    return PERSPECTIVE_UNKNOWN


# ---------------------------------------------------------------------------
# budget extraction - only a concrete amount in a payment-context sentence,
# never a bare number found anywhere in the text, never a vague range.
# ---------------------------------------------------------------------------

_BUDGET_CONTEXT_MARKERS = ("pay", "budget", "spend", "afford", "cost",
                          "price", "willing", "charge")
_VAGUE_BUDGET_MARKERS = ("up to", "around", "roughly", "about ", "~",
                         "maybe", "somewhere between", "give or take",
                         "approximately")
_CURRENCY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\$\s?(\d+(?:\.\d+)?)"), "USD"),
    (re.compile(r"€\s?(\d+(?:\.\d+)?)"), "EUR"),
    (re.compile(r"£\s?(\d+(?:\.\d+)?)"), "GBP"),
    (re.compile(r"(\d+(?:\.\d+)?)\s?(?:eur|euros?)\b", re.I), "EUR"),
    (re.compile(r"(\d+(?:\.\d+)?)\s?(?:usd|dollars?|bucks)\b", re.I), "USD"),
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def extract_stated_budget(text: str) -> PaymentEvidence:
    """A concrete amount from a payment-context sentence - never a guess,
    never a vague range. Returns the "we know nothing" default
    `PaymentEvidence()` (amount=0, UNCLEAR, is_estimate=True) when nothing
    qualifies - that default is never mistaken for a real observation
    because callers check `amount > 0 and not is_estimate` (see
    `provenance_summary`)."""
    for sentence in _sentences(text):
        low = sentence.lower()
        if not any(m in low for m in _BUDGET_CONTEXT_MARKERS):
            continue
        if any(m in low for m in _VAGUE_BUDGET_MARKERS):
            continue
        for pattern, currency in _CURRENCY_PATTERNS:
            m = pattern.search(sentence)
            if not m:
                continue
            try:
                amount = float(m.group(1))
            except ValueError:
                continue
            if amount <= 0:
                continue
            return PaymentEvidence(
                amount=amount, currency=currency,
                conditions=model.PAY_CONDITIONAL, is_estimate=False,
                evidence=(sentence.strip(),))
    return PaymentEvidence()


# ---------------------------------------------------------------------------
# builder / provider signal - a SECOND, INDEPENDENT structural signal, kept
# OUT of score_demand_quality() exactly like `perspective` (spec: Demand
# Validation phase, step 2 - "keinen Score-Threshold, keine Änderung der
# bestehenden Gewichte, Perspective nicht in den Score aufnehmen"). Same
# rule applies here: advisory evidence only, never a gate.
#
# Empirical motivation (64-signal cross-tab, see the perspective step):
# `perspective == SUPPLIER` only ever fires on the three HN title
# conventions (Show/Launch/Tell HN:) - it caught 7 of 23 real false
# positives and cost 2 real "I would pay" positives (both via `Tell HN:`,
# which is NOT reliably self-promotional). The other 15 false positives
# were founder/builder posts using plain `Ask HN:` phrasing with no
# title convention at all - invisible to a title-only signal. This marker
# instead scans the actual BODY TEXT (title included) for a first-person
# builder/provider claim, independent of any HN title convention.
#
# Deliberately sentence-scoped and qualifier-aware (spec: "vermeide False
# Positives wie 'I built this because I needed...' ... wenn der Post
# trotzdem echte Käufernachfrage enthält") - a bare builder-phrase match
# is NOT enough on its own:
#   - a same-sentence PERSONAL-USE qualifier ("for myself", "because I
#     needed") means the builder-phrase is backstory for the poster's OWN
#     need, not a pitch to anyone else - does not fire.
#   - a same-sentence bare PAST-EXPERIENCE qualifier ("before", "years
#     ago") with no accompanying OFFER marker in that sentence means the
#     builder-phrase is a general capability mention, not a current pitch
#     ("we built systems like this before") - does not fire UNLESS an
#     offer marker is also present ("I built X - anyone need it?" still
#     fires, because "anyone need" is an offer marker in the same
#     sentence).
# ---------------------------------------------------------------------------

BUILDER_YES = "BUILDER_YES"          # a first-person builder/provider claim was found
BUILDER_UNKNOWN = "BUILDER_UNKNOWN"  # no such claim was found
#: no BUILDER_NO state - a marker search can only ever assert "a
#: builder-phrase was found" (YES); absence of a phrase never lets us
#: safely assert the poster definitely is NOT a builder (same rule as
#: PROVENANCE_STATES: FACT/ESTIMATED/UNKNOWN never invents a false
#: negative either). See `provenance_summary`.
BUILDER_SIGNAL_STATES = (BUILDER_YES, BUILDER_UNKNOWN)

#: exact phrases from the spec plus their natural perfect-tense
#: contractions (same underlying claim, not a new category).
_BUILDER_MARKERS = (
    "i built", "i've built", "i have built",
    "i made", "i've made", "i have made",
    "i created", "i've created", "i have created",
    "i'm building", "i am building",
    "we built", "we've built", "we have built",
    "we made", "we've made", "we have made",
    "we created", "we've created", "we have created",
    "we're building", "we are building",
    "my product", "my saas",
    "our product", "our service", "our saas",
    "i launched", "i've launched", "i have launched",
    "we launched", "we've launched", "we have launched",
    "i just launched", "we just launched",
    "i developed", "i've developed", "i have developed",
    "we developed", "we've developed", "we have developed",
)
#: word-boundary-anchored, not a bare substring search (spec: "vermeide
#: ... stumpf nach einzelnen Wörtern suchen") - a naive `in` check on "my
#: product" also matches inside "my productivity", which is not a
#: builder claim at all (caught empirically on the 64-signal set, see
#: the perspective-step docstring above for that validation).
_BUILDER_MARKER_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(r"\b" + re.escape(m) + r"\b") for m in _BUILDER_MARKERS)
#: turns a builder-phrase into personal-use backstory, not a pitch to
#: others - e.g. "I built this because I needed a way to track expenses".
_BUILDER_SELF_USE_QUALIFIERS = (
    "because i needed", "because we needed", "for myself", "for my own use",
    "for my own", "just for me", "to scratch my own itch",
    "for personal use", "for our own use",
)
#: a bare mention of past capability ("we built systems like this
#: before") - only disqualifying when no offer marker is present too.
_BUILDER_PAST_EXPERIENCE_QUALIFIERS = ("before", "in the past", "previously", "years ago")
#: same-sentence evidence that the builder-phrase IS a current pitch, not
#: backstory - overrides the past-experience qualifier above.
_BUILDER_OFFER_MARKERS = (
    "anyone need", "would love your feedback", "would love feedback",
    "check it out", "sign up", "free tier", "let me know what you think",
    "here's what it", "here is what it", "we're live", "available now",
    "try it out", "feedback welcome", "http",
)


def classify_builder_signal(text: str, *, title: str = "") -> tuple[str, str]:
    """(state, quote). Scans title+body, sentence by sentence, for a
    first-person builder/provider claim - see the module comment above
    for exactly which same-sentence qualifiers suppress a match. Never
    reads score/intent/perspective; never affects them either."""
    blob = f"{title} {text}".strip()
    for sentence in _sentences(blob):
        low = sentence.lower()
        if not any(p.search(low) for p in _BUILDER_MARKER_RES):
            continue
        if any(q in low for q in _BUILDER_SELF_USE_QUALIFIERS):
            continue
        if (any(q in low for q in _BUILDER_PAST_EXPERIENCE_QUALIFIERS)
                and not any(o in low for o in _BUILDER_OFFER_MARKERS)):
            continue
        return BUILDER_YES, sentence.strip()
    return BUILDER_UNKNOWN, ""


# ---------------------------------------------------------------------------
# audience extraction - a curated, conservative marker list. False
# negatives (UNKNOWN) are the safe failure mode; this never paraphrases or
# infers an audience that was not named.
# ---------------------------------------------------------------------------

_AUDIENCE_MARKERS = (
    "as a solo founder", "as a freelance", "as a small agency",
    "as a startup founder", "as an indie hacker", "as a solo developer",
    "as a consultant", "as a small business owner", "as a freelancer",
    "for small agencies", "for freelancers", "for solo founders",
    "for indie hackers", "for small teams", "for small businesses",
    "for solo developers", "our small team", "our small agency",
    "i run a small", "i run an agency", "i freelance as a",
)


def extract_audience(text: str) -> str:
    """The verbatim sentence naming the audience, or "" (UNKNOWN)."""
    for sentence in _sentences(text):
        if any(m in sentence.lower() for m in _AUDIENCE_MARKERS):
            return sentence.strip()
    return ""


# ---------------------------------------------------------------------------
# urgency
# ---------------------------------------------------------------------------

_URGENCY_MARKERS = (
    "right now", "urgently", "urgent", "asap", "as soon as possible",
    "immediately", "this week", "by tomorrow", "before next week",
    "need this today", "critical", "time-sensitive", "time sensitive",
)


def find_urgency_markers(text: str) -> tuple[str, ...]:
    low = (text or "").lower()
    return tuple(m for m in _URGENCY_MARKERS if m in low)


# ---------------------------------------------------------------------------
# productizability - can the EXISTING BUILD_PRODUCT/deliverable stack
# plausibly fulfil this as a digital product? Deterministic marker-based
# assessment, not a fact about the world - always ESTIMATED provenance.
# ---------------------------------------------------------------------------

PRODUCTIZABLE_HIGH = "HIGH"
PRODUCTIZABLE_MEDIUM = "MEDIUM"
PRODUCTIZABLE_LOW = "LOW"
PRODUCTIZABILITY_LEVELS = (PRODUCTIZABLE_HIGH, PRODUCTIZABLE_MEDIUM, PRODUCTIZABLE_LOW)

_NOT_PRODUCTIZABLE_MARKERS = (
    "hardware", "physical product", "ship physical", "medical advice",
    "legal advice", "diagnose", "prescription", "custom software development",
    "bespoke development", "consulting engagement", "in-person", "on-site",
    "physical device", "3d print", "manufacture", "surgery", "medication",
    "attorney", "lawsuit", "tax advice", "financial advice",
    "investment advice", "clinical", "therapy session",
)
_DIGITAL_FRIENDLY_MARKERS = (
    "spreadsheet", "template", "script", "automation", "dashboard",
    "report", "checklist", "guide", "plugin", "extension", "saas",
    "api", "integration", "generator", "calculator",
)


def assess_productizability(text: str) -> tuple[str, tuple[str, ...]]:
    """(level, matched_markers). A not-productizable marker always wins
    over a digital-friendly one (fail closed toward LOW, not HIGH, on
    mixed signals)."""
    low = (text or "").lower()
    blockers = tuple(m for m in _NOT_PRODUCTIZABLE_MARKERS if m in low)
    if blockers:
        return PRODUCTIZABLE_LOW, blockers
    friendly = tuple(m for m in _DIGITAL_FRIENDLY_MARKERS if m in low)
    if friendly:
        return PRODUCTIZABLE_HIGH, friendly
    return PRODUCTIZABLE_MEDIUM, ()


# ---------------------------------------------------------------------------
# signal age
# ---------------------------------------------------------------------------

def signal_age_days(discovered_at: str, *, now_iso: str = "") -> float | None:
    """Days since the signal was posted, or None (UNKNOWN) if
    `discovered_at` is empty/unparseable - never guessed."""
    if not str(discovered_at or "").strip():
        return None
    try:
        posted = datetime.fromisoformat(str(discovered_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        now = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
              if now_iso else datetime.now(timezone.utc))
    except ValueError:
        now = datetime.now(timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - posted).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# DemandEvidence - the structured, provenance-tagged extraction result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemandEvidence:
    intent_level: str = INTENT_NONE
    intent_quote: str = ""
    budget: PaymentEvidence = field(default_factory=PaymentEvidence)
    audience_quote: str = ""
    urgency_markers: tuple = ()
    productizability: str = PRODUCTIZABLE_MEDIUM
    productizability_reasons: tuple = ()
    age_days: float | None = None
    #: how many OTHER, independently-discovered opportunities look like
    #: this one - a real count the CALLER supplies (this module does no
    #: store access; wiring that count in is a later step, see the
    #: module docstring). 0 is ambiguous between "checked, none found"
    #: and "never checked" - see `provenance_summary`, which reports it
    #: as UNKNOWN rather than FACT when it is 0.
    repeat_signal_count: int = 0
    source_type: str = ""
    #: structural only (title shape) - advisory, NEVER read by
    #: score_demand_quality(). See the "post perspective" section above.
    perspective: str = PERSPECTIVE_UNKNOWN
    #: structural only (title+body builder/provider phrase) - advisory,
    #: NEVER read by score_demand_quality(). See the "builder / provider
    #: signal" section above. Independent of `perspective`.
    builder_signal: str = BUILDER_UNKNOWN
    builder_quote: str = ""
    #: which SPECIFIC title convention matched (SHOW/LAUNCH/TELL/ASK/
    #: UNKNOWN) - a refinement of `perspective`, not a replacement.
    #: Advisory only, never read by score_demand_quality(). Not yet
    #: consumed by anything (see classify_title_prefix_type docstring) -
    #: this step only returns the evidence cleanly.
    title_prefix_type: str = PREFIX_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "intent_level": self.intent_level, "intent_quote": self.intent_quote,
            "budget": self.budget.to_dict(),
            "audience_quote": self.audience_quote,
            "urgency_markers": list(self.urgency_markers),
            "productizability": self.productizability,
            "productizability_reasons": list(self.productizability_reasons),
            "age_days": self.age_days,
            "repeat_signal_count": self.repeat_signal_count,
            "source_type": self.source_type,
            "perspective": self.perspective,
            "builder_signal": self.builder_signal,
            "builder_quote": self.builder_quote,
            "title_prefix_type": self.title_prefix_type,
        }


def build_demand_evidence(text: str, *, title: str = "", discovered_at: str = "",
                          now_iso: str = "", repeat_signal_count: int = 0,
                          source_type: str = "") -> DemandEvidence:
    """The single evidence-extraction entry point - pure text in,
    structured, provenance-clean evidence out. `title` is OPTIONAL and
    used ONLY for the structural `perspective`/`builder_signal`/
    `title_prefix_type` fields (see classify_post_perspective /
    classify_builder_signal / classify_title_prefix_type) - every other
    field is unaffected by it, consistent with existing callers that
    omit it."""
    level, quote = classify_purchase_intent(text)
    budget = extract_stated_budget(text)
    audience = extract_audience(text)
    urgency = find_urgency_markers(text)
    prod, prod_reasons = assess_productizability(text)
    age = signal_age_days(discovered_at, now_iso=now_iso)
    perspective = classify_post_perspective(title)
    builder_signal, builder_quote = classify_builder_signal(text, title=title)
    prefix_type = classify_title_prefix_type(title)
    return DemandEvidence(
        intent_level=level, intent_quote=quote, budget=budget,
        audience_quote=audience, urgency_markers=urgency,
        productizability=prod, productizability_reasons=prod_reasons,
        age_days=age, repeat_signal_count=max(0, int(repeat_signal_count)),
        source_type=source_type, perspective=perspective,
        builder_signal=builder_signal, builder_quote=builder_quote,
        title_prefix_type=prefix_type)


def provenance_summary(evidence: DemandEvidence) -> dict:
    """Explicit FACT / ESTIMATED / UNKNOWN per derived field - the single
    place that answers "is this real, a judgement call, or nothing at
    all?" for every part of the evidence."""
    return {
        "intent": FACT if evidence.intent_level != INTENT_NONE else UNKNOWN,
        "budget": (FACT if (evidence.budget.amount > 0
                            and not evidence.budget.is_estimate) else UNKNOWN),
        "audience": FACT if evidence.audience_quote else UNKNOWN,
        "urgency": FACT if evidence.urgency_markers else UNKNOWN,
        "age": FACT if evidence.age_days is not None else UNKNOWN,
        # a deterministic judgement call, not an observed fact
        "productizability": ESTIMATED,
        # 0 is ambiguous between "checked, none found" and "never
        # checked" (this module never checks) - only a positive count is
        # asserted as a fact.
        "repeat_signal_count": FACT if evidence.repeat_signal_count > 0 else UNKNOWN,
        # a real structural fact about the title (a prefix/question mark
        # either is or isn't there) whenever it was determinable
        "perspective": (FACT if evidence.perspective != PERSPECTIVE_UNKNOWN
                        else UNKNOWN),
        "builder_signal": (FACT if evidence.builder_signal == BUILDER_YES
                           else UNKNOWN),
        "title_prefix_type": (FACT if evidence.title_prefix_type != PREFIX_UNKNOWN
                              else UNKNOWN),
    }


# ---------------------------------------------------------------------------
# score - deterministic, explainable, ADVISORY ONLY. Never touches
# opportunity_type, verification, or any other module's state.
# ---------------------------------------------------------------------------

@dataclass
class DemandQualityScore:
    total: float
    evidence: DemandEvidence
    factors: dict = field(default_factory=dict)   # name -> {weight, present, sign}
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 3), "evidence": self.evidence.to_dict(),
                "provenance": provenance_summary(self.evidence),
                "factors": self.factors, "reasons": list(self.reasons)}


# (name, weight, predicate(evidence) -> bool, reason)
_POSITIVE_FACTORS = (
    ("explicit_purchase_intent", 0.20,
     lambda e: e.intent_level == INTENT_EXPLICIT,
     "explicit purchase intent quoted from the source"),
    ("problem_interest", 0.08,
     lambda e: e.intent_level in (INTENT_EXPLICIT, INTENT_PROBLEM),
     "the source is looking for a solution to a stated problem"),
    ("stated_budget", 0.15,
     lambda e: e.budget.amount > 0 and not e.budget.is_estimate,
     "a concrete budget was stated"),
    ("audience_named", 0.10,
     lambda e: bool(e.audience_quote),
     "the target audience is named in the source"),
    ("urgency", 0.08,
     lambda e: bool(e.urgency_markers),
     "the source expresses urgency"),
    ("digital_friendly", 0.10,
     lambda e: e.productizability == PRODUCTIZABLE_HIGH,
     "the described need matches a digital-product shape the fleet can build"),
    ("recent_signal", 0.07,
     lambda e: e.age_days is not None and e.age_days <= 14,
     "the signal is recent (<=14 days)"),
    ("repeat_signal", 0.12,
     lambda e: e.repeat_signal_count >= 1,
     "at least one other independent signal looks like this one"),
)

_NEGATIVE_FACTORS = (
    ("no_concrete_signal", 0.12,
     lambda e: e.intent_level == INTENT_NONE,
     "no purchase intent or problem-interest marker was found - a vague mention"),
    ("help_request_only", 0.08,
     lambda e: e.intent_level == INTENT_HELP,
     "a general help request, not a stated problem/purchase interest"),
    ("not_productizable", 0.25,
     lambda e: e.productizability == PRODUCTIZABLE_LOW,
     "the described need looks like hardware/medical/legal/bespoke work, "
     "not a digital product the fleet can build"),
    ("stale_signal", 0.10,
     lambda e: e.age_days is not None and e.age_days > 90,
     "the signal is stale (>90 days)"),
    ("unknown_age", 0.03,
     lambda e: e.age_days is None,
     "the signal has no parseable timestamp"),
)


def score_demand_quality(evidence: DemandEvidence) -> DemandQualityScore:
    """Deterministic, explainable 0..1 score. Every factor that fired is
    named with its weight and sign in `.factors`/`.reasons` - never a
    black-box number. Advisory only."""
    total = 0.0
    factors: dict = {}
    reasons: list = []
    for name, weight, pred, reason in _POSITIVE_FACTORS:
        present = bool(pred(evidence))
        factors[name] = {"weight": weight, "present": present, "sign": "+"}
        if present:
            total += weight
            reasons.append(f"+ {reason}")
    for name, weight, pred, reason in _NEGATIVE_FACTORS:
        present = bool(pred(evidence))
        factors[name] = {"weight": weight, "present": present, "sign": "-"}
        if present:
            total -= weight
            reasons.append(f"- {reason}")

    total = max(0.0, min(1.0, total))
    return DemandQualityScore(total=total, evidence=evidence, factors=factors,
                              reasons=reasons)


def score_demand_signal(text: str, *, title: str = "", discovered_at: str = "",
                        now_iso: str = "", repeat_signal_count: int = 0,
                        source_type: str = "") -> DemandQualityScore:
    """Convenience: build evidence from raw text, then score it. `total`
    is completely unaffected by `title`/`perspective` - see
    build_demand_evidence."""
    evidence = build_demand_evidence(
        text, title=title, discovered_at=discovered_at, now_iso=now_iso,
        repeat_signal_count=repeat_signal_count, source_type=source_type)
    return score_demand_quality(evidence)
