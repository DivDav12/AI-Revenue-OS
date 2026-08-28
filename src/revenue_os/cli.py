"""Command-line interface.

Read commands:
  run              discovery cycle against a source, then print the report
  report           print the report only (no discovery)
  candidate NAME   print one candidate's full state
  demo             full end-to-end walkthrough in a throwaway directory

Human decision commands (operate on the persistent --data-dir store):
  approve NAME / reject NAME
  investigate
  outcome NAME {validated|rejected} --metric TEXT
  prepare-launch
  launch NAME
  payment NAME AMOUNT

Cost-control commands (authorize/record only; never move money):
  budget NAME AMOUNT
  authorize-spend NAME AMOUNT --purpose TEXT [--ceiling N]
  deny-spend NAME AMOUNT --purpose TEXT --reason TEXT
  record-spend NAME AMOUNT
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .approval import record_decision
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
from .store import CandidateStore
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


def _cmd_run(args) -> int:
    store, revenue_ledger, spend_ledger = _load(_data_dir(args))
    source = build_source(args.source)
    if args.filter:
        source = FilteredSource(source, is_relevant)
    run_discovery_cycle(
        source,
        store,
        limit=args.limit,
        shortlist_n=args.shortlist,
        min_score=args.min_score,
    )
    print(render_text(pipeline_report(store, revenue_ledger, spend_ledger)))
    return 0


def _cmd_report(args) -> int:
    store, revenue_ledger, spend_ledger = _load(_data_dir(args))
    print(render_text(pipeline_report(store, revenue_ledger, spend_ledger)))
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


def _cmd_investigate(args) -> int:
    store, _, _ = _load(_data_dir(args))
    investigating = investigate_approved(store)
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


def _cmd_prepare_launch(args) -> int:
    store, _, _ = _load(_data_dir(args))
    validated = prepare_launch(store)
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
    run.add_argument("--source", choices=("static", "hn"), default="static")
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--shortlist", type=int, default=3)
    run.add_argument(
        "--filter", action="store_true", help="keep only commercially relevant signals"
    )
    run.add_argument(
        "--min-score", type=float, default=0.0, help="drop candidates below this score"
    )
    run.set_defaults(func=_cmd_run)

    sub.add_parser("report", parents=[common], help="print the report only").set_defaults(
        func=_cmd_report
    )

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

    sub.add_parser(
        "investigate", parents=[common], help="plan + advance all approved candidates"
    ).set_defaults(func=_cmd_investigate)

    outcome = sub.add_parser(
        "outcome", parents=[common, actor], help="record a validation outcome"
    )
    outcome.add_argument("name")
    outcome.add_argument("result", choices=("validated", "rejected"))
    outcome.add_argument("--metric", required=True, help="observed metric value")
    outcome.set_defaults(func=_cmd_outcome)

    sub.add_parser(
        "prepare-launch", parents=[common], help="attach offers to validated candidates"
    ).set_defaults(func=_cmd_prepare_launch)

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
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
