"""Affiliate chain orchestrator (spec sections 4-9, 15, 19-21).

Wires together, in order, every affiliate module built for this pipeline
- MATCH -> EVALUATE -> BUILD ASSET -> CREATE LINK -> DEPLOY -> DISTRIBUTE
- as one idempotent function `run_affiliate_chain()` that
`ecosystem/pipeline.py`'s `plan()` calls for the AFFILIATE strategy,
exactly the way `_plan_task_chain()` already does for TASK. Nothing here
introduces a new persistence mechanism: the result is stored on the
opportunity's own `strategy.plan` namespace via the same
`opportunity_store` the rest of `pipeline.py` already uses.

Fails closed, one step at a time, never partially fabricates progress:
  - no usable matching offer            -> HUMAN_REQUIRED (setup checklist)
  - asset fails the quality gate        -> HUMAN_REQUIRED (quality reasons)
  - deploy adapter has no credentials   -> HUMAN_REQUIRED (deploy blocked)
  - deploy succeeds                     -> SAFE_AUTONOMOUS chain completed;
                                            distribution plan attached
                                            (spec 7: still human-gated per
                                            channel where the platform
                                            requires it - see distribution.py)
"""

from __future__ import annotations

from .. import distribution as distribution_mod
from . import affiliate_assets, affiliate_links, affiliate_matching, affiliate_profitability
from .affiliate_model import AffiliateOfferStore
from .model import OpportunityDraft


def run_affiliate_tick(data_dir, *, limit: int = 20, now_iso: str = "") -> dict:
    """The autonomous-loop entry point (spec section 19): for every
    PLANNABLE opportunity not yet attempted for AFFILIATE, evaluate ->
    select -> (if AFFILIATE wins) plan, reusing the exact same
    `ecosystem.pipeline` functions the CLI already calls one at a time.

    Idempotent: `evaluate`/`select` simply recompute a deterministic
    projection (safe to redo), and `plan` skips an opportunity whose
    strategy.plan already has `kind == "affiliate_chain"` (already
    attempted - re-attempting a HUMAN_REQUIRED one is cheap and harmless,
    but never re-does completed work).

    One bad opportunity never kills the tick: any exception raised while
    processing a single opportunity is caught, recorded, and the loop
    continues (spec: "Fehler einzelner Quellen dürfen den gesamten Loop
    nicht zerstören")."""
    from ..opportunity_store import load_opportunities
    from . import pipeline as eco_pipeline
    from .model import PLANNABLE

    store = load_opportunities(data_dir)
    attempted, planned, human_required, errors = [], [], [], []
    n = 0
    for rec in store.all():
        if n >= limit:
            break
        oid = rec.get("id", "")
        vstatus = ((rec.get("discovery") or {}).get("verification") or {}).get("status")
        if vstatus not in PLANNABLE:
            continue
        existing_plan = (rec.get("strategy") or {}).get("plan") or {}
        if existing_plan.get("kind") == "affiliate_chain" and existing_plan.get("status") == "completed":
            continue   # already fully deployed - nothing left to (re)do
        n += 1
        attempted.append(oid)
        try:
            eco_pipeline.evaluate(data_dir, oid)
            sel = eco_pipeline.select(data_dir, oid)
            if sel.get("recommended") != "AFFILIATE":
                continue
            out = eco_pipeline.plan(data_dir, oid, actor="ecosystem_autonomy")
            if out.get("next_step_class") == "SAFE_AUTONOMOUS":
                planned.append(oid)
            else:
                human_required.append({"opportunity_id": oid,
                                       "reason": out.get("plan", {}).get("reason", "")})
        except Exception as exc:                # noqa: BLE001 - isolate per-opportunity
            errors.append({"opportunity_id": oid, "error": str(exc)})

    return {"attempted": attempted, "planned": planned,
           "human_required": human_required, "errors": errors,
           "ran_at": now_iso}


def run_affiliate_chain(data_dir, *, opportunity_id: str, draft: OpportunityDraft,
                        now_iso: str = "", source: str = "own_site",
                        deployment_adapter=None) -> dict:
    """Idempotent: safe to call again for the same opportunity - offer
    matching is deterministic, `build_asset`/`create_link` reuse existing
    rows, and `deploy_asset` only re-publishes when the rendered content
    actually changed (deployment.py's content-hash check)."""
    offers = AffiliateOfferStore.load(data_dir).all()
    if not offers:
        return {"kind": "affiliate_chain", "status": "human_required",
               "step": "match", "reason": "no affiliate offers on file yet - "
               "ingest at least one real, human-joined program first "
               "(see: revenue_os affiliate-setup-required)",
               "next_step_class": "HUMAN_REQUIRED"}

    all_matches = affiliate_matching.match_offers(draft, offers)
    usable = next((m for m in all_matches if m.offer.usable), None)
    if usable is None:
        nearest = all_matches[0] if all_matches else None
        reason = ("no usable (already-joined) offer matches this demand yet")
        if nearest is not None:
            reason += (f" - closest match is {nearest.offer.program_name!r} "
                      f"({nearest.offer.product_name!r}), which needs setup: "
                      f"status={nearest.offer.status}")
        return {"kind": "affiliate_chain", "status": "human_required",
               "step": "match", "reason": reason,
               "nearest_match": nearest.to_dict() if nearest else None,
               "next_step_class": "HUMAN_REQUIRED"}

    prof = affiliate_profitability.evaluate(usable)

    asset, quality_ok, quality_reasons = affiliate_assets.build_asset(
        data_dir, opportunity_id=opportunity_id, draft=draft, match=usable,
        cta_url="", now_iso=now_iso)   # cta_url resolved below, after the link exists
    if not quality_ok:
        return {"kind": "affiliate_chain", "status": "human_required",
               "step": "build_asset", "reason": "; ".join(quality_reasons),
               "match": usable.to_dict(), "profitability": prof.to_dict(),
               "asset_id": asset.asset_id, "next_step_class": "HUMAN_REQUIRED"}

    link = affiliate_links.create_link(
        data_dir, opportunity_id=opportunity_id, asset=asset, match=usable,
        source=source, now_iso=now_iso)

    import os
    tracking_base = os.environ.get("AFFILIATE_TRACKING_BASE_URL", "").rstrip("/")
    cta_url = f"{tracking_base}{link.redirect_path}" if tracking_base else link.target_url
    click_tracking_active = bool(tracking_base)

    deploy_out = affiliate_assets.deploy_asset(asset=asset, draft=draft, match=usable,
                                               cta_url=cta_url, adapter=deployment_adapter)
    if not deploy_out["deployed"]:
        return {"kind": "affiliate_chain", "status": "human_required",
               "step": "deploy", "reason": deploy_out.get("reasons") or [deploy_out.get("error", "")],
               "match": usable.to_dict(), "profitability": prof.to_dict(),
               "asset_id": asset.asset_id, "link_id": link.link_id,
               "next_step_class": "HUMAN_REQUIRED"}

    asset.live_url = deploy_out["live_url"]
    from .affiliate_model import AffiliateAssetStore
    astore = AffiliateAssetStore.load(data_dir)
    astore.upsert(asset)
    astore.save()

    dist_plan = distribution_mod.build_distribution_plan(
        opportunity={"id": opportunity_id, "title": draft.title,
                    "description": draft.description, "category": draft.category,
                    "target_customer": (draft.raw or {}).get("target_customer", "")},
        offer={"what_is_sold": usable.offer.product_name,
              "positioning": usable.offer.program_name, "price": usable.offer.product_price},
        signals={"probability": affiliate_matching.demand_strength(draft)},
        now=now_iso)

    return {
        "kind": "affiliate_chain", "status": "completed",
        "match": usable.to_dict(), "profitability": prof.to_dict(),
        "asset_id": asset.asset_id, "asset_live_url": asset.live_url,
        "link_id": link.link_id, "tracking_id": link.tracking_id,
        "click_tracking_active": click_tracking_active,
        "click_tracking_note": ("" if click_tracking_active else
                                "AFFILIATE_TRACKING_BASE_URL is not set - the deployed "
                                "page links directly to the offer; click_count will "
                                "stay 0 until a human exposes the tracking redirect "
                                "server (affiliate_tracking_server.py) behind a public "
                                "domain and sets this env var"),
        "distribution_plan": dist_plan,
        "next_step_class": "SAFE_AUTONOMOUS",
        "planned_at": now_iso,
    }
