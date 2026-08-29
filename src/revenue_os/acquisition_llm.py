"""Optional LLM relevance pass for the Acquisition Agent.

Off by default (`--score deterministic`). With `--score llm` each lead is
scored by ONE Claude call that judges whether the public post is a
founder *currently* struggling to get their first paying customers.

Reuses the shared LLM machinery unchanged:
  - budget_gate (cumulative cap) + a per-run --max-cost ceiling
  - CostMeter / record_llm_spend
  - LlmCache: a lead already scored is never re-charged
  - wrap_untrusted / UNTRUSTED_NOTE: the post title + body are third-party
    text and are fenced as untrusted data, never instructions

The model returns a structured verdict. It is instructed to NEVER claim
the person will become a customer - it only classifies the post.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .acquisition import _PROSPECT_TYPES
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

_PROMPT_VERSION = "1"
_MAX_TOKENS = 500
_MAX_REASON = 300

_RUBRIC = (
    "You classify ONE public forum/Q&A post for a service that sells a "
    "personalised customer-acquisition plan to solo founders. Decide "
    "whether the poster is, RIGHT NOW, a founder who has built or launched "
    "something and is struggling to get their first paying customers.\n\n"
    "Judge only the post. Do NOT claim or imply the person will buy "
    "anything or become a customer - you are only labelling the post.\n\n"
    "Examples:\n"
    "A: 'How do I get my first paying customer? I launched my SaaS two "
    "weeks ago and have zero customers.' -> is_active_problem true, "
    "relevance_score ~90, prospect_type active_problem.\n"
    "B: 'I got 1,000 customers. Here's how.' -> is_active_problem false, "
    "relevance_score ~5, prospect_type success_story.\n"
    "C: 'How an Angelpad startup got 1,000 customers.' -> false, ~5, "
    "success_story (a case study / article, not a person asking).\n"
    "D: 'How do I get users for my new productivity app?' -> possibly, "
    "relevance_score ~60, prospect_type seeking_advice or active_problem.\n"
    "E: 'Someone should build a tool for finding customers.' -> false, "
    "~15, prospect_type educational or irrelevant (an idea, not a "
    "founder asking for help).\n"
    "F: 'How do you allocate equity in a startup?' with an unrelated "
    "comment mentioning customers -> false, ~5, irrelevant.\n\n"
    "recommended_fit: 0-100, how well the EUR 29.90 Customer Launch Plan "
    "(a personalised 14-day acquisition plan) would fit THIS poster's "
    "situation. Call record_relevance once."
)

_TOOL = {
    "name": "record_relevance",
    "description": "Record the relevance classification for this post.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["relevance_score", "is_active_problem", "buying_intent",
                     "prospect_type", "reason", "recommended_fit"],
        "properties": {
            "relevance_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "is_active_problem": {"type": "boolean"},
            "buying_intent": {"type": "string", "enum": ["high", "medium", "low"]},
            "prospect_type": {"type": "string", "enum": list(_PROSPECT_TYPES)},
            "reason": {"type": "string"},
            "recommended_fit": {"type": "integer", "minimum": 0, "maximum": 100},
        },
    },
}


def _brief(view: dict) -> str:
    det = view.get("deterministic", {})
    return (
        f"Title: {view.get('title', '')}\n"
        f"Posted: {view.get('posted_at') or '(unknown)'}\n"
        f"Body: {view.get('text') or '(none)'}\n"
        f"(deterministic pre-score: {det.get('relevance_score')} / "
        f"{det.get('prospect_type')} / {det.get('buying_intent')})"
    )


def score_key(view: dict, model: str) -> str:
    raw = "\n".join([
        "acq-relevance", _PROMPT_VERSION, model,
        str(view.get("canonical_url", "")),
        str(view.get("title", "")),
        str(view.get("text", ""))[:500],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_score_cost_usd(views, model: str, cache=None) -> float:
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for v in views:
        if cache is not None and cache.get(score_key(v, model)) is not None:
            continue
        total_in += rubric_tokens + len(_brief(v)) // 4 + 30
        total_out += 120
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def _clean(data: dict) -> dict:
    intent = str(data.get("buying_intent", "")).lower()
    if intent not in ("high", "medium", "low"):
        intent = "low"
    ptype = str(data.get("prospect_type", "")).lower()
    if ptype not in _PROSPECT_TYPES:
        ptype = "unknown"
    try:
        rel = max(0, min(100, int(data["relevance_score"])))
        fit = max(0, min(100, int(data["recommended_fit"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("llm relevance response missing numeric fields") from exc
    return {
        "relevance_score": rel,
        "fit_score": rel,
        "active_problem": bool(data.get("is_active_problem")),
        "buying_intent": intent,
        "prospect_type": ptype,
        "recommended_fit": fit,
        "llm_reason": str(data.get("reason", "")).strip()[:_MAX_REASON],
    }


def score_view_llm(view: dict, *, client, model: str = DEFAULT_MODEL,
                   meter=None) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text", "text": _RUBRIC + UNTRUSTED_NOTE,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_relevance"},
        messages=[{"role": "user", "content": wrap_untrusted(_brief(view))}],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _clean(_tool_input(response, "record_relevance"))


@dataclass
class AcquisitionLlmScorer:
    """Callable (scoring_view) -> refined score dict. Cached, metered,
    ceiling-bounded. A cached lead costs nothing and ignores the ceiling."""

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

    def __call__(self, view: dict) -> dict:
        key = score_key(view, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return dict(hit["score"])
        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"acquisition llm cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}")
        score = score_view_llm(view, client=self.client, model=self.model,
                               meter=self.meter)
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"score": score, "model": self.model})
        return score
