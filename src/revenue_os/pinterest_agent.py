"""Roster `pinterest_distributor` - the Agent wrapper around
`ecosystem.pinterest_pins.draft_pin`.

A thin adapter so the drafter is registry-routable exactly like the other
roster agents (capability `prepare_pinterest_pin`). It does no work of
its own - it delegates to `pinterest_pins.draft_pin`, which only ever
drafts pin copy for an ALREADY-DEPLOYED affiliate asset.

Human-gated: this agent NEVER logs into Pinterest or posts anything. Its
output is a draft a person reviews and pins themselves, via a free
Pinterest account they create and control.
"""

from __future__ import annotations

from .agent import Agent
from .ecosystem.affiliate_model import AffiliateAsset
from .ecosystem.pinterest_pins import PinDraftError, draft_pin
from .messages import Result, Task


class PinterestDistributorAgent(Agent):
    role = "pinterest_distributor"
    objective = "Turn a deployed affiliate asset into a human-review Pinterest pin draft; never posts."
    capabilities = ("prepare_pinterest_pin",)

    def run(self, task: Task) -> Result:
        payload = task.payload or {}
        asset_data = payload.get("asset")
        if not isinstance(asset_data, dict) or not asset_data.get("asset_id"):
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['asset'] must be an AffiliateAsset dict carrying an asset_id",
            )
        data_dir = payload.get("data_dir")
        if not data_dir:
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['data_dir'] is required",
            )
        asset = AffiliateAsset.from_dict(asset_data)
        try:
            pin = draft_pin(
                data_dir, asset=asset,
                product_name=str(payload.get("product_name") or ""),
                category_label=str(payload.get("category_label") or ""),
                board_suggestion=str(payload.get("board_suggestion") or ""),
                now_iso=str(payload.get("now_iso") or ""),
            )
        except PinDraftError as exc:
            return Result(task_id=task.id, agent=self.name, status="error", error=str(exc))
        return Result(task_id=task.id, agent=self.name, status="ok", output=pin.to_dict())
