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
from .payments import PaymentAdapter, default_payment_adapter, process_payment_event
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

        return AdapterResult(ok=True, output={
            "provider": poll.provider,
            "payments": payments,
            "newly_booked": newly_booked,
            "rejected": rejected,
            "newly_booked_eur": round(sum(r["amount"] for r in newly_booked), 2),
            "opportunity_total_eur": round(total_after, 2),
            "first_sale": bool(first_sale),
        })


class OptimizeAdapter(TaskAdapter):
    task_types = ("OPTIMIZE",)
    name = "optimize"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        r = dict(ctx.opportunity.get("results", {}) or {})
        rev = float(r.get("revenue_eur", 0) or 0)
        hyp = ("has revenue - test a price increase and add a second landing "
               "variant" if rev > 0 else
               "no revenue yet - rewrite the headline around the sharpest pain "
               "point and re-measure")
        return AdapterResult(ok=True, output={"hypothesis": hyp,
                                              "based_on": r})


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
    reg.register(CheckRevenueAdapter())
    reg.register(AnalyzeAdapter())
    reg.register(OptimizeAdapter())
    return reg
