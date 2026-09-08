"""Resolve the real affiliate link to use for one social publication.

Never string-builds an Amazon URL and never invents a tracking tag - it
reuses `ecosystem/affiliate_links.create_link()`, which appends the
offer's own real, human-registered `tracking_value` (tag=airevenue-21)
verbatim, and mints a per-channel row so clicks from a given
platform/community are attributable separately.

Two link modes:
  guide   - the visitor lands on our own already-deployed buying-guide
            page (which itself carries the disclosed Amazon CTA). Safer
            for communities that dislike bare product links; also routes
            through our /go/<tracking_id> hop when the tracking server is
            public.
  direct  - the visitor lands directly on the Amazon product page with
            tag=airevenue-21 (used only where a direct product link is
            appropriate AND affiliate links are permitted).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ecosystem import affiliate_links
from ..ecosystem.affiliate_matching import AffiliateMatch, match_offers
from ..ecosystem.affiliate_model import (
    AffiliateAssetStore,
    AffiliateOfferStore,
)

LINK_MODE_GUIDE = "guide"
LINK_MODE_DIRECT = "direct"


@dataclass(frozen=True)
class ResolvedLink:
    ok: bool
    reason: str = ""
    link_id: str = ""
    tracking_id: str = ""
    offer_id: str = ""
    asset_id: str = ""
    opportunity_id: str = ""
    mode: str = ""
    #: the URL to actually put in the published content
    published_target: str = ""
    #: the raw external affiliate URL (Amazon w/ tag=) behind it
    affiliate_url: str = ""
    #: our own tracking-redirect path, "" until the tracking server is public
    redirect_path: str = ""
    guide_live_url: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "link_id": self.link_id,
            "tracking_id": self.tracking_id, "offer_id": self.offer_id,
            "asset_id": self.asset_id, "opportunity_id": self.opportunity_id,
            "mode": self.mode, "published_target": self.published_target,
            "affiliate_url": self.affiliate_url, "redirect_path": self.redirect_path,
            "guide_live_url": self.guide_live_url,
        }


def _amazon_match(draft, offer) -> AffiliateMatch:
    """Bind the given (already chosen) offer to a real AffiliateMatch for
    this draft, reusing the deterministic matcher. Falls back to a
    zero-score match if the matcher drops it (create_link only reads
    match.offer)."""
    for m in match_offers(draft, [offer], min_score=0.0):
        if m.offer.offer_id == offer.offer_id:
            return m
    return AffiliateMatch(offer=offer, match_score=0.0, matched_terms=[],
                          demand_strength=0.0)


def resolve_link(data_dir, *, draft, offer_id: str, opportunity_id: str,
                 platform: str, community: str = "",
                 mode: str = LINK_MODE_GUIDE, tracking_base_url: str = "",
                 now_iso: str = "") -> ResolvedLink:
    """Resolve (and persist, idempotently) the affiliate link for this
    publication. `draft` is an ecosystem OpportunityDraft for the matched
    opportunity. Fails closed: unusable offer, or `guide` mode with no
    deployed guide page, -> ok=False."""
    offers = AffiliateOfferStore.load(data_dir)
    offer = offers.get(offer_id)
    if offer is None:
        return ResolvedLink(False, f"offer {offer_id!r} not found")
    if not offer.usable:
        return ResolvedLink(False, f"offer {offer_id!r} is not usable "
                                   f"(status={offer.status}, active={offer.active})")

    assets = AffiliateAssetStore.load(data_dir)
    asset = None
    for a in assets.by_opportunity(opportunity_id):
        if a.offer_id == offer_id:
            asset = a
            break
    if asset is None:
        return ResolvedLink(
            False, f"no affiliate asset for opportunity {opportunity_id!r} + "
                   f"offer {offer_id!r} - build/deploy the guide page first")

    if mode == LINK_MODE_GUIDE and not asset.live_url:
        return ResolvedLink(
            False, f"asset {asset.asset_id} has no live_url yet - deploy the "
                   "guide page before linking to it, or use mode='direct'")

    match = _amazon_match(draft, offer)
    source = f"{platform}:{community}" if community else platform
    link = affiliate_links.create_link(
        data_dir, opportunity_id=opportunity_id, asset=asset, match=match,
        source=source, now_iso=now_iso)

    redirect_path = link.redirect_path
    base = (tracking_base_url or "").rstrip("/")
    tracked = f"{base}{redirect_path}" if base else ""

    if mode == LINK_MODE_GUIDE:
        published_target = asset.live_url
    else:
        # direct: prefer our own tracking hop when available, else the raw
        # Amazon affiliate URL (already carries tag=airevenue-21).
        published_target = tracked or link.target_url

    return ResolvedLink(
        ok=True, link_id=link.link_id, tracking_id=link.tracking_id,
        offer_id=offer_id, asset_id=asset.asset_id, opportunity_id=opportunity_id,
        mode=mode, published_target=published_target,
        affiliate_url=link.target_url, redirect_path=redirect_path,
        guide_live_url=asset.live_url)
