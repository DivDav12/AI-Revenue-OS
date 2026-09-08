"""Pinterest distribution flow.

Pinterest is a push channel (a visual search engine), not a discovery
feed - so this does not "find threads". It takes a real, usable Amazon
offer + one of our already-deployed guide pages, derives the dominant
search keyword from the extracted product intent, produces a Pin draft
(title / description / disclosure / creative brief), runs the compliance
gate, and then either:

  * creates the Pin through the official API v5 - ONLY when the operator
    has attested compliance (PINTEREST_AUTOPOST_CONFIRMED=1), the API is
    connected, a board resolves, AND a real creative (image_url) is
    supplied; or
  * returns HUMAN_REQUIRED with the finished draft + the exact remaining
    step (usually: produce the creative, or connect the API).

No fabricated impressions or clicks. No image generation here - a Pin
needs a real creative and that is a human/asset decision.
"""

from __future__ import annotations

import re

from ..ecosystem.affiliate_model import AffiliateAssetStore, AffiliateOfferStore
from ..ecosystem.model import OpportunityDraft, SourceMeta
from ..ecosystem import demand_signal, model, product_intent as pi_mod
from ..store import now_iso as _now_iso
from . import compliance, content, links
from .pinterest_api import PinterestClient, PinterestUnavailable
from .store import (
    STATE_HUMAN_REQUIRED,
    STATE_PUBLISHED,
    SocialAction,
    SocialActionStore,
    new_action_id,
)


def _draft_from_asset(asset) -> OpportunityDraft:
    return OpportunityDraft(
        title=(asset.guide_title or asset.title or asset.slug or "guide")[:200],
        description=asset.title or asset.slug or "",
        opportunity_type=model.TYPE_AFFILIATE,
        source_meta=SourceMeta(source="pinterest", source_type="demand_signal",
                               policy_status=model.POLICY_OK),
        source_id=asset.asset_id, category=asset.asset_type or "other")


def _best_keyword(offer, title_txt: str) -> str:
    """Pick the offer's own keyword that best fits this guide as a
    Pinterest search term. Prefers a multi-word phrase that overlaps the
    guide title (Pinterest ranks specific phrases), then max token
    overlap, then the longer phrase. Never invents a term - only ever
    returns one of the offer's human-supplied keywords."""
    title_tokens = set(re.findall(r"[a-z]+", title_txt.lower()))
    scored = []
    for k in (offer.keywords or []):
        ktoks = set(re.findall(r"[a-z]+", k.lower()))
        overlap = len(ktoks & title_tokens)
        if overlap == 0:
            continue
        multiword = 1 if (" " in k or "-" in k) else 0
        scored.append((multiword, overlap, len(k), k))
    if not scored:
        return ""
    scored.sort(reverse=True)
    return scored[0][3]


def _pick_asset(assets, offer_id: str, opportunity_id: str):
    cands = [a for a in assets if a.offer_id == offer_id]
    if opportunity_id:
        cands = [a for a in cands if a.opportunity_id == opportunity_id]
    deployed = [a for a in cands if a.live_url]
    return (deployed or cands or [None])[0]


def plan_pinterest(data_dir, *, offer_id: str, opportunity_id: str = "",
                   link_mode: str = links.LINK_MODE_GUIDE, board: str = "",
                   image_url: str = "", keyword: str = "",
                   tracking_base_url: str = "",
                   client: PinterestClient | None = None, environ=None,
                   now_iso: str = "") -> dict:
    now_iso = now_iso or _now_iso()
    offers = AffiliateOfferStore.load(data_dir)
    offer = offers.get(offer_id)
    if offer is None:
        return {"status": "ERROR", "reason": f"offer {offer_id!r} not found"}
    if not offer.usable:
        return {"status": "ERROR",
                "reason": f"offer {offer_id!r} not usable (status={offer.status})"}

    assets = AffiliateAssetStore.load(data_dir).all()
    asset = _pick_asset(assets, offer_id, opportunity_id)
    if asset is None:
        return {"status": "HUMAN_REQUIRED", "blocker": "no_asset",
                "reason": f"no affiliate asset for offer {offer_id!r}"}
    if link_mode == links.LINK_MODE_GUIDE and not asset.live_url:
        return {"status": "HUMAN_REQUIRED", "blocker": "no_deployed_guide",
                "reason": f"asset {asset.asset_id} has no live_url - deploy it "
                          "or use link_mode=direct"}

    draft = _draft_from_asset(asset)
    # Pinterest is a push channel keyed on a SEARCH KEYWORD, not a reply to
    # a thread - so there is no accept/reject intent gate here. The keyword
    # is derived, never invented: try the product-category extractor on the
    # guide title, else the offer keyword that best matches the guide, else
    # the offer's first keyword / category.
    title_txt = f"{asset.title} {asset.guide_title}".strip()
    ev = demand_signal.build_demand_evidence(title_txt, title=asset.title or "")
    extracted = pi_mod.extract_product_intent(ev, title=asset.title or "").category_phrase
    kw_by_overlap = _best_keyword(offer, title_txt)
    operator_kw = (keyword or "").strip()
    chosen_kw = (operator_kw or extracted or kw_by_overlap
                 or (offer.keywords[0] if offer.keywords else offer.category))
    product_intent = {"category_phrase": chosen_kw}
    keyword_basis = ("operator" if operator_kw else "extracted" if extracted
                     else "offer_keyword" if kw_by_overlap else "offer_default")

    rl = links.resolve_link(
        data_dir, draft=draft, offer_id=offer_id,
        opportunity_id=asset.opportunity_id, platform="pinterest",
        community=board or "default", mode=link_mode,
        tracking_base_url=tracking_base_url, now_iso=now_iso)
    if not rl.ok:
        return {"status": "HUMAN_REQUIRED", "blocker": "no_usable_link",
                "reason": rl.reason}

    pin = content.pinterest_pin(offer=offer, product_intent=product_intent,
                                link_url=rl.published_target, link_mode=link_mode)

    target_ref = f"pin:{offer_id}:{asset.asset_id}:{pin.dominant_keyword}"
    store = SocialActionStore.load(data_dir)
    if store.already_handled("pinterest", target_ref, offer_id):
        return {"status": "OK", "counts": {"new_actions": 0, "already_handled": 1},
                "note": "this Pin idea already has an action", "ran_at": now_iso}

    verdict = compliance.check("pinterest", board, has_affiliate_link=True,
                               environ=environ)

    action = SocialAction(
        action_id=new_action_id(), platform="pinterest",
        community=board or "", target_ref=target_ref,
        opportunity_id=asset.opportunity_id, offer_id=offer_id,
        asset_id=asset.asset_id, link_id=rl.link_id,
        intent_decision="PINTEREST_KEYWORD_PUSH", compliance=verdict.to_dict(),
        draft={**pin.to_dict(), "keyword_basis": keyword_basis,
               "resolved_link": rl.to_dict()},
        created_at=now_iso, updated_at=now_iso)

    cl = client if client is not None else PinterestClient(data_dir=data_dir, environ=environ)
    published = False
    reasons: list[str] = list(verdict.reasons)

    if verdict.auto_post_allowed and cl.available and image_url:
        try:
            board_id = cl.resolve_board_id(board)
            if not board_id:
                reasons.append(f"board {board!r} not found on the connected account")
            else:
                res = cl.create_pin(
                    board_id=board_id, title=pin.title, description=pin.description,
                    link=rl.published_target, alt_text=pin.alt_text,
                    image_url=image_url, i_have_read_the_rules=True)
                action.state = STATE_PUBLISHED
                action.published_url = res.get("url", "")
                action.published_at = now_iso
                action.published_by = "auto"
                action.draft["pin_id"] = res.get("pin_id", "")
                published = True
        except PinterestUnavailable as exc:
            reasons.append(f"create_pin failed: {exc}")

    if not published:
        action.state = STATE_HUMAN_REQUIRED
        missing = []
        if not cl.available:
            missing.append("connect the Pinterest API (social-pinterest-connect)")
        if not verdict.auto_post_allowed:
            missing.append("set PINTEREST_AUTOPOST_CONFIRMED=1 after reading the "
                           "current Pinterest affiliate/spam + paid-partnership rules")
        if not image_url:
            missing.append("produce the creative per `creative_brief` and re-run "
                           "with --image-url <hosted png>, or create the Pin "
                           "manually with the drafted title/description/link")
        action.human_action_needed = "; ".join(missing) or (
            "review the drafted Pin and publish it")

    store.upsert(action)
    store.save()

    return {
        "status": "OK",
        "offer_id": offer_id, "asset_id": asset.asset_id, "link_mode": link_mode,
        "action_id": action.action_id, "state": action.state,
        "published_url": action.published_url,
        "dominant_keyword": pin.dominant_keyword,
        "draft": action.draft,
        "compliance": verdict.to_dict(),
        "reasons": reasons,
        "counts": {"new_actions": 1},
        "ran_at": now_iso,
    }
