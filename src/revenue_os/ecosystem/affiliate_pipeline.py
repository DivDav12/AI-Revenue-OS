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


#: safe default: at most this many NEW autonomous affiliate actions (a
#: real MATCH -> ... -> DEPLOY chain that completes SAFE_AUTONOMOUS) per
#: run. A recurring night run therefore adds at most one new affiliate
#: asset per cycle - a human reviews it before the next one is created.
MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN = 1

#: the keyless, read-only public demand sources the night loop reads by
#: default - exactly the ones ecosystem.demand_sources already supports
#: (no new source, no credentials, no posting/commenting/account).
_NIGHT_DEMAND_SOURCES: tuple[str, ...] = (
    "demand-hn", "demand-stackexchange-recs", "demand-lemmy-buying")


def run_affiliate_tick(data_dir, *, limit: int = 20, now_iso: str = "",
                       max_actions: int = MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN) -> dict:
    """The autonomous-loop entry point (spec section 19): for every
    PLANNABLE opportunity not yet attempted for AFFILIATE, evaluate ->
    select -> (if AFFILIATE wins) plan, reusing the exact same
    `ecosystem.pipeline` functions the CLI already calls one at a time.

    Idempotent: `evaluate`/`select` simply recompute a deterministic
    projection (safe to redo), and `plan` skips an opportunity whose
    strategy.plan already has `kind == "affiliate_chain"` (already
    attempted - re-attempting a HUMAN_REQUIRED one is cheap and harmless,
    but never re-does completed work).

    `max_actions` (default `MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN`) caps
    how many NEW SAFE_AUTONOMOUS affiliate chains may complete in one run.
    Once the cap is hit, further AFFILIATE-selected opportunities are left
    for the next run and listed under `capped` - never silently dropped.

    One bad opportunity never kills the tick: any exception raised while
    processing a single opportunity is caught, recorded, and the loop
    continues (spec: "Fehler einzelner Quellen dürfen den gesamten Loop
    nicht zerstören")."""
    from ..opportunity_store import load_opportunities
    from . import pipeline as eco_pipeline
    from .model import PLANNABLE

    cap = max(0, int(max_actions))
    store = load_opportunities(data_dir)
    attempted, planned, human_required, capped, errors = [], [], [], [], []
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
            if len(planned) >= cap:
                capped.append(oid)   # deferred to the next run - not dropped
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
           "human_required": human_required, "capped": capped,
           "max_actions": cap, "errors": errors, "ran_at": now_iso}


def run_affiliate_chain(data_dir, *, opportunity_id: str, draft: OpportunityDraft,
                        now_iso: str = "", source: str = "own_site",
                        deployment_adapter=None, guide_title: str = "") -> dict:
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
        cta_url="", now_iso=now_iso,   # cta_url resolved below, after the link exists
        guide_title=guide_title)
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


def run_affiliate_night(data_dir, *, sources=None, source_names=None,
                        discover_limit: int = 15, tick_limit: int = 20,
                        max_actions: int = MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN,
                        discover_offers: bool = True, offer_networks=None,
                        environ=None, now_iso: str = "") -> dict:
    """ONE bounded, recurring-safe affiliate cycle over a SINGLE entry
    point - no new orchestrator, no new state:

        DiscoveryEngine(public demand sources, read-only)
          -> dedupe + verify + persist          (discovery.py, unchanged)
        discover_offer_candidates                (affiliate_discovery.py -
          -> search every AUTHORIZED offer         additive; stages real
             source for each PLANNABLE demand's     product search results
             ProductIntent, stage for human         for human completion.
             completion                             Creates NO usable offer,
                                                    joins NO program, calls
                                                    NO unauthorized network.)
        run_affiliate_tick
          -> evaluate -> select (ProductIntent + validated-offer match +
             profitability -> qualified TYPE_AFFILIATE)   (pipeline.py)
          -> plan -> run_affiliate_chain          (asset -> link -> deploy
             -> distribute, all existing, fail-closed per step)

    Safe by construction:
      * demand sources are keyless public GET APIs - no post, no comment,
        no account, no ad spend, no self-purchase, no fake activity.
      * per-source discovery errors are isolated (DiscoveryEngine) and
        per-opportunity tick errors are isolated (run_affiliate_tick) -
        one failure never stops the run.
      * `max_actions` (default MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN = 1)
        caps NEW autonomous affiliate actions per run.
      * no good chance -> `planned == []` -> action == "NO_ACTION".
      * the whole cycle runs inside `autonomous_context()`, so every
        money / PayPal / e-mail / paid-LLM call site hard-refuses.
      * offer discovery (`discover_offers=True`, default) only READS
        authorized product-search APIs and stages candidates for a human
        - it creates no usable offer, joins no program, and calls no
        unauthorized/unconfigured network. Set `discover_offers=False` to
        skip it entirely.

    `sources=` (a list of real `sources.OpportunitySource` objects)
    overrides `source_names=` - used by tests, mirrors
    `DiscoveryEngine(data_dir, sources=...)`. Recurrence is the caller's
    job (cron / Task Scheduler / `/loop`) - this runs exactly one cycle.
    """
    from ..action_class import autonomous_context
    from .discovery import DiscoveryEngine

    result: dict = {"ran_at": now_iso, "discovery": None, "offer_discovery": None,
                    "tick": None, "errors": [], "new_affiliate_actions": 0,
                    "action": "NO_ACTION"}

    with autonomous_context():
        srcs: list = []
        if sources is not None:
            srcs = list(sources)
            result["sources"] = [getattr(getattr(s, "meta", None), "source", "?") for s in srcs]
        else:
            from .sources import build_source

            names = tuple(source_names) if source_names else _NIGHT_DEMAND_SOURCES
            result["sources"] = list(names)
            for n in names:
                try:
                    srcs.append(build_source(n))
                except Exception as exc:                 # noqa: BLE001 - isolate a bad source
                    result["errors"].append(f"build_source({n!r}): {exc!r}")

        if srcs:
            try:
                rep = DiscoveryEngine(data_dir, sources=srcs).run(
                    limit_per_source=max(1, int(discover_limit)))
                result["discovery"] = rep.to_dict()
                result["errors"].extend(rep.errors)
            except Exception as exc:                     # noqa: BLE001
                result["errors"].append(f"discovery: {exc!r}")

        if discover_offers:
            try:
                from .affiliate_discovery import discover_offer_candidates

                od = discover_offer_candidates(
                    data_dir, networks=offer_networks,
                    limit=max(1, int(discover_limit)), now_iso=now_iso,
                    environ=environ)
                result["offer_discovery"] = od
                result["errors"].extend(od.get("errors") or [])
            except Exception as exc:                 # noqa: BLE001 - never stop the cycle
                result["errors"].append(f"offer_discovery: {exc!r}")

        try:
            tick = run_affiliate_tick(data_dir, limit=max(1, int(tick_limit)),
                                      now_iso=now_iso, max_actions=max_actions)
            result["tick"] = tick
            result["errors"].extend(str(e) for e in (tick.get("errors") or []))
            result["new_affiliate_actions"] = len(tick.get("planned") or [])
        except Exception as exc:                         # noqa: BLE001
            result["errors"].append(f"tick: {exc!r}")

    result["action"] = ("AFFILIATE_ACTION" if result["new_affiliate_actions"]
                        else "NO_ACTION")
    return result
