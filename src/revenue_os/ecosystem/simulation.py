"""Revenue Simulation Mode (spec sections 21 + 39).

Runs the WHOLE ecosystem loop over N opportunities with ZERO external side
effects: no money, no messages, no ads, no orders, no accounts, no
network, no writes to the real stores. Everything is a seeded, pure
projection so we can pressure-test the discovery / verification /
profitability / strategy engines at scale.

    generate N synthetic drafts
      -> verify        (real verification.verify)
      -> evaluate      (real profitability.evaluate)
      -> select        (real strategy.select_strategy)
      -> execute-sim   (seeded Bernoulli on the projected success prob)
      -> revenue-sim   (seeded draw around the projected revenue)
      -> analytics      (learning.OutcomeStore aggregation, in memory)

Deterministic: same (n, seed) -> byte-identical report.
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import model, verification
from .learning import OutcomeStore, Outcome
from .model import estimate_value
from .profitability import evaluate as _evaluate
from .sources import SyntheticSource
from .strategy import select_strategy


@dataclass
class SimulationReport:
    n: int
    seed: int
    discovered: int = 0
    by_verification: dict = field(default_factory=dict)
    by_strategy: dict = field(default_factory=dict)
    qualified: int = 0
    human_required: int = 0            # verification said a human must act
    prepared_human_closes: int = 0     # QUALIFIED but the fleet can only prepare
    blocked: int = 0
    rejected: int = 0
    executed: int = 0            # QUALIFIED + autonomously executable end-to-end
    successes: int = 0
    failures: int = 0
    expected_revenue_eur: float = 0.0    # sum of projected revenue on executed
    simulated_revenue_eur: float = 0.0   # seeded realised revenue
    simulated_profit_eur: float = 0.0
    analytics: dict = field(default_factory=dict)
    top_categories: list = field(default_factory=list)
    top_strategies: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n": self.n, "seed": self.seed, "discovered": self.discovered,
            "by_verification": dict(self.by_verification),
            "by_strategy": dict(self.by_strategy),
            "qualified": self.qualified, "human_required": self.human_required,
            "prepared_human_closes": self.prepared_human_closes,
            "blocked": self.blocked, "rejected": self.rejected,
            "executed": self.executed, "successes": self.successes,
            "failures": self.failures,
            "expected_revenue_eur": round(self.expected_revenue_eur, 2),
            "simulated_revenue_eur": round(self.simulated_revenue_eur, 2),
            "simulated_profit_eur": round(self.simulated_profit_eur, 2),
            "analytics": self.analytics,
            "top_categories": self.top_categories,
            "top_strategies": self.top_strategies,
        }


#: opportunity types the fleet can execute end-to-end without a human
_AUTONOMOUS_TYPES = frozenset({model.TYPE_DIGITAL_PRODUCT, model.TYPE_CONTENT})


def simulate(*, n: int = 1000, seed: int = 42,
             priority_weights: dict | None = None) -> SimulationReport:
    rng = random.Random(seed)
    report = SimulationReport(n=int(n), seed=int(seed))

    # generate n synthetic drafts (deterministic; rotate the archetype space)
    drafts = []
    per_batch = 200
    s = 0
    while len(drafts) < n:
        batch = SyntheticSource(seed=seed + s).discover(min(per_batch, n - len(drafts)))
        if not batch:
            break
        drafts.extend(batch)
        s += 1
    drafts = drafts[:n]
    report.discovered = len(drafts)

    outcomes = OutcomeStore(Path(tempfile.gettempdir()) / "_eco_sim_never_written.json")

    for draft in drafts:
        verdict = verification.verify(draft)
        vs = verdict.status
        report.by_verification[vs] = report.by_verification.get(vs, 0) + 1
        if vs == "REJECTED":
            report.rejected += 1
            continue
        if vs == "BLOCKED":
            report.blocked += 1
            continue
        if vs == "HUMAN_REQUIRED":
            report.human_required += 1
            continue
        if vs != "QUALIFIED":
            continue
        report.qualified += 1

        prof = _evaluate(draft)
        sel = select_strategy(draft, prof, priority_weights=priority_weights)
        strat = sel.recommended or "NONE"
        report.by_strategy[strat] = report.by_strategy.get(strat, 0) + 1
        if not sel.recommended:
            continue

        exp_rev = estimate_value(prof.expected_revenue)
        exp_cost = estimate_value(prof.expected_cost)
        exp_time = estimate_value(prof.expected_time_hours)
        p_success = estimate_value(prof.success_probability)

        autonomous = draft.opportunity_type in _AUTONOMOUS_TYPES
        if not autonomous:
            # fleet prepares, human closes - not an autonomous execution
            report.prepared_human_closes += 1
            continue

        report.executed += 1
        report.expected_revenue_eur += exp_rev

        success = rng.random() < p_success
        if success:
            realised = round(exp_rev * rng.uniform(0.7, 1.3), 2)
            report.successes += 1
        else:
            realised = 0.0
            report.failures += 1
        realised_cost = round(exp_cost * rng.uniform(0.8, 1.2), 2)
        report.simulated_revenue_eur += realised
        report.simulated_profit_eur += realised - realised_cost

        outcomes.record(Outcome(
            opportunity_id=draft.dedup_key(), strategy=strat,
            source=draft.source_meta.source if draft.source_meta else "synthetic",
            category=draft.category, opportunity_type=draft.opportunity_type,
            execution_time_hours=round(exp_time * rng.uniform(0.8, 1.4), 3),
            cost_eur=realised_cost, revenue_eur=realised, success=success,
            failure_reason="" if success else "sim: below success probability"))

    report.analytics = outcomes.aggregate()
    by_cat = report.analytics.get("by_category", {})
    by_strat = report.analytics.get("by_strategy", {})
    report.top_categories = sorted(
        ({"key": k, **v} for k, v in by_cat.items()),
        key=lambda d: (-d["profit"], -d["win_rate"]))[:5]
    report.top_strategies = sorted(
        ({"key": k, **v} for k, v in by_strat.items()),
        key=lambda d: (-d["profit"], -d["win_rate"]))[:5]
    return report
