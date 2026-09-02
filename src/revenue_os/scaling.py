"""Promotion decision + scaling adapters (Phase 15).

  evaluate_promotion(opp, policy) -> PromotionDecision      (pure, testable)
  adapter.scale(ScalingRequest) -> ScalingResult

After real measurement data exists AND Phase-14 has recorded at least one
optimization variant, the decision function checks the PERSISTED evidence
(cumulative visitors / leads / revenue, measurement cycles, existing
optimization + scaling history) and decides whether a variant has enough
evidence to be PROMOTED / scaled.

Conservative by default: no promotion on a single visitor or a single
lead, no promotion below the minimum measurement basis, at most
`max_scalings` per opportunity, and never the same variant twice.

The SCALE task runs like any other: TaskQueue -> Worker -> ScalingAdapter
-> ScalingResult -> EventLog + execution.scalings. It records a scaling
DECISION and performs only SAFE, INTERNAL, offline steps (queue an
owned-channel variant, draft SEO angles, draft an upsell idea). It NEVER:
  * spends money, buys ads, books a paid service
  * makes a PayPal / card payment
  * creates an account, does KYC, signs anything
  * posts to a social platform or sends an external message

If a future real scaling action would cost money, the adapter returns
`requires_approval="money"` and the worker BLOCKS the task
(BLOCKED_APPROVAL) instead of executing it. For Phase 15 the Fake / Null
adapters are enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# promotion decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionPolicy:
    min_measurement_cycles: int = 10   # traffic rounds of real data
    min_visitors: int = 60             # cumulative - a believable sample
    min_leads: int = 3                 # real demand, not one fluke
    max_scalings: int = 2              # hard cap per opportunity


DEFAULT_PROMOTION_POLICY = PromotionPolicy()


@dataclass
class PromotionDecision:
    promote: bool = False
    reason: str = ""
    variant_id: str = ""
    evidence: dict = field(default_factory=dict)


def _cumulative(series: list, kind: str, key: str) -> float:
    return sum(_num((s.get("metrics") or {}).get(key))
               for s in series if s.get("kind") == kind)


def evaluate_promotion(
        opp: dict, *,
        policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY) -> PromotionDecision:
    """Deterministic: does this opportunity have a variant with enough
    persisted evidence to promote / scale now? Explicit reason either way."""
    ex = opp.get("execution") or {}
    optimizations = ex.get("optimizations") or []
    if not optimizations:
        return PromotionDecision(reason="no optimization variant to promote")

    scalings = ex.get("scalings") or []
    if len([s for s in scalings if s.get("status") == "success"]) >= policy.max_scalings:
        return PromotionDecision(
            reason=f"scaling cap reached ({policy.max_scalings})")

    scaled = {s.get("variant_id") for s in scalings if s.get("status") == "success"}
    unscaled = [o for o in optimizations if o.get("variant_id") not in scaled]
    if not unscaled:
        return PromotionDecision(reason="all optimization variants already scaled")
    target = unscaled[-1]
    vid = str(target.get("variant_id", ""))

    series = ex.get("measurement_series") or []
    cycles = sum(1 for s in series if s.get("kind") == "traffic")
    visitors = _cumulative(series, "traffic", "visitors")
    leads = _cumulative(series, "leads", "leads")
    revenue = _num((ex.get("metrics") or {}).get("revenue", {}).get("revenue_eur"))

    evidence = {
        "variant_id": vid,
        "visitors": int(visitors),
        "leads": int(leads),
        "revenue_eur": round(revenue, 2),
        "measurement_cycles": cycles,
        "optimization_variants": len(optimizations),
        "prior_scalings": len(scalings),
    }

    if cycles < policy.min_measurement_cycles:
        return PromotionDecision(
            reason=f"insufficient measurement basis: {cycles} cycle(s) < "
                   f"{policy.min_measurement_cycles}",
            variant_id=vid, evidence=evidence)
    if visitors < policy.min_visitors:
        return PromotionDecision(
            reason=f"only {int(visitors)} visitor(s) < {policy.min_visitors} - "
                   "not a believable sample to scale on",
            variant_id=vid, evidence=evidence)
    if revenue <= 0 and leads < policy.min_leads:
        return PromotionDecision(
            reason=f"no traction signal: EUR 0 revenue and {int(leads)} lead(s) "
                   f"< {policy.min_leads}",
            variant_id=vid, evidence=evidence)

    signal = (f"EUR {revenue:.2f} revenue" if revenue > 0
              else f"{int(leads)} leads")
    evidence["reason"] = (
        f"variant {vid}: {signal} over {cycles} measurement cycles / "
        f"{int(visitors)} visitors - enough evidence to scale the working offer")
    return PromotionDecision(True, evidence["reason"], vid, evidence)


# ---------------------------------------------------------------------------
# scaling adapter
# ---------------------------------------------------------------------------

@dataclass
class ScalingRequest:
    opportunity_id: str
    variant_id: str
    variant: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    focus: str = ""


@dataclass
class ScalingResult:
    success: bool
    provider: str
    scale_id: str = ""
    actions: list = field(default_factory=list)   # safe internal steps "queued"
    error: str = ""
    blocked: bool = False
    requires_approval: str = ""     # "money" if a real action would cost money
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success, "provider": self.provider,
            "scale_id": self.scale_id, "actions": list(self.actions),
            "error": self.error, "blocked": self.blocked,
            "requires_approval": self.requires_approval,
            "details": dict(self.details),
        }


class ScalingAdapter:
    provider = "base"

    def scale(self, request: ScalingRequest) -> ScalingResult:  # pragma: no cover
        raise NotImplementedError


class NullScalingAdapter(ScalingAdapter):
    provider = "none"

    def scale(self, request: ScalingRequest) -> ScalingResult:
        return ScalingResult(
            success=False, blocked=True, provider=self.provider,
            error="no scaling provider is configured - the scaling path is "
                  "ready, a real provider adapter must be wired")


_SAFE_ACTIONS = (
    "queued a second owned-channel announcement variant",
    "drafted 3 evergreen SEO landing angles for the working offer",
    "drafted an upsell / bundle idea for the next buyer cohort",
)


class FakeScalingAdapter(ScalingAdapter):
    """Deterministic, offline. Performs only SAFE INTERNAL steps (all
    'drafted' / 'queued', nothing executed). No ads, no spend, no accounts,
    no posting. Idempotent per (opportunity, variant)."""

    provider = "fake"

    def __init__(self, *, fail: bool = False, blocked: bool = False,
                 error: str = "", requires_approval: str = "") -> None:
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.requires_approval = requires_approval
        self.calls: list[tuple[str, str]] = []
        self._scaled: dict[tuple[str, str], ScalingResult] = {}

    def scale(self, request: ScalingRequest) -> ScalingResult:
        self.calls.append((request.opportunity_id, request.variant_id))
        if self.blocked:
            return ScalingResult(success=False, blocked=True,
                                 provider=self.provider,
                                 error=self.error or "fake: no scaling provider")
        if self.requires_approval:
            return ScalingResult(success=False, provider=self.provider,
                                 requires_approval=self.requires_approval,
                                 error=f"fake: a real scaling action here would "
                                       f"need a {self.requires_approval} approval")
        if self.fail:
            return ScalingResult(success=False, provider=self.provider,
                                 error=self.error or "fake: scaling step failed")

        key = (request.opportunity_id, request.variant_id)
        prior = self._scaled.get(key)
        if prior is not None:
            from dataclasses import replace
            return replace(prior, details={**prior.details,
                                           "duplicate_suppressed": True})

        vsuffix = request.variant_id.rsplit("-", 1)[-1] or "01"
        res = ScalingResult(
            success=True, provider=self.provider,
            scale_id=f"scale-{str(request.opportunity_id)[:12]}-{vsuffix}",
            actions=list(_SAFE_ACTIONS),
            details={"focus": request.focus,
                     "based_on": dict(request.evidence)})
        self._scaled[key] = res
        return res


def default_scaling_adapter() -> ScalingAdapter:
    return NullScalingAdapter()
