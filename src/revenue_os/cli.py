"""Command-line interface.

Read commands:
  run              discovery cycle against a source, then print the report
                   (--evaluator llm opt-in; keyword heuristic by default)
  report           print the report only (no discovery)
  llm-costs        print recorded AI operating spend
  outcomes         retrospective on validated vs rejected candidates
  dashboard        write a static HTML pipeline snapshot (no discovery)
  candidate NAME   print one candidate's full state
  demo             full end-to-end walkthrough in a throwaway directory

Human decision commands (operate on the persistent --data-dir store):
  approve NAME / reject NAME
  investigate       (--planner llm opt-in; template by default)
  outcome NAME {validated|rejected} --metric TEXT
  prepare-launch    (--proposer llm opt-in; template by default)
  launch NAME
  payment NAME AMOUNT

Cost-control commands (authorize/record only; never move money):
  budget NAME AMOUNT
  authorize-spend NAME AMOUNT --purpose TEXT [--ceiling N]
  deny-spend NAME AMOUNT --purpose TEXT --reason TEXT
  record-spend NAME AMOUNT
  llm-budget [AMOUNT]   show or raise the cumulative AI spend cap
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .approval import record_decision
from .dashboard import render_html
from .discovery_log import DiscoveryLog
from .llm_budget import LlmBudget
from .llm_spend import LlmSpendLog, entry_from
from .filtering import is_relevant
from .report import pipeline_report, render_candidate, render_text
from .revenue import RevenueLedger, mark_launched, record_payment
from .sources import FilteredSource, build_source
from .spend import (
    DEFAULT_CEILING,
    SpendLedger,
    SpendRequest,
    authorize_spend,
    deny_spend,
    record_spend,
    set_budget,
)
from .store import CandidateStore, now_iso
from .validation import record_validation_outcome
from .workflow import investigate_approved, prepare_launch, run_discovery_cycle

_DEFAULT_DATA_DIR = "data"


def _data_dir(args) -> Path:
    explicit = getattr(args, "data_dir", None)
    return Path(explicit or os.environ.get("REVENUE_OS_DATA_DIR", _DEFAULT_DATA_DIR))


def _load(data_dir: Path):
    return (
        CandidateStore.load(data_dir / "candidates.json"),
        RevenueLedger.load(data_dir / "revenue.json"),
        SpendLedger.load(data_dir / "spend.json"),
    )


def _require(store: CandidateStore, name: str):
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    return candidate


# --- read commands ---------------------------------------------------------


def _discovery_log(data_dir: Path) -> DiscoveryLog:
    return DiscoveryLog.load(data_dir / "discovery_runs.json")


def _llm_spend_log(data_dir: Path) -> LlmSpendLog:
    return LlmSpendLog.load(data_dir / "llm_spend.json")


def _record_llm_spend(data_dir: Path, activity: str, worker) -> None:
    log = _llm_spend_log(data_dir)
    log.add(entry_from(activity, worker))
    log.save()


def _llm_budget(data_dir: Path) -> LlmBudget:
    return LlmBudget.load(data_dir / "llm_budget.json")


def _budget_gate(data_dir: Path, est: float, per_run_ceiling: float) -> float:
    """Refuse if recorded LLM spend + this run's estimate exceeds the
    cumulative cap; otherwise return the effective per-run ceiling
    (never more than what is left under the cap)."""
    cap = _llm_budget(data_dir).cap
    spent = _llm_spend_log(data_dir).summary()["total_cost_usd"]
    remaining = round(cap - spent, 4)
    if remaining <= 0 or est > remaining:
        raise ValueError(
            f"recorded LLM spend ${spent} + estimated ${est} exceeds the "
            f"cumulative cap ${cap}; raise it with `llm-budget <amount>`"
        )
    return min(per_run_ceiling, remaining)


def _build_normalizer(args, source, data_dir: Path):
    """Return (normalizer, evaluator_name, est_cost_usd, cache).

    Keyword path: the deterministic default. LLM path (opt-in): fetch the
    signals once, estimate spend for the ones not already cached, refuse
    if that exceeds the ceiling, then hand back a metered, cache-backed,
    ceiling-bounded normalizer.
    """
    from .normalize import to_opportunity

    if args.evaluator == "keyword":
        return to_opportunity, "keyword", 0.0, None

    from .llm_cache import LlmCache
    from .llm_normalize import LlmNormalizer, build_client, estimate_cost_usd

    cache = LlmCache.load(data_dir / "llm_cache.json")
    signals = source.fetch(args.limit)
    est = estimate_cost_usd(
        signals, args.model, cache=None if args.refresh_eval else cache
    )
    if est > args.max_eval_cost:
        raise ValueError(
            f"estimated eval cost ${est} exceeds --max-eval-cost "
            f"${args.max_eval_cost}; nothing was evaluated"
        )
    ceiling = _budget_gate(data_dir, est, args.max_eval_cost)
    normalizer = LlmNormalizer(
        client=build_client(),
        model=args.model,
        max_cost_usd=ceiling,
        cache=cache,
        refresh=args.refresh_eval,
    )
    return normalizer, "llm", est, cache


def _cmd_run(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    discovery_log = _discovery_log(data_dir)
    source = build_source(args.source, args.source_path)
    if args.filter:
        source = FilteredSource(source, is_relevant)
    normalizer, evaluator, est_cost, cache = _build_normalizer(args, source, data_dir)
    run_discovery_cycle(
        source,
        store,
        limit=args.limit,
        shortlist_n=args.shortlist,
        min_score=args.min_score,
        log=discovery_log,
        normalizer=normalizer,
        evaluator=evaluator,
        est_cost_usd=est_cost,
    )
    if cache is not None:
        cache.save()
    if evaluator == "llm":
        _record_llm_spend(data_dir, "evaluate", normalizer)
        meter = getattr(normalizer, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(normalizer, "ceiling_hit", False) else ""
        print(
            f"llm evaluator: est ${est_cost}, actual ${actual}; "
            f"cache {normalizer.cache_hits} hit / "
            f"{normalizer.cache_misses} miss{note}"
        )
    print(render_text(pipeline_report(
        store, revenue_ledger, spend_ledger, discovery_log,
        _llm_spend_log(data_dir), _llm_budget(data_dir),
    )))
    return 0


def _cmd_report(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    print(render_text(pipeline_report(
        store, revenue_ledger, spend_ledger,
        _discovery_log(data_dir), _llm_spend_log(data_dir), _llm_budget(data_dir),
    )))
    return 0


def _cmd_llm_budget(args) -> int:
    data_dir = _data_dir(args)
    budget = _llm_budget(data_dir)
    if args.amount is None:
        spent = _llm_spend_log(data_dir).summary()["total_cost_usd"]
        print(
            f"cap ${budget.cap}  spent ${spent}  "
            f"remaining ${round(budget.cap - spent, 4)}"
        )
        return 0
    new_cap = budget.set_cap(args.amount, actor=args.actor)
    print(f"llm budget cap -> ${new_cap}")
    return 0


def _cmd_outcomes(args) -> int:
    from .opportunity import CRITERIA
    from .retro import outcome_retro

    store, _, _ = _load(_data_dir(args))
    retro = outcome_retro(store)
    c = retro["counts"]
    have = c["validated"] + c["rejected"]
    if not retro["ready"]:
        print(f"(need more recorded outcomes; have {have})")
        return 0
    tot = retro["total"]
    print(
        f"validated {c['validated']} / rejected {c['rejected']}  "
        f"avg score {tot['validated_avg']} vs {tot['rejected_avg']}"
    )
    print(f"  {'criterion':<24} {'validated':>10} {'rejected':>10} {'gap':>8}")
    for name in CRITERIA:
        row = retro["by_criterion"][name]
        print(
            f"  {name:<24} {row['validated_avg']:>10} "
            f"{row['rejected_avg']:>10} {row['gap']:>+8}"
        )
    print("recorded outcomes:")
    for o in retro["outcomes"]:
        print(f"  {o['name']} [{o['outcome']}] score={o['score']} -> {o['metric_value']}")
    return 0


def _cmd_llm_costs(args) -> int:
    data_dir = _data_dir(args)
    entries = _llm_spend_log(data_dir).entries()
    if not entries:
        print("(no LLM runs recorded)")
        return 0
    for e in entries:
        print(
            f"{e['ts']}  {e['activity']:<8} {e.get('model', '')}  "
            f"calls={e.get('api_calls', 0)} "
            f"tokens={e.get('input_tokens', 0)}+{e.get('output_tokens', 0)} "
            f"cost=${e.get('cost_usd', 0)} "
            f"cache={e.get('cache_hits', 0)}h/{e.get('cache_misses', 0)}m"
        )
    s = LlmSpendLog.load(data_dir / "llm_spend.json").summary()
    by = s["by_activity"]
    print(
        f"total ${s['total_cost_usd']} over {s['runs']} run(s), "
        f"{s['total_api_calls']} api call(s) "
        f"(evaluate ${by['evaluate']} plan ${by['plan']} offer ${by['offer']})"
    )
    return 0


def _cmd_dashboard(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    report = pipeline_report(
        store, revenue_ledger, spend_ledger,
        _discovery_log(data_dir), _llm_spend_log(data_dir), _llm_budget(data_dir),
    )
    html = render_html(report, generated_at=now_iso())
    out = Path(args.out) if args.out else data_dir / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"dashboard written: {out}")
    return 0


def _cmd_candidate(args) -> int:
    store, _, _ = _load(_data_dir(args))
    print(render_candidate(_require(store, args.name)))
    return 0


def _cmd_demo(args) -> int:
    from .runner import demo

    demo()
    return 0


# --- human decision commands ---------------------------------------------


def _cmd_approve(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_decision(store, args.name, "approve", approver=args.actor, note=args.note)
    print(f"approved: {out.name} -> {out.status}")
    return 0


def _cmd_reject(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_decision(store, args.name, "reject", approver=args.actor, note=args.note)
    print(f"rejected: {out.name} -> {out.status}")
    return 0


def _build_planner(args, store, data_dir: Path):
    """Return (planner, cache). Template path: the deterministic default.
    LLM path (opt-in): estimate spend over the approved candidates,
    refuse if it exceeds the ceiling, then a metered, cached planner."""
    from .validation import plan_validation

    if args.planner == "template":
        return plan_validation, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .llm_plan import LlmPlanner, estimate_plan_cost_usd

    approved = [c for c in store.all() if c.status == "approved"]
    cache = LlmCache.load(data_dir / "llm_plan_cache.json")
    est = estimate_plan_cost_usd(
        approved, args.model, cache=None if args.refresh_plan else cache
    )
    if est > args.max_plan_cost:
        raise ValueError(
            f"estimated plan cost ${est} exceeds --max-plan-cost "
            f"${args.max_plan_cost}; nothing was planned"
        )
    ceiling = _budget_gate(data_dir, est, args.max_plan_cost)
    planner = LlmPlanner(
        client=build_client(), model=args.model,
        max_cost_usd=ceiling, cache=cache, refresh=args.refresh_plan,
    )
    return planner, cache


def _cmd_investigate(args) -> int:
    data_dir = _data_dir(args)
    store, _, _ = _load(data_dir)
    planner, cache = _build_planner(args, store, data_dir)
    investigating = investigate_approved(store, planner=planner)
    if cache is not None:
        cache.save()
    if args.planner == "llm":
        _record_llm_spend(data_dir, "plan", planner)
        meter = getattr(planner, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(planner, "ceiling_hit", False) else ""
        print(
            f"llm planner: actual ${actual}; cache {planner.cache_hits} hit / "
            f"{planner.cache_misses} miss{note}"
        )
    print(f"investigating: {len(investigating)} candidate(s)")
    return 0


def _cmd_outcome(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_validation_outcome(
        store, args.name, args.result, metric_value=args.metric, actor=args.actor,
        note=args.note,
    )
    print(f"outcome: {out.name} -> {out.status}")
    return 0


def _build_proposer(args, store, data_dir: Path):
    """Return (proposer, cache). Template path: the deterministic default.
    LLM path (opt-in): estimate spend over the validated candidates,
    refuse if it exceeds the ceiling, then a metered, cached proposer."""
    from .offer import propose_offer

    if args.proposer == "template":
        return propose_offer, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .llm_offer import LlmOfferProposer, estimate_offer_cost_usd

    pending = [c for c in store.all() if c.status == "validated" and not c.offer]
    cache = LlmCache.load(data_dir / "llm_offer_cache.json")
    est = estimate_offer_cost_usd(
        pending, args.model, cache=None if args.refresh_offer else cache
    )
    if est > args.max_offer_cost:
        raise ValueError(
            f"estimated offer cost ${est} exceeds --max-offer-cost "
            f"${args.max_offer_cost}; nothing was proposed"
        )
    ceiling = _budget_gate(data_dir, est, args.max_offer_cost)
    proposer = LlmOfferProposer(
        client=build_client(), model=args.model,
        max_cost_usd=ceiling, cache=cache, refresh=args.refresh_offer,
    )
    return proposer, cache


def _cmd_prepare_launch(args) -> int:
    data_dir = _data_dir(args)
    store, _, _ = _load(data_dir)
    proposer, cache = _build_proposer(args, store, data_dir)
    validated = prepare_launch(store, proposer=proposer)
    if cache is not None:
        cache.save()
    if args.proposer == "llm":
        _record_llm_spend(data_dir, "offer", proposer)
        meter = getattr(proposer, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(proposer, "ceiling_hit", False) else ""
        print(
            f"llm proposer: actual ${actual}; cache {proposer.cache_hits} hit / "
            f"{proposer.cache_misses} miss{note}"
        )
    print(f"prepared: offer attached to {len(validated)} validated candidate(s)")
    return 0


def _cmd_launch(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = mark_launched(store, args.name, actor=args.actor, note=args.note)
    print(f"launched: {out.name} -> {out.status}")
    return 0


def _cmd_payment(args) -> int:
    store, revenue_ledger, _ = _load(_data_dir(args))
    out = record_payment(
        store, revenue_ledger, args.name, args.amount, actor=args.actor, note=args.note
    )
    print(f"payment: {out.name} {args.amount} -> {out.status}")
    return 0


# --- cost-control commands (authorize/record only; never move money) ----


def _cmd_budget(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    cap = set_budget(spend_ledger, args.name, args.amount, approver=args.actor)
    print(f"budget: {args.name} -> {cap}")
    return 0


def _cmd_authorize_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    request = SpendRequest(
        candidate_name=args.name,
        purpose=args.purpose,
        amount=args.amount,
        requested_by=args.actor,
    )
    authorize_spend(spend_ledger, request, approver=args.actor, ceiling=args.ceiling)
    print(f"authorized: {args.name} {args.amount} (purpose: {args.purpose})")
    return 0


def _cmd_deny_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    request = SpendRequest(
        candidate_name=args.name,
        purpose=args.purpose,
        amount=args.amount,
        requested_by=args.actor,
    )
    deny_spend(spend_ledger, request, approver=args.actor, reason=args.reason)
    print(f"denied: {args.name} {args.amount} (reason: {args.reason})")
    return 0


def _cmd_record_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    record_spend(spend_ledger, args.name, args.amount, actor=args.actor, note=args.note)
    print(f"spent: {args.name} {args.amount}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", default=argparse.SUPPRESS, help="state directory (default: ./data)"
    )

    actor_only = argparse.ArgumentParser(add_help=False)
    actor_only.add_argument("--actor", default="human-owner", help="who is acting")

    actor = argparse.ArgumentParser(add_help=False, parents=[actor_only])
    actor.add_argument("--note", default="", help="note recorded in history")

    parser = argparse.ArgumentParser(prog="revenue_os", parents=[common])
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="discovery cycle then report")
    run.add_argument("--source", choices=("static", "hn", "file"), default="static")
    run.add_argument(
        "--source-path", default=None, help="signal JSON file (required for --source file)"
    )
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--shortlist", type=int, default=3)
    run.add_argument(
        "--filter", action="store_true", help="keep only commercially relevant signals"
    )
    run.add_argument(
        "--min-score", type=float, default=0.0, help="drop candidates below this score"
    )
    run.add_argument(
        "--evaluator", choices=("keyword", "llm"), default="keyword",
        help="how to score signals (default: deterministic keyword heuristic)",
    )
    run.add_argument(
        "--model", default="claude-sonnet-5", help="model for --evaluator llm"
    )
    run.add_argument(
        "--max-eval-cost", type=float, default=1.0,
        help="USD ceiling for one --evaluator llm run (default 1.00)",
    )
    run.add_argument(
        "--refresh-eval", action="store_true",
        help="ignore cached llm scores and re-call the API",
    )
    run.set_defaults(func=_cmd_run)

    sub.add_parser("report", parents=[common], help="print the report only").set_defaults(
        func=_cmd_report
    )

    sub.add_parser(
        "llm-costs", parents=[common], help="print recorded AI operating spend"
    ).set_defaults(func=_cmd_llm_costs)

    sub.add_parser(
        "outcomes", parents=[common],
        help="retrospective: how validated vs rejected candidates scored",
    ).set_defaults(func=_cmd_outcomes)

    llm_budget = sub.add_parser(
        "llm-budget", parents=[common, actor_only],
        help="show or raise the cumulative AI spend cap",
    )
    llm_budget.add_argument(
        "amount", type=float, nargs="?", default=None,
        help="new cap in USD (omit to show cap/spent/remaining)",
    )
    llm_budget.set_defaults(func=_cmd_llm_budget)

    dash = sub.add_parser(
        "dashboard", parents=[common], help="write a static HTML pipeline snapshot"
    )
    dash.add_argument(
        "--out", default=None, help="output path (default: <data-dir>/dashboard.html)"
    )
    dash.set_defaults(func=_cmd_dashboard)

    cand = sub.add_parser("candidate", parents=[common], help="show one candidate")
    cand.add_argument("name")
    cand.set_defaults(func=_cmd_candidate)

    sub.add_parser("demo", parents=[common], help="end-to-end walkthrough").set_defaults(
        func=_cmd_demo
    )

    approve = sub.add_parser("approve", parents=[common, actor], help="approve a candidate")
    approve.add_argument("name")
    approve.set_defaults(func=_cmd_approve)

    reject = sub.add_parser("reject", parents=[common, actor], help="reject a candidate")
    reject.add_argument("name")
    reject.set_defaults(func=_cmd_reject)

    investigate = sub.add_parser(
        "investigate", parents=[common], help="plan + advance all approved candidates"
    )
    investigate.add_argument(
        "--planner", choices=("template", "llm"), default="template",
        help="how to design the validation test (default: deterministic template)",
    )
    investigate.add_argument(
        "--model", default="claude-sonnet-5", help="model for --planner llm"
    )
    investigate.add_argument(
        "--max-plan-cost", type=float, default=0.5,
        help="USD ceiling for one --planner llm run (default 0.50)",
    )
    investigate.add_argument(
        "--refresh-plan", action="store_true",
        help="ignore cached llm plans and re-call the API",
    )
    investigate.set_defaults(func=_cmd_investigate)

    outcome = sub.add_parser(
        "outcome", parents=[common, actor], help="record a validation outcome"
    )
    outcome.add_argument("name")
    outcome.add_argument("result", choices=("validated", "rejected"))
    outcome.add_argument("--metric", required=True, help="observed metric value")
    outcome.set_defaults(func=_cmd_outcome)

    prep = sub.add_parser(
        "prepare-launch", parents=[common], help="attach offers to validated candidates"
    )
    prep.add_argument(
        "--proposer", choices=("template", "llm"), default="template",
        help="how to draft the first offer (default: deterministic template)",
    )
    prep.add_argument("--model", default="claude-sonnet-5", help="model for --proposer llm")
    prep.add_argument(
        "--max-offer-cost", type=float, default=0.5,
        help="USD ceiling for one --proposer llm run (default 0.50)",
    )
    prep.add_argument(
        "--refresh-offer", action="store_true",
        help="ignore cached llm offers and re-call the API",
    )
    prep.set_defaults(func=_cmd_prepare_launch)

    launch = sub.add_parser("launch", parents=[common, actor], help="mark an offer live")
    launch.add_argument("name")
    launch.set_defaults(func=_cmd_launch)

    payment = sub.add_parser(
        "payment", parents=[common, actor], help="record a received payment"
    )
    payment.add_argument("name")
    payment.add_argument("amount", type=float)
    payment.set_defaults(func=_cmd_payment)

    budget = sub.add_parser(
        "budget", parents=[common, actor_only], help="set/raise a candidate's spend cap"
    )
    budget.add_argument("name")
    budget.add_argument("amount", type=float)
    budget.set_defaults(func=_cmd_budget)

    auth = sub.add_parser(
        "authorize-spend", parents=[common, actor_only], help="authorize a spend request"
    )
    auth.add_argument("name")
    auth.add_argument("amount", type=float)
    auth.add_argument("--purpose", required=True, help="what the spend is for")
    auth.add_argument(
        "--ceiling", type=float, default=DEFAULT_CEILING,
        help="max authorizable amount (default 0.0)",
    )
    auth.set_defaults(func=_cmd_authorize_spend)

    deny = sub.add_parser(
        "deny-spend", parents=[common, actor_only], help="deny a spend request"
    )
    deny.add_argument("name")
    deny.add_argument("amount", type=float)
    deny.add_argument("--purpose", required=True, help="what the spend was for")
    deny.add_argument("--reason", required=True, help="why it was denied")
    deny.set_defaults(func=_cmd_deny_spend)

    rec = sub.add_parser(
        "record-spend", parents=[common, actor], help="log money already spent"
    )
    rec.add_argument("name")
    rec.add_argument("amount", type=float)
    rec.set_defaults(func=_cmd_record_spend)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        args.command = "report"
        func = _cmd_report
    try:
        return func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
