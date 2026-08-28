"""LLM decision policy for the one discretionary choice.

Everything in flight is handled deterministically. The only real
judgment call is at the bottom of the rule tree: keep discovering
opportunities, or stop and wait for the human to act on the queue.
This module answers that - and only that - with one small Claude call.

The action is a two-value enum the model cannot escape; a malformed
answer falls back to the deterministic rule; structural overrides
(discovery exhausted, budget capped) are applied by the caller after
this returns. No cache - the state changes every tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .llm_normalize import (
    CostMeter,
    DEFAULT_MODEL,
    UNTRUSTED_NOTE,
    _tool_input,
    wrap_untrusted,
)

_MAX_TOKENS = 300
_CHOICES = ("discover", "stop")

_RUBRIC = (
    "You run a solo operator's revenue pipeline. Everything in flight is "
    "already handled automatically; your only choice now is whether to "
    "DISCOVER more opportunities or STOP and wait for the human to act on "
    "what is queued.\n"
    "Choose 'discover' when the shortlist is thin versus the target, when "
    "discovery is stale, or when the recorded outcomes suggest the current "
    "candidates are weak.\n"
    "Choose 'stop' when there is already enough queued for the human, or "
    "when discovering more would just pile up unreviewed candidates.\n"
    "Call choose_action once with a one-sentence rationale."
)

_TOOL = {
    "name": "choose_action",
    "description": "Record the next pipeline action.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "rationale"],
        "properties": {
            "action": {"type": "string"},
            "rationale": {"type": "string"},
        },
    },
}


def summarize_for_decision(obs: dict, goal) -> dict:
    """Compact, safe state for the decision call - counts and ages only,
    no candidate detail."""
    r = obs["report"]
    c = r["status_counts"]
    retro = r.get("outcomes", {}) or {}
    return {
        "shortlisted": c["shortlisted"],
        "shortlist_target": goal.shortlist_n,
        "discovered_untriaged": c["discovered"],
        "approved": c["approved"],
        "investigating": c["investigating"],
        "validated_or_beyond": c["validated"] + c["launched"] + c["earning"],
        "target_validated": goal.target_validated,
        "last_discovery_age_days": obs["last_discovery_age_days"],
        "discovery_stale_after_days": goal.discovery_stale_days,
        "queued_for_human": [i["next_action"] for i in r["action_queue"]],
        "outcomes_ready": bool(retro.get("ready")),
        "most_predictive_criteria": list(retro.get("most_predictive", [])),
    }


def decide_llm(summary: dict, *, client, model: str = DEFAULT_MODEL, meter=None) -> tuple[str, str]:
    """Return (action, rationale). Raises ValueError on a bad response."""
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _RUBRIC + UNTRUSTED_NOTE,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "choose_action"},
        messages=[{
            "role": "user",
            "content": wrap_untrusted(json.dumps(summary, sort_keys=True)),
        }],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    data = _tool_input(response, "choose_action")
    action = str(data.get("action", "")).strip().lower()
    if action not in _CHOICES:
        raise ValueError(f"decide action {action!r} not in {_CHOICES}")
    return action, str(data.get("rationale", "")).strip()[:300]


@dataclass
class LlmDecisionPolicy:
    """Callable(summary) -> (action, rationale) | None. Returns None on a
    per-action ceiling hit or any bad response, so the caller uses the
    deterministic rule."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 0.05
    meter: CostMeter = field(default=None)
    ceiling_hit: bool = False
    calls: int = 0

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    cache_hits = 0

    @property
    def cache_misses(self) -> int:
        return self.calls

    def __call__(self, summary: dict) -> tuple[str, str] | None:
        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            return None
        try:
            action, rationale = decide_llm(
                summary, client=self.client, model=self.model, meter=self.meter
            )
        except Exception:
            return None
        self.calls += 1
        return action, rationale
