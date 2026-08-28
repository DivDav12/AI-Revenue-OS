"""LLM-backed normalization (opt-in).

One Claude call per signal assigns the eight criterion estimates and a
short rationale, replacing the keyword nudger in normalize.py. This
module is never imported unless the caller selects the 'llm' evaluator.

Safety:
  - off by default; the keyword path stays the default everywhere
  - `anthropic` is an optional dependency, imported lazily here
  - no tools are exposed to the model; signal text is untrusted
    single-turn user content
  - a per-run USD ceiling halts further calls (LlmNormalizer)
  - token usage is measured (CostMeter) and surfaced by the caller
  - unchanged signals are served from a local cache (see llm_cache.py),
    so re-runs cost nothing

No money is moved anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .normalize import _slug
from .opportunity import CRITERIA, SCORE_MAX, SCORE_MIN, Opportunity

DEFAULT_MODEL = "claude-sonnet-5"
_MAX_RATIONALE = 280
_MAX_TOKENS = 600

# Bump when the rubric or tool schema changes materially; the LlmCache
# key includes this, so a bump invalidates every cached entry.
_PROMPT_VERSION = "1"

# USD per 1M tokens (input, output); used only to estimate/measure spend.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
_FALLBACK_PRICE = (5.0, 25.0)

_CRITERION_HELP = {
    "startup_affordability": "cheap to start (5 = almost no upfront cost)",
    "automation_potential": "runs with little ongoing human effort",
    "demand": "real, evidenced demand for this",
    "competition_headroom": "room to enter despite existing competitors",
    "legal_feasibility": "clearly legal, low regulatory risk",
    "speed_to_first_revenue": "short time to the first paying customer",
    "profit_potential": "healthy margin once running",
    "scalability": "grows without proportional cost",
}

_RUBRIC = (
    "You score revenue opportunities for a solo operator. Rate each "
    "criterion from 0 to 5; higher is always better. Criteria:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in _CRITERION_HELP.items())
    + "\n\nUse the full range, not just the middle. Judge only from the "
    "signal text; if it is vague, score demand and profit low. Then give "
    "a one-sentence rationale (at most 40 words). Call record_scores once."
)

_TOOL = {
    "name": "record_scores",
    "description": "Record the 0-5 criterion scores and a short rationale.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": list(CRITERIA) + ["rationale"],
        "properties": {
            **{name: {"type": "number"} for name in CRITERIA},
            "rationale": {"type": "string"},
        },
    },
}


@dataclass
class CostMeter:
    """Accumulates token usage for one model and converts it to USD."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage) -> None:
        if usage is None:
            return
        for attr in ("input_tokens", "cache_read_input_tokens",
                     "cache_creation_input_tokens"):
            self.input_tokens += int(getattr(usage, attr, 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    @property
    def cost_usd(self) -> float:
        in_rate, out_rate = _PRICES.get(self.model, _FALLBACK_PRICE)
        return round(
            self.input_tokens / 1e6 * in_rate + self.output_tokens / 1e6 * out_rate, 4
        )


def cache_key(signal, model: str) -> str:
    text = getattr(signal, "text", "") or ""
    raw = f"eval\n{_PROMPT_VERSION}\n{model}\n{signal.title}\n{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_cost_usd(signals, model: str, cache=None) -> float:
    """Local pre-flight estimate (no API call): ~chars/4 in, ~350 out per
    signal. Signals already in `cache` are free and are skipped."""
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for signal in signals:
        if cache is not None and cache.get(cache_key(signal, model)) is not None:
            continue
        body = len(signal.title) + len(getattr(signal, "text", "") or "")
        total_in += rubric_tokens + body // 4 + 40
        total_out += 350
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def build_client():
    """Construct a default Anthropic client, with clear errors for the
    two common misconfigurations (missing package, missing credentials)."""
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        raise ValueError(
            "llm evaluator needs the 'anthropic' package: "
            "pip install 'revenue-os[llm]'"
        ) from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # missing / invalid credentials surface here
        raise ValueError(f"could not create Anthropic client: {exc}") from exc


def _tool_input(response, tool_name: str = "record_scores") -> dict:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and \
                getattr(block, "name", None) == tool_name:
            raw = block.input
            if isinstance(raw, str):
                raw = json.loads(raw)
            return dict(raw)
    raise ValueError(f"llm response had no {tool_name} tool call")


def _validate_scores(data: dict) -> dict[str, float]:
    estimates: dict[str, float] = {}
    for name in CRITERIA:
        try:
            value = float(data[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"llm response missing/invalid {name!r}") from exc
        if not SCORE_MIN <= value <= SCORE_MAX:
            raise ValueError(f"llm score {name}={value} out of range")
        estimates[name] = round(value, 2)
    return estimates


def _build(signal, estimates: dict[str, float], rationale: str) -> Opportunity:
    return Opportunity(
        name=_slug(signal.title),
        description=signal.title,
        source=signal.source,
        raw_ref=signal.url or signal.external_id,
        rationale=str(rationale).strip()[:_MAX_RATIONALE],
        estimate_source="llm",
        **estimates,
    )


def to_opportunity_llm(signal, *, client, model: str = DEFAULT_MODEL, meter=None) -> Opportunity:
    """Score one signal with a single Claude call.

    Raises ValueError on a malformed or out-of-range response; the caller
    (DiscoveryAgent) skips that signal and the cycle continues.
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
        tool_choice={"type": "tool", "name": "record_scores"},
        messages=[{
            "role": "user",
            "content": f"Title: {signal.title}\n\nText: {signal.text or '(none)'}",
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))

    data = _tool_input(response)
    estimates = _validate_scores(data)
    return _build(signal, estimates, data.get("rationale", ""))


class CostCeilingExceeded(Exception):
    """Raised by LlmNormalizer once the run's USD ceiling is reached."""


@dataclass
class LlmNormalizer:
    """Callable signal -> Opportunity that reuses cached scores, meters
    spend on cache misses, and stops calling the API once `max_cost_usd`
    is reached. Cache hits cost nothing and ignore the ceiling."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 1.0
    meter: CostMeter = field(default=None)
    cache: object = None
    refresh: bool = False
    ceiling_hit: bool = False
    cache_hits: int = 0
    cache_misses: int = 0

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    def __call__(self, signal) -> Opportunity:
        key = cache_key(signal, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return _build(signal, dict(hit["scores"]), hit.get("rationale", ""))

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"eval cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        opp = to_opportunity_llm(
            signal, client=self.client, model=self.model, meter=self.meter
        )
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(
                key,
                {"scores": opp.estimates(), "rationale": opp.rationale,
                 "model": self.model},
            )
        return opp
