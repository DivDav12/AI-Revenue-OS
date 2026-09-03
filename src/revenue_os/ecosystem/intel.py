"""Read-only ecosystem intelligence for JARVIS + the dashboard (spec 26).

Pure aggregation over the persisted stores - opportunity_store,
ecosystem_discovery log, ecosystem_outcomes. No fabricated metrics: every
number is counted from real state.
"""

from __future__ import annotations

from pathlib import Path

from ..opportunity_store import load_opportunities
from . import model
from .discovery import latest_discovery
from .learning import OutcomeStore


def _vstatus(rec: dict) -> str:
    return ((rec.get("discovery") or {}).get("verification") or {}).get(
        "status", model.V_DISCOVERED)


def ecosystem_status(data_dir) -> dict:
    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    recs = store.all()

    real = [r for r in recs if r.get("origin") == "real"]
    synth = [r for r in recs if r.get("origin") != "real"]

    by_v: dict[str, int] = {}
    by_strat: dict[str, int] = {}
    by_type: dict[str, int] = {}
    human_actions: list[dict] = []
    for r in recs:
        v = _vstatus(r)
        by_v[v] = by_v.get(v, 0) + 1
        s = (r.get("strategy") or {}).get("recommended") or ""
        if s:
            by_strat[s] = by_strat.get(s, 0) + 1
        t = (r.get("discovery") or {}).get("opportunity_type") or ""
        if t:
            by_type[t] = by_type.get(t, 0) + 1
        d = r.get("discovery") or {}
        if v == model.V_HUMAN_REQUIRED or d.get("policy_status") == model.POLICY_HUMAN_SETUP_REQUIRED:
            human_actions.append({
                "opportunity_id": r["id"], "title": r.get("title", "")[:70],
                "reason": (d.get("verification") or {}).get("reasons", ["human step required"])[0]
                if d.get("verification") else "human step required",
                "source": d.get("source", ""),
                "policy_status": d.get("policy_status", ""),
            })
        plan = (r.get("strategy") or {}).get("plan") or {}
        if plan.get("next_step_class") == "HUMAN_REQUIRED":
            human_actions.append({
                "opportunity_id": r["id"], "title": r.get("title", "")[:70],
                "reason": plan.get("note", "external step needs a human"),
                "strategy": (r.get("strategy") or {}).get("recommended", ""),
            })

    outcomes = OutcomeStore.load(data_dir).aggregate()

    return {
        "discovery": {
            "total": len(recs),
            "real": len(real),
            "synthetic": len(synth),
            "by_verification": by_v,
            "qualified": by_v.get(model.V_QUALIFIED, 0),
            "rejected": by_v.get(model.V_REJECTED, 0),
            "human_required": by_v.get(model.V_HUMAN_REQUIRED, 0),
            "blocked": by_v.get(model.V_BLOCKED, 0),
            "last_run": latest_discovery(data_dir),
        },
        "opportunity_types": by_type,
        "strategies": {
            "selected": by_strat,
            "service_share": round(
                by_strat.get(model.STRAT_SERVICE, 0) / sum(by_strat.values()), 3)
            if by_strat else 0.0,
        },
        "outcomes": {
            "settled": outcomes["settled"],
            "wins": outcomes["wins"],
            "win_rate": outcomes["overall_win_rate"],
            "revenue_eur": outcomes["total_revenue_eur"],
            "profit_eur": outcomes["total_profit_eur"],
            "best_categories": sorted(
                ({"key": k, **v} for k, v in outcomes["by_category"].items()),
                key=lambda d: -d["profit"])[:5],
        },
        "human_actions": human_actions,
    }
