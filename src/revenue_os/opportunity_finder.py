"""Opportunity Finder - decides which scored opportunities are worth
pursuing and which to shortlist.

Pure and deterministic: given the evaluator's scores it applies the
min-score gate, ranks by total, and names the top N for the shortlist.
It persists nothing and crosses no gate - run_discovery_cycle applies
the decision to the store.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task


class OpportunityFinderAgent(Agent):
    role = "opportunity_finder"
    objective = "Rank scored opportunities and select a shortlist."
    capabilities = ("select",)

    def run(self, task: Task) -> Result:
        scored = task.payload.get("scored")
        if not isinstance(scored, list):
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['scored'] must be a list of {name,total}",
            )
        try:
            min_score = float(task.payload.get("min_score", 0.0))
            shortlist_n = max(0, int(task.payload.get("shortlist_n", 3)))
        except (TypeError, ValueError) as exc:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error=str(exc))

        ranked = sorted(
            scored, key=lambda s: s.get("total", 0.0), reverse=True
        )
        kept = [s["name"] for s in ranked if s.get("total", 0.0) >= min_score]
        dropped = [s["name"] for s in ranked if s.get("total", 0.0) < min_score]
        shortlist = kept[:shortlist_n]
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={
                "ranking": [s["name"] for s in ranked],
                "kept": kept,
                "shortlist": shortlist,
                "dropped": dropped,
            },
        )
