"""Ad Campaign Readiness - architecture for FUTURE budget-gated paid ads.

NOT ACTIVE. No ad platform is connected, no campaign is ever launched,
funded, or scheduled by this module. It exists only to:

  1. Define the structured plan a human must explicitly supply (budget,
     max CPC, max CPA, stop-loss) before paid spend could ever even be
     proposed - never a default, never invented.
  2. Compute, from REAL, already-settled commission data, whether the
     minimum conversion-evidence bar is met yet - "we have a real,
     deployed page" is never treated as sufficient justification for ad
     spend on its own.

This is a SECOND, INDEPENDENT gate on top of the one that already exists:
`action_class.py` already classifies `buy_ads` / `fund_ad_test` /
`launch_paid_ad_campaign` as MONEY/ADVERTISE, which `autonomy.py` already
routes to HUMAN_APPROVAL_REQUIRED - that gate is unchanged and still
applies regardless of anything this module reports. This module cannot
lower it, bypass it, or spend anything; it only decides whether the
EVIDENCE a human would want before approving spend already exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from .affiliate_model import SETTLED_COMMISSION_STATUSES, CommissionStore

#: never auto-lowered - a human raising it is fine, a human (or this
#: module) silently lowering the bar to make spend look "ready" is not.
_DEFAULT_MIN_CONFIRMED_CONVERSIONS = 3


@dataclass(frozen=True)
class AdCampaignPlan:
    """The explicit terms a human must supply. No field has a spend-
    enabling default - constructing one is not itself an approval to
    spend anything, it only makes readiness evaluable."""

    offer_id: str
    channel: str                       # a label only (e.g. "google_search") - no integration exists
    budget_eur: float
    max_cpc_eur: float
    max_cpa_eur: float
    stop_loss_eur: float
    min_confirmed_conversions: int = _DEFAULT_MIN_CONFIRMED_CONVERSIONS

    def to_dict(self) -> dict:
        return {
            "offer_id": self.offer_id, "channel": self.channel,
            "budget_eur": round(self.budget_eur, 2),
            "max_cpc_eur": round(self.max_cpc_eur, 2),
            "max_cpa_eur": round(self.max_cpa_eur, 2),
            "stop_loss_eur": round(self.stop_loss_eur, 2),
            "min_confirmed_conversions": self.min_confirmed_conversions,
        }


def ad_campaign_readiness(data_dir, *, offer_id: str, plan: AdCampaignPlan | None = None) -> dict:
    """Pure, read-only, never spends anything. Reuses the REAL, already-
    settled `CommissionStore` (the same ledger-backed data
    `affiliate_intel.affiliate_status()` reports as `revenue_eur`) - never
    a projection or estimate of future conversions."""
    commissions = [c for c in CommissionStore.load(data_dir).all()
                  if c.offer_id == offer_id and c.status in SETTLED_COMMISSION_STATUSES]
    confirmed_conversions = len(commissions)
    confirmed_revenue_eur = round(sum(c.amount for c in commissions), 2)

    min_required = plan.min_confirmed_conversions if plan else _DEFAULT_MIN_CONFIRMED_CONVERSIONS
    evidence_ready = confirmed_conversions >= min_required

    reasons: list[str] = []
    if not evidence_ready:
        reasons.append(
            f"only {confirmed_conversions} confirmed (settled) conversion(s) on file for this "
            f"offer - need >= {min_required} real conversions before paid spend is even "
            "proposed (this floor is never lowered automatically)")
    if plan is None:
        reasons.append(
            "no AdCampaignPlan supplied - a human must set budget_eur/max_cpc_eur/"
            "max_cpa_eur/stop_loss_eur explicitly before this can be evaluated further")

    ready_to_propose = evidence_ready and plan is not None
    return {
        "offer_id": offer_id,
        "confirmed_conversions": confirmed_conversions,
        "confirmed_revenue_eur": confirmed_revenue_eur,
        "min_confirmed_conversions_required": min_required,
        "evidence_ready": evidence_ready,
        "plan": plan.to_dict() if plan else None,
        "status": "READY_FOR_HUMAN_APPROVAL" if ready_to_propose else "NOT_READY",
        "always_requires_human_approval": True,
        "reasons": reasons,
        "note": "This module never spends money, connects to an ad platform, or launches a "
               "campaign. action_class.py's existing MONEY/ADVERTISE classification "
               "(buy_ads / fund_ad_test / launch_paid_ad_campaign) still requires separate, "
               "explicit human approval regardless of this evidence check.",
    }
