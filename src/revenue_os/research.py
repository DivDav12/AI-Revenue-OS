"""Opportunity research (opt-in) - the second specialized agent.

Given a shortlisted candidate, one Claude call produces a structured
research note: competition read, demand evidence, legal flags, and a
proceed / caution / avoid verdict. No web access - the note is a
hypothesis from the signal and the model's general knowledge, stored
with basis = "model knowledge, no web".

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
    UNTRUSTED_NOTE,
    _FALLBACK_PRICE,
    _PRICES,
    _tool_input,
    wrap_untrusted,
)
from .llm_plan import _candidate_brief
from .messages import Result, Task
from .store import Candidate, now_iso

_RESEARCH_PROMPT_VERSION = "1"
_MAX_TOKENS = 700
_MAX_TEXT = 400
_VERDICTS = ("proceed", "caution", "avoid")

_RUBRIC = (
    "You research a revenue opportunity a solo operator is considering. "
    "You have NO web access - base your read on what the signal implies "
    "and your general knowledge, and say plainly where you are uncertain.\n"
    "competition: who else serves this need and how crowded it looks.\n"
    "demand_evidence: what suggests real willingness to pay (or that it is "
    "thin).\n"
    "legal_flags: regulatory, IP, terms-of-service, or data-privacy concerns "
    "- or 'none apparent'.\n"
    "verdict: proceed, caution, or avoid.\n"
    "Call record_research once with a one-sentence rationale."
)

_TOOL = {
    "name": "record_research",
    "description": "Record the research note for this opportunity.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["competition", "demand_evidence", "legal_flags",
                     "verdict", "rationale"],
        "properties": {
            "competition": {"type": "string"},
            "demand_evidence": {"type": "string"},
            "legal_flags": {"type": "string"},
            "verdict": {"type": "string"},
            "rationale": {"type": "string"},
        },
    },
}


def research_cache_key(candidate: Candidate, model: str) -> str:
    raw = "\n".join([
        "research", _RESEARCH_PROMPT_VERSION, model, candidate.name,
        candidate.description, _candidate_brief(candidate),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_research_cost_usd(candidates, model: str, cache=None) -> float:
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    rubric_tokens = len(_RUBRIC) // 4
    total_in = total_out = 0
    for cand in candidates:
        if cache is not None and cache.get(research_cache_key(cand, model)) is not None:
            continue
        total_in += rubric_tokens + len(_candidate_brief(cand)) // 4 + 40
        total_out += 350
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate, 4)


def _clean(value: str) -> str:
    return str(value).strip()[:_MAX_TEXT]


def _note_from_data(data: dict, model: str) -> dict:
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in _VERDICTS:
        raise ValueError(f"research verdict {verdict!r} not in {_VERDICTS}")
    for key in ("competition", "demand_evidence", "legal_flags", "rationale"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"research note {key!r} is empty")
    return {
        "competition": _clean(data["competition"]),
        "demand_evidence": _clean(data["demand_evidence"]),
        "legal_flags": _clean(data["legal_flags"]),
        "verdict": verdict,
        "rationale": _clean(data["rationale"]),
        "basis": "model knowledge, no web",
        "model": model,
        "researched_at": now_iso(),
    }


def research_candidate_llm(candidate: Candidate, *, client, model: str = DEFAULT_MODEL,
                           meter=None) -> dict:
    """Produce one research note with a single Claude call. Raises
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
        tool_choice={"type": "tool", "name": "record_research"},
        messages=[{
            "role": "user",
            "content": wrap_untrusted(_candidate_brief(candidate)),
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return _note_from_data(_tool_input(response, "record_research"), model)


@dataclass
class ResearchWorker:
    """Callable candidate -> note that reuses cached notes, meters spend
    on cache misses, and stops calling the API once max_cost_usd is
    reached."""

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

    def __call__(self, candidate: Candidate) -> dict:
        key = research_cache_key(candidate, self.model)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return dict(hit["note"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"research cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}"
            )
        note = research_candidate_llm(
            candidate, client=self.client, model=self.model, meter=self.meter
        )
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"note": note, "model": self.model})
        return note


class ResearchAgent(Agent):
    """Turns a candidate carried in task.payload['candidate'] into a
    research note using task.payload['worker']."""

    role = "researcher"
    objective = "Research a shortlisted revenue opportunity."
    capabilities = ("research",)

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
            output={"candidate_name": candidate.name, "research": note},
        )
