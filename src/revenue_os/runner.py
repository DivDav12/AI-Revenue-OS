"""Wire the Orchestrator with a registry of agents and run demo cycles."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .agent import DiscoveryAgent, EvaluatorAgent, ReverseAgent, WorkerAgent
from .approval import record_decision
from .messages import Result, Task
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .sources import RawSignal, StaticSource
from .revenue import RevenueLedger, mark_launched, record_payment, revenue_summary
from .store import CandidateStore
from .validation import record_validation_outcome
from .workflow import investigate_approved, prepare_launch, run_discovery_cycle

_SAMPLE_SIGNALS = [
    RawSignal(
        title="Show HN: an open-source no-code automation platform",
        text="We built a self-serve tool to automate repetitive API workflows.",
        source="sample",
        external_id="s1",
    ),
    RawSignal(
        title="Ask HN: how do you find your first paying customers?",
        text="Bootstrapped founder looking for revenue and pricing advice.",
        source="sample",
        external_id="s2",
    ),
    RawSignal(
        title="Launch: a marketplace for reusable document templates",
        text="MVP is live, free tier plus paid plans.",
        source="sample",
        external_id="s3",
    ),
    RawSignal(
        title="A weekend project with no obvious business model",
        text="Just something I made for fun.",
        source="sample",
        external_id="s4",
    ),
]


def build_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    registry.register(EvaluatorAgent(name="evaluator"))
    registry.register(
        DiscoveryAgent(StaticSource(_SAMPLE_SIGNALS), name="discovery")
    )
    return Orchestrator(registry=registry)


def run_once(tasks: list[Task] | None = None) -> list[Result]:
    """Run a single execution cycle over the given (or a default) task."""
    orchestrator = build_orchestrator()
    default = [Task(objective="M4 smoke test: confirm the runtime cycle works")]
    for task in tasks if tasks is not None else default:
        orchestrator.add_task(task)
    return orchestrator.run_cycle()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    tmp = Path(tempfile.mkdtemp(prefix="revenue-os-"))
    store = CandidateStore.load(tmp / "candidates.json")

    candidates = run_discovery_cycle(
        StaticSource(_SAMPLE_SIGNALS), store, limit=10, shortlist_n=3
    )
    print("Persisted candidates (ranked):")
    for rank, cand in enumerate(candidates, start=1):
        print(f"  {rank}. {cand.name}: {cand.total} ({cand.verdict}) [{cand.status}]")

    shortlisted = [c for c in candidates if c.status == "shortlisted"]
    if not shortlisted:
        print(f"\nStore file: {store.path}")
        return

    decided = record_decision(
        store,
        shortlisted[0].name,
        "approve",
        approver="human-owner",
        note="demo approval",
    )
    print(f"\nHuman decision recorded: {decided.name} -> {decided.status}")

    investigating = investigate_approved(store)
    for cand in investigating:
        print(f"\nValidation plan for {cand.name}:")
        print(f"  hypothesis: {cand.plan['hypothesis']}")
        print(f"  cheapest test: {cand.plan['cheapest_test']}")
        print(f"  success metric: {cand.plan['success_metric']}")
        print(f"  max cost: {cand.plan['max_cost']}")

    if not investigating:
        print(f"\nStore file: {store.path}")
        return

    final = record_validation_outcome(
        store,
        investigating[0].name,
        "validated",
        metric_value="27 waitlist signups",
        actor="human-owner",
        note="demo outcome",
    )
    print(f"\nValidation outcome recorded: {final.name} -> {final.status}")

    for cand in prepare_launch(store):
        print(f"\nProposed offer for {cand.name}:")
        print(f"  sells: {cand.offer['what_is_sold']}")
        print(f"  price: {cand.offer['price']} {cand.offer['currency']} "
              f"(estimate: {cand.offer['price_is_estimate']})")
        print(f"  delivery: {cand.offer['delivery']}")

    ledger = RevenueLedger.load(tmp / "revenue.json")
    launched = mark_launched(store, final.name, actor="human-owner", note="offer live")
    print(f"\nOffer launched: {launched.name} -> {launched.status}")

    earning = record_payment(
        store, ledger, final.name, 29.0, actor="human-owner", note="first sale"
    )
    print(f"Payment recorded: {earning.name} -> {earning.status}")
    print(f"\nRevenue summary: {revenue_summary(store, ledger)}")
    print(f"\nStore file: {store.path}")
