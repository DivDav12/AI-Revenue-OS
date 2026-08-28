"""Construct the opt-in LLM leaf workers - evaluator, validation planner,
offer proposer - with the M23 spend ledger, the M24 cumulative cap, and
the per-run cache all wired in.

Shared by the CLI and the operator agent so neither can bypass the
safety machinery. The deterministic modes ("keyword" / "template")
return the plain functions and touch none of it.
"""

from __future__ import annotations

from pathlib import Path

from .llm_budget import LlmBudget
from .llm_spend import LlmSpendLog, entry_from


def llm_budget(data_dir) -> LlmBudget:
    return LlmBudget.load(Path(data_dir) / "llm_budget.json")


def llm_spend_log(data_dir) -> LlmSpendLog:
    return LlmSpendLog.load(Path(data_dir) / "llm_spend.json")


def record_llm_spend(data_dir, activity: str, worker) -> None:
    log = llm_spend_log(data_dir)
    log.add(entry_from(activity, worker))
    log.save()


def budget_gate(data_dir, est: float, per_run_ceiling: float) -> float:
    """Refuse if recorded LLM spend + this run's estimate exceeds the
    cumulative cap; otherwise return the effective per-run ceiling
    (never more than what is left under the cap)."""
    cap = llm_budget(data_dir).cap
    spent = llm_spend_log(data_dir).summary()["total_cost_usd"]
    remaining = round(cap - spent, 4)
    if remaining <= 0 or est > remaining:
        raise ValueError(
            f"recorded LLM spend ${spent} + estimated ${est} exceeds the "
            f"cumulative cap ${cap}; raise it with `llm-budget <amount>`"
        )
    return min(per_run_ceiling, remaining)


def build_evaluator(*, mode: str, source, limit: int, model: str,
                    max_cost_usd: float, refresh: bool, data_dir):
    """Return (normalizer, evaluator_name, est_cost_usd, cache)."""
    from .normalize import to_opportunity

    if mode == "keyword":
        return to_opportunity, "keyword", 0.0, None

    from .llm_cache import LlmCache
    from .llm_normalize import LlmNormalizer, build_client, estimate_cost_usd

    data_dir = Path(data_dir)
    cache = LlmCache.load(data_dir / "llm_cache.json")
    signals = source.fetch(limit)
    est = estimate_cost_usd(signals, model, cache=None if refresh else cache)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated eval cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was evaluated"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd)
    normalizer = LlmNormalizer(
        client=build_client(), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh,
    )
    return normalizer, "llm", est, cache


def build_planner(*, mode: str, store, model: str, max_cost_usd: float,
                  refresh: bool, data_dir):
    """Return (planner, cache)."""
    from .validation import plan_validation

    if mode == "template":
        return plan_validation, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .llm_plan import LlmPlanner, estimate_plan_cost_usd

    data_dir = Path(data_dir)
    approved = [c for c in store.all() if c.status == "approved"]
    cache = LlmCache.load(data_dir / "llm_plan_cache.json")
    est = estimate_plan_cost_usd(approved, model, cache=None if refresh else cache)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated plan cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was planned"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd)
    planner = LlmPlanner(
        client=build_client(), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh,
    )
    return planner, cache


def build_researcher(*, mode: str, store, model: str, max_cost_usd: float,
                     refresh: bool, data_dir):
    """Return (worker, cache). 'off' -> (None, None)."""
    if mode == "off":
        return None, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .research import ResearchWorker, estimate_research_cost_usd

    data_dir = Path(data_dir)
    pending = [c for c in store.all() if c.status == "shortlisted" and not c.research]
    cache = LlmCache.load(data_dir / "llm_research_cache.json")
    est = estimate_research_cost_usd(pending, model, cache=None if refresh else cache)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated research cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was researched"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd)
    worker = ResearchWorker(
        client=build_client(), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh,
    )
    return worker, cache


def build_decider(*, mode: str, model: str, max_cost_usd: float, data_dir):
    """Return an LlmDecisionPolicy, or None for the deterministic 'rules'
    policy. Raises ValueError if the cumulative cap is already exhausted."""
    if mode == "rules":
        return None

    from .decide_llm import LlmDecisionPolicy
    from .llm_normalize import build_client

    ceiling = budget_gate(data_dir, 0.01, max_cost_usd)  # one small call
    return LlmDecisionPolicy(client=build_client(), model=model, max_cost_usd=ceiling)


def build_proposer(*, mode: str, store, model: str, max_cost_usd: float,
                   refresh: bool, data_dir):
    """Return (proposer, cache)."""
    from .offer import propose_offer

    if mode == "template":
        return propose_offer, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .llm_offer import LlmOfferProposer, estimate_offer_cost_usd

    data_dir = Path(data_dir)
    pending = [c for c in store.all() if c.status == "validated" and not c.offer]
    cache = LlmCache.load(data_dir / "llm_offer_cache.json")
    est = estimate_offer_cost_usd(pending, model, cache=None if refresh else cache)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated offer cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was proposed"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd)
    proposer = LlmOfferProposer(
        client=build_client(), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh,
    )
    return proposer, cache
