"""Campaign Optimizer (#15, marketing cluster, HUMAN-GATED) - analysis
+ recommendations.

Reads campaign metrics and produces a performance read plus optimization
RECOMMENDATIONS. It changes no budget and publishes nothing:
`auto_applied` is always False.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task


def _rate(n, d) -> float:
    return round(n / d, 4) if d else 0.0


def _variant_metrics(v: dict) -> dict:
    imp = float(v.get("impressions", 0) or 0)
    clk = float(v.get("clicks", 0) or 0)
    spend = float(v.get("spend", 0) or 0)
    conv = float(v.get("conversions", 0) or 0)
    return {
        "name": str(v.get("name") or v.get("angle") or "variant"),
        "impressions": imp, "clicks": clk, "spend": round(spend, 2),
        "conversions": conv,
        "ctr": _rate(clk, imp),
        "cpc": round(spend / clk, 2) if clk else None,
        "cvr": _rate(conv, clk),
        "cpa": round(spend / conv, 2) if conv else None,
    }


def build_optimization(campaign_plan: dict, campaign_metrics: list) -> dict:
    rows = [_variant_metrics(v) for v in (campaign_metrics or []) if isinstance(v, dict)]
    total_conv = sum(r["conversions"] for r in rows)
    total_clicks = sum(r["clicks"] for r in rows)
    total_spend = round(sum(r["spend"] for r in rows), 2)

    priced = [r for r in rows if r["cpa"] is not None]
    winning = sorted(priced, key=lambda r: r["cpa"])[:2]
    win_names = {r["name"] for r in winning}
    losing = [r for r in rows
              if r["name"] not in win_names and (r["cpa"] is None and r["clicks"] >= 30
                                                 or (r["cpa"] or 0) > 3 * (winning[0]["cpa"] if winning else 1))]

    actions = []
    for r in losing:
        actions.append(f"RECOMMEND: pause '{r['name']}' (no efficient conversions)")
    for r in winning:
        actions.append(f"RECOMMEND: keep '{r['name']}' and test a higher budget by hand")
    if not rows:
        actions.append("RECOMMEND: collect more data before any change")

    if total_conv >= 20:
        confidence = "high"
    elif total_conv >= 5 or total_clicks >= 200:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "performance_summary": {
            "variants": rows,
            "total_spend": total_spend,
            "total_conversions": total_conv,
            "blended_cpa": round(total_spend / total_conv, 2) if total_conv else None,
        },
        "winning_variants": [r["name"] for r in winning],
        "losing_variants": [r["name"] for r in losing],
        "optimization_actions": actions,
        "confidence": confidence,
        "auto_applied": False,
        "human_gate_required": True,
        "note": "recommendations only - no budget changed, nothing published",
    }


class CampaignOptimizerAgent(Agent):
    role = "campaign_optimizer"
    objective = "Analyse campaign metrics; recommend, never apply."
    capabilities = ("optimize_campaigns",)

    def run(self, task: Task) -> Result:
        metrics = task.payload.get("campaign_metrics")
        if not isinstance(metrics, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['campaign_metrics'] must be a list")
        plan = task.payload.get("campaign_plan")
        if plan is not None and not isinstance(plan, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['campaign_plan'] must be a dict when given")
        out = build_optimization(plan or {}, metrics)
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
