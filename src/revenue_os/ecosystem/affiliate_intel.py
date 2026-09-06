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


class FunnelStatusError(ValueError):
    """Unknown opportunity id - never guesses a funnel state for one."""


def affiliate_funnel_status(data_dir, opportunity_id: str) -> dict:
    """One opportunity's affiliate funnel end to end, split into READY
    (facts we can verify about our own setup right now) vs WAITING
    (things that can only be confirmed by something external - real
    traffic, a real affiliate-network conversion, a real commission
    payout - and are never guessed or fabricated here).

    Pure read model over the SAME stores `affiliate_status()` already
    reads plus `opportunity_store` - no new persistence, no new gate."""
    from ..opportunity_store import load_opportunities

    store = load_opportunities(data_dir)
    rec = store.get(opportunity_id)
    if rec is None:
        raise FunnelStatusError(f"unknown opportunity {opportunity_id!r}")

    plan = ((rec.get("strategy") or {}).get("plan")) or {}
    is_affiliate_chain = plan.get("kind") == "affiliate_chain"
    chain_completed = is_affiliate_chain and plan.get("status") == "completed"

    asset_id = plan.get("asset_id", "")
    link_id = plan.get("link_id", "")
    asset_live_url = plan.get("asset_live_url", "")

    offer_id = ((plan.get("match") or {}).get("offer_id", ""))
    offers = {o.offer_id: o for o in AffiliateOfferStore.load(data_dir).all()}
    offer = offers.get(offer_id)

    links = {l.link_id: l for l in AffiliateLinkStore.load(data_dir).all()}
    link = links.get(link_id)

    commissions = [c for c in CommissionStore.load(data_dir).all()
                  if c.opportunity_id == opportunity_id]
    settled = [c for c in commissions if c.status in SETTLED_COMMISSION_STATUSES]

    discovery = rec.get("discovery") or {}
    is_synthetic = rec.get("origin") != "real"
    is_editorial = discovery.get("source_type") == "editorial_pick"
    #: "discovered_real" = an independently-arising real demand signal
    #: (HN/StackExchange/Lemmy/...); "editorial_pick" = a human explicitly
    #: authorized a proactive guide for a real product, NOT tied to one
    #: discovered post (see ecosystem.editorial.build_editorial_opportunity);
    #: "synthetic" = test/simulation data. Never conflated with each other -
    #: this is exactly the distinction the caller needs to judge whether a
    #: deployed page is backed by observed demand or an editorial decision.
    demand_basis = ("synthetic" if is_synthetic
                    else "editorial_pick" if is_editorial else "discovered_real")

    ready = {
        "demand_source_real": demand_basis == "discovered_real",
        "offer_selected": bool(offer_id and offer is not None),
        "offer_usable": bool(offer is not None and offer.usable),
        "asset_generated": bool(asset_id),
        "page_deployed": bool(asset_live_url),
        "affiliate_url_configured": bool(offer is not None and offer.product_url),
        "click_tracking_active": bool(plan.get("click_tracking_active", False)),
    }
    waiting_on = {
        "real_traffic": link.click_count if link else 0,
        "real_affiliate_conversion": link.conversion_count if link else 0,
        "commission_confirmation_eur": round(sum(c.amount for c in settled), 2),
    }

    if not is_affiliate_chain:
        next_action = ("run select-strategy then plan-strategy for this opportunity - "
                       "no AFFILIATE chain has been planned yet")
    elif not chain_completed:
        next_action = f"blocked at step {plan.get('step', '?')!r}: {plan.get('reason', '')}"
    elif not waiting_on["real_traffic"]:
        next_action = "everything on our side is live - waiting for a real visitor to click the CTA"
    elif not waiting_on["real_affiliate_conversion"]:
        next_action = "real clicks recorded - waiting for a real conversion on systeme.io's side"
    elif not waiting_on["commission_confirmation_eur"]:
        next_action = ("a conversion may have happened - confirm it via the affiliate network's "
                       "own dashboard, then run affiliate-record-commission")
    else:
        next_action = "commission confirmed - nothing pending"

    return {
        "opportunity_id": opportunity_id,
        "title": rec.get("title", ""),
        "demand_source": discovery.get("source", ""),
        "demand_basis": demand_basis,
        "offer": {"offer_id": offer_id, "program_name": offer.program_name if offer else "",
                  "network": offer.network if offer else "",
                  "affiliate_url": offer.product_url if offer else ""},
        "asset_id": asset_id,
        "asset_live_url": asset_live_url,
        "link_id": link_id,
        "ready": ready,
        "waiting_on": waiting_on,
        "next_action": next_action,
    }


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


def traffic_readiness(data_dir) -> dict:
    """Traffic-engine read model (spec: Phase 11) - every field a real,
    persisted fact or an explicit, hardcoded policy label
    (`paid_ads: "DISABLED"`), never an inferred or projected metric.
    Revenue is NEVER inferred from clicks - it is always the same
    settled-commission sum `affiliate_status()` reports as `revenue_eur`."""
    from ..opportunity_store import load_opportunities

    assets = AffiliateAssetStore.load(data_dir).all()
    deployed = [a for a in assets if a.live_url]
    waiting = [a for a in assets if not a.live_url]

    completed_chain_plans = []
    for rec in load_opportunities(data_dir).all():
        plan = (rec.get("strategy") or {}).get("plan") or {}
        if plan.get("kind") == "affiliate_chain" and plan.get("status") == "completed":
            completed_chain_plans.append(plan)

    seo_ready = bool(deployed)
    organic_communities_ready = any(p.get("distribution_plan") for p in completed_chain_plans)
    click_tracking_active = any(p.get("click_tracking_active") for p in completed_chain_plans)

    settled = [c for c in CommissionStore.load(data_dir).all()
              if c.status in SETTLED_COMMISSION_STATUSES]

    return {
        "content_assets": {"deployed": len(deployed), "waiting": len(waiting)},
        "traffic_channels": {
            "seo": "READY" if seo_ready else "WAITING",
            "organic_communities": "READY" if organic_communities_ready else "WAITING",
            "paid_ads": "DISABLED",
        },
        "tracking": {
            "page_tracking": "not_implemented - no analytics are wired; static GitHub Pages "
                             "hosting has no server-side page-view tracking",
            "affiliate_click_tracking": ("active" if click_tracking_active else
                                        "not_active - AFFILIATE_TRACKING_BASE_URL is not set / "
                                        "the tracking redirect server is not deployed behind a "
                                        "public domain yet (see "
                                        "ecosystem.affiliate_tracking_server / "
                                        "`revenue_os serve-affiliate-tracker`)"),
        },
        "conversions_confirmed": len(settled),
        "revenue_confirmed_eur": round(sum(c.amount for c in settled), 2),
        "deployed_page_urls": [a.live_url for a in deployed],
    }
