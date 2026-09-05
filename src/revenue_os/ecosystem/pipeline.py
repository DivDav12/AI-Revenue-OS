"""Ecosystem pipeline: evaluate a stored opportunity, pick a strategy, and
turn a QUALIFIED one into an executable task chain (spec sections 9-12, 42).

    evaluate(data_dir, oid)        -> writes the `evaluation` namespace
    select(data_dir, oid)          -> writes the `strategy` namespace
    plan(data_dir, oid)            -> for the selected strategy, produce the
                                      real ExecutionTask chain

Strategy -> chain:
  PRODUCT  -> the existing, proven `acceptance.accept_opportunity()` chain
              (PLAN -> BUILD_PRODUCT -> BUILD_PAGE -> VALIDATE -> DEPLOY(gate)
               -> DISTRIBUTE -> CHECK_*). The full real payment/checkout/
              delivery stack is reused unchanged.
  TASK / AFFILIATE / ECOMMERCE / SERVICE / OTHER
           -> a *prepared plan* is recorded on the opportunity and the
              opportunity is marked HUMAN_REQUIRED for the external step
              (submit the task / apply to an affiliate program / open a
              store). The fleet does all preparable work; it never creates
              an account, spends money, or submits on a platform that
              forbids automation. Full autonomous chains for these
              strategies are later phases (spec sections 11, 13, 14).

Nothing here moves money or performs a protected action - `acceptance`
already routes DEPLOY through the money-approval gate.
"""

from __future__ import annotations

from pathlib import Path

from ..opportunity_store import load_opportunities
from ..store import now_iso
from . import model, verification
from .model import OpportunityDraft, SourceMeta, estimate_value
from .profitability import evaluate as _evaluate_profitability
from .strategy import select_strategy


class EcosystemError(ValueError):
    """A pipeline step could not run (unknown opportunity, wrong state, ...)."""


def draft_from_record(rec: dict) -> OpportunityDraft:
    """Rebuild the normalized draft from a persisted opportunity record so
    the pure engines (verify / evaluate / strategy) can run against it."""
    d = rec.get("discovery") or {}
    meta = SourceMeta(
        source=d.get("source", "") or rec.get("source", "unknown"),
        source_type=d.get("source_type", "unknown"),
        source_url=d.get("source_url", ""),
        access_method=d.get("access_method") or (
            model.ACCESS_SYNTHETIC if rec.get("origin") != "real"
            else model.ACCESS_PUBLIC_WEB),
        automation_allowed=bool(d.get("automation_allowed", False)),
        requires_login=bool(d.get("requires_login", False)),
        requires_human=bool(d.get("requires_human", False)),
        policy_status=d.get("policy_status", model.POLICY_OK),
    )
    otype = d.get("opportunity_type") or _guess_type(rec.get("category", "other"))
    pe = d.get("payment_evidence") or {}
    se = d.get("submission_evidence") or {}
    return OpportunityDraft(
        title=rec.get("title", ""),
        description=rec.get("required_work", ""),
        opportunity_type=otype,
        evidence=list(d.get("evidence") or []) or [rec.get("title", "")],
        source_meta=meta,
        source_id=d.get("source_id", ""),
        source_url=d.get("source_url", ""),
        discovered_at=d.get("discovered_at", ""),
        est_pay_eur=float(d.get("est_pay_eur", rec.get("est_revenue_eur", 0.0)) or 0.0),
        est_time_minutes=float(d.get("est_time_minutes", 0.0) or 0.0),
        demand_hint=float(d.get("demand_hint", rec.get("probability", 0.0)) or 0.0),
        category=rec.get("category", "other"),
        raw={"target_customer": rec.get("target_customer", "")},
        payment_evidence=model.PaymentEvidence(
            amount=float(pe.get("amount", 0.0) or 0.0),
            currency=str(pe.get("currency", "")),
            conditions=str(pe.get("conditions") or model.PAY_UNCLEAR),
            is_estimate=bool(pe.get("is_estimate", True)),
            evidence=tuple(pe.get("evidence") or ())) if pe else model.PaymentEvidence(),
        submission_evidence=model.SubmissionEvidence(
            submission_url=str(se.get("submission_url", "")),
            submission_method=str(se.get("submission_method") or model.SUBMIT_UNKNOWN),
            requires_login=bool(se.get("requires_login", False)),
            requires_captcha=bool(se.get("requires_captcha", False)),
            requires_identity=bool(se.get("requires_identity", False)),
            has_api_submission=bool(se.get("has_api_submission", False)),
            deadline=str(se.get("deadline", "")),
            required_deliverable=str(se.get("required_deliverable", ""))
        ) if se else model.SubmissionEvidence(),
    )


def _guess_type(category: str) -> str:
    from .sources import _type_for_category
    return _type_for_category(category)


def _load(data_dir, oid: str):
    store = load_opportunities(data_dir)
    rec = store.get(oid)
    if rec is None:
        raise EcosystemError(f"unknown opportunity {oid!r}")
    return store, rec


def evaluate(data_dir, oid: str, *, weights: dict | None = None) -> dict:
    """Run + persist the deterministic profitability projection."""
    store, rec = _load(data_dir, oid)
    draft = draft_from_record(rec)
    prof = _evaluate_profitability(draft, weights=weights)
    payload = {**prof.to_dict(), "evaluated_at": now_iso(),
               "opportunity_type": draft.opportunity_type}
    store.record_evaluation(oid, payload)
    store.save()
    return payload


def _prefer_affiliate_if_matched(data_dir, draft: OpportunityDraft, selection):
    """spec section 2: the generic `strategy.py` heuristic has no
    visibility into real, human-joined affiliate offer economics - it
    scores TYPE_AFFILIATE opportunities on the same capital/speed/
    automation profile as everything else, which often makes PRODUCT win
    even when a genuinely profitable affiliate match already exists. If a
    real, USABLE offer matches this demand AND its dedicated affiliate
    profitability projection (affiliate_profitability.py, not the generic
    per-type table) is positive, that concrete path overrides the generic
    pick - additive, and scoped strictly to TYPE_AFFILIATE drafts, so
    every other opportunity type's selection is completely untouched."""
    if draft.opportunity_type != model.TYPE_AFFILIATE or not selection.recommended:
        return selection
    if selection.recommended == model.STRAT_AFFILIATE:
        return selection
    from .affiliate_matching import best_usable_match
    from .affiliate_model import AffiliateOfferStore
    from .affiliate_profitability import evaluate as eval_affiliate

    offers = AffiliateOfferStore.load(data_dir).all()
    match = best_usable_match(draft, offers)
    if match is None:
        return selection
    aff_profit = estimate_value(eval_affiliate(match).expected_profit)
    if aff_profit <= 0:
        return selection
    previous = selection.recommended
    selection.recommended = model.STRAT_AFFILIATE
    selection.reason = (
        f"overridden: a real, already-joined affiliate offer "
        f"({match.offer.program_name!r}) matches this demand with positive "
        f"projected economics (EUR {aff_profit:.2f} expected profit) - "
        f"preferred over the generic strategy heuristic's {previous!r} pick")
    return selection


def select(data_dir, oid: str, *, priority_weights: dict | None = None,
           weights: dict | None = None) -> dict:
    """Score every viable strategy and record the recommendation."""
    store, rec = _load(data_dir, oid)
    draft = draft_from_record(rec)
    prof = _evaluate_profitability(draft, weights=weights)
    selection = select_strategy(draft, prof, priority_weights=priority_weights)
    selection = _prefer_affiliate_if_matched(data_dir, draft, selection)
    payload = {**selection.to_dict(), "selected_at": now_iso()}
    store.record_strategy(oid, payload)
    # keep evaluation fresh alongside the selection it was based on
    store.record_evaluation(oid, {**prof.to_dict(), "evaluated_at": now_iso(),
                                  "opportunity_type": draft.opportunity_type})
    store.save()
    return payload


_TASK_CHAIN: tuple[str, ...] = ("PLAN_TASK", "EXECUTE_TASK", "VERIFY_RESULT")


def _plan_task_chain(data_dir, oid: str, rec: dict, *, actor: str) -> dict:
    """TASK strategy (spec 11): a real, persistent ExecutionTask chain that
    prepares the deliverable end to end (PLAN_TASK -> EXECUTE_TASK ->
    VERIFY_RESULT, all SAFE_AUTONOMOUS - task_class.py). Idempotent - a
    second call reuses the existing tasks. Mirrors
    acceptance.accept_opportunity's shape (SELECTED + execution.accepted) so
    execution_view()/pending_actions() surface it for free. Unlike the
    PRODUCT chain there is no DEPLOY/DISTRIBUTE/CHECK_* - submitting the
    finished deliverable on the source platform, and recording what actually
    happened, stay explicit human actions (see acceptance.pending_actions'
    SUBMIT_TASK row and record_task_outcome() below)."""
    from ..acceptance import _ensure_selected
    from ..events import load_events
    from ..execution import load_tasks

    store = load_opportunities(data_dir)
    moves = _ensure_selected(store, oid, actor)

    q = load_tasks(data_dir)
    ev = load_events(data_dir)
    ids: dict[str, str] = {}
    created: list[str] = []
    reused: list[str] = []
    existing_ids = {t.task_id for t in q.all()}
    prev = None
    for ttype in _TASK_CHAIN:
        deps = [ids[prev]] if prev else []
        t = q.create(oid, ttype, depends_on=deps, priority=5,
                     idempotency_key=f"plan_task:{oid}:{ttype}",
                     input={"title": rec.get("title", ""), "accepted_by": actor})
        ids[ttype] = t.task_id
        if t.task_id in existing_ids:
            reused.append(ttype)
        else:
            created.append(ttype)
            ev.emit("TASK_CREATED", task_id=t.task_id, opportunity_id=oid,
                    task_type=ttype, actor=actor, depends_on=list(deps),
                    priority=5)
        prev = ttype

    res = q.resolve_dependencies()
    q.save()
    for a, b in moves:
        ev.emit("OPPORTUNITY_TRANSITIONED", opportunity_id=oid, actor=actor,
                **{"from": a, "to": b, "reason": "accepted for execution"})
    for tid in res.get("promoted", []):
        tt = q.get(tid)
        ev.emit("TASK_READY", task_id=tid, opportunity_id=oid,
                task_type=tt.task_type, actor=actor)
    ev.save()

    store.mark_accepted(oid, by=actor, task_ids=list(ids.values()))
    store.save()

    return {"kind": "task_chain", "engine": "ecosystem_task",
            "chain": list(_TASK_CHAIN), "created": created, "reused": reused,
            "planned_at": now_iso()}


_PREPARED_STRATEGIES = {
    model.STRAT_TASK: ("prepare the deliverable/answer for this task, then a "
                       "human submits it on the source platform (the evidence "
                       "did not confirm an autonomous-candidate task kind - "
                       "see discovery.verification.checks.task_kind)"),
    model.STRAT_AFFILIATE: ("prepare comparison/how-to assets; a human joins the "
                            "affiliate program (HUMAN_SETUP_REQUIRED) before any link goes live"),
    model.STRAT_ECOMMERCE: ("prepare listings + margin analysis; a human opens the "
                            "store account and funds inventory (money + KYC gate)"),
    model.STRAT_SERVICE: ("prepare the service offer + outreach draft; a human "
                          "sends outreach / closes the client"),
    model.STRAT_OTHER: ("prepare assets; next external step needs a human decision"),
}


def plan(data_dir, oid: str, *, actor: str = "ecosystem") -> dict:
    """Turn a QUALIFIED opportunity + its selected strategy into an
    executable plan."""
    store, rec = _load(data_dir, oid)

    strat_ns = rec.get("strategy") or {}
    recommended = strat_ns.get("recommended", "")
    if not recommended:
        raise EcosystemError(
            f"{oid}: no strategy selected yet - run select() first")

    vstatus = ((rec.get("discovery") or {}).get("verification") or {}).get("status")
    if vstatus not in model.PLANNABLE:
        raise EcosystemError(
            f"{oid}: verification status is {vstatus!r}, not QUALIFIED - "
            "cannot plan a real chain")

    if recommended == model.STRAT_PRODUCT:
        from ..acceptance import accept_opportunity
        result = accept_opportunity(data_dir, oid, actor=actor)
        store2 = load_opportunities(data_dir)
        s = store2.get(oid).get("strategy") or {}
        s["plan"] = {"kind": "task_chain", "engine": "acceptance",
                     "chain": [c["task_type"] for c in result.get("chain", [])],
                     "planned_at": now_iso()}
        store2.record_strategy(oid, s)
        store2.save()
        return {"opportunity_id": oid, "strategy": recommended,
                "kind": "task_chain", "acceptance": result}

    if recommended == model.STRAT_TASK:
        # HARD GATE (discovery quality layer): a high TaskQualityScore never
        # substitutes for this. Only an evidence-classified autonomous task
        # kind gets the real chain; JOB/SERVICE_LEAD can never even reach
        # here (verification.py already routes them to HUMAN_REQUIRED, so
        # plan() would have raised above) - this catches OTHER/unclassified
        # (e.g. a legacy record verified before this layer existed).
        checks = (((rec.get("discovery") or {}).get("verification") or {})
                  .get("checks") or {})
        task_kind = checks.get("task_kind", "")
        if task_kind in model.AUTONOMOUS_TASK_KINDS:
            result = _plan_task_chain(data_dir, oid, rec, actor=actor)
            store2 = load_opportunities(data_dir)
            s = store2.get(oid).get("strategy") or {}
            s["plan"] = result
            store2.record_strategy(oid, s)
            store2.save()
            return {"opportunity_id": oid, "strategy": recommended,
                    "kind": "task_chain", "plan": result}
        # falls through to the prepared/human-gated path below

    if recommended == model.STRAT_AFFILIATE:
        # Affiliate Revenue Pipeline: a real MATCH->EVALUATE->BUILD ASSET->
        # CREATE LINK->DEPLOY->DISTRIBUTE chain, synchronous (template
        # asset generation, no LLM/worker step needed) - see
        # affiliate_pipeline.run_affiliate_chain for the fail-closed
        # per-step HUMAN_REQUIRED gates (no usable offer / quality gate /
        # deploy credentials).
        from .affiliate_pipeline import run_affiliate_chain
        result = run_affiliate_chain(data_dir, opportunity_id=oid,
                                     draft=draft_from_record(rec), now_iso=now_iso())
        strat_ns["plan"] = result
        store.record_strategy(oid, strat_ns)
        store.add_experiment(
            oid, "strategy_plan",
            f"AFFILIATE: {result['status']}"
            + (f" ({result.get('step', '')}: {result.get('reason', '')})"
               if result["status"] != "completed" else
               f" - asset live at {result.get('asset_live_url', '')}"))
        store.save()
        return {"opportunity_id": oid, "strategy": recommended,
                "kind": result["kind"], "plan": result,
                "next_step_class": result["next_step_class"]}

    # prepared plan for the remaining strategies (spec 13/14 are later)
    note = _PREPARED_STRATEGIES.get(recommended, _PREPARED_STRATEGIES[model.STRAT_OTHER])
    plan_payload = {"kind": "prepared", "recommended": recommended,
                    "note": note, "planned_at": now_iso(),
                    "autonomous_chain_available": False,
                    "next_step_class": "HUMAN_REQUIRED"}
    strat_ns["plan"] = plan_payload
    store.record_strategy(oid, strat_ns)
    store.add_experiment(oid, "strategy_plan",
                         f"{recommended}: prepared plan, external step needs a human")
    store.save()
    return {"opportunity_id": oid, "strategy": recommended,
            "kind": "prepared", "note": note,
            "next_step_class": "HUMAN_REQUIRED"}


def record_task_outcome(data_dir, oid: str, *, success: bool, amount: float = 0.0,
                        currency: str = "EUR", ref: str = "", note: str = "",
                        actor: str = "human") -> dict:
    """Human, out-of-band confirmation of a TASK-strategy real-world outcome
    (spec 11): a human took the VERIFY_RESULT-approved deliverable, submitted
    it themselves on the source platform (the fleet never does that - see
    action_class's platform-posting rules / acceptance.pending_actions'
    SUBMIT_TASK row), and is now recording what actually happened. A plain,
    synchronous, human-triggered call - never used by the worker, never
    inside autonomous_context() - exactly like acceptance.deliver_now() for
    the PRODUCT chain.

    success=True books the REAL, already-received payment into the SAME
    ledger CHECK_REVENUE uses (revenue.record_opportunity_payment) -
    idempotent by `ref` - then moves the opportunity
    VALIDATING -> FIRST_SALE -> ACTIVE (the task's "delivery" already
    happened when the human submitted it; there is no separate DELIVER
    step, unlike the PRODUCT chain). success requires a positive `amount`
    and a stable `ref` - this records a CONFIRMED payment, not a hopeful
    outcome. success=False records a settled loss (rejected / unpaid /
    never submitted) and moves nothing.

    Every outcome - win or loss - is fed to the learning loop
    (ecosystem.learning.record_outcome) so future DISCOVER / SELECT_STRATEGY
    runs weight TASK opportunities by what actually happened, not a guess.
    Fails closed on: unknown opportunity, no VERIFIED deliverable ever
    produced, or an invalid success/amount/ref combination - never
    fabricates a payment or an outcome.
    """
    from .. import opportunity_state as ostate
    from ..events import load_events
    from ..execution import load_tasks
    from ..revenue import RevenueLedger, record_opportunity_payment
    from . import learning

    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    rec = store.get(oid)
    if rec is None:
        raise EcosystemError(f"unknown opportunity {oid!r}")

    q = load_tasks(data_dir)
    verify_tasks = [t for t in q.by_opportunity(oid) if t.task_type == "VERIFY_RESULT"]
    if not any(t.status == "SUCCEEDED" for t in verify_tasks):
        raise EcosystemError(
            f"{oid}: no successful VERIFY_RESULT - no verified deliverable was "
            "ever produced for this task, refusing to record an outcome")

    if success:
        if amount <= 0:
            raise EcosystemError(
                "success=True requires a positive amount - this records a "
                "CONFIRMED real payment, not a hopeful outcome")
        if not ref:
            raise EcosystemError(
                "a stable payment reference (--ref) is required to record a "
                "real payment idempotently")

    ledger_path = data_dir / "revenue.json"
    booked_outcome = None
    if success:
        ledger = RevenueLedger.load(ledger_path)
        if ledger.has_ref(ref):
            return {"opportunity_id": oid, "outcome": "already_recorded",
                    "ref": ref, "state": rec.get("state")}
        booked = record_opportunity_payment(
            ledger, opportunity_id=oid, amount=amount, ref=ref, currency=currency,
            note=note or "task payment (human-confirmed)", actor=actor)
        booked_outcome = booked["outcome"]

        ev = load_events(data_dir)
        ev.emit("PAYMENT_DETECTED", opportunity_id=oid, actor=actor,
                reference=ref, amount=amount, currency=currency, provider="manual",
                customer_ref="")
        ev.emit("REVENUE_RECORDED", opportunity_id=oid, actor=actor,
                reference=ref, ledger_ref=ref, amount=amount, currency=currency,
                opportunity_total_eur=booked.get("opportunity_total", 0))
        for tgt, why in (("FIRST_SALE", "task payment confirmed"),
                         ("ACTIVE", "the task was fulfilled at submission - "
                                    "no separate delivery step")):
            cur = (store.get(oid) or {}).get("state") or ostate.INITIAL
            if ostate.can_transition(cur, tgt):
                tr = store.transition(oid, tgt, reason=f"record_task_outcome: {why}",
                                      source="human", actor=actor)
                ev.emit("OPPORTUNITY_TRANSITIONED", opportunity_id=oid, actor=actor,
                        **{"from": tr["previous_state"], "to": tr["next_state"],
                           "reason": tr["reason"]})
        store.save()
        ev.save()
    else:
        ev = load_events(data_dir)
        ev.emit("TASK_OUTCOME_RECORDED", opportunity_id=oid, actor=actor,
                success=False, amount=0.0, currency=currency, ref=ref, note=note)
        ev.save()

    store.add_experiment(
        oid, "task_outcome",
        (f"paid: EUR {amount:.2f}" if success else
         f"not paid/accepted: {note or 'no reason given'}"),
        result="success" if success else "failure")
    store.save()

    d = rec.get("discovery") or {}
    outcome = learning.Outcome(
        opportunity_id=oid,
        strategy=str((rec.get("strategy") or {}).get("recommended") or model.STRAT_TASK),
        source=str(d.get("source", "")),
        category=str(rec.get("category", "other")),
        opportunity_type=str(d.get("opportunity_type") or model.TYPE_TASK),
        execution_time_hours=round(float(d.get("est_time_minutes", 0.0) or 0.0) / 60.0, 3),
        cost_eur=0.0,
        revenue_eur=float(amount) if success else 0.0,
        success=bool(success),
        failure_reason="" if success else (note or "not accepted / not paid"),
        settled=True)
    learning.record_outcome(data_dir, outcome)

    return {"opportunity_id": oid, "outcome": "success" if success else "failure",
            "amount": float(amount) if success else 0.0, "ref": ref,
            "state": store.get(oid).get("state"), "booked": booked_outcome}
