"""Affiliate link creation, attribution + click tracking (spec sections 5-6).

Attribution chain, one row per hop, all joinable by id:

    Demand (opportunity_id) -> Asset (asset_id) -> Offer (offer_id)
        -> Link (link_id, tracking_id) -> Click (click_id) -> Commission

`create_link()` never fabricates a network subid the offer does not
support - `tracking_id` is always OUR OWN id (safe to expose, carries no
external secret), threaded through the offer's own `tracking_param` when
the network has one (e.g. Amazon's `tag=`), or just appended as a plain
query parameter otherwise. If a network gives us no conversion feed at
all, `link.conversion_count` simply stays 0 and no commission is ever
guessed into existence - see `affiliate_revenue.py`.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .affiliate_model import (
    AffiliateAsset,
    AffiliateLink,
    AffiliateLinkStore,
    ClickEvent,
    ClickStore,
    new_id,
)
from .affiliate_matching import AffiliateMatch


def _append_tracking(url: str, param: str, value: str) -> str:
    """Append `param=value` to `url`'s query string without disturbing any
    existing parameters. Pure string work - no network."""
    if not url:
        return url
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def create_link(data_dir, *, opportunity_id: str, asset: AffiliateAsset,
                match: AffiliateMatch, source: str, now_iso: str = "") -> AffiliateLink:
    """Idempotent: a second call for the same (asset_id, offer_id, source)
    reuses the existing link rather than minting a new tracking_id (so
    re-running plan()/the autonomous loop never orphans click history)."""
    store = AffiliateLinkStore.load(data_dir)
    offer = match.offer
    for existing in store.all():
        if (existing.asset_id == asset.asset_id and existing.offer_id == offer.offer_id
                and existing.source == source):
            return existing

    link_id = new_id("link")
    tracking_id = new_id("trk")
    target = offer.product_url
    if offer.tracking_param:
        # a real, static, pre-registered value (e.g. Amazon's own `tag=`)
        # MUST be used verbatim - a program's tracking id is not something
        # the fleet may invent a fresh one of per link/click. Only when
        # the offer has no static value does a network's own per-link
        # subid convention apply, and our own internal id is a reasonable
        # value for that.
        target = _append_tracking(target, offer.tracking_param,
                                  offer.tracking_value or tracking_id)
    else:
        target = _append_tracking(target, "subid", tracking_id)

    link = AffiliateLink(
        link_id=link_id, opportunity_id=opportunity_id, asset_id=asset.asset_id,
        offer_id=offer.offer_id, source=source, tracking_id=tracking_id,
        target_url=target, redirect_path=f"/go/{tracking_id}", created_at=now_iso)
    store.upsert(link)
    store.save()
    return link


def record_click(data_dir, *, tracking_id: str = "", link_id: str = "",
                 channel: str = "", now_iso: str = "") -> dict:
    """Record one click, data-sparse by construction: only link identity,
    timestamp, and the distribution channel WE assigned when we published
    the asset there (never anything derived from the visitor - no IP, no
    user agent, no cookie). Resolves `tracking_id` -> `link_id` when the
    caller only has the public tracking id (the redirect handler's case).
    Unknown tracking_id/link_id -> a no-op dict, never a crash (a stray or
    forged tracking id must not raise)."""
    link_store = AffiliateLinkStore.load(data_dir)
    link = (link_store.get_by_tracking_id(tracking_id) if tracking_id
           else link_store.get(link_id))
    if link is None:
        return {"recorded": False, "reason": "unknown link"}

    click_store = ClickStore.load(data_dir)
    click = ClickEvent(click_id=new_id("clk"), link_id=link.link_id,
                       ts=now_iso, channel=channel or link.source)
    click_store.record(click)
    click_store.save()

    link.click_count += 1
    link_store.upsert(link)
    link_store.save()
    return {"recorded": True, "click_id": click.click_id, "link_id": link.link_id,
            "target_url": link.target_url}


def link_economics(data_dir, link_id: str) -> dict:
    """Read-only rollup for one link - what JARVIS/CLI actually display."""
    link = AffiliateLinkStore.load(data_dir).get(link_id)
    if link is None:
        return {}
    clicks = ClickStore.load(data_dir).by_link(link_id)
    return {**link.to_dict(), "recorded_clicks": len(clicks)}
