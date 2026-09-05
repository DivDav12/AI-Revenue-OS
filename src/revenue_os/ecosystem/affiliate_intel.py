"""Read-only affiliate intelligence for JARVIS/CLI (spec section 17).

Same rule as `ecosystem/intel.py`: pure aggregation over persisted state,
no fabricated metrics, no secrets (API keys/tokens are never stored on
any affiliate row in the first place - see affiliate_model.py).
"""

from __future__ import annotations

from .affiliate_model import (
    AffiliateAssetStore,
    AffiliateLinkStore,
    AffiliateOfferStore,
    CommissionStore,
    SETTLED_COMMISSION_STATUSES,
)
from .affiliate_scaling import optimization_report
from .affiliate_sources import setup_required_networks


def affiliate_status(data_dir) -> dict:
    offers = AffiliateOfferStore.load(data_dir).all()
    assets = AffiliateAssetStore.load(data_dir).all()
    links = AffiliateLinkStore.load(data_dir).all()
    commissions = CommissionStore.load(data_dir).all()

    usable_offers = [o for o in offers if o.usable]
    pending_comm = [c for c in commissions if c.status == "PENDING"]
    settled_comm = [c for c in commissions if c.status in SETTLED_COMMISSION_STATUSES]
    reversed_comm = [c for c in commissions if c.status == "REVERSED"]

    total_clicks = sum(l.click_count for l in links)
    total_conversions = sum(l.conversion_count for l in links)
    total_commission = round(sum(c.amount for c in settled_comm), 2)
    total_cost = round(sum(l.cost_eur for l in links), 2)
    total_profit = round(total_commission - total_cost, 2)

    top_offers = sorted(links, key=lambda l: -l.commission_eur)[:5]
    top_assets_by_id = {}
    for l in links:
        top_assets_by_id[l.asset_id] = top_assets_by_id.get(l.asset_id, 0.0) + l.commission_eur
    top_assets = sorted(top_assets_by_id.items(), key=lambda kv: -kv[1])[:5]

    channel_totals: dict[str, float] = {}
    for l in links:
        channel_totals[l.source] = channel_totals.get(l.source, 0.0) + l.commission_eur
    top_channels = sorted(channel_totals.items(), key=lambda kv: -kv[1])[:5]

    opt = optimization_report(data_dir)

    return {
        "offers": {"total": len(offers), "usable": len(usable_offers),
                  "human_setup_required": len(offers) - len(usable_offers)},
        "assets": {"total": len(assets), "deployed": sum(1 for a in assets if a.live_url)},
        "links": {"total": len(links)},
        "clicks": total_clicks,
        "conversions": total_conversions,
        "commissions": {
            "pending_estimated_eur": round(sum(c.amount for c in pending_comm), 2),
            "confirmed_or_paid_eur": total_commission,
            "reversed_count": len(reversed_comm),
        },
        "revenue_eur": total_commission,
        "cost_eur": total_cost,
        "profit_eur": total_profit,
        "top_offers_by_commission": [{"link_id": l.link_id, "offer_id": l.offer_id,
                                      "commission_eur": l.commission_eur} for l in top_offers],
        "top_assets_by_commission": [{"asset_id": a, "commission_eur": round(v, 2)}
                                     for a, v in top_assets],
        "top_channels_by_commission": [{"channel": c, "commission_eur": round(v, 2)}
                                       for c, v in top_channels],
        "stopped_links": opt["stop"],
        "scaling_candidates": opt["scale"],
        "human_setup_required": setup_required_networks(data_dir),
    }
