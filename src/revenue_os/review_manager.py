"""Review Manager (#20, support cluster) - deterministic feedback read.

Aggregates real feedback the caller supplies (reviews, support cases,
notes on delivered plans). It fabricates nothing: no feedback in means
empty results out. Findings are routed back to the product / offer /
copy / quality agents.
"""

from __future__ import annotations

import re
from collections import Counter

from .agent import Agent
from .messages import Result, Task

_NEG = ("bad", "poor", "disappointed", "waste", "refund", "useless", "generic",
        "not helpful", "confusing", "slow", "late")
_POS = ("great", "helpful", "excellent", "clear", "worth", "love", "useful",
        "actionable", "fast", "thorough")
_WISH = re.compile(r"\b(wish|should|would be better|need more|more detail|add)\b", re.I)

_STOP = frozenset("the a an and or of to in is it for this that i you my with was "
                  "not but be have has plan more".split())
_TOKEN = re.compile(r"[a-z][a-z0-9+.#-]{2,}")

_FEEDS_INTO = ("product_researcher", "designer", "copywriter", "quality_control")


def _rating(item: dict):
    try:
        return float(item.get("rating"))
    except (TypeError, ValueError):
        return None


def build_review_report(feedback, support_cases=None, delivered_plans=None) -> dict:
    items = [f for f in (feedback or []) if isinstance(f, dict) and str(f.get("text", "")).strip()]
    texts = [str(f["text"]) for f in items]

    counter: Counter[str] = Counter()
    for t in texts:
        for tok in _TOKEN.findall(t.lower()):
            if tok not in _STOP and len(tok) > 2:
                counter[tok] += 1
    themes = [[w, n] for w, n in counter.most_common(10) if n > 1]

    complaints, positive, improvements = [], [], []
    for f, t in zip(items, texts):
        r = _rating(f)
        low = t.lower()
        if (r is not None and r <= 2) or any(k in low for k in _NEG):
            complaints.append(t)
        if (r is not None and r >= 4) or any(k in low for k in _POS):
            positive.append(t)
        if _WISH.search(t):
            improvements.append(t)

    n = len(items)
    ratio = len(complaints) / n if n else 0.0
    priority = "high" if ratio >= 0.34 else ("medium" if complaints else "low")

    return {
        "reviewed_count": n,
        "support_cases_seen": len(support_cases or []),
        "delivered_plans_seen": len(delivered_plans or []),
        "themes": themes,
        "complaints": complaints,
        "positive_signals": positive,
        "improvement_requests": improvements,
        "priority": priority,
        "feeds_into": list(_FEEDS_INTO),
        "fabricated": 0,
        "note": "aggregated from supplied feedback only; nothing invented",
    }


class ReviewManagerAgent(Agent):
    role = "review_manager"
    objective = "Cluster real customer feedback into themes and priorities."
    capabilities = ("manage_reviews",)

    def run(self, task: Task) -> Result:
        feedback = task.payload.get("feedback")
        if not isinstance(feedback, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['feedback'] must be a list")
        for key in ("support_cases", "delivered_plans"):
            if key in task.payload and not isinstance(task.payload[key], list):
                return Result(task_id=task.id, agent=self.name, status="error",
                              error=f"payload['{key}'] must be a list when given")
        out = build_review_report(
            feedback, task.payload.get("support_cases"),
            task.payload.get("delivered_plans"),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
