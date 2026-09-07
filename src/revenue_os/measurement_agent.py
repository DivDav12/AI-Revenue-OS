"""Roster `measurement_agent` - click aggregation + commission rollups.

Click counting is self-hosted, $0, and fully autonomous (our own
`/go/<id>` redirect - `ecosystem.affiliate_model.ClickStore` - no PII, no
login). Commission figures are a different story: no affiliate network
this codebase supports gives a programmatic conversion feed without
approval-gated API access a brand-new site cannot realistically reach
(e.g. Amazon's Creators API requires 10 prior qualifying sales - a
documented chicken-and-egg limitation, not hidden). So this agent reports
what it can measure automatically (clicks) and states plainly when a
number is not available rather than ever guessing a conversion.

Recording an actual commission a human read off their network dashboard
stays a separate, explicit, human-invoked action
(`ecosystem.affiliate_revenue.record_pending_commission` /
`confirm_commission`) - this agent only ever aggregates and reports
already-recorded facts, never invents or requests one.
"""

from __future__ import annotations

from .agent import Agent
from .ecosystem.affiliate_model import AffiliateLinkStore, ClickStore
from .ecosystem.affiliate_revenue import opportunity_commission_summary
from .messages import Result, Task


def measure_opportunity(data_dir, opportunity_id: str) -> dict:
    """Everything measurable right now for one opportunity: click counts
    per link (self-hosted, automatic) + commission rollup (human-fed,
    honestly labeled)."""
    links = AffiliateLinkStore.load(data_dir).by_opportunity(opportunity_id)
    click_counts = ClickStore.load(data_dir).count_by_link()
    per_link = [
        {"link_id": link.link_id, "target_url": link.target_url,
         "click_count": click_counts.get(link.link_id, 0)}
        for link in links
    ]
    total_clicks = sum(row["click_count"] for row in per_link)
    commissions = opportunity_commission_summary(data_dir, opportunity_id)

    return {
        "opportunity_id": opportunity_id,
        "links": per_link,
        "total_clicks": total_clicks,
        "commissions": commissions,
        "note": ("click counts are automatic (self-hosted redirect); commission "
                "figures are only ever what a human recorded from their network "
                "dashboard - no live conversion feed exists for any supported "
                "network in this codebase"),
    }


class MeasurementAgent(Agent):
    role = "measurement_agent"
    objective = ("Aggregate real, already-recorded click and commission data for one "
                "opportunity. Never estimates or invents a conversion.")
    capabilities = ("measure_opportunity",)

    def run(self, task: Task) -> Result:
        payload = task.payload or {}
        data_dir = payload.get("data_dir")
        opportunity_id = payload.get("opportunity_id")
        if not data_dir or not opportunity_id:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload requires 'data_dir' and 'opportunity_id'")
        return Result(task_id=task.id, agent=self.name, status="ok",
                      output=measure_opportunity(data_dir, opportunity_id))
