"""Competitor analysis (opt-in) - the discovery cluster's competition read.

Given a shortlisted candidate, one Claude call produces a structured
competitive note: named competitors, pricing landscape, a
differentiation angle, and a crowded / contested / open verdict. No web
access - the note is a hypothesis from the signal and the model's
general knowledge, stored with basis = "model knowledge, no web".

Advisory only: it changes no score and crosses no gate. Reuses the
evaluator's client / meter / ceiling / cache machinery.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .agent import Agent
from .llm_normalize import (
    CostCeilingExceeded,
    CostMeter,
    DEFAULT_MODEL,
    SEARCH_PRICE_USD,
    UNTRUSTED_NOTE,
    _FALLBACK_PRICE,
    _PRICES,
    _tool_input,
    grounded_tool_call,
    web_search_tool,
    wrap_untrusted,
)
from .llm_plan import _candidate_brief
from .messages import Result, Task
from .research import _BLOCKED_DOMAINS, _MAX_SEARCHES, _SOURCE_SCHEMA
from .store import Candidate, now_iso

_COMPETITION_PROMPT_VERSION = "1"
_MAX_TOKENS = 800
_MAX_TEXT = 400
_VERDICTS = ("crowded", "contested", "open")

_RUBRIC = (
    "You size up the competition for a revenue opportunity a solo operator "
    "is considering. You have NO web access - base your read on what the "
    "signal implies and your general knowledge, and say plainly where you "
    "are uncertain.\n"
    "named_competitors: who already serves this need (names or clear "
    "categories), or 'none obvious'.\n"
    "pricing_landscape: how incumbents charge and roughly how much.\n"
    "differentiation_angle: the most credible wedge for a small new entrant.\n"
    "saturation: how crowded the space looks and why.\n"
    "verdict: crowded, contested, or open.\n"
    "Call record_competition_analysis once with a one-sentence rationale."
)

_WEB_RUBRIC = (
    "You size up the competition for a revenue opportunity a solo operator "
    "is considering. Search the web to find who serves this need and how "
    "they price it. Then:\n"
    "named_competitors: who already serves this need (names or clear "
    "categories), or 'none obvious'.\n"
    "pricing_landscape: how incumbents charge and roughly how much.\n"
    "differentiation_angle: the most credible wedge for a small new entrant.\n"
    "saturation: how crowded the space looks and why.\n"
    "verdict: crowded, contested, or open.\n"
    "Call record_competition_analysis once with a one-sentence rationale and "
    "a sources array (url + title) for every page you relied on."
)

_TOOL = {
    "name": "record_competition_analysis",
    "description": "Record the competitive read for this opportunity.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["named_competitors", "pricing_landscape",
                     "differentiation_angle", "saturation", "verdict",
                     "rationale"],
        "properties": {
            "named_competitors": {"type": "string"},
            "pricing_landscape": {"type": "string"},
            "differentiation_angle": {"type": "string"},
            "saturation": {"type": "string"},
            "verdict": {"type": "string"},
            "rationale": {"type": "string"},
        },
    },
}

_WEB_TOOL = {
    **_TOOL,
    "input_schema": {
        **_TOOL["input_schema"],
        "required": _TOOL["input_schema"]["required"] + ["sources"],
        "properties": {**_TOOL["input_schema"]["properties"],
                       "sources": _SOURCE_SCHEMA},
    },
}


def competition_cache_key(candidate: Candidate, model: str, mode: str = "llm") -> str:
    raw = "\n".join([
        "competition", _COMPETITION_PROMPT_VERSION, mode, model, candidate.name,
        candidate.description, _candidate_brief(candidate),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_competition_cost_usd(candidates, model: str, cache=None,
                                  mode: str = "llm") -> float:
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC if mode == "llm" else _WEB_RUBRIC) // 4
    web = mode == "web"
    total_in = total_out = 0
    fees = 0.0
    for cand in candidates:
        if cache is not None and cache.get(
                competition_cache_key(cand, model, mode)) is not None:
            continue
        per_in = rubric_tokens + len(_candidate_brief(cand)) // 4 + 40
        total_in += int(per_in * (2.5 if web else 1))
        total_out += 380
        fees += _MAX_SEARCHES * SEARCH_PRICE_USD if web else 0.0
    return round(
        total_in / 1e6 * in_rate + total_out / 1e6 * out_rate + fees, 4)


def _clean(value: str) -> str:
    return str(value).strip()[:_MAX_TEXT]


def _clean_sources(raw) -> list[dict]:
    out = []
    for item in raw or []:
        url = str(item.get("url", "")).strip()
        title = str(item.get("title", "")).strip()
        if "." in url and title:
            out.append({"url": url[:300], "title": title[:_MAX_TEXT]})
    return out


def _note_from_data(data: dict, model: str, *, basis: str | None = None) -> dict:
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in _VERDICTS:
        raise ValueError(f"competition verdict {verdict!r} not in {_VERDICTS}")
    for key in ("named_competitors", "pricing_landscape",
                "differentiation_angle", "saturation", "rationale"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"competition note {key!r} is empty")
    note = {
        "named_competitors": _clean(data["named_competitors"]),
        "pricing_landscape": _clean(data["pricing_landscape"]),
        "differentiation_angle": _clean(data["differentiation_angle"]),
        "saturation": _clean(data["saturation"]),
        "verdict": verdict,
        "rationale": _clean(data["rationale"]),
        "basis": basis or "model knowledge, no web",
        "model": model,
        "analyzed_at": now_iso(),
    }
    if basis and basis.startswith("web search"):
        sources = _clean_sources(data.get("sources"))
        if not sources:
            raise ValueError("web competition note has no usable sources")
        note["sources"] = sources
    return note


def analyze_competition_llm(candidate: Candidate, *, client, model: str = DEFAULT_MODEL,
                            meter=None) -> dict:
    """Produce one competition note with a single Claude call. Raises
    ValueError on a malformed response."""
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _RUBRIC + UNTRUSTED_NOTE,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_competition_analysis"},
        messages=[{
            "role": "user",
            "content": wrap_untrusted(_candidate_brief(candidate)),
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _note_from_data(_tool_input(response, "record_competition_analysis"), model)


def analyze_competition_web(candidate: Candidate, *, client, model: str = DEFAULT_MODEL,
                            meter=None, max_searches: int = _MAX_SEARCHES,
                            blocked_domains=_BLOCKED_DOMAINS) -> dict:
    """Produce one competition note grounded in real web search, with a
    sources list. Server-tool errors do not raise."""
    data, searches, any_error = grounded_tool_call(
        client, model,
        system=_WEB_RUBRIC,
        brief=_candidate_brief(candidate),
        tools=[web_search_tool(max_uses=max_searches, blocked_domains=blocked_domains),
               _WEB_TOOL],
        record_name="record_competition_analysis",
        meter=meter,
    )
    suffix = " (partial)" if any_error else ""
    return _note_from_data(
        data, model, basis=f"web search{suffix}, {searches} sources")


@dataclass
class CompetitionWorker:
    """Callable candidate -> note that reuses cached notes, meters spend
    on cache misses, and stops calling the API once max_cost_usd is
    reached."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 0.5
    meter: CostMeter = field(default=None)
    cache: object = None
    refresh: bool = False
    mode: str = "llm"            # "llm" | "web"
    ceiling_hit: bool = False
    cache_hits: int = 0
    cache_misses: int = 0

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    def __call__(self, candidate: Candidate) -> dict:
        key = competition_cache_key(candidate, self.model, self.mode)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return dict(hit["note"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"competition cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        fn = analyze_competition_web if self.mode == "web" else analyze_competition_llm
        note = fn(candidate, client=self.client, model=self.model, meter=self.meter)
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"note": note, "model": self.model})
        return note


class CompetitorAnalyzerAgent(Agent):
    """Turns a candidate carried in task.payload['candidate'] into a
    competition note using task.payload['worker']."""

    role = "competitor_analyzer"
    objective = "Size up the competition for a shortlisted opportunity."
    capabilities = ("analyze_competition",)

    def run(self, task: Task) -> Result:
        candidate = task.payload.get("candidate")
        worker = task.payload.get("worker")
        if not isinstance(candidate, Candidate) or worker is None:
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload needs a Candidate and a worker",
            )
        try:
            note = worker(candidate)
        except Exception as exc:
            return Result(
                task_id=task.id, agent=self.name, status="error", error=str(exc)
            )
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={"candidate_name": candidate.name, "competition": note},
        )
