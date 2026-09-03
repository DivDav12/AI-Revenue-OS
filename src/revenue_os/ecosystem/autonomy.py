"""Autonomy levels (spec section 18).

This is a PRESENTATION layer over the existing `action_class` firewall - it
does not add policy. It maps an ecosystem *activity* to:

  * an activity class  : READ_ONLY | RESEARCH | BUILD | DRAFT | PUBLISH |
                         CONTACT | SELL | DELIVER | BUY | PAY | ADVERTISE
  * an autonomy verdict: AUTONOMOUS_ALLOWED | HUMAN_APPROVAL_REQUIRED |
                         HUMAN_REQUIRED | BLOCKED

The verdict always comes from `action_class.classify()` on the underlying
action kind, so the money / identity / legal / TOS gates are exactly the
ones the rest of the system already enforces. Fail closed on an unknown
activity.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import action_class as _ac

READ_ONLY = "READ_ONLY"
RESEARCH = "RESEARCH"
BUILD = "BUILD"
DRAFT = "DRAFT"
PUBLISH = "PUBLISH"
CONTACT = "CONTACT"
SELL = "SELL"
DELIVER = "DELIVER"
BUY = "BUY"
PAY = "PAY"
ADVERTISE = "ADVERTISE"
ACTIVITY_CLASSES = (READ_ONLY, RESEARCH, BUILD, DRAFT, PUBLISH, CONTACT, SELL,
                    DELIVER, BUY, PAY, ADVERTISE)

AUTONOMOUS_ALLOWED = "AUTONOMOUS_ALLOWED"
HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
BLOCKED = "BLOCKED"

# activity -> (class, representative action_class kind)
_ACTIVITY = {
    "discover":            (RESEARCH, "discover_opportunity"),
    "verify":              (RESEARCH, "verify_opportunity"),
    "evaluate":            (RESEARCH, "evaluate_profitability"),
    "select_strategy":     (RESEARCH, "select_strategy"),
    "simulate":            (READ_ONLY, "simulate_revenue"),
    "learn":               (READ_ONLY, "learn_from_outcomes"),
    "build_product":       (BUILD, "create_digital_product"),
    "build_page":          (BUILD, "build_landing_page"),
    "prepare_task":        (BUILD, "prepare_task_solution"),
    "prepare_listing":     (BUILD, "prepare_store_listing"),
    "affiliate_research":  (RESEARCH, "affiliate_offer_research"),
    "supplier_research":   (RESEARCH, "supplier_research"),
    "draft_outreach":      (DRAFT, "draft_outreach_message"),
    "draft_social":        (DRAFT, "create_social_draft"),
    "publish_owned":       (PUBLISH, "publish_website"),
    "deploy_checkout":     (SELL, "deploy_page"),
    "contact_customer":    (CONTACT, "post_public_reply"),
    "deliver_product":     (DELIVER, "create_digital_product"),
    "place_supplier_order": (BUY, "place_supplier_order"),
    "fund_ad_test":        (ADVERTISE, "fund_ad_test"),
    "run_paid_ads":        (ADVERTISE, "launch_paid_ad_campaign"),
    "join_affiliate_program": (CONTACT, "register_on_platform"),
    "open_store_account":  (CONTACT, "create_service_account"),
}


@dataclass(frozen=True)
class AutonomyVerdict:
    activity: str
    activity_class: str
    verdict: str
    reason: str

    def to_dict(self) -> dict:
        return {"activity": self.activity, "activity_class": self.activity_class,
                "verdict": self.verdict, "reason": self.reason}


def classify_activity(activity: str, context: dict | None = None) -> AutonomyVerdict:
    a = (activity or "").strip()
    if a not in _ACTIVITY:
        return AutonomyVerdict(a or "<empty>", READ_ONLY, BLOCKED,
                               f"unknown ecosystem activity {a!r} - failing closed")
    cls, kind = _ACTIVITY[a]
    v = _ac.classify(kind, context)
    ac = v.action_class
    if ac is _ac.ActionClass.SAFE_AUTONOMOUS:
        verdict = AUTONOMOUS_ALLOWED
    elif ac is _ac.ActionClass.MONEY_APPROVAL_REQUIRED:
        verdict = HUMAN_APPROVAL_REQUIRED
    elif ac in (_ac.ActionClass.IDENTITY_APPROVAL_REQUIRED,
                _ac.ActionClass.LEGAL_APPROVAL_REQUIRED):
        verdict = HUMAN_REQUIRED
    else:                                   # SAFETY_BLOCKED
        verdict = BLOCKED
    # a CONTACT / account activity on a third-party platform is HUMAN_REQUIRED
    # even when action_class would allow the underlying kind
    if cls == CONTACT and a in ("join_affiliate_program", "open_store_account"):
        verdict = HUMAN_REQUIRED
    return AutonomyVerdict(a, cls, verdict, v.reason)
