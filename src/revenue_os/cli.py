"""Minimal command-line interface.

Subcommands:
  run     discovery cycle against a source, then print the report
  report  print the report only (no discovery)
  demo    full end-to-end walkthrough in a throwaway directory
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .report import pipeline_report, render_text
from .revenue import RevenueLedger
from .sources import build_source
from .spend import SpendLedger
from .store import CandidateStore
from .workflow import run_discovery_cycle

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


def _cmd_run(args) -> int:
    store, revenue_ledger, spend_ledger = _load(_data_dir(args))
    source = build_source(args.source)
    run_discovery_cycle(
        source, store, limit=args.limit, shortlist_n=args.shortlist
    )
    print(render_text(pipeline_report(store, revenue_ledger, spend_ledger)))
    return 0


def _cmd_report(args) -> int:
    store, revenue_ledger, spend_ledger = _load(_data_dir(args))
    print(render_text(pipeline_report(store, revenue_ledger, spend_ledger)))
    return 0


def _cmd_demo(args) -> int:
    from .runner import demo

    demo()
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        help="state directory (default: ./data)",
    )

    parser = argparse.ArgumentParser(prog="revenue_os", parents=[common])
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="discovery cycle then report")
    run.add_argument("--source", choices=("static", "hn"), default="static")
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--shortlist", type=int, default=3)
    run.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", parents=[common], help="print the report only")
    rep.set_defaults(func=_cmd_report)

    dem = sub.add_parser("demo", parents=[common], help="end-to-end walkthrough")
    dem.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.command = "report"
        return _cmd_report(args)
    return args.func(args)
