"""Learning loop (spec section 22).

Deterministic, explainable feedback - NOT ML. After each opportunity
settles we record what happened; `aggregate()` rolls it up by
category / strategy / source / channel; `priority_weights()` turns those
rollups into multipliers the discovery and strategy engines consume next
time. Every weight is a plain ratio of settled outcomes, so a human can
read the store and reproduce the number by hand.

    file : <data-dir>/ecosystem_outcomes.json   (append-only JSON list)
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..store import now_iso

_OUTCOMES = "ecosystem_outcomes.json"
_MIN_SETTLED = 5          # below this, no weighting (avoid over-fitting noise)
_WEIGHT_FLOOR, _WEIGHT_CEIL = 0.5, 1.6


@dataclass
class Outcome:
    opportunity_id: str
    strategy: str = ""
    source: str = ""
    category: str = ""
    opportunity_type: str = ""
    distribution_channel: str = ""
    execution_time_hours: float = 0.0
    cost_eur: float = 0.0
    revenue_eur: float = 0.0
    success: bool = False
    failure_reason: str = ""
    settled: bool = True
    recorded_at: str = field(default_factory=now_iso)

    @property
    def profit_eur(self) -> float:
        return round(self.revenue_eur - self.cost_eur, 2)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "opportunity_id", "strategy", "source", "category",
            "opportunity_type", "distribution_channel", "execution_time_hours",
            "cost_eur", "revenue_eur", "success", "failure_reason", "settled",
            "recorded_at")}
        d["profit_eur"] = self.profit_eur
        return d


class OutcomeStore:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []

    @classmethod
    def load(cls, data_dir) -> "OutcomeStore":
        s = cls(Path(data_dir) / _OUTCOMES)
        if s.path.exists():
            try:
                raw = json.loads(s.path.read_text(encoding="utf-8"))
                s._rows = [dict(r) for r in raw] if isinstance(raw, list) else []
            except json.JSONDecodeError:
                s._rows = []
        return s

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._rows, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def record(self, outcome: Outcome) -> dict:
        row = outcome.to_dict()
        self._rows.append(row)
        return row

    def rows(self) -> list[dict]:
        return list(self._rows)

    # --- rollups -----------------------------------------------------
    def aggregate(self) -> dict:
        settled = [r for r in self._rows if r.get("settled")]

        def _by(field_name: str) -> dict:
            groups: dict[str, dict] = {}
            for r in settled:
                k = str(r.get(field_name) or "unknown")
                g = groups.setdefault(k, {"n": 0, "wins": 0, "revenue": 0.0,
                                          "profit": 0.0, "hours": 0.0})
                g["n"] += 1
                g["wins"] += 1 if r.get("success") else 0
                g["revenue"] += float(r.get("revenue_eur") or 0.0)
                g["profit"] += float(r.get("profit_eur") or 0.0)
                g["hours"] += float(r.get("execution_time_hours") or 0.0)
            for g in groups.values():
                g["win_rate"] = round(g["wins"] / g["n"], 3) if g["n"] else 0.0
                g["revenue"] = round(g["revenue"], 2)
                g["profit"] = round(g["profit"], 2)
                g["profit_per_hour"] = (round(g["profit"] / g["hours"], 2)
                                        if g["hours"] > 0 else 0.0)
            return groups

        wins = sum(1 for r in settled if r.get("success"))
        return {
            "settled": len(settled),
            "wins": wins,
            "overall_win_rate": round(wins / len(settled), 3) if settled else 0.0,
            "total_revenue_eur": round(sum(float(r.get("revenue_eur") or 0.0)
                                           for r in settled), 2),
            "total_profit_eur": round(sum(float(r.get("profit_eur") or 0.0)
                                          for r in settled), 2),
            "by_category": _by("category"),
            "by_strategy": _by("strategy"),
            "by_source": _by("source"),
            "by_channel": _by("distribution_channel"),
            "by_type": _by("opportunity_type"),
        }

    def priority_weights(self) -> dict:
        """Deterministic multipliers keyed by category / strategy / source.
        A group's weight = its win_rate divided by the overall win_rate,
        clamped to [0.5, 1.6]. Groups without enough settled data get 1.0.
        """
        agg = self.aggregate()
        if agg["settled"] < _MIN_SETTLED:
            return {"_note": f"need >= {_MIN_SETTLED} settled outcomes "
                    f"({agg['settled']} so far) - no weighting applied",
                    "category": {}, "strategy": {}, "source": {}}
        overall = agg["overall_win_rate"] or 0.001

        def _w(groups: dict) -> dict:
            out = {}
            for k, g in groups.items():
                if g["n"] < 3:
                    continue
                raw = g["win_rate"] / overall if overall else 1.0
                out[k] = round(max(_WEIGHT_FLOOR, min(_WEIGHT_CEIL, raw)), 3)
            return out

        return {
            "category": _w(agg["by_category"]),
            "strategy": {k.lower(): v for k, v in _w(agg["by_strategy"]).items()},
            "source": _w(agg["by_source"]),
            "based_on_settled": agg["settled"],
        }


def record_outcome(data_dir, outcome: Outcome) -> dict:
    s = OutcomeStore.load(data_dir)
    row = s.record(outcome)
    s.save()
    return row


# ---------------------------------------------------------------------------
# per-source quality metrics (spec: discovery quality layer, section 6) -
# purely derived from persisted state, no new stored counters. Deterministic
# aggregation, not ML. Feeds future discovery/source prioritisation - not
# wired into scoring yet, this pass only makes the numbers real and visible.
# ---------------------------------------------------------------------------

def source_quality(data_dir) -> dict:
    """discovered/qualified/verified/executable come from opportunity_store
    records (one row per discovered opportunity); human_submitted/
    successful/paid/revenue/profit/win_rate come from the settled
    OutcomeStore (today populated by ecosystem.pipeline.record_task_outcome
    for the TASK strategy - a PRODUCT-path settlement feed is future work)."""
    from ..opportunity_store import load_opportunities
    from . import model

    store = load_opportunities(data_dir)
    outcomes = OutcomeStore.load(data_dir).rows()
    by_source: dict[str, dict] = {}

    def _row(name: str) -> dict:
        return by_source.setdefault(name, {
            "discovered": 0, "qualified": 0, "verified": 0, "executable": 0,
            "human_submitted": 0, "successful": 0, "paid": 0,
            "revenue_eur": 0.0, "profit_eur": 0.0,
        })

    for rec in store.all():
        d = rec.get("discovery") or {}
        source = str(d.get("source") or "unknown")
        r = _row(source)
        r["discovered"] += 1
        vstatus = (d.get("verification") or {}).get("status", "")
        if vstatus == model.V_QUALIFIED:
            r["qualified"] += 1
            r["verified"] += 1
        elif vstatus == model.V_VERIFIED:
            r["verified"] += 1
        if (rec.get("execution") or {}).get("accepted"):
            r["executable"] += 1

    for o in outcomes:
        source = str(o.get("source") or "unknown")
        r = _row(source)
        r["human_submitted"] += 1
        revenue = float(o.get("revenue_eur") or 0.0)
        if o.get("success"):
            r["successful"] += 1
            if revenue > 0:
                r["paid"] += 1
        r["revenue_eur"] += revenue
        r["profit_eur"] += float(o.get("profit_eur") or 0.0)

    for r in by_source.values():
        r["win_rate"] = (round(r["successful"] / r["human_submitted"], 3)
                         if r["human_submitted"] else 0.0)
        r["revenue_eur"] = round(r["revenue_eur"], 2)
        r["profit_eur"] = round(r["profit_eur"], 2)

    return {"by_source": by_source}
