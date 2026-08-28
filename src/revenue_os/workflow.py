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
from .discovery_log import DiscoveryLog
from .messages import Task
from .normalize import to_opportunity
from .opportunity import Opportunity, OpportunityScore
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .offer import propose_offer
from .store import Candidate, CandidateStore, now_iso
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


def _discover(source, limit: int, normalizer=to_opportunity) -> list[Opportunity]:
    discovery = Orchestrator(registry=AgentRegistry())
    discovery.register(DiscoveryAgent(source, name="discovery", normalizer=normalizer))
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
    min_score: float = 0.0,
    orchestrator: Orchestrator | None = None,
) -> list[OpportunityScore]:
    """CEO flow: discover signals -> normalize -> evaluate -> rank -> select top_n.

    Selection only flags candidates for further investigation; no action
    is taken and no financially or legally sensitive step is performed.
    """
    opportunities = _discover(source, limit)
    ranked = run_evaluation(opportunities, orchestrator=orchestrator)
    ranked = [s for s in ranked if s.total >= min_score]
    return ranked[: max(0, top_n)]


def run_discovery_cycle(
    source,
    store: CandidateStore,
    *,
    limit: int = 10,
    shortlist_n: int = 3,
    min_score: float = 0.0,
    log: DiscoveryLog | None = None,
    normalizer=to_opportunity,
    evaluator: str = "keyword",
    est_cost_usd: float = 0.0,
) -> list[Candidate]:
    """Discover, evaluate, persist candidates, and auto-shortlist the top N.

    Candidates scoring below min_score (default 0.0 = no gate) are not
    persisted. Idempotent: re-runs refresh scores but never downgrade a
    candidate a human has already acted on. Returns all stored candidates.

    `normalizer` maps a signal to an Opportunity (default: the keyword
    heuristic). When a DiscoveryLog is supplied, one entry recording the
    run's counts and evaluator cost is appended and saved.
    """
    opportunities = _discover(source, limit, normalizer)
    if not opportunities:
        logger.warning("discovery returned no opportunities (source failure or empty)")
    ranked = run_evaluation(opportunities)
    evaluated = len(ranked)
    kept = [s for s in ranked if s.total >= min_score]
    dropped = len(ranked) - len(kept)
    if dropped:
        logger.info("dropped %d candidate(s) below min_score=%s", dropped, min_score)
    ranked = kept
    scores = {s.opportunity_name: s for s in ranked}

    new_count = 0
    refreshed_count = 0
    for opp in opportunities:
        score = scores.get(opp.name)
        if score is None:
            continue
        existed = store.get(opp.name) is not None
        store.upsert(
            Candidate(
                name=opp.name,
                description=opp.description,
                source=opp.source,
                raw_ref=opp.raw_ref,
                total=score.total,
                verdict=score.verdict,
                breakdown=score.breakdown,
                rationale=opp.rationale,
                estimate_source=opp.estimate_source,
            )
        )
        if existed:
            refreshed_count += 1
        else:
            new_count += 1

    shortlisted_count = 0
    for score in ranked[: max(0, shortlist_n)]:
        name = score.opportunity_name
        cand = store.get(name)
        if cand is not None and cand.status == "discovered":
            store.put(
                lifecycle.advance(
                    cand, "shortlisted", note="auto-shortlist", actor="system"
                )
            )
            shortlisted_count += 1

    if log is not None:
        meter = getattr(normalizer, "meter", None)
        log.add(
            {
                "ts": now_iso(),
                "source": getattr(source, "name", ""),
                "limit": limit,
                "fetched": len(opportunities),
                "filtered_out": int(getattr(source, "dropped", 0)),
                "dropped_below_score": dropped,
                "evaluated": evaluated,
                "kept": len(kept),
                "new": new_count,
                "refreshed": refreshed_count,
                "shortlisted": shortlisted_count,
                "evaluator": evaluator,
                "est_cost_usd": round(float(est_cost_usd), 4),
                "actual_cost_usd": round(meter.cost_usd, 4) if meter is not None else 0.0,
                "cost_ceiling_hit": bool(getattr(normalizer, "ceiling_hit", False)),
                "eval_cache_hits": int(getattr(normalizer, "cache_hits", 0)),
                "eval_cache_misses": int(getattr(normalizer, "cache_misses", 0)),
            }
        )
        log.save()

    store.save()
    return store.all()


def investigate_approved(store: CandidateStore, planner=plan_validation) -> list[Candidate]:
    """Attach a validation plan to each approved candidate and start investigation.

    `planner` maps a candidate to a ValidationPlan (default: the template
    planner). If it raises, that candidate is left 'approved' for a later
    retry and the rest continue. Idempotent. Returns the candidates now
    in 'investigating'.
    """
    for cand in list(store.all()):
        if cand.status != "approved":
            continue
        try:
            plan = planner(cand)
        except Exception as exc:  # a bad plan must not strand the others
            logger.warning("could not plan %r, left approved: %s", cand.name, exc)
            continue
        advanced = lifecycle.advance(
            cand, "investigating", note="auto-investigate", actor="system"
        )
        store.put(replace(advanced, plan=plan.to_dict()))
    store.save()
    return [c for c in store.all() if c.status == "investigating"]


def prepare_launch(store: CandidateStore, proposer=propose_offer) -> list[Candidate]:
    """Attach a proposed first paid offer to each validated candidate.

    `proposer` maps a candidate to an Offer (default: the template
    proposer). If it raises, that candidate is left offer-less for a
    later retry and the rest continue. Status is not changed: launching
    the offer is a human act (revenue.mark_launched). Idempotent.
    """
    for cand in list(store.all()):
        if cand.status != "validated" or cand.offer:
            continue
        try:
            offer = proposer(cand)
        except Exception as exc:  # a bad offer must not strand the others
            logger.warning("could not propose offer for %r: %s", cand.name, exc)
            continue
        store.put(replace(cand, offer=offer.to_dict()))
    store.save()
    return [c for c in store.all() if c.status == "validated"]
