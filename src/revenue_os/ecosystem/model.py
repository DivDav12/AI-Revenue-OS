"""Shared vocabulary + data shapes for the ecosystem.

Pure: constants, small frozen dataclasses, and estimate helpers. No I/O.

Design rule (spec sections 7 + 20): a discovered opportunity carries only
facts that came from a real source, plus values that are explicitly marked
`is_estimate: true`. Nothing here fabricates demand, revenue, or market
data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# origin - is this real or test data?
# ---------------------------------------------------------------------------

ORIGIN_SYNTHETIC = "synthetic"
ORIGIN_REAL = "real"
ORIGINS = (ORIGIN_SYNTHETIC, ORIGIN_REAL)

# ---------------------------------------------------------------------------
# how a source is accessed + whether automation is allowed there
# ---------------------------------------------------------------------------

ACCESS_OFFICIAL_API = "official_api"     # documented, keyless-or-keyed public API
ACCESS_PUBLIC_FEED = "public_feed"       # RSS / Atom / JSON feed meant for consumption
ACCESS_PUBLIC_WEB = "public_web"         # a public page, fetched politely, robots-respecting
ACCESS_CURATED_FILE = "curated_file"     # a human-curated local JSON list of signals
ACCESS_SYNTHETIC = "synthetic"           # generated test data, not a real source
ACCESS_METHODS = (ACCESS_OFFICIAL_API, ACCESS_PUBLIC_FEED, ACCESS_PUBLIC_WEB,
                  ACCESS_CURATED_FILE, ACCESS_SYNTHETIC)

# policy_status - the single gate that decides whether the fleet may act on
# a source at all. Fail closed: an unknown status is treated as BLOCKED.
POLICY_OK = "OK"                         # public read + fleet may act autonomously
POLICY_HUMAN_REQUIRED = "HUMAN_REQUIRED"  # a human must perform the external action
POLICY_HUMAN_SETUP_REQUIRED = "HUMAN_SETUP_REQUIRED"  # needs an account / API key first
POLICY_BLOCKED = "BLOCKED_BY_POLICY"     # the platform forbids the planned automation
POLICY_STATUSES = (POLICY_OK, POLICY_HUMAN_REQUIRED, POLICY_HUMAN_SETUP_REQUIRED,
                   POLICY_BLOCKED)

# ---------------------------------------------------------------------------
# verification lifecycle (spec section 8) - a sub-status inside `discovery`
# ---------------------------------------------------------------------------

V_DISCOVERED = "DISCOVERED"
V_VERIFYING = "VERIFYING"
V_VERIFIED = "VERIFIED"
V_QUALIFIED = "QUALIFIED"
V_REJECTED = "REJECTED"
V_HUMAN_REQUIRED = "HUMAN_REQUIRED"
V_BLOCKED = "BLOCKED"
#: the ceiling for a human-fed opportunity (spec: Human-Fed Task Source).
#: The underlying facts came from a person typing them in, not a live
#: system - `verify()` can only confirm internal CONSISTENCY (the claimed
#: amount matches the quoted evidence, no vague-payment markers, no
#: personal/identity content, ...), never independently confirm the claim
#: against the source. This status says exactly that: consistency-checked,
#: human-attested - never conflated with V_QUALIFIED's "an automated
#: source's own claim passed every gate".
V_HUMAN_ATTESTED = "HUMAN_ATTESTED"
VERIFICATION_STATUSES = (V_DISCOVERED, V_VERIFYING, V_VERIFIED, V_QUALIFIED,
                         V_REJECTED, V_HUMAN_REQUIRED, V_BLOCKED,
                         V_HUMAN_ATTESTED)

#: a QUALIFIED (automated-source) or HUMAN_ATTESTED (human-fed, consistency-
#: checked) opportunity may be planned into a real task chain.
PLANNABLE = frozenset({V_QUALIFIED, V_HUMAN_ATTESTED})

# ---------------------------------------------------------------------------
# opportunity types - WHAT kind of money-making thing this is
# ---------------------------------------------------------------------------

TYPE_TASK = "task"                 # a small paid online task / gig unit of work
TYPE_DIGITAL_PRODUCT = "digital_product"
TYPE_SOFTWARE_TOOL = "software_tool"
TYPE_AFFILIATE = "affiliate"
TYPE_ECOMMERCE = "ecommerce"
TYPE_DROPSHIPPING = "dropshipping"
TYPE_SERVICE = "service"
TYPE_CONTENT = "content"
TYPE_OTHER = "other"
OPPORTUNITY_TYPES = (TYPE_TASK, TYPE_DIGITAL_PRODUCT, TYPE_SOFTWARE_TOOL,
                     TYPE_AFFILIATE, TYPE_ECOMMERCE, TYPE_DROPSHIPPING,
                     TYPE_SERVICE, TYPE_CONTENT, TYPE_OTHER)

# ---------------------------------------------------------------------------
# monetisation strategies (spec section 10) - HOW we make money from an
# opportunity. An opportunity may support several; the strategy engine picks.
# ---------------------------------------------------------------------------

STRAT_TASK = "TASK"
STRAT_PRODUCT = "PRODUCT"
STRAT_AFFILIATE = "AFFILIATE"
STRAT_ECOMMERCE = "ECOMMERCE"
STRAT_SERVICE = "SERVICE"
STRAT_OTHER = "OTHER"
STRATEGIES = (STRAT_TASK, STRAT_PRODUCT, STRAT_AFFILIATE, STRAT_ECOMMERCE,
              STRAT_SERVICE, STRAT_OTHER)


# ---------------------------------------------------------------------------
# TASK sub-classification (discovery quality layer) - WHAT KIND of paid-work
# signal this actually is, decided from evidence (task_signal.py), never
# guessed from the title. Only INSTANT_PAID_TASK / BOUNTY_OR_CONTEST /
# MICROTASK are candidates for the autonomous PLAN_TASK/EXECUTE_TASK/
# VERIFY_RESULT chain; SERVICE_LEAD / JOB always stay HUMAN_REQUIRED - they
# inherently need an application/negotiation a human must do. A high
# TaskQualityScore never substitutes for this classification (hard gate).
# ---------------------------------------------------------------------------

TASK_INSTANT_PAID = "INSTANT_PAID_TASK"   # a discrete task with a stated fixed payment
TASK_BOUNTY = "BOUNTY_OR_CONTEST"         # a bounty / contest prize for a deliverable
TASK_MICRO = "MICROTASK"                  # a small, repeatable, low-effort paid task
TASK_SERVICE_LEAD = "SERVICE_LEAD"        # someone wants a service done - needs a proposal
TASK_JOB = "JOB"                          # an employment/hiring posting, not a discrete task
TASK_OTHER = "OTHER"                      # ambiguous / contradictory / insufficient evidence
TASK_KINDS = (TASK_INSTANT_PAID, TASK_BOUNTY, TASK_MICRO, TASK_SERVICE_LEAD,
             TASK_JOB, TASK_OTHER)

#: the ONLY task kinds worth even considering for autonomous execution -
#: everything else (including OTHER) stays on the human-gated path.
AUTONOMOUS_TASK_KINDS: frozenset[str] = frozenset(
    {TASK_INSTANT_PAID, TASK_BOUNTY, TASK_MICRO})

# --- payment evidence -------------------------------------------------

PAY_GUARANTEED = "GUARANTEED"     # the source states payment is unconditional
PAY_CONDITIONAL = "CONDITIONAL"   # payment depends on acceptance/merge/review
PAY_UNCLEAR = "UNCLEAR"           # the source did not clearly state conditions
PAYMENT_CONDITIONS = (PAY_GUARANTEED, PAY_CONDITIONAL, PAY_UNCLEAR)

# --- submission evidence -----------------------------------------------

SUBMIT_API = "API"
SUBMIT_FORM = "FORM"
SUBMIT_EMAIL = "EMAIL"
SUBMIT_PLATFORM_UI = "PLATFORM_UI"
SUBMIT_UNKNOWN = "UNKNOWN"
SUBMISSION_METHODS = (SUBMIT_API, SUBMIT_FORM, SUBMIT_EMAIL, SUBMIT_PLATFORM_UI,
                      SUBMIT_UNKNOWN)


@dataclass(frozen=True)
class PaymentEvidence:
    """What the source actually said about money for a TASK-like
    opportunity - never a guess dressed up as a fact. Defaults are the
    "we know nothing" state (`amount=0`, `conditions=UNCLEAR`,
    `is_estimate=True`); only evidence a source really provided should set
    a positive amount / GUARANTEED / CONDITIONAL. `is_estimate=False` is
    reserved for a source-stated, unconditional, verifiable number."""
    amount: float = 0.0
    currency: str = ""
    conditions: str = PAY_UNCLEAR
    is_estimate: bool = True
    evidence: tuple = ()

    def to_dict(self) -> dict:
        return {"amount": round(float(self.amount), 2), "currency": self.currency,
                "conditions": self.conditions, "is_estimate": bool(self.is_estimate),
                "evidence": list(self.evidence)}


@dataclass(frozen=True)
class SubmissionEvidence:
    """What the source said about how the work is actually submitted or
    claimed. Every blocker defaults to False/"" - an unknown source never
    silently grants automation; a source must say explicitly that no
    login/CAPTCHA/identity step exists (in practice: leave the defaults,
    since we can rarely prove a negative - see task_signal's scoring,
    which treats UNKNOWN submission info as a quality penalty, not a
    green light)."""
    submission_url: str = ""
    submission_method: str = SUBMIT_UNKNOWN
    requires_login: bool = False
    requires_captcha: bool = False
    requires_identity: bool = False
    has_api_submission: bool = False
    deadline: str = ""                 # ISO timestamp, "" = no stated deadline
    required_deliverable: str = ""

    def to_dict(self) -> dict:
        return {"submission_url": self.submission_url,
                "submission_method": self.submission_method,
                "requires_login": bool(self.requires_login),
                "requires_captcha": bool(self.requires_captcha),
                "requires_identity": bool(self.requires_identity),
                "has_api_submission": bool(self.has_api_submission),
                "deadline": self.deadline,
                "required_deliverable": self.required_deliverable}


# ---------------------------------------------------------------------------
# estimate helper - every non-observed number is wrapped so a reader can
# never mistake it for a fact (spec section 7 + 20).
# ---------------------------------------------------------------------------

def estimate(value: float, basis: str = "") -> dict:
    """A number the system guessed, not a number it observed."""
    try:
        v = round(float(value), 4)
    except (TypeError, ValueError):
        v = 0.0
    return {"value": v, "is_estimate": True, "basis": str(basis or "heuristic")}


def estimate_value(maybe_estimate) -> float:
    """Read the number out of an `estimate()` dict, or coerce a bare number."""
    if isinstance(maybe_estimate, dict):
        try:
            return float(maybe_estimate.get("value", 0.0))
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(maybe_estimate)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# source metadata (spec section 6) - every source declares this
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceMeta:
    source: str                       # short id, e.g. "hacker-news"
    source_type: str                  # e.g. "demand_signal", "job_board", "synthetic"
    source_url: str = ""              # canonical URL of the source
    access_method: str = ACCESS_PUBLIC_WEB
    automation_allowed: bool = False  # may the fleet act on findings without a human?
    requires_login: bool = False
    requires_human: bool = False
    policy_status: str = POLICY_OK

    def to_dict(self) -> dict:
        return {
            "source": self.source, "source_type": self.source_type,
            "source_url": self.source_url, "access_method": self.access_method,
            "automation_allowed": bool(self.automation_allowed),
            "requires_login": bool(self.requires_login),
            "requires_human": bool(self.requires_human),
            "policy_status": self.policy_status,
        }


# ---------------------------------------------------------------------------
# the normalized pre-persist opportunity (what a Source yields)
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def norm_title(title: str) -> str:
    """Lower-cased, whitespace-collapsed, punctuation-stripped - for dedup."""
    t = _WS.sub(" ", str(title or "").lower()).strip()
    return re.sub(r"[^a-z0-9 ]+", "", t)


@dataclass
class OpportunityDraft:
    """A candidate opportunity a Source produced, before verification /
    evaluation / persistence."""

    title: str
    description: str = ""
    opportunity_type: str = TYPE_OTHER
    evidence: list = field(default_factory=list)      # verbatim quotes / facts from the source
    source_meta: SourceMeta | None = None
    source_id: str = ""                               # the item id at the source
    source_url: str = ""                              # the item URL at the source
    discovered_at: str = ""
    # optional coarse hints the source can supply (all treated as estimates)
    est_pay_eur: float = 0.0
    est_time_minutes: float = 0.0
    demand_hint: float = 0.0            # 0..1 - how strong the demand signal is
    category: str = "other"            # opportunity_store CATEGORIES value
    raw: dict = field(default_factory=dict)
    # discovery quality layer (spec: TASK typing) - default to the "we know
    # nothing yet" state; only a source that actually parsed this out of the
    # real listing should populate it.
    payment_evidence: PaymentEvidence = field(default_factory=PaymentEvidence)
    submission_evidence: SubmissionEvidence = field(default_factory=SubmissionEvidence)

    def dedup_key(self) -> str:
        if self.source_meta and self.source_id:
            return f"{self.source_meta.source}:{self.source_id}"
        return norm_title(self.title)
