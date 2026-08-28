"""Normalize a RawSignal into a structured Opportunity.

Deterministic and keyword-based. This is an explicit placeholder for a
future analysis agent: estimates start at a neutral midpoint and are
nudged by transparent keyword rules. No LLM, no I/O.
"""

from __future__ import annotations

import re

from .opportunity import CRITERIA, SCORE_MAX, SCORE_MIN, Opportunity

NEUTRAL = 2.5
NUDGE = 1.0

# keyword -> criterion it raises. Lowercase, matched as whole words.
KEYWORD_WEIGHTS: dict[str, str] = {
    "automation": "automation_potential",
    "automate": "automation_potential",
    "no-code": "automation_potential",
    "nocode": "automation_potential",
    "api": "automation_potential",
    "self-serve": "scalability",
    "marketplace": "scalability",
    "saas": "scalability",
    "platform": "scalability",
    "open source": "demand",
    "open-source": "demand",
    "hiring": "demand",
    "customers": "demand",
    "revenue": "profit_potential",
    "profitable": "profit_potential",
    "pricing": "profit_potential",
    "free": "startup_affordability",
    "bootstrap": "startup_affordability",
    "bootstrapped": "startup_affordability",
    "open data": "legal_feasibility",
    "public data": "legal_feasibility",
    "launch": "speed_to_first_revenue",
    "mvp": "speed_to_first_revenue",
}


def _clamp(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, value))


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "untitled-signal"


def to_opportunity(signal) -> Opportunity:
    haystack = f"{signal.title} {signal.text}".lower()
    estimates = {name: NEUTRAL for name in CRITERIA}
    for keyword, criterion in KEYWORD_WEIGHTS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", haystack):
            estimates[criterion] = _clamp(estimates[criterion] + NUDGE)
    return Opportunity(
        name=_slug(signal.title),
        description=signal.title,
        source=signal.source,
        raw_ref=signal.url or signal.external_id,
        estimate_source="keyword",
        **estimates,
    )
