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
    entry = entry_from(activity, worker)
    log = llm_spend_log(data_dir)
    log.add(entry)
    log.save()
    try:  # mirror into the gateway's call-by-call audit
        from .action_class import in_autonomous_context
        from .llm_gateway import gateway
        gateway(data_dir).record(
            task=activity, model=entry.get("model", ""),
            est_usd=entry.get("cost_usd", 0.0), actual_usd=entry.get("cost_usd", 0.0),
            in_tokens=entry.get("input_tokens", 0),
            out_tokens=entry.get("output_tokens", 0),
            cache_hit=entry.get("cache_hits", 0) > 0,
            autonomous=in_autonomous_context(),
            outcome="ceiling_hit" if entry.get("ceiling_hit") else "ok")
    except Exception:
        pass


def budget_gate(data_dir, est: float, per_run_ceiling: float,
                *, task: str = "generic") -> float:
    """Refuse if this LLM run would breach ANY limit - the pre-sale hard
    cap, the cumulative `llm_budget.json` cap, OR the LLM gateway's
    per-call / task / hourly / daily / global / rate limits. Otherwise
    return the effective per-run ceiling (the smallest headroom).

    Inside the autonomous loop this also enforces the gateway's
    `autonomous_enabled` switch (default off -> raises LlmUnavailable)."""
    from .budget import guard, presale_active, presale_remaining_usd
    from .llm_gateway import gateway

    guard(data_dir, est)   # BudgetBlocked before the first sale

    cap = llm_budget(data_dir).cap
    spent = llm_spend_log(data_dir).summary()["total_cost_usd"]
    remaining = round(cap - spent, 4)
    if remaining <= 0 or est > remaining:
        raise ValueError(
            f"recorded LLM spend ${spent} + estimated ${est} exceeds the "
            f"cumulative cap ${cap}; raise it with `llm-budget <amount>`"
        )

    gw_ceiling = gateway(data_dir).preflight(est, task=task)   # may raise

    ceiling = min(per_run_ceiling, remaining, gw_ceiling)
    if presale_active(data_dir):
        ceiling = min(ceiling, presale_remaining_usd(data_dir))
    return ceiling


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
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="evaluate")
    normalizer = LlmNormalizer(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
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
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="plan")
    planner = LlmPlanner(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
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

    worker_mode = "web" if mode == "web" else "llm"
    data_dir = Path(data_dir)
    pending = [c for c in store.all() if c.status == "shortlisted" and not c.research]
    cache = LlmCache.load(data_dir / "llm_research_cache.json")
    est = estimate_research_cost_usd(
        pending, model, cache=None if refresh else cache, mode=worker_mode)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated research cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was researched"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="research")
    worker = ResearchWorker(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh, mode=worker_mode,
    )
    return worker, cache


def build_competitor_analyzer(*, mode: str, store, model: str, max_cost_usd: float,
                              refresh: bool, data_dir):
    """Return (worker, cache). 'off' -> (None, None)."""
    if mode == "off":
        return None, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .competition import CompetitionWorker, estimate_competition_cost_usd

    worker_mode = "web" if mode == "web" else "llm"
    data_dir = Path(data_dir)
    pending = [
        c for c in store.all() if c.status == "shortlisted" and not c.competition
    ]
    cache = LlmCache.load(data_dir / "llm_competition_cache.json")
    est = estimate_competition_cost_usd(
        pending, model, cache=None if refresh else cache, mode=worker_mode)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated competition cost ${est} exceeds the ${max_cost_usd} "
            "ceiling; nothing was analysed"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="competition")
    worker = CompetitionWorker(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh, mode=worker_mode,
    )
    return worker, cache


def build_copywriter(*, mode: str, store, model: str, max_cost_usd: float,
                     refresh: bool, data_dir):
    """Return (worker, cache). 'off' -> (None, None)."""
    if mode == "off":
        return None, None

    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .copywriter import CopywriterWorker, estimate_copy_cost_usd

    data_dir = Path(data_dir)
    pairs = [
        (c, dict(c.offer)) for c in store.all()
        if c.status == "validated" and c.offer and not c.launch_draft
    ]
    cache = LlmCache.load(data_dir / "llm_copy_cache.json")
    est = estimate_copy_cost_usd(pairs, model, cache=None if refresh else cache)
    if est > max_cost_usd:
        raise ValueError(
            f"estimated copy cost ${est} exceeds the ${max_cost_usd} ceiling; "
            "nothing was drafted"
        )
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="copy")
    worker = CopywriterWorker(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
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

    ceiling = budget_gate(data_dir, 0.01, max_cost_usd, task="decide")  # one small call
    return LlmDecisionPolicy(client=build_client(data_dir), model=model, max_cost_usd=ceiling)


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
    ceiling = budget_gate(data_dir, est, max_cost_usd, task="offer")
    proposer = LlmOfferProposer(
        client=build_client(data_dir), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh,
    )
    return proposer, cache
