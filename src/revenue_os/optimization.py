"""Optimization decision + adapters (Phase 14).

  evaluate_optimization(opp, policy) -> OptimizationDecision   (pure, testable)
  adapter.optimize(OptimizationRequest) -> OptimizationResult

After real measurement data exists, the decision function looks at the
persisted time series + the current opportunity state and decides whether
a SAFE, INTERNAL optimization is warranted (rewrite copy / CTA, a pricing
or positioning hypothesis, a new variant idea). It never optimizes on a
single watchdog tick, never on too little data, and is fully deterministic
and explainable.

The OPTIMIZE task runs like any other: TaskQueue -> Worker -> adapter ->
AdapterResult -> EventLog. It produces a VARIANT DRAFT recorded on the
opportunity (`execution.optimizations`). It NEVER:
  * spends money, buys ads, creates an account, does KYC
  * sends an external message, posts to a platform
  * deploys or promotes the variant live
  * signs anything or triggers a payment
A variant that later needs any of those is left for a downstream task under
the existing approval system - Phase 14 stops at "hypothesis recorded".

Fail-closed when no optimization provider is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: an opportunity may be optimized only while in one of these states
SUITABLE_STATES: frozenset[str] = frozenset({
    "MEASURING", "FIRST_VISITOR", "FIRST_LEAD", "NO_TRACTION", "ACTIVE",
})


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptimizationPolicy:
    min_measurement_rounds: int = 8   # traffic rounds before any optimization
    min_visitors: int = 30            # enough traffic to judge conversion
    max_variants: int = 3             # hard cap per opportunity - no explosion
    cooldown_rounds: int = 6          # rounds between successive variants


DEFAULT_OPTIMIZATION_POLICY = OptimizationPolicy()


@dataclass
class OptimizationDecision:
    optimize: bool = False
    reason: str = ""
    focus: str = ""                   # offer_landing_copy | landing_copy |
                                      # offer_pricing | cta | scale_variant
    signal: dict = field(default_factory=dict)


def _cumulative(series: list, kind: str, key: str) -> float:
    return sum(_num((s.get("metrics") or {}).get(key))
               for s in series if s.get("kind") == kind)


def evaluate_optimization(
        opp: dict, *,
        policy: OptimizationPolicy = DEFAULT_OPTIMIZATION_POLICY) -> OptimizationDecision:
    """Deterministic: should this opportunity get a safe internal
    optimization now? Returns a decision with an explicit reason either way."""
    state = opp.get("state")
    if state not in SUITABLE_STATES:
        return OptimizationDecision(
            reason=f"state {state!r} is not a suitable state for optimization")

    ex = opp.get("execution") or {}
    series = ex.get("measurement_series") or []
    traffic_rounds = sum(1 for s in series if s.get("kind") == "traffic")
    if traffic_rounds < policy.min_measurement_rounds:
        return OptimizationDecision(
            reason=f"insufficient measurement basis: {traffic_rounds} traffic "
                   f"round(s) < {policy.min_measurement_rounds}")

    opts = ex.get("optimizations") or []
    if len(opts) >= policy.max_variants:
        return OptimizationDecision(
            reason=f"variant cap reached ({policy.max_variants})")
    if opts:
        since = traffic_rounds - int(opts[-1].get("rounds_at_creation", 0))
        if since < policy.cooldown_rounds:
            return OptimizationDecision(
                reason=f"cooldown: only {since} round(s) since the last variant")

    visitors = _cumulative(series, "traffic", "visitors")
    leads = _cumulative(series, "leads", "leads")
    clicks = _cumulative(series, "traffic", "clicks")
    revenue = _num((ex.get("metrics") or {}).get("revenue", {}).get("revenue_eur"))
    sig = {"traffic_rounds": traffic_rounds, "visitors": int(visitors),
           "leads": int(leads), "clicks": int(clicks), "revenue_eur": revenue}

    if state == "NO_TRACTION":
        return OptimizationDecision(
            True,
            f"no traction after {traffic_rounds} rounds / {int(visitors)} "
            "visitors - rewrite the offer + landing page around the sharpest "
            "pain point", "offer_landing_copy", sig)

    if visitors < policy.min_visitors:
        return OptimizationDecision(
            reason=f"only {int(visitors)} visitor(s) - not enough traffic to "
                   "judge conversion (a distribution problem, not an "
                   "optimization one)")

    if leads == 0 and revenue == 0:
        return OptimizationDecision(
            True,
            f"{int(visitors)} visitors over {traffic_rounds} rounds, 0 leads, "
            "0 sales - the page is not converting; rewrite the headline + CTA",
            "landing_copy", sig)
    if leads > 0 and revenue == 0:
        return OptimizationDecision(
            True,
            f"{int(leads)} lead(s) but 0 sales - test offer scope / trust / "
            "pricing", "offer_pricing", sig)
    if revenue > 0 and state == "ACTIVE":
        return OptimizationDecision(
            True,
            f"EUR {revenue:.2f} booked - generate a variant to widen the top "
            "of the funnel", "scale_variant", sig)
    if clicks > 0 and visitors > 0 and (clicks / visitors) < 0.05:
        return OptimizationDecision(
            True,
            f"click-through {int(clicks)}/{int(visitors)} is weak - sharpen "
            "the CTA", "cta", sig)

    return OptimizationDecision(reason="no clear conversion-weakness signal")


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

@dataclass
class OptimizationRequest:
    opportunity_id: str
    focus: str
    signal: dict = field(default_factory=dict)
    opportunity: dict = field(default_factory=dict)
    variant_number: int = 1


@dataclass
class OptimizationResult:
    success: bool
    provider: str
    variant_id: str = ""
    focus: str = ""
    hypothesis: str = ""
    variant: dict = field(default_factory=dict)
    rationale: str = ""
    requires_before_live: list = field(default_factory=list)
    error: str = ""
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success, "provider": self.provider,
            "variant_id": self.variant_id, "focus": self.focus,
            "hypothesis": self.hypothesis, "variant": dict(self.variant),
            "rationale": self.rationale,
            "requires_before_live": list(self.requires_before_live),
            "error": self.error, "blocked": self.blocked,
        }


class OptimizationAdapter:
    provider = "base"

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:  # pragma: no cover
        raise NotImplementedError


class NullOptimizationAdapter(OptimizationAdapter):
    provider = "none"

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        return OptimizationResult(
            success=False, blocked=True, provider=self.provider,
            focus=request.focus,
            error="no optimization provider is configured - the optimization "
                  "path is ready, a real provider adapter must be wired")


_REQUIRES_BEFORE_LIVE = ("build_page", "validate_page", "deploy_approval")


class FakeOptimizationAdapter(OptimizationAdapter):
    """Deterministic, offline. No LLM, no network. Produces a variant DRAFT
    only - it is never built, deployed, or promoted here."""

    provider = "fake"

    def __init__(self, *, fail: bool = False, blocked: bool = False,
                 error: str = "") -> None:
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        self.calls.append((request.opportunity_id, request.focus,
                           request.variant_number))
        if self.blocked:
            return OptimizationResult(success=False, blocked=True,
                                      provider=self.provider, focus=request.focus,
                                      error=self.error or "fake: no credentials")
        if self.fail:
            return OptimizationResult(success=False, provider=self.provider,
                                      focus=request.focus,
                                      error=self.error or "fake: optimize failed")

        n = int(request.variant_number)
        title = str(request.opportunity.get("title") or "offer")
        vid = f"var-{str(request.opportunity_id)[:12]}-{n:02d}"
        by_focus = {
            "offer_landing_copy": {"headline": f"{title} - the clear version",
                                   "cta": "Start now",
                                   "note": "rewritten around the pain point"},
            "landing_copy": {"headline": f"Stop guessing about {title}",
                             "cta": "Get the fix"},
            "offer_pricing": {"price_hypothesis": "try a smaller-scope tier at -20%",
                              "trust": "add two concrete outcomes + a refund line"},
            "cta": {"cta": "See it in 60 seconds", "placement": "above the fold"},
            "scale_variant": {"angle": "a second buyer segment",
                              "headline": f"{title} for teams"},
        }
        return OptimizationResult(
            success=True, provider=self.provider, variant_id=vid,
            focus=request.focus,
            hypothesis=(f"Variant {n} ({request.focus}) lifts conversion versus "
                        "the current page"),
            variant=by_focus.get(request.focus, {"headline": f"{title} v{n}"}),
            rationale=(f"deterministic variant from signal {request.signal}"
                       if request.signal else "deterministic fake variant"),
            requires_before_live=list(_REQUIRES_BEFORE_LIVE))


def default_optimization_adapter() -> OptimizationAdapter:
    return NullOptimizationAdapter()
