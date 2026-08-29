"""Trend Hunter - deterministic aggregation over the real candidate
corpus.

No LLM, no web, no new signals: it summarises what discovery has
already found - recurring keywords, source mix, score spread, run
count - so the operator and the dashboard can see emerging themes
without anything being invented.
"""

from __future__ import annotations

import re
from collections import Counter

from .agent import Agent
from .messages import Result, Task

_STOPWORDS = frozenset("""
a an the and or of for to in on with your you our we is are be it this that
from at as by using use used app tool free open source new get make build
platform simple based via into out up not no yes can will just like more
""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9+.#-]{2,}")


def _keywords(texts: list[str], top: int = 15) -> list[list]:
    counter: Counter[str] = Counter()
    for text in texts:
        for tok in _TOKEN.findall(text.lower()):
            if tok in _STOPWORDS or len(tok) < 3:
                continue
            counter[tok] += 1
    return [[w, n] for w, n in counter.most_common(top) if n > 1]


def build_trend_report(candidates: list[dict], runs: int) -> dict:
    texts = [
        f"{c.get('name', '')} {c.get('description', '')}" for c in candidates
    ]
    sources: Counter[str] = Counter(
        c.get("source", "") or "unknown" for c in candidates
    )
    totals = [float(c.get("total", 0.0)) for c in candidates]
    return {
        "count": len(candidates),
        "runs": int(runs),
        "keywords": _keywords(texts),
        "sources": dict(sources.most_common()),
        "score_avg": round(sum(totals) / len(totals), 2) if totals else 0.0,
        "score_max": round(max(totals), 2) if totals else 0.0,
    }


class TrendHunterAgent(Agent):
    role = "trend_hunter"
    objective = "Summarise recurring themes across discovered opportunities."
    capabilities = ("analyze_trends",)

    def run(self, task: Task) -> Result:
        candidates = task.payload.get("candidates")
        if not isinstance(candidates, list):
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['candidates'] must be a list",
            )
        report = build_trend_report(candidates, task.payload.get("runs", 0))
        return Result(
            task_id=task.id, agent=self.name, status="ok", output=report
        )
