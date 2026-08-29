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
from .calibration import calibration_weights
from .discovery_log import DiscoveryLog
from .messages import Task
from .normalize import to_opportunity
from .opportunity import Opportunity, OpportunityScore
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .team import build_team
from .offer import propose_offer
from .store import Candidate, CandidateStore, now_iso
from .validation import plan_validation

logger = logging.getLogger(__name__)


def _default_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(EvaluatorAgent(name="evaluator"))
    return Orchestrator(registry=registry)


def run_evaluation(
    opportunities: list[Opportunity],
    orchestrator: Orchestrator | None = None,
    *,
    weights: dict[str, float] | None = None,
) -> list[OpportunityScore]:
    orchestrator = orchestrator or _default_orchestrator()
    for opp in opportunities:
        orchestrator.add_task(
            Task(
                objective=f"evaluate opportunity: {opp.name}",
                capability="evaluate",
                payload={"opportunity": opp, "weights": weights},
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
    calibrated: bool = False,
    sink=None,
) -> list[Candidate]:
    """Discover, evaluate, persist candidates, and auto-shortlist the top N.

    Runs the real discovery team: Market Scanner fans one 'evaluate' task
    per opportunity out to the Evaluator, then the Opportunity Finder
    ranks the scores and names the shortlist. `sink` (a TaskLog.record)
    receives every dispatched task with its lineage.

    Candidates scoring below min_score (default 0.0 = no gate) are not
    persisted. Idempotent: re-runs refresh scores but never downgrade a
    candidate a human has already acted on. Returns all stored candidates.
    """
    weights = calibration_weights(store) if calibrated else None
    team = build_team(source=source, normalizer=normalizer, sink=sink)
    root = Task(
        objective="discover opportunities",
        capability="discover",
        payload={"limit": limit, "then": "evaluate", "weights": weights},
    )
    team.add_task(root)

    opportunities: list[Opportunity] = []
    scores: dict[str, OpportunityScore] = {}
    for result in team.run_cycle():
        if result.status != "ok":
            continue
        if "opportunities" in result.output:
            opportunities = result.output["opportunities"]
        elif "opportunity_name" in result.output:
            scores[result.output["opportunity_name"]] = OpportunityScore(
                opportunity_name=result.output["opportunity_name"],
                total=result.output["total"],
                verdict=result.output["verdict"],
                breakdown=result.output["breakdown"],
            )
    if not opportunities:
        logger.warning("discovery returned no opportunities (source failure or empty)")

    evaluated = len(scores)
    finder = Task(
        objective="select opportunities",
        capability="select",
        parent_id=root.id,
        depth=1,
        payload={
            "scored": [
                {"name": s.opportunity_name, "total": s.total, "verdict": s.verdict}
                for s in scores.values()
            ],
            "min_score": min_score,
            "shortlist_n": shortlist_n,
        },
    )
    team.add_task(finder)
    selection = team.run_cycle()[0].output
    kept_names = set(selection["kept"])
    dropped = len(selection["dropped"])
    if dropped:
        logger.info("dropped %d candidate(s) below min_score=%s", dropped, min_score)

    new_count = 0
    refreshed_count = 0
    for opp in opportunities:
        score = scores.get(opp.name)
        if score is None or opp.name not in kept_names:
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
    for name in selection["shortlist"]:
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
                "kept": len(kept_names),
                "new": new_count,
                "refreshed": refreshed_count,
                "shortlisted": shortlisted_count,
                "evaluator": evaluator,
                "est_cost_usd": round(float(est_cost_usd), 4),
                "actual_cost_usd": round(meter.cost_usd, 4) if meter is not None else 0.0,
                "cost_ceiling_hit": bool(getattr(normalizer, "ceiling_hit", False)),
                "eval_cache_hits": int(getattr(normalizer, "cache_hits", 0)),
                "eval_cache_misses": int(getattr(normalizer, "cache_misses", 0)),
                "calibrated": bool(calibrated),
                "weights_applied": weights is not None,
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


def research_shortlisted(store: CandidateStore, worker, *, sink=None) -> list[Candidate]:
    """Dispatch a research task per shortlisted candidate without a note,
    through the Orchestrator to a ResearchAgent, and attach each note.

    Idempotent: candidates that already have a note are skipped. A failed
    research leaves that candidate un-noted for a later retry. `sink` (a
    TaskLog.record) receives each dispatched task. Returns the candidates
    researched this call.
    """
    from .research import ResearchAgent

    pending = [
        c for c in store.all() if c.status == "shortlisted" and not c.research
    ]
    if not pending:
        return []

    orchestrator = Orchestrator(registry=AgentRegistry(), sink=sink)
    orchestrator.register(ResearchAgent(name="product_researcher"))
    for cand in pending:
        orchestrator.add_task(Task(
            objective=f"research: {cand.name}",
            capability="research",
            payload={"candidate": cand, "worker": worker},
        ))

    researched: list[Candidate] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            logger.warning("research failed: %s", result.error)
            continue
        name = result.output["candidate_name"]
        cand = store.get(name)
        if cand is not None:
            updated = replace(cand, research=dict(result.output["research"]))
            store.put(updated)
            researched.append(updated)
    store.save()
    return researched


def analyze_competition_shortlisted(store: CandidateStore, worker, *,
                                    sink=None) -> list[Candidate]:
    """Dispatch an analyze_competition task per shortlisted candidate
    without a note, through the Orchestrator to a CompetitorAnalyzerAgent,
    and attach each note.

    Idempotent: candidates that already have a note are skipped. A failed
    analysis leaves that candidate un-noted for a later retry. `sink` (a
    TaskLog.record) receives each dispatched task. Returns the candidates
    analysed this call.
    """
    from .competition import CompetitorAnalyzerAgent

    pending = [
        c for c in store.all() if c.status == "shortlisted" and not c.competition
    ]
    if not pending:
        return []

    orchestrator = Orchestrator(registry=AgentRegistry(), sink=sink)
    orchestrator.register(CompetitorAnalyzerAgent(name="competitor_analyzer"))
    for cand in pending:
        orchestrator.add_task(Task(
            objective=f"competition: {cand.name}",
            capability="analyze_competition",
            payload={"candidate": cand, "worker": worker},
        ))

    analysed: list[Candidate] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            logger.warning("competition analysis failed: %s", result.error)
            continue
        cand = store.get(result.output["candidate_name"])
        if cand is not None:
            updated = replace(cand, competition=dict(result.output["competition"]))
            store.put(updated)
            analysed.append(updated)
    store.save()
    return analysed


def write_copy_for_validated(store: CandidateStore, worker, *,
                             sink=None) -> list[Candidate]:
    """Dispatch a write_copy task per validated candidate that has an
    offer and no launch draft, through the Orchestrator to a
    CopywriterAgent, and attach each draft.

    Idempotent: candidates that already have a draft are skipped. A failed
    draft leaves that candidate un-drafted for a later retry. `sink` (a
    TaskLog.record) receives each dispatched task. Returns the candidates
    drafted this call. Crosses no gate - status is unchanged.
    """
    from .copywriter import CopywriterAgent

    pending = [
        c for c in store.all()
        if c.status == "validated" and c.offer and not c.launch_draft
    ]
    if not pending:
        return []

    orchestrator = Orchestrator(registry=AgentRegistry(), sink=sink)
    orchestrator.register(CopywriterAgent(name="copywriter"))
    for cand in pending:
        orchestrator.add_task(Task(
            objective=f"copy: {cand.name}",
            capability="write_copy",
            payload={"candidate": cand, "offer": dict(cand.offer), "worker": worker},
        ))

    drafted: list[Candidate] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            logger.warning("copywriting failed: %s", result.error)
            continue
        cand = store.get(result.output["candidate_name"])
        if cand is not None:
            updated = replace(cand, launch_draft=dict(result.output["launch_draft"]))
            store.put(updated)
            drafted.append(updated)
    store.save()
    return drafted


def package_deliverables(store: CandidateStore, data_dir, *,
                         sink=None) -> list[Candidate]:
    """Dispatch a package_deliverable task per validated candidate that
    has an offer and no deliverable, write the two files under
    <data_dir>/deliverables/<name>/, and attach a `deliverable` note.

    Deterministic, idempotent, failure-isolated. Crosses no gate -
    status is unchanged and nothing is published.
    """
    from pathlib import Path

    from .deliverable import DeliverablePackagerAgent

    data_dir = Path(data_dir)
    pending = [
        c for c in store.all()
        if c.status == "validated" and c.offer and not c.deliverable
    ]
    if not pending:
        return []

    orchestrator = Orchestrator(registry=AgentRegistry(), sink=sink)
    orchestrator.register(DeliverablePackagerAgent(name="content_creator"))
    for cand in pending:
        orchestrator.add_task(Task(
            objective=f"package: {cand.name}",
            capability="package_deliverable",
            payload={
                "candidate": {"name": cand.name, "description": cand.description},
                "offer": dict(cand.offer),
                "draft": dict(cand.launch_draft),
                "plan": dict(cand.plan),
            },
        ))

    packaged: list[Candidate] = []
    for result in orchestrator.run_cycle():
        if result.status != "ok":
            logger.warning("packaging failed: %s", result.error)
            continue
        cand = store.get(result.output["candidate_name"])
        if cand is None:
            continue
        out_dir = data_dir / "deliverables" / cand.name
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "landing.html").write_text(
            result.output["landing_html"], encoding="utf-8")
        (out_dir / "README.txt").write_text(
            result.output["readme"], encoding="utf-8")
        updated = replace(cand, deliverable=dict(result.output["deliverable"]))
        store.put(updated)
        packaged.append(updated)
    store.save()
    return packaged
