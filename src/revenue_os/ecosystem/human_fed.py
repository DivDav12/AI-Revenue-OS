"""Human-Fed Task Source - the minimal, safe bridge for a person to feed a
real, already-discovered paid task into the ecosystem (spec: "Human-Fed Task
Source" implementation step, 2026-09-04).

This is NOT an autonomy advance. It replaces "a human tells the system
about a task" with a validated, deterministic ingestion contract; discovery,
scoring, planning, drafting and internal verification still all reuse the
EXISTING architecture unchanged (`verification.verify`, `task_signal`,
`DiscoveryEngine`, `pipeline.plan`, the PLAN_TASK/EXECUTE_TASK/VERIFY_RESULT
chain, `acceptance.pending_actions`, `record_task_outcome`). Nothing here
fetches `task_url`, logs in, solves a CAPTCHA, or submits anything - that
stays HUMAN_REQUIRED by construction (spec sections 5.7, 5.10).

    HumanFedTaskSource   an OpportunitySource wrapping ONE already-validated
                        draft - satisfies `sources.OpportunitySource`
    parse_task_json()    the strict schema validator (spec 5.2-5.6)
    ingest_task()        orchestrates: validate -> dedup pre-check ->
                        DiscoveryEngine.run() -> locate the resulting record

Platform policy (spec: Phase 1 compliance gate) is DATA, not code - one
`PLATFORM_POLICY` table with a verbatim citation per entry. Supporting a new
platform is a table row, never a second adapter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import model
from .model import OpportunityDraft, PaymentEvidence, SourceMeta, SubmissionEvidence

SCHEMA_VERSION = 1

_REQUIRED_FIELDS = frozenset({
    "schema_version", "platform", "task_url", "title", "description",
    "offered_payment", "currency", "payment_evidence_quote",
    "payment_is_explicit", "login_required", "source_access_method",
    "captured_at",
})
_OPTIONAL_FIELDS = frozenset({
    "external_task_id", "requirements", "task_category", "payment_basis",
    "expires_at", "estimated_human_minutes", "submission_method",
    "submission_notes", "qualification_requirements", "locale",
    "task_quantity", "quality_notes",
})
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


class IngestionError(ValueError):
    """The ingested task JSON does not conform to the schema (spec 5.2/5.3),
    or is otherwise structurally unsafe. Never creates an Opportunity."""


# ---------------------------------------------------------------------------
# Phase 1 compliance gate - platform policy table (data, not code)
# ---------------------------------------------------------------------------

PLATFORM_ALLOWED = "allowed"
PLATFORM_REQUIRES_HUMAN_REVIEW = "requires_human_review"
PLATFORM_DISALLOWED = "disallowed"

#: platform key -> {status, hosts (task_url allowlist), citation}. Every
#: entry's `citation` is a verbatim quote + source + retrieval date - see
#: the implementation report for the full research trail. A platform NOT in
#: this table gets `_DEFAULT_POLICY` (fail-safe: requires_human_review,
#: never `allowed` by default).
PLATFORM_POLICY: dict[str, dict] = {
    "clickworker": {
        "status": PLATFORM_DISALLOWED,
        "hosts": frozenset({"clickworker.com", "www.clickworker.com",
                            "workplace.clickworker.com"}),
        "citation": (
            "Clickworker General Terms & Conditions (Rest of World), "
            "workplace.clickworker.com/en/agreements/10123, fetched "
            "2026-09-04: § 4.7 'The Clickworker is expressly "
            "prohibited from using automated methods to provide the "
            "service. This includes, for example, bots, scripts, and "
            "other similar methods...' and § 4.6 'The transfer of "
            "the project and the processing by third parties are "
            "expressly prohibited unless this is expressly permitted in "
            "the project description.' No AI-assistance carve-out."
        ),
    },
    "prolific": {
        "status": PLATFORM_DISALLOWED,
        "hosts": frozenset({"prolific.com", "www.prolific.com",
                            "app.prolific.com"}),
        "citation": (
            "Prolific, 'How Prolific detects bots and AI in online "
            "research' (prolific.com/resources), fetched 2026-09-04: "
            "'Prolific participants who are determined to have used AI "
            "tools or similar methods will have their submissions "
            "rejected by researchers' - actively enforced via automated "
            "LLM-usage detection ('LLM checks')."
        ),
    },
    "algora": {
        "status": PLATFORM_REQUIRES_HUMAN_REVIEW,
        "hosts": frozenset({"algora.io"}),
        "citation": (
            "Algora Terms of Service (algora.io/legal/terms), fetched "
            "2026-09-04: Section 8 'Prohibited Uses' restricts bots/"
            "spiders accessing the SITE, not the method used to prepare "
            "a bounty PR's content - no clause on AI-assisted code found. "
            "Third-party guidance (gigs.sh/p/algora): 'Agent welcomed: "
            "no' / 'No agent-specific policy - Algora is API-friendly "
            "but does not publicly invite bots.' Unclear, not prohibited "
            "by the primary source - treated as requiring human review."
        ),
    },
    "kaggle": {
        "status": PLATFORM_ALLOWED,
        "hosts": frozenset({"kaggle.com", "www.kaggle.com"}),
        "citation": (
            "Kaggle General Competition Rules (kaggle.com), fetched "
            "2026-09-04: 'Individual participants and Teams may use "
            "automated machine learning tool(s) (\"AMLT\") ... to create "
            "a Submission' and may win a Prize using one, subject to "
            "license/eligibility requirements."
        ),
    },
    "intigriti": {
        "status": PLATFORM_REQUIRES_HUMAN_REVIEW,
        "hosts": frozenset({"intigriti.com", "www.intigriti.com",
                            "app.intigriti.com"}),
        "citation": (
            "No single Intigriti-wide clause on AI-assisted report "
            "writing found. Cross-program industry survey (secondary "
            "source, stingrai.io 'Who Actually Bans AI-Written Bug "
            "Reports? 2026 Census', 53 programs), fetched 2026-09-04: 0 "
            "ban AI assistance outright; most require human verification "
            "before submission - treated as requiring human review "
            "pending a per-program primary-source check."
        ),
    },
    "hackerone": {
        "status": PLATFORM_REQUIRES_HUMAN_REVIEW,
        "hosts": frozenset({"hackerone.com", "www.hackerone.com"}),
        "citation": (
            "Same industry-wide survey basis as 'intigriti' above "
            "(no HackerOne-specific primary quote found) - treated as "
            "requiring human review pending a per-program check."
        ),
    },
}

_DEFAULT_POLICY = {
    "status": PLATFORM_REQUIRES_HUMAN_REVIEW,
    "hosts": frozenset(),
    "citation": "unknown platform - no policy on file; fails safe to "
               "human review, never autonomous",
}

_STATUS_TO_MODEL_POLICY = {
    PLATFORM_ALLOWED: model.POLICY_OK,
    PLATFORM_REQUIRES_HUMAN_REVIEW: model.POLICY_HUMAN_REQUIRED,
    PLATFORM_DISALLOWED: model.POLICY_BLOCKED,
}


def platform_policy_for(platform: str) -> dict:
    return PLATFORM_POLICY.get((platform or "").strip().lower(), _DEFAULT_POLICY)


# ---------------------------------------------------------------------------
# payment evidence consistency (spec 5.3)
# ---------------------------------------------------------------------------

_KNOWN_CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "CAD",
    "AUD", "JPY",
})
_CURRENCY_SYMBOLS: dict[str, tuple[str, ...]] = {
    "EUR": ("€", "eur"), "USD": ("$", "usd"), "GBP": ("£", "gbp"),
    "CHF": ("chf",), "SEK": ("sek", "kr"), "NOK": ("nok", "kr"),
    "DKK": ("dkk", "kr"), "PLN": ("pln", "zł"), "CZK": ("czk", "kč"),
    "CAD": ("cad",), "AUD": ("aud",), "JPY": ("¥", "jpy"),
}
#: unscore markers - present anywhere in the quote, the amount is NOT
#: belastbar (reliable) evidence, regardless of the number (spec 5.3).
_VAGUE_MARKERS = ("up to", "bis zu", "average", "potential", "earn money",
                  "competitive pay", "starting at", "starting from",
                  " ab ", "~", "approx", "estimated")

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _amounts_in(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text):
        try:
            out.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            continue
    return out


def _check_payment(offered_payment: float, currency: str, quote: str,
                   payment_is_explicit: bool) -> str:
    """Returns "" if the evidence is belastbar (reliable); otherwise a
    short, human-readable reason it is not - never raises. Ingestion still
    succeeds either way (spec 5.4: insufficient evidence -> HUMAN_REQUIRED,
    never a hard reject)."""
    if not payment_is_explicit:
        return "payment_is_explicit is false - the human marked this as not " \
               "explicitly stated by the platform"
    q = (quote or "").lower()
    hit = next((m for m in _VAGUE_MARKERS if m in q), None)
    if hit:
        return f"payment_evidence_quote contains a vagueness marker ({hit!r})"
    amounts = _amounts_in(quote or "")
    if not any(abs(a - offered_payment) < 0.01 for a in amounts):
        return (f"payment_evidence_quote does not contain the offered "
               f"amount ({offered_payment})")
    symbols = _CURRENCY_SYMBOLS.get(currency, ())
    if symbols and not any(s in q for s in symbols):
        return f"payment_evidence_quote does not mention the currency ({currency!r})"
    return ""


# ---------------------------------------------------------------------------
# content eligibility (spec 5.6) - separate from task_signal's TASK_KIND
# classifier by design: this asks "does completing this require the
# ACCOUNT HOLDER'S own identity/opinion/senses", not "what kind of paid-work
# signal is this". Scoped to the human_fed path only.
# ---------------------------------------------------------------------------

_PERSONAL_CONTENT_MARKERS = (
    "your opinion", "your experience", "tell us about yourself",
    "in your own words", "personal opinion", "your personal",
    "describe your", "about yourself", "your daily life",
    "upload a photo", "upload a video", "take a photo", "take a selfie",
    "record yourself", "record a video", "your location", "your gps",
    "your device", "your browsing history", "log into your account",
    "log in to your account", "connect your account", "your bank account",
    "biometric", "your face", "your fingerprint", "voice recording of you",
    "penetration test", "exploit the", "hack into", "bypass security",
    "vulnerability scan", "port scan",
)


def _check_personal_content(description: str, requirements: str) -> str:
    blob = f"{description or ''} {requirements or ''}".lower()
    hit = next((m for m in _PERSONAL_CONTENT_MARKERS if m in blob), None)
    return f"content mentions {hit!r}" if hit else ""


# ---------------------------------------------------------------------------
# untrusted content - basic exfiltration sanity net (spec 5.7). This never
# fires in normal operation (the rendered deliverable is built ONLY from
# `spec` fields the task chain itself derived - see
# task_adapters._render_task_solution - never from os.environ); it exists
# as a regression guard, checked by task_adapters.ExecuteTaskAdapter before
# any deliverable is written to disk.
# ---------------------------------------------------------------------------

_SECRET_NAME_RE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|SECRET|TOKEN|PASSWORD)\b")
_ABS_PATH_RE = re.compile(
    r"[A-Za-z]:\\\\?[^\s\"']+|/(?:home|Users|etc|root)/[^\s\"']+")


def scan_for_exfiltration(content: str) -> list[str]:
    """Returns a list of matched patterns; empty when the content looks
    safe to persist."""
    hits: list[str] = []
    if _SECRET_NAME_RE.search(content or ""):
        hits.append("looks like an environment-variable / secret name")
    if _ABS_PATH_RE.search(content or ""):
        hits.append("contains an absolute local filesystem path")
    return hits


# ---------------------------------------------------------------------------
# schema validation
# ---------------------------------------------------------------------------

_EXT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _require_str(raw: dict, key: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise IngestionError(f"{key!r} must be a non-empty string")
    return v.strip()


def _optional_float(raw: dict, key: str) -> float:
    v = raw.get(key)
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise IngestionError(f"{key!r} must be a number")
    return float(v)


def _require_bool(raw: dict, key: str) -> bool:
    v = raw.get(key)
    if not isinstance(v, bool):
        raise IngestionError(f"{key!r} must be a boolean")
    return v


def _validate_url(url: str, hosts: frozenset) -> str:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise IngestionError(f"task_url is not a valid URL: {exc}") from exc
    if parsed.scheme != "https":
        raise IngestionError("task_url must be https")
    if not parsed.netloc:
        raise IngestionError("task_url has no host")
    host = parsed.netloc.lower()
    if hosts and host not in hosts:
        raise IngestionError(
            f"task_url host {host!r} is not on the allowlist for this "
            f"platform ({sorted(hosts)})")
    return url


@dataclass
class ParsedTask:
    draft: OpportunityDraft
    platform: str
    platform_policy_status: str
    payment_ok: bool
    payment_reason: str
    content_flag: str
    external_task_id: str = ""


def parse_task_json(raw: dict) -> ParsedTask:
    """The strict schema validator (spec 5.2). Raises `IngestionError` on
    any STRUCTURAL violation (missing/unknown/mistyped field, malformed
    URL, wrong schema_version). A CONTENT-quality issue (insufficient
    payment evidence, personal-content marker) never raises here - it is
    recorded on the resulting draft and routed to HUMAN_REQUIRED by
    verification.verify() (spec 5.4)."""
    if not isinstance(raw, dict):
        raise IngestionError("task JSON must be an object")

    unknown = set(raw) - _ALL_FIELDS
    if unknown:
        raise IngestionError(f"unknown field(s): {sorted(unknown)}")
    missing = _REQUIRED_FIELDS - set(raw)
    if missing:
        raise IngestionError(f"missing required field(s): {sorted(missing)}")

    if raw.get("schema_version") != SCHEMA_VERSION:
        raise IngestionError(
            f"unsupported schema_version {raw.get('schema_version')!r} - "
            f"this build understands {SCHEMA_VERSION}")

    if raw.get("source_access_method") != "human_fed":
        raise IngestionError("source_access_method must be exactly 'human_fed'")

    platform = _require_str(raw, "platform").lower()
    title = _require_str(raw, "title")
    description = _require_str(raw, "description")
    payment_evidence_quote = _require_str(raw, "payment_evidence_quote")
    payment_is_explicit = _require_bool(raw, "payment_is_explicit")
    login_required = _require_bool(raw, "login_required")
    captured_at_raw = _require_str(raw, "captured_at")

    try:
        datetime.fromisoformat(captured_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestionError(f"captured_at is not a valid ISO-8601 timestamp: "
                             f"{exc}") from exc

    offered_payment = raw.get("offered_payment")
    if isinstance(offered_payment, bool) or not isinstance(offered_payment, (int, float)):
        raise IngestionError("offered_payment must be a number, not a string/range")
    if offered_payment <= 0:
        raise IngestionError("offered_payment must be > 0")
    offered_payment = float(offered_payment)

    currency = raw.get("currency")
    if not isinstance(currency, str) or currency.strip().upper() != currency.strip() \
            or not re.match(r"^[A-Z]{3}$", currency.strip()):
        raise IngestionError(f"currency {currency!r} is not a well-formed "
                             "ISO-4217 code")
    currency = currency.strip()
    currency_known = currency in _KNOWN_CURRENCIES

    policy = platform_policy_for(platform)
    task_url = _validate_url(_require_str(raw, "task_url"), policy["hosts"])

    external_task_id = str(raw.get("external_task_id") or "").strip()
    if external_task_id and not _EXT_ID_RE.match(external_task_id):
        raise IngestionError(f"external_task_id {external_task_id!r} contains "
                             "characters that are not safe as an identifier")

    requirements = str(raw.get("requirements") or "").strip()
    submission_notes = str(raw.get("submission_notes") or "").strip()
    payment_basis = str(raw.get("payment_basis") or "").strip()
    expires_at = str(raw.get("expires_at") or "").strip()
    if expires_at:
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IngestionError(f"expires_at is not a valid ISO-8601 "
                                 f"timestamp: {exc}") from exc

    # --- content-quality checks: never raise, only annotate ------------
    payment_reason = ""
    if not currency_known:
        payment_reason = f"currency {currency!r} is not a recognised code"
    else:
        payment_reason = _check_payment(offered_payment, currency,
                                        payment_evidence_quote, payment_is_explicit)
    payment_ok = not payment_reason

    content_flag = _check_personal_content(description, requirements)

    conditions = model.PAY_UNCLEAR
    is_estimate = True
    if payment_ok:
        is_estimate = False
        conditions = (model.PAY_GUARANTEED if "guarant" in payment_basis.lower()
                     else model.PAY_CONDITIONAL)

    payment_evidence = PaymentEvidence(
        amount=offered_payment, currency=currency, conditions=conditions,
        is_estimate=is_estimate, evidence=(payment_evidence_quote,))

    submission_evidence = SubmissionEvidence(
        submission_url=task_url,
        submission_method=str(raw.get("submission_method") or model.SUBMIT_UNKNOWN),
        requires_login=login_required,
        requires_captcha=False,
        requires_identity=False,
        has_api_submission=False,
        deadline=expires_at,
        required_deliverable=requirements)

    source_meta = SourceMeta(
        source=f"human_fed:{platform}", source_type="human_fed",
        source_url=task_url, access_method=model.ACCESS_CURATED_FILE,
        automation_allowed=False, requires_login=login_required,
        requires_human=True,
        policy_status=_STATUS_TO_MODEL_POLICY[policy["status"]])

    evidence = [payment_evidence_quote]
    if requirements:
        evidence.append(requirements)

    raw_extra: dict = {
        "target_customer": "", "platform": platform,
        "platform_policy_status": policy["status"],
        "platform_policy_citation": policy["citation"],
        "submission_notes": submission_notes,
    }
    if not payment_ok:
        raw_extra["payment_evidence_insufficient"] = payment_reason
    if content_flag:
        raw_extra["requires_personal_judgment"] = content_flag

    draft = OpportunityDraft(
        title=title, description=description,
        opportunity_type=model.TYPE_TASK, evidence=evidence,
        source_meta=source_meta, source_id=external_task_id,
        source_url=task_url, discovered_at=captured_at_raw,
        est_pay_eur=(offered_payment if currency == "EUR" else 0.0),
        est_time_minutes=_optional_float(raw, "estimated_human_minutes"),
        demand_hint=0.5,          # a real, human-confirmed listing = real demand
        raw=raw_extra,
        payment_evidence=payment_evidence,
        submission_evidence=submission_evidence)

    return ParsedTask(draft=draft, platform=platform,
                      platform_policy_status=policy["status"],
                      payment_ok=payment_ok, payment_reason=payment_reason,
                      content_flag=content_flag,
                      external_task_id=external_task_id)


# ---------------------------------------------------------------------------
# the source (spec 5.1) - wraps ONE pre-validated draft
# ---------------------------------------------------------------------------

class HumanFedTaskSource:
    """`OpportunitySource` wrapping a single, already-validated,
    human-supplied draft. No network, no re-fetch, no auto re-poll - a
    human re-runs `ingest-task` to refresh a listing."""

    def __init__(self, draft: OpportunityDraft) -> None:
        self._draft = draft
        self.meta = draft.source_meta

    def discover(self, limit: int) -> list[OpportunityDraft]:
        return [self._draft] if limit > 0 else []


# ---------------------------------------------------------------------------
# orchestration - reuses DiscoveryEngine end to end (spec: no parallel
# system). See module docstring.
# ---------------------------------------------------------------------------

def _find_by_source_id(store, source: str, source_id: str) -> dict | None:
    for rec in store.all():
        d = rec.get("discovery") or {}
        if d.get("source") == source and d.get("source_id") == source_id:
            return rec
    return None


def _find_by_fingerprint(store, fp: str) -> dict | None:
    for rec in store.all():
        if (rec.get("discovery") or {}).get("task_fingerprint") == fp:
            return rec
    return None


def _load_json_file(path) -> dict:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise IngestionError(f"could not read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestionError(f"{p} is not valid JSON: {exc}") from exc
    return data


def _summary(rec: dict, parsed: ParsedTask, *, duplicate: bool) -> dict:
    d = rec.get("discovery") or {}
    v = d.get("verification") or {}
    checks = v.get("checks") or {}
    status = v.get("status", "")

    if status == model.V_BLOCKED:
        next_action = "platform policy disallows this - no autonomous " \
                     "action possible; handle entirely by hand or discard"
    elif status == model.V_HUMAN_REQUIRED:
        reason = (v.get("reasons") or ["a human must act"])[0]
        next_action = f"resolve manually - {reason}"
    elif status == model.V_REJECTED:
        reason = (v.get("reasons") or ["rejected"])[0]
        next_action = f"rejected - {reason}"
    elif status in (model.V_QUALIFIED, model.V_HUMAN_ATTESTED):
        next_action = (f"revenue_os select-strategy {rec['id']}, then "
                       f"plan-strategy {rec['id']}, then revenue_os worker "
                       "to prepare the draft")
    else:
        next_action = "check ecosystem-status for this opportunity"

    return {
        "opportunity_id": rec["id"],
        "platform": parsed.platform,
        "classification": checks.get("task_kind", ""),
        "verification_status": status,
        "task_quality_score": (checks.get("task_quality") or {}).get("total"),
        "payment_evidence_status": ("ok" if parsed.payment_ok
                                    else (parsed.payment_reason or "insufficient")),
        "duplicate": bool(duplicate),
        "platform_policy_status": parsed.platform_policy_status,
        "next_action": next_action,
    }


def ingest_task(data_dir, file_path, *, actor: str = "human") -> dict:
    """Validate + ingest one human-fed task JSON file. Idempotent by
    `external_task_id` when given (spec 5.8: it takes precedence); falls
    back to the existing TASK fingerprint dedupe otherwise. Reuses
    `DiscoveryEngine` end to end - no parallel persistence path."""
    from .discovery import DiscoveryEngine
    from . import task_signal
    from ..opportunity_store import load_opportunities

    data_dir = Path(data_dir)
    raw = _load_json_file(file_path)
    parsed = parse_task_json(raw)

    store = load_opportunities(data_dir)
    source = parsed.draft.source_meta.source
    if parsed.external_task_id:
        existing = _find_by_source_id(store, source, parsed.external_task_id)
        if existing is not None:
            return _summary(existing, parsed, duplicate=True)

    before_ids = {r["id"] for r in store.all()}
    DiscoveryEngine(data_dir, sources=[HumanFedTaskSource(parsed.draft)]).run(
        limit_per_source=1)

    store2 = load_opportunities(data_dir)
    fp = task_signal.task_fingerprint(parsed.draft)
    rec = _find_by_fingerprint(store2, fp)
    if rec is None:
        raise IngestionError(
            "internal: could not locate the ingested opportunity after "
            "discovery - this should never happen for a TYPE_TASK draft")

    duplicate = rec["id"] in before_ids
    return _summary(rec, parsed, duplicate=duplicate)
