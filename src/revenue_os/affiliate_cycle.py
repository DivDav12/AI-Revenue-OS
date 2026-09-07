"""One bounded cycle of the affiliate/content pipeline:

    Opportunity (real discovery -> select)
      -> Affiliate Chain (content -> QC -> link -> GitHub Pages deploy)
      -> Distribution (Pinterest pin draft, rate-limited, human posts)
      -> Digital Product (parallel, best-effort, never blocks the chain)
      -> Measurement -> Optimization

Not a persistent daemon - safe to invoke repeatedly on a schedule
(cron / GitHub Actions / a human running the CLI) at $0. Every step
above SAFE_AUTONOMOUS stops cleanly and is reported in `human_actions`,
never silently skipped and never crashing the rest of the cycle -
`run_cycle()` always returns a report, even when every step needed a
human.

GitHub Pages deployment is autonomous ONLY when an already-configured
GITHUB_TOKEN + GITHUB_PAGES_REPO resolve
(`deployment.default_deployment_adapter().authorized`) - this module
never requests, prints, or rotates that credential; it only checks
whether deploy would succeed BEFORE attempting it, so a missing
credential is reported as one clear HUMAN SETUP REQUIRED line instead of
a confusing failure deep in the chain. `deploy.py`'s own `_redact()`
guarantees the token itself never appears in any error message this
module could surface.

Digital-product generation is a genuinely separate, parallel path
(mandatory correction #3): its own exceptions are caught and reported
without affecting the affiliate chain's result, and it runs regardless
of whether the chain completed, was blocked, or errored.
"""

from __future__ import annotations

from .deployment import default_deployment_adapter
from .ecosystem import digital_products as dp
from .ecosystem.affiliate_model import AffiliateAssetStore
from .ecosystem.affiliate_pipeline import run_affiliate_chain
from .ecosystem.pipeline import draft_from_record
from .ecosystem.pinterest_pins import PinDraftError, draft_pin
from .measurement_agent import measure_opportunity
from .opportunity_agent import DEFAULT_REAL_SOURCES, discover_real_signals, select_opportunity
from .optimization_agent import optimize
from .opportunity_store import load_opportunities


def run_cycle(data_dir, *, source_names=DEFAULT_REAL_SOURCES, limit_per_source: int = 25,
             now_iso: str = "", source_kwargs: dict | None = None,
             deployment_adapter=None) -> dict:
    """One pass through the whole pipeline for the single best real
    opportunity this cycle finds. Never raises for an ordinary blocked
    step - every blocker lands in the returned dict's `human_actions`
    list, in order.

    `deployment_adapter=` lets a test inject `deployment.FakeDeploymentAdapter()`
    - the default (`None`) resolves the REAL, credential-gated GitHub
    Pages adapter, exactly like every other caller in this codebase."""
    human_actions: list[str] = []
    report: dict = {"ran_at": now_iso}

    # 1. Opportunity: real public-signal discovery -> deterministic select.
    try:
        discovery = discover_real_signals(data_dir, source_names=source_names,
                                          limit_per_source=limit_per_source,
                                          source_kwargs=source_kwargs)
        report["discovery"] = discovery.to_dict()
    except Exception as exc:  # noqa: BLE001 - one bad cycle never crashes the caller
        report["discovery_error"] = str(exc)
        report["human_actions"] = human_actions
        return report

    selection = select_opportunity(data_dir)
    report["selection"] = selection
    if selection["status"] != "SELECTED":
        human_actions.append(f"OPPORTUNITY: {selection['reason']}")
        report["human_actions"] = human_actions
        return report

    opportunity_id = selection["selected"]["opportunity_id"]
    rec = load_opportunities(data_dir).get(opportunity_id)
    draft = draft_from_record(rec)

    # 2. Affiliate Chain: content -> QC -> link -> GitHub Pages deploy.
    #    Autonomous ONLY if a GitHub credential already resolves (or a
    #    test explicitly injected a fake adapter - never a real bypass).
    adapter = deployment_adapter if deployment_adapter is not None else default_deployment_adapter()
    if not adapter.authorized:
        human_actions.append(
            "DEPLOYMENT: no GitHub Pages credential configured (GITHUB_TOKEN + "
            "GITHUB_PAGES_REPO) - set them yourself; the fleet never requests, "
            "prints, or rotates this credential")
        chain_result = {"status": "human_required", "step": "deploy_precheck",
                        "reason": "no GitHub Pages credential configured"}
    else:
        chain_result = run_affiliate_chain(data_dir, opportunity_id=opportunity_id,
                                           draft=draft, now_iso=now_iso,
                                           deployment_adapter=adapter)
        if chain_result["status"] != "completed":
            human_actions.append(
                f"AFFILIATE CHAIN ({chain_result.get('step', '?')}): "
                f"{chain_result.get('reason', '')}")
    report["chain"] = chain_result

    # 3. Distribution: Pinterest pin draft - only once something is
    #    actually live to point at. Rate-limited (our own policy, not an
    #    official Pinterest limit - see pinterest_pins.py).
    if chain_result.get("status") == "completed":
        asset = AffiliateAssetStore.load(data_dir).get(chain_result["asset_id"])
        try:
            pin = draft_pin(data_dir, asset=asset,
                            product_name=chain_result["match"]["product_name"],
                            category_label=draft.category, now_iso=now_iso)
            report["pin"] = pin.to_dict()
            human_actions.append(f"PINTEREST: review and post draft {pin.pin_id}")
        except PinDraftError as exc:
            report["pin_error"] = str(exc)
            human_actions.append(f"PINTEREST: {exc}")

    # 4. Digital product: parallel, best-effort. Runs regardless of the
    #    chain's outcome above and never affects it either way.
    try:
        product = dp.generate_product_draft(
            data_dir, opportunity_id=opportunity_id, topic=draft.title,
            evidence=tuple(draft.evidence or ()), category=draft.category, now_iso=now_iso)
        report["digital_product"] = product.to_dict()
        if not dp.any_platform_configured():
            human_actions.append(
                "DIGITAL PRODUCT: generated, but no Gumroad/Payhip account confirmed - "
                "create one yourself (free, no card) and upload the file at your own pace")
    except Exception as exc:  # noqa: BLE001 - never lets this block the chain result above
        report["digital_product_error"] = str(exc)

    # 5. Measurement + 6. Optimization - pure read/report, always safe.
    report["measurement"] = measure_opportunity(data_dir, opportunity_id)
    report["optimization"] = optimize(data_dir)
    report["human_actions"] = human_actions
    return report
