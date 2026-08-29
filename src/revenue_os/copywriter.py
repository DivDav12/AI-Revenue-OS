"""Launch copy (opt-in) - the first build-cluster agent.

Given a human-validated candidate that already carries a proposed offer,
one Claude call drafts the sales copy: headline, subheadline, body,
primary call to action, and three FAQ entries. No web access.

Draft only: it changes no status and publishes nothing. The draft is
stored on the candidate with basis = "model draft, not published"; the
human owner still launches.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .agent import Agent
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
from .messages import Result, Task
from .store import Candidate, now_iso

_COPY_PROMPT_VERSION = "1"
_MAX_TOKENS = 1200
_MAX_TEXT = 600
_FAQ_COUNT = 3

_RUBRIC = (
    "You draft the sales copy for a small, honest first paid offer. Write "
    "plainly, no hype, no fake urgency, no invented testimonials or "
    "statistics. Base every claim on the opportunity and the offer given.\n"
    "headline: one clear line naming the outcome.\n"
    "subheadline: one line on who it is for and the pain it removes.\n"
    "body: 2-3 short paragraphs - the problem, what they get, why it is "
    "credible from a small operator.\n"
    "primary_cta: the single action, matching the offer's call to action.\n"
    "faq: exactly three question/answer pairs a first buyer would ask "
    "(price, delivery, refund/what-if).\n"
    "Call record_launch_copy once."
)

_TOOL = {
    "name": "record_launch_copy",
    "description": "Record the draft launch copy for this offer.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "subheadline", "body", "primary_cta", "faq"],
        "properties": {
            "headline": {"type": "string"},
            "subheadline": {"type": "string"},
            "body": {"type": "string"},
            "primary_cta": {"type": "string"},
            "faq": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "answer"],
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _offer_brief(offer: dict) -> str:
    return "\n".join([
        f"Sells: {offer.get('what_is_sold', '')}",
        f"Price: {offer.get('price', '')} {offer.get('currency', 'USD')} "
        f"({offer.get('delivery', '')})",
        f"Call to action: {offer.get('call_to_action', '')}",
        f"Positioning: {offer.get('positioning', '') or '(none)'}",
    ])


def copy_cache_key(candidate: Candidate, offer: dict, model: str) -> str:
    raw = "\n".join([
        "copy", _COPY_PROMPT_VERSION, model, candidate.name,
        candidate.description, _candidate_brief(candidate),
        json.dumps(offer, sort_keys=True),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_copy_cost_usd(pairs, model: str, cache=None) -> float:
    """pairs: iterable of (candidate, offer)."""
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for cand, offer in pairs:
        if cache is not None and cache.get(copy_cache_key(cand, offer, model)) is not None:
            continue
        total_in += rubric_tokens + len(_candidate_brief(cand)) // 4 + \
            len(_offer_brief(offer)) // 4 + 40
        total_out += 900
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def _clean(value: str) -> str:
    return str(value).strip()[:_MAX_TEXT]


def _draft_from_data(data: dict, model: str) -> dict:
    for key in ("headline", "subheadline", "body", "primary_cta"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"launch copy {key!r} is empty")
    faq = data.get("faq")
    if not isinstance(faq, list) or len(faq) != _FAQ_COUNT:
        raise ValueError(f"launch copy needs exactly {_FAQ_COUNT} faq entries")
    clean_faq = []
    for item in faq:
        q, a = str(item.get("question", "")).strip(), str(item.get("answer", "")).strip()
        if not q or not a:
            raise ValueError("launch copy faq entry is incomplete")
        clean_faq.append({"question": q[:_MAX_TEXT], "answer": a[:_MAX_TEXT]})
    return {
        "headline": _clean(data["headline"]),
        "subheadline": _clean(data["subheadline"]),
        "body": _clean(data["body"]),
        "primary_cta": _clean(data["primary_cta"]),
        "faq": clean_faq,
        "basis": "model draft, not published",
        "model": model,
        "drafted_at": now_iso(),
    }


def write_copy_llm(candidate: Candidate, offer: dict, *, client,
                   model: str = DEFAULT_MODEL, meter=None) -> dict:
    """Draft launch copy with a single Claude call. Raises ValueError on a
    malformed response."""
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _RUBRIC + UNTRUSTED_NOTE,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_launch_copy"},
        messages=[{
            "role": "user",
            "content": wrap_untrusted(
                _candidate_brief(candidate) + "\n\nOffer:\n" + _offer_brief(offer)
            ),
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _draft_from_data(_tool_input(response, "record_launch_copy"), model)


@dataclass
class CopywriterWorker:
    """Callable (candidate, offer) -> draft that reuses cached drafts,
    meters spend on cache misses, and stops calling the API once
    max_cost_usd is reached."""

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

    def __call__(self, candidate: Candidate, offer: dict) -> dict:
        key = copy_cache_key(candidate, offer, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return dict(hit["draft"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"copy cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        draft = write_copy_llm(
            candidate, offer, client=self.client, model=self.model, meter=self.meter
        )
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"draft": draft, "model": self.model})
        return draft


class CopywriterAgent(Agent):
    """Turns a candidate + offer carried in task.payload into a launch-copy
    draft using task.payload['worker']."""

    role = "copywriter"
    objective = "Draft the sales copy for a validated offer."
    capabilities = ("write_copy",)

    def run(self, task: Task) -> Result:
        candidate = task.payload.get("candidate")
        offer = task.payload.get("offer")
        worker = task.payload.get("worker")
        if not isinstance(candidate, Candidate) or not isinstance(offer, dict) \
                or worker is None:
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload needs a Candidate, an offer dict and a worker",
            )
        try:
            draft = worker(candidate, offer)
        except Exception as exc:
            return Result(
                task_id=task.id, agent=self.name, status="error", error=str(exc)
            )
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={"candidate_name": candidate.name, "launch_draft": draft},
        )
