"""LLM-backed first-offer proposal (opt-in).

One Claude call turns a validated candidate into a concrete first paid
offer: what to sell, a starting price, delivery type, a call to action,
and a one-line positioning. The deterministic template proposer
(offer.propose_offer) stays the default.

Reuses the evaluator's client/meter/ceiling/cache machinery. Proposing
an offer moves no money; price_is_estimate stays True and the human
still runs `launch` and records every `payment`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .llm_normalize import (
    CostCeilingExceeded,
    CostMeter,
    DEFAULT_MODEL,
    UNTRUSTED_NOTE,
    _FALLBACK_PRICE,
    _PRICES,
    _tool_input,
    wrap_untrusted,
)
from .llm_plan import _candidate_brief
from .offer import Offer
from .store import Candidate, now_iso

_OFFER_PROMPT_VERSION = "2"
_MAX_TOKENS = 600
_MAX_TEXT = 300
_DELIVERIES = ("digital", "manual", "subscription")

_RUBRIC = (
    "You propose the first paid offer for a revenue opportunity that a solo "
    "operator has already validated (real paying demand confirmed). Propose "
    "the smallest offer that could take money this week. Pick delivery: "
    "digital (a file or tool bought and downloaded), manual (you do the work "
    "per client), or subscription (recurring). Set a concrete starting price "
    "in USD that is realistic for the delivery type and audience, not "
    "aspirational. Give a one-line call_to_action and a one-line positioning "
    "(who it is for + the pain it removes). Call record_offer once."
    + UNTRUSTED_NOTE
)

_TOOL = {
    "name": "record_offer",
    "description": "Record the first paid offer.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "what_is_sold", "price", "currency", "delivery",
            "call_to_action", "positioning",
        ],
        "properties": {
            "what_is_sold": {"type": "string"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
            "delivery": {"type": "string"},
            "call_to_action": {"type": "string"},
            "positioning": {"type": "string"},
        },
    },
}


def _offer_brief(candidate: Candidate) -> str:
    lines = [_candidate_brief(candidate)]
    plan = candidate.plan or {}
    if plan.get("hypothesis"):
        lines.append(f"Validated hypothesis: {plan['hypothesis']}")
    if plan.get("cheapest_test"):
        lines.append(f"Test run: {plan['cheapest_test']}")
    outcome = candidate.outcome or {}
    if outcome.get("metric_value"):
        lines.append(f"Confirmed result: {outcome['metric_value']}")
    return "\n".join(lines)


def offer_cache_key(candidate: Candidate, model: str) -> str:
    raw = "\n".join([
        "offer", _OFFER_PROMPT_VERSION, model, candidate.name,
        candidate.description, _offer_brief(candidate),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_offer_cost_usd(candidates, model: str, cache=None) -> float:
    """Local pre-flight estimate (no API call)."""
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for cand in candidates:
        if cache is not None and cache.get(offer_cache_key(cand, model)) is not None:
            continue
        total_in += rubric_tokens + len(_offer_brief(cand)) // 4 + 40
        total_out += 250
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def _offer_from_data(candidate: Candidate, data: dict) -> Offer:
    delivery = str(data.get("delivery", "")).lower()
    if delivery not in _DELIVERIES:
        raise ValueError(f"llm offer delivery {delivery!r} not in {_DELIVERIES}")
    try:
        price = round(float(data["price"]), 2)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("llm offer price missing/invalid") from exc
    if price <= 0:
        raise ValueError("llm offer price must be positive")
    for key in ("what_is_sold", "call_to_action"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"llm offer {key!r} is empty")
    return Offer(
        candidate_name=candidate.name,
        what_is_sold=str(data["what_is_sold"]).strip()[:_MAX_TEXT],
        price=price,
        delivery=delivery,
        call_to_action=str(data["call_to_action"]).strip()[:_MAX_TEXT],
        currency=str(data.get("currency", "USD")).strip()[:8] or "USD",
        price_is_estimate=True,
        positioning=str(data.get("positioning", "")).strip()[:_MAX_TEXT],
        created_at=now_iso(),
    )


def _offer_from_todict(candidate: Candidate, d: dict) -> Offer:
    """Rebuild an Offer from its own serialized form (cache hit)."""
    return Offer(
        candidate_name=candidate.name,
        what_is_sold=d["what_is_sold"],
        price=d["price"],
        delivery=d["delivery"],
        call_to_action=d["call_to_action"],
        currency=d.get("currency", "USD"),
        price_is_estimate=d.get("price_is_estimate", True),
        positioning=d.get("positioning", ""),
        created_at=d.get("created_at", ""),
    )


def propose_offer_llm(candidate: Candidate, *, client, model: str = DEFAULT_MODEL,
                      meter=None) -> Offer:
    """Propose one first offer with a single Claude call.

    Raises ValueError on a malformed response; the caller leaves the
    candidate without an offer for a later retry.
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
        tool_choice={"type": "tool", "name": "record_offer"},
        messages=[{
            "role": "user",
            "content": wrap_untrusted(_offer_brief(candidate)),
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _offer_from_data(candidate, _tool_input(response, "record_offer"))


@dataclass
class LlmOfferProposer:
    """Callable candidate -> Offer that reuses cached offers, meters spend
    on cache misses, and stops calling the API once `max_cost_usd` is
    reached. Cache hits cost nothing."""

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

    def __call__(self, candidate: Candidate) -> Offer:
        key = offer_cache_key(candidate, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return _offer_from_todict(candidate, hit["offer"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"offer cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        offer = propose_offer_llm(
            candidate, client=self.client, model=self.model, meter=self.meter
        )
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"offer": offer.to_dict(), "model": self.model})
        return offer
