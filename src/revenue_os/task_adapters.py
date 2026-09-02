"""Concrete task adapters + the default registry.

Each adapter maps one or more `task_type`s to real work done by an
EXISTING deterministic roster agent (via `agent_runner.run_agent`, EUR 0,
no network) or a small pure synthesis step. Nothing here spends money,
calls a paid API, touches PayPal, takes an identity/legal action, or posts
anywhere.

task_types WITHOUT an adapter here (DEPLOY, DISTRIBUTE, CHECK_TRAFFIC/
LEADS/REVENUE, DELIVER, SPAWN_VARIANT, SCALE) are handled in later phases;
until then the worker fails such a task cleanly as "no adapter".
"""

from __future__ import annotations

from . import agent_runner
from .deployment import (
    DeploymentAdapter,
    DeploymentArtifact,
    default_deployment_adapter,
    slugify,
    valid_live_url,
)
from .delivery_adapters import (
    DeliveryAdapter,
    DeliveryArtifact,
    DeliveryRecipient,
    default_delivery_adapter,
)
from .distribution_adapters import (
    DRAFT_CHANNELS,
    DistributionAdapter,
    DistributionRequest,
    default_distribution_adapter,
)
from .measurement import (
    MeasurementAdapter,
    default_measurement_adapter,
)
from .measurement import _num
from .optimization import (
    OptimizationAdapter,
    OptimizationRequest,
    default_optimization_adapter,
)
from .payments import PaymentAdapter, default_payment_adapter, process_payment_event
from .scaling import ScalingAdapter, ScalingRequest, default_scaling_adapter
from .worker import AdapterContext, AdapterRegistry, AdapterResult, TaskAdapter

# ---------------------------------------------------------------------------
# deterministic offer / copy synthesis (mirrors the autonomy loop's shape)
# ---------------------------------------------------------------------------

def _offer(opp: dict) -> dict:
    price = max(9.0, round(float(opp.get("est_revenue_eur", 0) or 0) / 6.0, 2)) or 19.0
    return {
        "what_is_sold": opp.get("title", "digital offer"),
        "price": price, "currency": "EUR", "delivery": "digital",
        "price_is_estimate": True,
        "positioning": f"For {opp.get('target_customer', 'a specific customer')}.",
        "includes": [opp.get("required_work", "the core deliverable"),
                     "a short how-to-use guide"],
        "call_to_action": "Get it",
        "disclaimer": "Early experiment - you are buying a specific deliverable, "
                      "not guaranteed business results.",
    }


def _copy(opp: dict) -> dict:
    return {
        "headline": opp.get("title", ""),
        "subheadline": f"Made for {opp.get('target_customer', 'you')}.",
        "body": opp.get("required_work", ""),
        "primary_cta": "Get it",
        "faq": [{"question": "Is this a subscription?", "answer": "No, one-off."},
                {"question": "Refunds?", "answer": "Yes if it does not fit."}],
    }


# ---------------------------------------------------------------------------
# agent-backed adapter
# ---------------------------------------------------------------------------

class AgentTaskAdapter(TaskAdapter):
    """Routes a task_type to a live, non-human-gated roster capability."""

    def __init__(self, task_types, capability: str, payload_fn, *,
                 objective: str = "task") -> None:
        self.task_types = tuple(task_types)
        self.capability = capability
        self.payload_fn = payload_fn
        self.objective = objective
        self.name = f"agent:{capability}"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        res = agent_runner.run_agent(
            ctx.data_dir, self.capability, self.payload_fn(ctx),
            objective=f"{self.objective}: {ctx.opportunity.get('title', '')}",
            persist=False)
        if res.status != "ok":
            # control-plane blocks / bad payloads are worth retrying once the
            # operator fixes them; a hard agent error is retryable too.
            return AdapterResult(ok=False, retryable=True,
                                 error=res.error or "agent returned an error")
        out = dict(res.output)
        if out.get("human_gate_required") or out.get("_gate") == "human":
            return AdapterResult(ok=False, retryable=False,
                                 error=f"{self.capability} output is human-gated "
                                       "- not an autonomous success")
        if out.get("qc_status") == "block":
            issues = out.get("blocking_issues") or out.get("failed_checks") or []
            return AdapterResult(ok=False, retryable=False,
                                 output=out,
                                 error="quality check blocked: " + "; ".join(
                                     str(i) for i in issues))
        return AdapterResult(ok=True, output=out)


# --- payload builders ------------------------------------------------------

def _p_select(ctx: AdapterContext) -> dict:
    o = ctx.opportunity
    return {"scored": [{"name": o.get("id") or o.get("title", ""),
                        "total": float(o.get("score", 0) or 0)}],
            "min_score": 0.0, "shortlist_n": 1}


def _p_distribution(ctx: AdapterContext) -> dict:
    o = ctx.opportunity
    return {"opportunity": {"id": o.get("id", ""), "name": o.get("id", ""),
                            "title": o.get("title", ""),
                            "target_customer": o.get("target_customer", ""),
                            "category": o.get("category", ""),
                            "required_work": o.get("required_work", ""),
                            "probability": o.get("probability")},
            "offer": _offer(o), "copy": _copy(o)}


def _p_package(ctx: AdapterContext) -> dict:
    o = ctx.opportunity
    return {"candidate": {"name": o.get("id") or o.get("title", "offer"),
                          "description": o.get("title", "")},
            "offer": _offer(o), "draft": _copy(o),
            "plan": {"hypothesis": o.get("title", "")}}


def _p_qc(ctx: AdapterContext) -> dict:
    o = ctx.opportunity
    pkg = (ctx.dep_outputs.get("BUILD_PAGE")
           or ctx.dep_outputs.get("BUILD_PRODUCT")
           or ctx.dep_outputs.get("CREATE_CONTENT") or {})
    return {"offer": _offer(o), "copy": _copy(o),
            "landing_page": pkg.get("landing_html", ""),
            "launch_plan": {"hypothesis": o.get("title", "")},
            "agent_results": [{"output": v} for v in ctx.dep_outputs.values()
                              if isinstance(v, dict)],
            "expected_business_email": ""}


# ---------------------------------------------------------------------------
# pure synthesis adapters
# ---------------------------------------------------------------------------

class PlanAdapter(TaskAdapter):
    task_types = ("PLAN",)
    name = "plan"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        o = ctx.opportunity
        return AdapterResult(ok=True, output={
            "offer": _offer(o), "copy": _copy(o),
            "hypothesis": f"{o.get('title', 'this offer')} gets its first sale "
                          "within 30 days of a live page + one owned channel",
            "plan": {"steps": ["build page", "validate page", "deploy",
                               "distribute (owned)", "measure"]},
        })


class AnalyzeAdapter(TaskAdapter):
    task_types = ("ANALYZE",)
    name = "analyze"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        r = dict(ctx.opportunity.get("results", {}) or {})
        return AdapterResult(ok=True, output={
            "opportunity_id": ctx.opportunity.get("id", ""),
            "cycles": r.get("cycles", 0),
            "revenue_eur": r.get("revenue_eur", 0),
            "note": "deterministic snapshot of persisted results - real traffic "
                    "/ lead / conversion metrics arrive with the measurement "
                    "loop (Phase 10)",
        })


def _announce_html(opp: dict, channel: str, live_url: str) -> str:
    title = str(opp.get("title") or "our new offer")
    audience = str(opp.get("target_customer") or "you")
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{title}</title>"
        f"<h1>{title}</h1>"
        f"<p>A new resource for {audience}.</p>"
        + (f'<p><a href="{live_url}">See it here</a></p>' if live_url else "")
        + "<p><small>Published on an owned channel. No tracking beyond the "
          "site's own analytics.</small></p>")


def _social_draft(opp: dict, channel: str, live_url: str) -> dict:
    title = str(opp.get("title") or "our new offer")
    platform = "reddit" if channel == "community_draft" else "x/linkedin"
    return {
        "channel": channel,
        "platform": platform,
        "title": f"Built {title} - would love feedback",
        "body": (f"I put together {title} for "
                 f"{opp.get('target_customer') or 'a specific niche'}. "
                 "Not selling hard - genuinely want to know if the framing "
                 "lands. Details / demo: (link)"),
        "url": live_url,
        "cta": "honest feedback welcome",
        "reason": "owned + community drafts are the free, rule-respecting way "
                  "to get the first eyes on a launch",
        "auto_post": False,
        "note": "DRAFT ONLY - a human reads the community's self-promotion "
                "rules and posts this (or not). Nothing is posted "
                "automatically.",
    }


class DistributeTaskAdapter(TaskAdapter):
    """Runs one DISTRIBUTE task for ONE channel.

      owned_web / owned_content -> publish an announcement page on the
        operator's own channel via a DistributionAdapter. A real
        published_url lets the worker move LIVE -> ACQUIRING_TRAFFIC.
      community_draft / social_draft -> build a ready-to-review DRAFT. It
        is handled entirely in-process (no adapter, no network) and NEVER
        auto-posted; it drives no state change.

    Idempotent: a confirmed distribution for the same (channel,
    content_hash) recorded on the opportunity short-circuits.
    """

    task_types = ("DISTRIBUTE",)
    name = "distribute"

    def __init__(self, adapter: DistributionAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> DistributionAdapter:
        return self._adapter if self._adapter is not None else default_distribution_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        inp = ctx.task.input or {}
        channel = str(inp.get("channel") or "owned_web")
        opp = ctx.opportunity
        oid = ctx.task.opportunity_id
        live_url = (opp.get("execution") or {}).get("live_url", "")

        if channel in DRAFT_CHANNELS:
            import hashlib as _hl
            draft = _social_draft(opp, channel, live_url)
            digest = _hl.sha256(
                repr(sorted(draft.items())).encode("utf-8")).hexdigest()[:16]
            return AdapterResult(ok=True, output={
                "success": True, "channel": channel, "draft_only": True,
                "published_url": "", "destination": f"draft:{channel}",
                "distribution_id": f"draft-{oid[:12]}-{channel}",
                "content_hash": digest, "draft": draft})

        content = {"html": _announce_html(opp, channel, live_url)}
        req = DistributionRequest(opportunity_id=oid, channel=channel,
                                  content=content, live_url=live_url,
                                  metadata={"title": opp.get("title", "")})
        chash = req.content_hash()

        prior = next(
            (d for d in (opp.get("execution") or {}).get("distributions", [])
             if d.get("channel") == channel and d.get("content_hash") == chash
             and d.get("status") == "success"), None)
        if prior:
            return AdapterResult(ok=True, output={**prior, "success": True,
                                                  "channel": channel,
                                                  "content_hash": chash,
                                                  "idempotent": True})

        result = self._adapter_for().distribute(req)
        out = {**result.to_dict(), "content_hash": chash}
        if not result.success:
            if result.blocked:
                # no owned channel configured: distribution is a NO-OP, not
                # an error. The DISTRIBUTE task completes (nothing published,
                # no state change) so the dependent CHECK_* tasks still run;
                # the operator wires a channel to actually promote the offer.
                return AdapterResult(ok=True, output={
                    **out, "distributed": False,
                    "note": "no owned distribution channel configured - "
                            "distribution skipped, nothing published"})
            return AdapterResult(
                ok=False, output=out, retryable=True,
                error=f"distribution failed: {result.error}")
        if not result.draft_only and not valid_live_url(result.published_url):
            return AdapterResult(
                ok=False, output=out, retryable=False,
                error="distribution reported success but returned no "
                      "published_url")
        return AdapterResult(ok=True, output=out)


class DeployTaskAdapter(TaskAdapter):
    """Publishes the built landing page through a DeploymentAdapter.

    The DEPLOY task is born BLOCKED_APPROVAL (acceptance.CHAIN) - a human
    must release it before the worker ever calls this. Here we only:
      * refuse if there is no built page to deploy
      * short-circuit if this opportunity already has a confirmed live_url
        for the same content (idempotency)
      * call the adapter and translate the DeploymentResult:
          blocked / transient failure -> retryable AdapterResult failure
          success without a valid URL  -> non-retryable failure
          success + valid URL          -> ok (worker then -> LIVE)
    """

    task_types = ("DEPLOY",)
    name = "deploy"

    def __init__(self, adapter: DeploymentAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> DeploymentAdapter:
        return self._adapter if self._adapter is not None else default_deployment_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        page = (ctx.dep_outputs.get("BUILD_PAGE")
                or ctx.dep_outputs.get("BUILD_PRODUCT") or {})
        html = str(page.get("landing_html") or "")
        if not html.strip():
            return AdapterResult(
                ok=False, retryable=False,
                error="no landing_html from BUILD_PAGE - nothing to deploy")

        oid = ctx.opportunity.get("id") or ctx.task.opportunity_id
        slug = slugify(oid)
        artifact = DeploymentArtifact(opportunity_id=ctx.task.opportunity_id,
                                      slug=slug, files={"index.html": html})

        prior = (ctx.opportunity.get("execution") or {}).get("deployment") or {}
        if (prior.get("success") and valid_live_url(prior.get("live_url", ""))
                and prior.get("content_hash") == artifact.content_hash()):
            return AdapterResult(ok=True, output={**prior, "idempotent": True})

        result = self._adapter_for().deploy(artifact)
        out = {**result.to_dict(), "slug": slug,
               "content_hash": artifact.content_hash()}

        if not result.success:
            return AdapterResult(
                ok=False, output=out,
                retryable=True,      # missing creds / transient - fixable, retry
                error=(f"deployment BLOCKED: {result.error}" if result.blocked
                       else f"deployment failed: {result.error}"))
        if not valid_live_url(result.live_url):
            return AdapterResult(
                ok=False, output=out, retryable=False,
                error="deployment reported success but returned no valid live_url")
        return AdapterResult(ok=True, output=out)


class CheckRevenueAdapter(TaskAdapter):
    """Turns the CHECK_REVENUE task into the real incoming-payment path:

      poll the payment provider  (PaymentAdapter)
        -> process each confirmed event  (process_payment_event, idempotent)
          -> revenue.record_opportunity_payment  (the shared RevenueLedger)

    The worker then emits PAYMENT_DETECTED / REVENUE_RECORDED for the rows
    that were NEWLY booked this run, and transitions the opportunity to
    FIRST_SALE if this run booked its first-ever revenue.

    Books INCOMING revenue only - no capture, no transfer, no spend.
    """

    task_types = ("CHECK_REVENUE",)
    name = "check-revenue"

    def __init__(self, payment_adapter: PaymentAdapter | None = None) -> None:
        self._pa = payment_adapter

    def _pa_for(self) -> PaymentAdapter:
        return self._pa if self._pa is not None else default_payment_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        from .revenue import RevenueLedger

        oid = ctx.task.opportunity_id
        poll = self._pa_for().poll(opportunity_id=oid)
        if not poll.ok:
            return AdapterResult(
                ok=False,
                retryable=not poll.blocked,   # "no provider" won't self-heal
                error=(f"payment check BLOCKED: {poll.error}" if poll.blocked
                       else f"payment provider error: {poll.error}"),
                output={"provider": poll.provider, "blocked": poll.blocked})

        ledger_path = ctx.data_dir / "revenue.json"
        total_before = RevenueLedger.load(ledger_path).total_for(oid)

        payments: list[dict] = []
        newly_booked: list[dict] = []
        rejected: list[dict] = []
        for ev in poll.events:
            ledger = RevenueLedger.load(ledger_path)      # reload: prior loop booked
            r = process_payment_event(ledger, ev, actor="check-revenue")
            if not r.success:
                rejected.append({"reference": ev.reference, "reason": r.error})
                continue
            row = {"reference": r.reference, "amount": r.amount,
                   "currency": r.currency, "provider": r.provider,
                   "ledger_ref": r.payment_id, "customer_ref": r.customer_ref,
                   "already_booked": r.already_booked}
            payments.append(row)
            if not r.already_booked:
                newly_booked.append(row)

        total_after = RevenueLedger.load(ledger_path).total_for(oid)
        first_sale = total_before <= 0.0 and total_after > 0.0
        cycle = int((ctx.task.input or {}).get("cycle", 0))

        return AdapterResult(ok=True, output={
            "provider": poll.provider,
            "payments": payments,
            "newly_booked": newly_booked,
            "rejected": rejected,
            "newly_booked_eur": round(sum(r["amount"] for r in newly_booked), 2),
            "opportunity_total_eur": round(total_after, 2),
            "first_sale": bool(first_sale),
            # Phase 10: CHECK_REVENUE is also a recurring measurement
            "kind": "revenue", "cycle": cycle,
            "metrics": {"revenue_eur": round(total_after, 2),
                        "payments": len(payments),
                        "newly_booked_eur": round(
                            sum(r["amount"] for r in newly_booked), 2)},
        })


class _MeasurementCheckAdapter(TaskAdapter):
    """Shared body for CHECK_TRAFFIC / CHECK_LEADS: poll an analytics
    provider, coerce the metrics, hand them to the worker. The worker
    persists the time series, emits MEASUREMENT_RECORDED, and drives the
    LIVE -> MEASURING -> FIRST_VISITOR / FIRST_LEAD transitions."""

    _kind = ""
    _keys: tuple[str, ...] = ()

    def __init__(self, adapter: MeasurementAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> MeasurementAdapter:
        return self._adapter if self._adapter is not None else default_measurement_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        live_url = (ctx.opportunity.get("execution") or {}).get("live_url", "")
        snap = self._adapter_for().measure(
            kind=self._kind, opportunity_id=ctx.task.opportunity_id,
            live_url=live_url)
        if not snap.ok:
            return AdapterResult(
                ok=False, retryable=not snap.blocked,
                error=(f"{self._kind} check BLOCKED: {snap.error}" if snap.blocked
                       else f"{self._kind} provider error: {snap.error}"),
                output={"provider": snap.provider, "blocked": snap.blocked})
        raw = snap.metrics if isinstance(snap.metrics, dict) else {}
        metrics = {k: _num(raw.get(k)) for k in self._keys}
        return AdapterResult(ok=True, output={
            "kind": self._kind, "provider": snap.provider, "metrics": metrics,
            "cycle": int((ctx.task.input or {}).get("cycle", 0))})


class CheckTrafficAdapter(_MeasurementCheckAdapter):
    task_types = ("CHECK_TRAFFIC",)
    name = "check-traffic"
    _kind = "traffic"
    _keys = ("visitors", "clicks", "impressions")


class CheckLeadsAdapter(_MeasurementCheckAdapter):
    task_types = ("CHECK_LEADS",)
    name = "check-leads"
    _kind = "leads"
    _keys = ("leads", "signups")


class DeliverTaskAdapter(TaskAdapter):
    """Delivers the purchased digital product for ONE confirmed payment.

    A DELIVER task is spawned by the worker on each newly-booked payment
    (idempotency_key = opp + provider ref), so there is exactly one per
    payment. Here we additionally:
      * refuse if the payment carried no customer reference
      * short-circuit (idempotent) if this payment_ref already has a
        confirmed delivery recorded on the opportunity (survives restart)
      * call the DeliveryAdapter and translate the DeliveryResult
    """

    task_types = ("DELIVER",)
    name = "deliver"

    def __init__(self, adapter: DeliveryAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> DeliveryAdapter:
        return self._adapter if self._adapter is not None else default_delivery_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        inp = ctx.task.input or {}
        pref = str(inp.get("payment_ref", ""))
        if not pref:
            return AdapterResult(ok=False, retryable=False,
                                 error="DELIVER task has no payment_ref")

        prior = ((ctx.opportunity.get("execution") or {}).get("deliveries")
                 or {}).get(pref)
        if prior and prior.get("success"):
            return AdapterResult(ok=True, output={**prior, "payment_ref": pref,
                                                  "idempotent": True})

        customer_ref = str(inp.get("customer_ref", ""))
        if not customer_ref:
            return AdapterResult(
                ok=False, retryable=False,
                error="the payment carried no customer reference - a digital "
                      "product cannot be delivered")

        opp = ctx.opportunity
        live_url = (opp.get("execution") or {}).get("live_url", "")
        artifact = DeliveryArtifact(
            opportunity_id=ctx.task.opportunity_id,
            product_name=opp.get("title", "your purchase"),
            live_url=live_url,
            body=(f"Thank you for your purchase of \"{opp.get('title', '')}\".\n\n"
                  + (f"Access it here: {live_url}\n" if live_url else "")))
        recipient = DeliveryRecipient(reference=customer_ref,
                                      opportunity_id=ctx.task.opportunity_id)

        result = self._adapter_for().deliver(artifact, recipient)
        out = {**result.to_dict(), "payment_ref": pref}
        if not result.success:
            return AdapterResult(
                ok=False, output=out,
                retryable=not result.blocked,   # "no provider" won't self-heal
                error=(f"delivery BLOCKED: {result.error}" if result.blocked
                       else f"delivery failed: {result.error}"))
        return AdapterResult(ok=True, output=out)


class ScaleTaskAdapter(TaskAdapter):
    """Runs one SCALE task: take a Phase-14 optimization variant that the
    promotion policy judged to have enough evidence, and execute SAFE
    INTERNAL scaling steps via a ScalingAdapter.

      * short-circuit (idempotent) if this variant already has a confirmed
        scaling recorded on the opportunity
      * blocked (no provider) -> non-retryable failure
      * requires_approval -> non-retryable failure carrying the approval
        type; the worker blocks the task on that approval, it is NOT executed
      * success -> ok; the worker records the scaling + SCALE_COMPLETED
    """

    task_types = ("SCALE",)
    name = "scale"

    def __init__(self, adapter: ScalingAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> ScalingAdapter:
        return self._adapter if self._adapter is not None else default_scaling_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        inp = ctx.task.input or {}
        vid = str(inp.get("variant_id", ""))
        if not vid:
            return AdapterResult(ok=False, retryable=False,
                                 error="SCALE task has no variant_id")

        ex = ctx.opportunity.get("execution") or {}
        prior = next((s for s in ex.get("scalings", [])
                      if s.get("variant_id") == vid
                      and s.get("status") == "success"), None)
        if prior:
            return AdapterResult(ok=True, output={**prior, "success": True,
                                                  "idempotent": True})

        variant = next((o for o in ex.get("optimizations", [])
                        if o.get("variant_id") == vid), None)
        if variant is None:
            return AdapterResult(ok=False, retryable=False,
                                 error=f"optimization variant {vid!r} not found")

        req = ScalingRequest(opportunity_id=ctx.task.opportunity_id,
                             variant_id=vid, variant=dict(variant),
                             evidence=dict(inp.get("evidence") or {}),
                             focus=str(variant.get("focus", "")))
        result = self._adapter_for().scale(req)
        out = {**result.to_dict(), "variant_id": vid}
        if result.requires_approval:
            return AdapterResult(
                ok=False, retryable=False, output=out,
                error=f"scaling requires a {result.requires_approval} approval "
                      "- not executed")
        if not result.success:
            return AdapterResult(
                ok=False, output=out, retryable=not result.blocked,
                error=(f"scaling BLOCKED: {result.error}" if result.blocked
                       else f"scaling failed: {result.error}"))
        if not result.scale_id:
            return AdapterResult(ok=False, retryable=False, output=out,
                                 error="scaling produced no scale_id")
        return AdapterResult(ok=True, output=out)


class OptimizeAdapter(TaskAdapter):
    """Runs one OPTIMIZE task: turn the measurement signal into a SAFE
    INTERNAL variant DRAFT (copy / CTA / pricing hypothesis / variant idea)
    via an OptimizationAdapter. The draft is recorded on the opportunity by
    the worker - it is never built, deployed, or promoted here."""

    task_types = ("OPTIMIZE",)
    name = "optimize"

    def __init__(self, adapter: OptimizationAdapter | None = None) -> None:
        self._adapter = adapter

    def _adapter_for(self) -> OptimizationAdapter:
        return self._adapter if self._adapter is not None else default_optimization_adapter()

    def run(self, ctx: AdapterContext) -> AdapterResult:
        inp = ctx.task.input or {}
        req = OptimizationRequest(
            opportunity_id=ctx.task.opportunity_id,
            focus=str(inp.get("focus") or "landing_copy"),
            signal=dict(inp.get("signal") or {}),
            opportunity=dict(ctx.opportunity),
            variant_number=int(inp.get("variant_number", 1)))
        result = self._adapter_for().optimize(req)
        out = {**result.to_dict(),
               "variant_number": req.variant_number,
               "reason": str(inp.get("reason", ""))}
        if not result.success:
            return AdapterResult(
                ok=False, output=out, retryable=not result.blocked,
                error=(f"optimization BLOCKED: {result.error}" if result.blocked
                       else f"optimization failed: {result.error}"))
        if not result.variant_id:
            return AdapterResult(ok=False, output=out, retryable=False,
                                 error="optimization produced no variant_id")
        return AdapterResult(ok=True, output=out)


# ---------------------------------------------------------------------------
# the default registry
# ---------------------------------------------------------------------------

def default_registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(AgentTaskAdapter(("SCORE",), "select", _p_select,
                                  objective="score"))
    reg.register(AgentTaskAdapter(("RESEARCH",), "research_distribution",
                                  _p_distribution, objective="research"))
    reg.register(PlanAdapter())
    reg.register(AgentTaskAdapter(
        ("BUILD_PRODUCT", "BUILD_PAGE", "CREATE_CONTENT"),
        "package_deliverable", _p_package, objective="build"))
    reg.register(AgentTaskAdapter(
        ("VALIDATE_PRODUCT", "VALIDATE_PAGE"),
        "quality_check", _p_qc, objective="validate"))
    reg.register(DeployTaskAdapter())
    reg.register(DistributeTaskAdapter())
    reg.register(CheckTrafficAdapter())
    reg.register(CheckLeadsAdapter())
    reg.register(CheckRevenueAdapter())
    reg.register(DeliverTaskAdapter())
    reg.register(AnalyzeAdapter())
    reg.register(OptimizeAdapter())
    reg.register(ScaleTaskAdapter())
    return reg
