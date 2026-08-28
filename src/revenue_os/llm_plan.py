"""LLM-backed validation planning (opt-in).

One Claude call turns an approved candidate into a concrete cheapest
test, with an estimated real-world cost and a needs_human_budget flag.
The deterministic template planner (validation.plan_validation) stays
the default.

Reuses the evaluator's client/meter/ceiling/cache machinery
(llm_normalize, llm_cache). No money is moved anywhere; a costed test
still requires an explicit `budget` + `authorize-spend` by the human.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .llm_normalize import (
    CostCeilingExceeded,
    CostMeter,
    DEFAULT_MODEL,
    _PRICES,
    _FALLBACK_PRICE,
    _tool_input,
)
from .opportunity import CRITERIA
from .validation import ValidationPlan
from .store import Candidate, now_iso

_PLAN_PROMPT_VERSION = "1"
_MAX_TOKENS = 700
_MAX_TEXT = 400
_EFFORTS = ("low", "medium")

_RUBRIC = (
    "You design the single cheapest test that would confirm real, paying "
    "demand for a revenue opportunity run by a solo operator. Keep it doable "
    "in under two weeks. Prefer a test that costs nothing (outreach, "
    "interviews, a manual concierge trial). Only propose a paid test "
    "(landing page + ads, a domain, a prototype) when a free test genuinely "
    "cannot answer the demand question; then set needs_human_budget true and "
    "estimated_cost_usd to a realistic total. Call record_plan once.\n"
    "Fields: hypothesis (what paying demand you expect), cheapest_test (one "
    "concrete action), success_metric (a number that settles go/no-go), "
    "effort (low or medium), estimated_cost_usd (0 for a free test), "
    "needs_human_budget (true only if estimated_cost_usd > 0)."
)

_TOOL = {
    "name": "record_plan",
    "description": "Record the single cheapest validation test.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "hypothesis", "cheapest_test", "success_metric", "effort",
            "estimated_cost_usd", "needs_human_budget",
        ],
        "properties": {
            "hypothesis": {"type": "string"},
            "cheapest_test": {"type": "string"},
            "success_metric": {"type": "string"},
            "effort": {"type": "string"},
            "estimated_cost_usd": {"type": "number"},
            "needs_human_budget": {"type": "boolean"},
        },
    },
}


def _candidate_brief(candidate: Candidate) -> str:
    scores = ", ".join(
        f"{name}={candidate.breakdown.get(name)}" for name in CRITERIA
        if candidate.breakdown.get(name) is not None
    )
    lines = [f"Opportunity: {candidate.description or candidate.name}"]
    if scores:
        lines.append(f"Scores (0-5): {scores}")
    if candidate.rationale:
        lines.append(f"Evaluator note: {candidate.rationale}")
    return "\n".join(lines)


def plan_cache_key(candidate: Candidate, model: str) -> str:
    raw = "\n".join([
        "plan", _PLAN_PROMPT_VERSION, model, candidate.name,
        candidate.description, _candidate_brief(candidate),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_plan_cost_usd(candidates, model: str, cache=None) -> float:
    """Local pre-flight estimate (no API call)."""
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for cand in candidates:
        if cache is not None and cache.get(plan_cache_key(cand, model)) is not None:
            continue
        total_in += rubric_tokens + len(_candidate_brief(cand)) // 4 + 40
        total_out += 300
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def _plan_from_data(candidate: Candidate, data: dict) -> ValidationPlan:
    effort = str(data.get("effort", "")).lower()
    if effort not in _EFFORTS:
        raise ValueError(f"llm plan effort {effort!r} not in {_EFFORTS}")
    try:
        cost = round(float(data["estimated_cost_usd"]), 2)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("llm plan estimated_cost_usd missing/invalid") from exc
    if cost < 0:
        raise ValueError("llm plan estimated_cost_usd is negative")
    needs_budget = bool(data.get("needs_human_budget", False)) or cost > 0
    for key in ("hypothesis", "cheapest_test", "success_metric"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"llm plan {key!r} is empty")
    return ValidationPlan(
        candidate_name=candidate.name,
        hypothesis=str(data["hypothesis"]).strip()[:_MAX_TEXT],
        cheapest_test=str(data["cheapest_test"]).strip()[:_MAX_TEXT],
        success_metric=str(data["success_metric"]).strip()[:_MAX_TEXT],
        effort=effort,
        max_cost=cost,
        needs_human_budget=needs_budget,
        created_at=now_iso(),
    )


def _plan_from_todict(candidate: Candidate, d: dict) -> ValidationPlan:
    """Rebuild a ValidationPlan from its own serialized form (cache hit)."""
    return ValidationPlan(
        candidate_name=candidate.name,
        hypothesis=d["hypothesis"],
        cheapest_test=d["cheapest_test"],
        success_metric=d["success_metric"],
        effort=d["effort"],
        max_cost=d.get("max_cost", 0.0),
        needs_human_budget=d.get("needs_human_budget", False),
        created_at=d.get("created_at", ""),
    )


def plan_validation_llm(candidate: Candidate, *, client, model: str = DEFAULT_MODEL,
                        meter=None) -> ValidationPlan:
    """Design one cheapest test with a single Claude call.

    Raises ValueError on a malformed response; the caller leaves the
    candidate 'approved' for a later retry.
    """
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _RUBRIC,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_plan"},
        messages=[{"role": "user", "content": _candidate_brief(candidate)}],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _plan_from_data(candidate, _tool_input(response, "record_plan"))


@dataclass
class LlmPlanner:
    """Callable candidate -> ValidationPlan that reuses cached plans,
    meters spend on cache misses, and stops calling the API once
    `max_cost_usd` is reached. Cache hits cost nothing."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 0.5
    meter: CostMeter = field(default=None)
    cache: object = None
    refresh: bool = False
    ceiling_hit: bool = False
    cache_hits: int = 0
    cache_misses: int = 0

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    def __call__(self, candidate: Candidate) -> ValidationPlan:
        key = plan_cache_key(candidate, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return _plan_from_todict(candidate, hit["plan"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"plan cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        plan = plan_validation_llm(
            candidate, client=self.client, model=self.model, meter=self.meter
        )
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"plan": plan.to_dict(), "model": self.model})
        return plan
