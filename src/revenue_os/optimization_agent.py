"""Roster `optimization_agent` - recompute niche/network priority weights
from Measurement's data, and flag (never auto-delete) zero-traffic assets.

Reuses the existing, unmodified `ecosystem.learning.OutcomeStore` - plain
ratios of settled outcomes, not ML (spec: "Deterministic, explainable
feedback"). Below 5 settled outcomes it deliberately applies no weighting
at all (avoids over-fitting noise on a handful of data points) - this
agent does not lower that bar.

Zero-traffic flagging is a recommendation only: even though deleting a
page in our own GitHub Pages repo would be `SAFE_AUTONOMOUS` under
action_class.py (an owned channel), this agent never does it - it only
ever surfaces a list for a human to look at. Documented limitation: no
age threshold is applied (a page live for one hour and a page live for
three months both show up if they have zero clicks) - a human interprets
the list in context; over-engineering a decay model here was avoided
deliberately (mandatory correction: do not over-engineer the MVP).
"""

from __future__ import annotations

from .agent import Agent
from .ecosystem.affiliate_model import AffiliateAssetStore, AffiliateLinkStore, ClickStore
from .ecosystem.learning import OutcomeStore
from .messages import Result, Task


def zero_traffic_assets(data_dir) -> list[dict]:
    """Deployed assets with zero recorded clicks across every link that
    points at them - flagged for human review, never auto-deleted."""
    assets = AffiliateAssetStore.load(data_dir).all()
    click_counts = ClickStore.load(data_dir).count_by_link()

    links_by_asset: dict[str, list] = {}
    for link in AffiliateLinkStore.load(data_dir).all():
        links_by_asset.setdefault(link.asset_id, []).append(link)

    flagged = []
    for asset in assets:
        if not asset.live_url:
            continue
        links = links_by_asset.get(asset.asset_id, [])
        total = sum(click_counts.get(link.link_id, 0) for link in links)
        if total == 0:
            flagged.append({"asset_id": asset.asset_id, "live_url": asset.live_url,
                            "opportunity_id": asset.opportunity_id})
    return flagged


def optimize(data_dir) -> dict:
    weights = OutcomeStore.load(data_dir).priority_weights()
    flagged = zero_traffic_assets(data_dir)
    return {
        "priority_weights": weights,
        "zero_traffic_assets": flagged,
        "flagged_count": len(flagged),
    }


class OptimizationAgent(Agent):
    role = "optimization_agent"
    objective = ("Recompute deterministic priority weights from settled outcomes and "
                "flag (never delete) zero-traffic deployed assets for human review.")
    capabilities = ("optimize",)

    def run(self, task: Task) -> Result:
        payload = task.payload or {}
        data_dir = payload.get("data_dir")
        if not data_dir:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['data_dir'] is required")
        return Result(task_id=task.id, agent=self.name, status="ok",
                      output=optimize(data_dir))
