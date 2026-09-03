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
from .model import OpportunityDraft, SourceMeta
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


def select(data_dir, oid: str, *, priority_weights: dict | None = None,
           weights: dict | None = None) -> dict:
    """Score every viable strategy and record the recommendation."""
    store, rec = _load(data_dir, oid)
    draft = draft_from_record(rec)
    prof = _evaluate_profitability(draft, weights=weights)
    selection = select_strategy(draft, prof, priority_weights=priority_weights)
    payload = {**selection.to_dict(), "selected_at": now_iso()}
    store.record_strategy(oid, payload)
    # keep evaluation fresh alongside the selection it was based on
    store.record_evaluation(oid, {**prof.to_dict(), "evaluated_at": now_iso(),
                                  "opportunity_type": draft.opportunity_type})
    store.save()
    return payload


_PREPARED_STRATEGIES = {
    model.STRAT_TASK: ("prepare the deliverable/answer for this task, then a "
                       "human submits it on the source platform"),
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

    # prepared plan for the non-product strategies (spec 11/13/14 are later)
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
