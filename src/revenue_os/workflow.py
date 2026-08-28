"""Evaluation workflow: score a set of opportunities and rank them.

Dispatches one 'evaluate' Task per Opportunity through the Orchestrator,
collects the Results, and returns the successful scores ranked by total
(highest first). No LLM, no I/O.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from . import lifecycle
from .agent import DiscoveryAgent, EvaluatorAgent
from .messages import Task
from .opportunity import Opportunity, OpportunityScore
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .offer import propose_offer
from .store import Candidate, CandidateStore
from .validation import plan_validation

logger = logging.getLogger(__name__)


def _default_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(EvaluatorAgent(name="evaluator"))
    return Orchestrator(registry=registry)


def run_evaluation(
    opportunities: list[Opportunity], orchestrator: Orchestrator | None = None
) -> list[OpportunityScore]:
    orchestrator = orchestrator or _default_orchestrator()
    for opp in opportunities:
        orchestrator.add_task(
            Task(
                objective=f"evaluate opportunity: {opp.name}",
                capability="evaluate",
                payload={"opportunity": opp},
            )
        )
    scores: list[OpportunityScore] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            continue
        scores.append(
            OpportunityScore(
                opportunity_name=result.output["opportunity_name"],
                total=result.output["total"],
                verdict=result.output["verdict"],
                breakdown=result.output["breakdown"],
            )
        )
    scores.sort(key=lambda s: s.total, reverse=True)
    return scores


def _discover(source, limit: int) -> list[Opportunity]:
    discovery = Orchestrator(registry=AgentRegistry())
    discovery.register(DiscoveryAgent(source, name="discovery"))
    discovery.add_task(
        Task(
            objective="discover opportunities",
            capability="discover",
            payload={"limit": limit},
        )
    )
    opportunities: list[Opportunity] = []
    for result in discovery.run_cycle():
        if result.status == "ok":
            opportunities.extend(result.output["opportunities"])
    return opportunities


def discover_evaluate_select(
    source,
    *,
    limit: int = 10,
    top_n: int = 5,
    orchestrator: Orchestrator | None = None,
) -> list[OpportunityScore]:
    """CEO flow: discover signals -> normalize -> evaluate -> rank -> select top_n.

    Selection only flags candidates for further investigation; no action
    is taken and no financially or legally sensitive step is performed.
    """
    opportunities = _discover(source, limit)
    ranked = run_evaluation(opportunities, orchestrator=orchestrator)
    return ranked[: max(0, top_n)]


def run_discovery_cycle(
    source,
    store: CandidateStore,
    *,
    limit: int = 10,
    shortlist_n: int = 3,
) -> list[Candidate]:
    """Discover, evaluate, persist candidates, and auto-shortlist the top N.

    Idempotent: re-runs refresh scores but never downgrade a candidate a
    human has already acted on. Returns all stored candidates, ranked.
    """
    opportunities = _discover(source, limit)
    if not opportunities:
        logger.warning("discovery returned no opportunities (source failure or empty)")
    ranked = run_evaluation(opportunities)
    scores = {s.opportunity_name: s for s in ranked}

    for opp in opportunities:
        score = scores.get(opp.name)
        if score is None:
            continue
        store.upsert(
            Candidate(
                name=opp.name,
                description=opp.description,
                source=opp.source,
                raw_ref=opp.raw_ref,
                total=score.total,
                verdict=score.verdict,
                breakdown=score.breakdown,
            )
        )

    for score in ranked[: max(0, shortlist_n)]:
        name = score.opportunity_name
        cand = store.get(name)
        if cand is not None and cand.status == "discovered":
            store.put(
                lifecycle.advance(
                    cand, "shortlisted", note="auto-shortlist", actor="system"
                )
            )

    store.save()
    return store.all()


def investigate_approved(store: CandidateStore) -> list[Candidate]:
    """Attach a validation plan to each approved candidate and start investigation.

    Idempotent: only 'approved' candidates are touched. Returns the
    candidates now in 'investigating'.
    """
    for cand in list(store.all()):
        if cand.status != "approved":
            continue
        plan = plan_validation(cand)
        advanced = lifecycle.advance(
            cand, "investigating", note="auto-investigate", actor="system"
        )
        store.put(replace(advanced, plan=plan.to_dict()))
    store.save()
    return [c for c in store.all() if c.status == "investigating"]


def prepare_launch(store: CandidateStore) -> list[Candidate]:
    """Attach a proposed first paid offer to each validated candidate.

    Status is not changed: launching the offer is a human act
    (revenue.mark_launched). Idempotent. Returns validated candidates.
    """
    for cand in list(store.all()):
        if cand.status != "validated" or cand.offer:
            continue
        store.put(replace(cand, offer=propose_offer(cand).to_dict()))
    store.save()
    return [c for c in store.all() if c.status == "validated"]
