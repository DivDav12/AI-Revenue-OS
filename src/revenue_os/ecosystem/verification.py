"""Opportunity verification (spec section 8).

A discovered opportunity is a *claim*. `verify()` is a pure, deterministic
gate that turns the claim into one of:

    DISCOVERED -> VERIFYING -> VERIFIED -> QUALIFIED   (proceed)
                            -> REJECTED               (drop it)
                            -> HUMAN_REQUIRED         (a person must act)
                            -> BLOCKED                (policy forbids automation)

Only a QUALIFIED opportunity may be planned into a real task chain
(`model.PLANNABLE`). Fail closed: missing evidence, an unknown policy
status, or a capability we do not have -> not QUALIFIED.

No network, no LLM. It checks the *shape and provenance* of what the
source gave us, not the live world - live re-checks belong to the
discovery source itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import model
from .model import OpportunityDraft

#: opportunity types the current execution stack can actually fulfil
#: autonomously end-to-end (build -> page -> deploy -> PayPal -> SMTP deliver).
_FLEET_CAN_DELIVER = frozenset({
    model.TYPE_DIGITAL_PRODUCT, model.TYPE_CONTENT,
})
#: types the fleet can prepare fully but a human must submit/close
_FLEET_PREPARES_HUMAN_CLOSES = frozenset({
    model.TYPE_TASK, model.TYPE_SERVICE, model.TYPE_SOFTWARE_TOOL,
    model.TYPE_AFFILIATE, model.TYPE_ECOMMERCE, model.TYPE_DROPSHIPPING,
})


@dataclass
class VerificationResult:
    status: str
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    requires_human: bool = False
    blocked: bool = False

    def to_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons),
                "checks": dict(self.checks), "requires_human": self.requires_human,
                "blocked": self.blocked}


def verify(draft: OpportunityDraft, *, now_iso: str = "",
           min_pay_eur: float = 3.0) -> VerificationResult:
    """Deterministic verification of one draft."""
    checks: dict = {}
    reasons: list[str] = []
    meta = draft.source_meta

    # 1. provenance - a real source with a stable id/url, or synthetic
    checks["has_source"] = meta is not None
    if meta is None:
        return VerificationResult(model.V_REJECTED, ["no source metadata"],
                                  checks)
    checks["source"] = meta.source
    checks["synthetic"] = meta.access_method == model.ACCESS_SYNTHETIC

    # 2. policy gate - fail closed on anything but a known-good status
    policy = meta.policy_status
    checks["policy_status"] = policy
    if policy == model.POLICY_BLOCKED:
        return VerificationResult(
            model.V_BLOCKED,
            [f"source {meta.source!r} forbids the planned automation"],
            checks, blocked=True)
    if policy in (model.POLICY_HUMAN_SETUP_REQUIRED,):
        return VerificationResult(
            model.V_HUMAN_REQUIRED,
            [f"source {meta.source!r} needs a human to wire an account / API key"],
            checks, requires_human=True)
    if policy not in (model.POLICY_OK, model.POLICY_HUMAN_REQUIRED):
        return VerificationResult(model.V_REJECTED,
                                  [f"unknown policy_status {policy!r}"], checks)

    # 3. evidence - the claim must be backed by verbatim facts from the source
    evidence = [e for e in (draft.evidence or []) if str(e).strip()]
    checks["evidence_count"] = len(evidence)
    if not evidence:
        return VerificationResult(model.V_REJECTED,
                                  ["no evidence from the source"], checks)
    checks["has_title"] = bool(str(draft.title or "").strip())
    if not checks["has_title"]:
        return VerificationResult(model.V_REJECTED, ["no title"], checks)

    # 4. type + capability match
    otype = draft.opportunity_type
    checks["opportunity_type"] = otype
    if otype not in model.OPPORTUNITY_TYPES:
        return VerificationResult(model.V_REJECTED,
                                  [f"unknown opportunity_type {otype!r}"], checks)
    can_deliver = otype in _FLEET_CAN_DELIVER
    prepares = otype in _FLEET_PREPARES_HUMAN_CLOSES
    checks["fleet_can_deliver_autonomously"] = can_deliver
    checks["fleet_prepares_human_closes"] = prepares
    if not can_deliver and not prepares:
        return VerificationResult(
            model.V_REJECTED,
            [f"the fleet cannot fulfil an {otype!r} opportunity"], checks)

    # 5. compensation realism (only when the source gave a number)
    pay = float(draft.est_pay_eur or 0.0)
    checks["est_pay_eur"] = pay
    if pay and pay < float(min_pay_eur):
        reasons.append(f"stated pay EUR {pay:.2f} is below the floor "
                       f"EUR {min_pay_eur:.2f}")
        return VerificationResult(model.V_REJECTED, reasons, checks)

    # 6. an external-action opportunity on a login-gated source needs a human
    if prepares and not can_deliver:
        if meta.requires_login or meta.policy_status == model.POLICY_HUMAN_REQUIRED:
            reasons.append("the fleet can prepare the work but a human must "
                           "submit it on the source platform")
            return VerificationResult(model.V_HUMAN_REQUIRED, reasons, checks,
                                      requires_human=True)

    # passed every gate
    status = model.V_QUALIFIED if (can_deliver or prepares) else model.V_VERIFIED
    reasons.append("verified: real source, evidence present, policy OK, "
                   "fleet can act")
    return VerificationResult(status, reasons, checks)
