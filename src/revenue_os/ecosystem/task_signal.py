"""Discovery quality layer for TASK-like opportunities.

Distinguishes real, monetisable, autonomously-actionable task signals from
job postings, service leads, and weak demand chatter - BEFORE anything is
planned into a real execution chain. Two independent pieces:

  classify_task_kind(draft)  -> one of model.TASK_KINDS, evidence-based
                                (never guesses from the title alone - see
                                `_blob()`). Fail-closed: ambiguous or
                                contradictory evidence -> OTHER, never an
                                autonomous candidate.
  score_task_quality(draft)  -> TaskQualityScore: a deterministic,
                                explainable 0..1 score with a named,
                                inspectable factor breakdown. Advisory only
                                - it NEVER substitutes for verification's
                                hard gates (verification.py) or for
                                `classify_task_kind`'s autonomy decision
                                (pipeline.py checks task_kind, not score).

Also: `task_fingerprint()` (stable TASK dedup key robust to a re-scrape
changing its URL/timestamp/source_id) and `is_expired()` (a stated deadline
that has passed).

Pure, deterministic. No I/O, no network, no LLM.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import model
from .model import OpportunityDraft, PaymentEvidence, SubmissionEvidence

# ---------------------------------------------------------------------------
# evidence-only text markers. `_blob()` deliberately excludes `draft.title` -
# classification must come from `evidence` (verbatim source quotes) and
# `description`, never a guess at the headline.
# ---------------------------------------------------------------------------

_BOUNTY_MARKERS = ("bounty", "contest", "prize", "hackathon", "challenge reward")
_MICRO_MARKERS = ("microtask", "micro-task", "micro task", "quick task",
                  "5-minute", "5 minute", "data entry", "data labeling",
                  "data labelling", "small task", "one-off task")
_INSTANT_PAID_MARKERS = ("i will pay", "will pay $", "fixed price", "paid task",
                         "pay on completion", "pay upon delivery", "flat fee")
_JOB_MARKERS = ("hiring", "we're hiring", "job opening", "full-time", "full time",
                "part-time", "part time", "permanent position", "job posting",
                "who is hiring")
_SERVICE_LEAD_MARKERS = ("looking for a freelancer", "seeking freelancer",
                         "freelancer? seeking", "need someone to",
                         "looking for someone to help", "freelancer needed")


def _blob(draft: OpportunityDraft) -> str:
    """Only the source's own words - evidence quotes + description. Never
    the title (spec: "nicht anhand des Titels raten")."""
    parts = list(draft.evidence or [])
    if draft.description:
        parts.append(draft.description)
    return " ".join(str(p) for p in parts).lower()


def classify_task_kind(draft: OpportunityDraft) -> str:
    """Evidence-based TASK sub-classification. A strong structured signal
    (real payment_evidence + a submission path needing no application) wins
    over text markers; hiring/service language mixed with task/bounty
    language is contradictory and fails closed to OTHER."""
    blob = _blob(draft)
    pay = draft.payment_evidence or PaymentEvidence()
    sub = draft.submission_evidence or SubmissionEvidence()

    has_bounty = any(m in blob for m in _BOUNTY_MARKERS)
    has_micro = any(m in blob for m in _MICRO_MARKERS)
    has_instant = any(m in blob for m in _INSTANT_PAID_MARKERS)
    has_job = any(m in blob for m in _JOB_MARKERS)
    has_service_lead = any(m in blob for m in _SERVICE_LEAD_MARKERS)

    structured_instant = (
        pay.amount > 0 and pay.conditions != model.PAY_UNCLEAR
        and (sub.has_api_submission or sub.submission_method in
             (model.SUBMIT_API, model.SUBMIT_FORM))
        and not (has_job or has_service_lead))

    hire_side = has_job or has_service_lead
    paid_side = has_bounty or has_micro or has_instant or structured_instant

    if hire_side and paid_side:
        return model.TASK_OTHER            # contradictory evidence - fail closed
    if structured_instant or has_instant:
        return model.TASK_INSTANT_PAID
    if has_bounty:
        return model.TASK_BOUNTY
    if has_micro:
        return model.TASK_MICRO
    if has_job:
        return model.TASK_JOB
    if has_service_lead:
        return model.TASK_SERVICE_LEAD
    return model.TASK_OTHER


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

def is_expired(sub: SubmissionEvidence, now_iso: str = "") -> bool:
    """True when `sub.deadline` parses to a real timestamp strictly before
    `now_iso` (or wall-clock now). No deadline / unparseable deadline ->
    not expired (we never invent a deadline the source did not state)."""
    dl = (sub.deadline or "").strip()
    if not dl:
        return False
    try:
        deadline_dt = datetime.fromisoformat(dl)
    except ValueError:
        return False
    try:
        now_dt = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    except ValueError:
        now_dt = datetime.now(timezone.utc)
    if deadline_dt.tzinfo is None:
        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return deadline_dt < now_dt


# ---------------------------------------------------------------------------
# quality score - deterministic, explainable, advisory only
# ---------------------------------------------------------------------------

@dataclass
class TaskQualityScore:
    total: float
    task_kind: str
    autonomous_candidate: bool
    factors: dict = field(default_factory=dict)   # name -> {weight, present, sign}
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 3), "task_kind": self.task_kind,
                "autonomous_candidate": self.autonomous_candidate,
                "factors": self.factors, "reasons": list(self.reasons)}


# (name, weight, predicate(draft, task_kind, payment, submission), reason)
_POSITIVE_FACTORS = (
    ("concrete_task", 0.10,
     lambda d, k, p, s: bool((d.description or "").strip()) and bool(d.evidence),
     "a concrete task description backed by evidence"),
    ("concrete_payment", 0.18,
     lambda d, k, p, s: p.amount > 0 and p.conditions != model.PAY_UNCLEAR,
     "a concrete payment amount with known conditions"),
    ("guaranteed_payment", 0.10,
     lambda d, k, p, s: p.conditions == model.PAY_GUARANTEED,
     "payment is guaranteed, not conditional"),
    ("clear_deliverable", 0.10,
     lambda d, k, p, s: bool(s.required_deliverable.strip()),
     "the required deliverable is stated"),
    ("clear_submission", 0.10,
     lambda d, k, p, s: bool(s.submission_url.strip()) or s.has_api_submission,
     "a concrete submission path exists"),
    ("has_deadline", 0.05,
     lambda d, k, p, s: bool(s.deadline.strip()),
     "a deadline is stated"),
    ("no_application_needed", 0.10,
     lambda d, k, p, s: k in model.AUTONOMOUS_TASK_KINDS,
     "no application/negotiation is required to start"),
    ("sufficient_information", 0.07,
     lambda d, k, p, s: len(d.evidence or []) >= 2 or len((d.description or "")) >= 40,
     "enough information to act on"),
    ("repeatable_structure", 0.05,
     lambda d, k, p, s: k in (model.TASK_MICRO, model.TASK_BOUNTY),
     "a repeatable task family (microtask/bounty)"),
)

_NEGATIVE_FACTORS = (
    ("job_description_only", 0.20,
     lambda d, k, p, s: k == model.TASK_JOB,
     "this is a job posting, not a discrete paid task"),
    ("service_lead_only", 0.15,
     lambda d, k, p, s: k == model.TASK_SERVICE_LEAD,
     "this is a service lead needing a proposal, not a discrete task"),
    ("unclear_payment", 0.15,
     lambda d, k, p, s: p.amount <= 0 and p.conditions == model.PAY_UNCLEAR,
     "payment is unclear / not stated"),
    ("unclear_submission", 0.10,
     lambda d, k, p, s: s.submission_method == model.SUBMIT_UNKNOWN and not s.submission_url,
     "no clear submission path"),
    ("requires_login", 0.20,
     lambda d, k, p, s: bool(s.requires_login),
     "submission requires a login"),
    ("requires_captcha", 0.25,
     lambda d, k, p, s: bool(s.requires_captcha),
     "submission requires solving a CAPTCHA"),
    ("requires_identity", 0.20,
     lambda d, k, p, s: bool(s.requires_identity),
     "submission requires identity verification"),
    ("contradictory_signals", 0.15,
     lambda d, k, p, s: (k == model.TASK_OTHER and (
         any(m in _blob(d) for m in _JOB_MARKERS + _SERVICE_LEAD_MARKERS)
         and any(m in _blob(d) for m in
                 _BOUNTY_MARKERS + _MICRO_MARKERS + _INSTANT_PAID_MARKERS))),
     "the evidence mixes hiring/service language with task/bounty language"),
    ("expired", 0.30,
     lambda d, k, p, s: is_expired(s),
     "the stated deadline has already passed"),
    ("pure_demand_signal", 0.10,
     lambda d, k, p, s: k == model.TASK_OTHER and p.amount <= 0,
     "a demand signal with no concrete monetary opportunity yet"),
)


def score_task_quality(draft: OpportunityDraft, task_kind: str | None = None
                       ) -> TaskQualityScore:
    """Deterministic, explainable 0..1 score. Every factor that fired is
    named with its weight and sign in `.factors` / `.reasons` - never a
    black-box number. Advisory only: never widens what verification.py or
    pipeline.plan() allow to run autonomously."""
    k = task_kind or classify_task_kind(draft)
    pay = draft.payment_evidence or PaymentEvidence()
    sub = draft.submission_evidence or SubmissionEvidence()

    total = 0.0
    factors: dict = {}
    reasons: list = []
    for name, weight, pred, reason in _POSITIVE_FACTORS:
        present = bool(pred(draft, k, pay, sub))
        factors[name] = {"weight": weight, "present": present, "sign": "+"}
        if present:
            total += weight
            reasons.append(f"+ {reason}")
    for name, weight, pred, reason in _NEGATIVE_FACTORS:
        present = bool(pred(draft, k, pay, sub))
        factors[name] = {"weight": weight, "present": present, "sign": "-"}
        if present:
            total -= weight
            reasons.append(f"- {reason}")

    total = max(0.0, min(1.0, total))
    return TaskQualityScore(total=total, task_kind=k,
                            autonomous_candidate=k in model.AUTONOMOUS_TASK_KINDS,
                            factors=factors, reasons=reasons)


# ---------------------------------------------------------------------------
# dedupe fingerprint - robust to a re-scrape changing the URL / source_id /
# timestamp / minor text (spec: TASK dedupe). Two drafts from the same
# source with the same normalized, digit-stripped title are almost
# certainly the same underlying offer even under a fresh id/url/timestamp.
# ---------------------------------------------------------------------------

_DIGITS_RE = re.compile(r"\d+")


def task_fingerprint(draft: OpportunityDraft) -> str:
    source = draft.source_meta.source if draft.source_meta else ""
    norm = model.norm_title(draft.title)
    norm = _DIGITS_RE.sub("#", norm)
    core = f"{source}|{norm}"
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:24]
