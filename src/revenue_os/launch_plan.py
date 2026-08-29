"""Customer Launch Plan drafting (opt-in) - the fulfillment writer.

Given a paid, human-reviewed intake submission, ONE Claude call (web-
grounded by default) drafts the personalised plan: business analysis,
ideal customer profile, real acquisition opportunities with sources, a
prioritised strategy, a 14-day action plan, 2-3 outreach templates, and
a next-steps checklist. A deterministic quality-control pass then
validates the shape before the draft is stored.

Draft only. It changes no candidate status, moves no money, and sends
nothing to the customer. The human owner approves the draft
(`plan-approve`) before it is rendered for delivery.

Reuses the evaluator's client / meter / ceiling / cache machinery and
`grounded_tool_call` from llm_normalize (same web-search plumbing as
research.py / competition.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .agent import Agent
from .intake import INTAKE_FIELDS
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
from .messages import Result, Task
from .research import _BLOCKED_DOMAINS, _MAX_SEARCHES, _SOURCE_SCHEMA
from .store import now_iso

_PLAN_PROMPT_VERSION = "1"
_MAX_TOKENS = 3200
_MAX_TEXT = 1200
_ACTION_DAYS = 14
_MIN_OPPORTUNITIES = 5
_MAX_OPPORTUNITIES = 10
_MIN_TEMPLATES = 2
_MAX_TEMPLATES = 3
_MIN_NEXT_STEPS = 3

# phrases that would make the plan a promise of results rather than a strategy
_BANNED = ("guarantee", "guaranteed", "guarantees")

_SECTIONS = (
    "business_analysis", "ideal_customer", "acquisition_opportunities",
    "prioritized_strategy", "action_plan_14_day", "outreach_templates",
    "next_steps",
)

_SECTION_GUIDE = (
    "business_analysis: what they sell, the problem it solves, the core "
    "value proposition.\n"
    "ideal_customer: the most likely customer, their relevant "
    "characteristics, and where that audience actually gathers.\n"
    "acquisition_opportunities: 5-10 concrete opportunities - a specific "
    "platform, community, channel, or approach each, with why it fits THIS "
    "business and a realistic first step. No generic 'use social media'.\n"
    "prioritized_strategy: rank the opportunities by expected usefulness, "
    "name what to try first, and explain the reasoning.\n"
    "action_plan_14_day: exactly 14 entries (day 1..14), each with a focus "
    "and concrete actions.\n"
    "outreach_templates: 2-3 templates adapted to this business (a cold "
    "message, a community post, etc.).\n"
    "next_steps: a short checklist of the highest-impact actions to take "
    "immediately.\n"
)

_RUBRIC = (
    "You write a personalised Customer Launch Plan for a solo operator who "
    "has PAID for research and strategy - not for guaranteed customers. Use "
    "ONLY the facts they gave you plus your general knowledge. Never invent "
    "their traction, revenue, customers, or testimonials. Never promise or "
    "imply guaranteed results.\n"
    + _SECTION_GUIDE
    + "Call record_launch_plan exactly once."
)

_WEB_RUBRIC = (
    "You write a personalised Customer Launch Plan for a solo operator who "
    "has PAID for research and strategy - not for guaranteed customers. "
    "Search the web to find real, currently-active places this specific "
    "business could reach customers (named communities, directories, "
    "marketplaces, newsletters, events, partners). Use ONLY the operator's "
    "own facts plus what you find. Never invent their traction, revenue, "
    "customers, or testimonials. Never promise or imply guaranteed results.\n"
    + _SECTION_GUIDE
    + "Call record_launch_plan once with a sources array (url + title) for "
    "every page you relied on."
)

_PLAN_PROPERTIES = {
    "business_analysis": {
        "type": "object", "additionalProperties": False,
        "required": ["what_sold", "problem_solved", "value_proposition"],
        "properties": {
            "what_sold": {"type": "string"},
            "problem_solved": {"type": "string"},
            "value_proposition": {"type": "string"},
        },
    },
    "ideal_customer": {
        "type": "object", "additionalProperties": False,
        "required": ["profile", "characteristics", "where_to_reach"],
        "properties": {
            "profile": {"type": "string"},
            "characteristics": {"type": "string"},
            "where_to_reach": {"type": "string"},
        },
    },
    "acquisition_opportunities": {
        "type": "array",
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "channel", "why_relevant", "first_step"],
            "properties": {
                "name": {"type": "string"},
                "channel": {"type": "string"},
                "why_relevant": {"type": "string"},
                "first_step": {"type": "string"},
            },
        },
    },
    "prioritized_strategy": {
        "type": "object", "additionalProperties": False,
        "required": ["ranking", "start_with", "reasoning"],
        "properties": {
            "ranking": {"type": "array", "items": {"type": "string"}},
            "start_with": {"type": "string"},
            "reasoning": {"type": "string"},
        },
    },
    "action_plan_14_day": {
        "type": "array",
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["day", "focus", "actions"],
            "properties": {
                "day": {"type": "integer"},
                "focus": {"type": "string"},
                "actions": {"type": "string"},
            },
        },
    },
    "outreach_templates": {
        "type": "array",
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "context", "body"],
            "properties": {
                "name": {"type": "string"},
                "context": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    },
    "next_steps": {"type": "array", "items": {"type": "string"}},
}

_TOOL = {
    "name": "record_launch_plan",
    "description": "Record the personalised Customer Launch Plan.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": list(_SECTIONS),
        "properties": _PLAN_PROPERTIES,
    },
}

_WEB_TOOL = {
    **_TOOL,
    "input_schema": {
        **_TOOL["input_schema"],
        "required": _TOOL["input_schema"]["required"] + ["sources"],
        "properties": {**_PLAN_PROPERTIES, "sources": _SOURCE_SCHEMA},
    },
}


def intake_brief(fields: dict) -> str:
    """The buyer's own answers, as a plain brief. Call sites fence this as
    untrusted before sending it to the model."""
    lines = []
    for key, label in INTAKE_FIELDS:
        value = str(fields.get(key, "")).strip() or "(not provided)"
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def launch_plan_cache_key(order_id: str, fields: dict, model: str,
                          mode: str = "web") -> str:
    raw = "\n".join([
        "launch_plan", _PLAN_PROMPT_VERSION, mode, model, str(order_id),
        json.dumps({k: fields.get(k, "") for k, _ in INTAKE_FIELDS},
                   sort_keys=True),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_launch_plan_cost_usd(fields: dict, model: str, *,
                                  mode: str = "web") -> float:
    in_rate, out_rate = _PRICES.get(model, _FALLBACK_PRICE)
    web = mode == "web"
    rubric_tokens = len(_WEB_RUBRIC if web else _RUBRIC) // 4
    brief_tokens = len(intake_brief(fields)) // 4 + 40
    total_in = int((rubric_tokens + brief_tokens) * (3.0 if web else 1.0))
    total_out = 2200
    fees = _MAX_SEARCHES * SEARCH_PRICE_USD if web else 0.0
    return round(total_in / 1e6 * in_rate + total_out / 1e6 * out_rate + fees, 4)


# --- pure shaping + quality control -----------------------------------

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


def _obj(data: dict, key: str, sub: tuple[str, ...]) -> dict:
    raw = data.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"launch plan {key!r} is missing or not an object")
    out = {}
    for name in sub:
        text = str(raw.get(name, "")).strip()
        if not text:
            raise ValueError(f"launch plan {key}.{name} is empty")
        out[name] = _clean(text)
    return out


def _plan_from_data(data: dict, model: str, *, basis: str) -> dict:
    plan = {
        "business_analysis": _obj(data, "business_analysis",
                                  ("what_sold", "problem_solved",
                                   "value_proposition")),
        "ideal_customer": _obj(data, "ideal_customer",
                               ("profile", "characteristics", "where_to_reach")),
        "prioritized_strategy": {},
        "acquisition_opportunities": [],
        "action_plan_14_day": [],
        "outreach_templates": [],
        "next_steps": [],
        "basis": basis,
        "model": model,
        "drafted_at": now_iso(),
    }

    for item in data.get("acquisition_opportunities") or []:
        if not isinstance(item, dict):
            continue
        row = {k: str(item.get(k, "")).strip()
               for k in ("name", "channel", "why_relevant", "first_step")}
        if all(row.values()):
            plan["acquisition_opportunities"].append(
                {k: _clean(v) for k, v in row.items()})

    strat = data.get("prioritized_strategy")
    if not isinstance(strat, dict):
        raise ValueError("launch plan prioritized_strategy is missing")
    ranking = [str(x).strip() for x in (strat.get("ranking") or []) if str(x).strip()]
    for name in ("start_with", "reasoning"):
        if not str(strat.get(name, "")).strip():
            raise ValueError(f"launch plan prioritized_strategy.{name} is empty")
    plan["prioritized_strategy"] = {
        "ranking": [r[:_MAX_TEXT] for r in ranking],
        "start_with": _clean(strat["start_with"]),
        "reasoning": _clean(strat["reasoning"]),
    }

    for item in data.get("action_plan_14_day") or []:
        if not isinstance(item, dict):
            continue
        try:
            day = int(item.get("day"))
        except (TypeError, ValueError):
            continue
        focus = str(item.get("focus", "")).strip()
        actions = str(item.get("actions", "")).strip()
        if focus and actions:
            plan["action_plan_14_day"].append(
                {"day": day, "focus": _clean(focus), "actions": _clean(actions)})

    for item in data.get("outreach_templates") or []:
        if not isinstance(item, dict):
            continue
        row = {k: str(item.get(k, "")).strip() for k in ("name", "context", "body")}
        if row["name"] and row["body"]:
            plan["outreach_templates"].append({
                "name": _clean(row["name"]),
                "context": _clean(row["context"]),
                "body": _clean(row["body"]),
            })

    plan["next_steps"] = [
        _clean(s) for s in (str(x).strip() for x in data.get("next_steps") or [])
        if s
    ]

    if basis.startswith("web search"):
        sources = _clean_sources(data.get("sources"))
        if not sources:
            raise ValueError("web launch plan has no usable sources")
        plan["sources"] = sources

    return plan


def qc_plan(plan: dict) -> dict:
    """Deterministic quality control. Raises ValueError listing every
    failure; on success stamps `qc` and returns the plan."""
    problems: list[str] = []

    days = [d["day"] for d in plan["action_plan_14_day"]]
    if sorted(days) != list(range(1, _ACTION_DAYS + 1)):
        problems.append(
            f"action_plan_14_day must have days 1..{_ACTION_DAYS} exactly "
            f"(got {sorted(days)})")

    n_opp = len(plan["acquisition_opportunities"])
    if not _MIN_OPPORTUNITIES <= n_opp <= _MAX_OPPORTUNITIES:
        problems.append(
            f"need {_MIN_OPPORTUNITIES}-{_MAX_OPPORTUNITIES} acquisition "
            f"opportunities (got {n_opp})")

    n_tpl = len(plan["outreach_templates"])
    if not _MIN_TEMPLATES <= n_tpl <= _MAX_TEMPLATES:
        problems.append(
            f"need {_MIN_TEMPLATES}-{_MAX_TEMPLATES} outreach templates "
            f"(got {n_tpl})")

    if len(plan["next_steps"]) < _MIN_NEXT_STEPS:
        problems.append(f"need at least {_MIN_NEXT_STEPS} next steps")

    if not plan["prioritized_strategy"].get("ranking"):
        problems.append("prioritized_strategy.ranking is empty")

    haystack = json.dumps(plan, ensure_ascii=False).lower()
    hit = [w for w in _BANNED if w in haystack]
    if hit:
        problems.append(f"plan contains promise language: {', '.join(sorted(set(hit)))}")

    if plan["basis"].startswith("web search") and not plan.get("sources"):
        problems.append("web plan has no sources")

    if problems:
        raise ValueError("quality control failed: " + "; ".join(problems))

    plan["qc"] = {
        "passed": True,
        "checks": [
            f"{_ACTION_DAYS}-day plan complete",
            f"{n_opp} acquisition opportunities",
            f"{n_tpl} outreach templates",
            f"{len(plan['next_steps'])} next steps",
            "no guarantee/promise language",
            (f"{len(plan.get('sources', []))} sources"
             if plan["basis"].startswith("web search") else "no-web mode"),
        ],
        "checked_at": now_iso(),
    }
    return plan


# --- the two call paths ----------------------------------------------

def draft_plan_llm(fields: dict, *, client, model: str = DEFAULT_MODEL,
                   meter=None) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text", "text": _RUBRIC + UNTRUSTED_NOTE,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_launch_plan"},
        messages=[{"role": "user", "content": wrap_untrusted(intake_brief(fields))}],
    )
    if meter is not None:
        meter.add(getattr(response, "usage", None))
    return qc_plan(_plan_from_data(
        _tool_input(response, "record_launch_plan"), model,
        basis="model knowledge, no web"))


def draft_plan_web(fields: dict, *, client, model: str = DEFAULT_MODEL,
                   meter=None, max_searches: int = _MAX_SEARCHES,
                   blocked_domains=_BLOCKED_DOMAINS) -> dict:
    data, searches, any_error = grounded_tool_call(
        client, model,
        system=_WEB_RUBRIC,
        brief=intake_brief(fields),
        tools=[web_search_tool(max_uses=max_searches, blocked_domains=blocked_domains),
               _WEB_TOOL],
        record_name="record_launch_plan",
        meter=meter,
    )
    suffix = " (partial)" if any_error else ""
    return qc_plan(_plan_from_data(
        data, model, basis=f"web search{suffix}, {searches} sources"))


@dataclass
class LaunchPlanWorker:
    """Callable (order_id, intake_fields) -> plan that reuses cached
    plans, meters spend, and stops calling the API once max_cost_usd is
    reached."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 1.0
    meter: CostMeter = field(default=None)
    cache: object = None
    refresh: bool = False
    mode: str = "web"            # "web" | "llm"
    ceiling_hit: bool = False
    cache_hits: int = 0
    cache_misses: int = 0

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    def __call__(self, order_id: str, fields: dict) -> dict:
        key = launch_plan_cache_key(order_id, fields, self.model, self.mode)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return dict(hit["plan"])

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"launch-plan cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}")

        fn = draft_plan_web if self.mode == "web" else draft_plan_llm
        plan = fn(fields, client=self.client, model=self.model, meter=self.meter)
        self.cache_misses += 1
        if self.cache is not None:
            self.cache.put(key, {"plan": plan, "model": self.model})
        return plan


class LaunchPlanAgent(Agent):
    """Turns a paid, reviewed intake submission carried in
    task.payload['intake'] into a Customer Launch Plan draft using
    task.payload['worker']."""

    role = "fulfillment_writer"
    objective = "Draft a personalised Customer Launch Plan from a paid intake."
    capabilities = ("draft_launch_plan",)

    def run(self, task: Task) -> Result:
        entry = task.payload.get("intake")
        worker = task.payload.get("worker")
        if not isinstance(entry, dict) or not isinstance(entry.get("fields"), dict) \
                or worker is None:
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload needs an intake dict (with fields) and a worker",
            )
        try:
            plan = worker(entry["order_id"], entry["fields"])
        except Exception as exc:
            return Result(
                task_id=task.id, agent=self.name, status="error", error=str(exc)
            )
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={"order_id": entry["order_id"], "plan": plan},
        )
