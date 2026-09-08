"""Measurement rollup for social distribution - real data only.

Sources of truth, never fabricated:
  * affiliate clicks   -> ecosystem/affiliate_links click store (populated
                          only by the real /go/<tracking_id> redirect
                          server once it is public)
  * Pinterest metrics  -> Pinterest API v5 pin analytics (impressions,
                          pin clicks, outbound clicks) for PUBLISHED pins
  * revenue            -> NOT here. Commissions are booked only from real
                          Amazon PartnerNet data via
                          `revenue_os affiliate-record-commission`.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..ecosystem import affiliate_revenue
from ..ecosystem.affiliate_links import link_economics
from ..store import now_iso as _now_iso
from .pinterest_api import PinterestClient, PinterestUnavailable
from .store import STATE_MEASURED, STATE_PUBLISHED, SocialActionStore


def measure(data_dir, *, days: int = 30, pinterest_client: PinterestClient | None = None,
            environ=None, now_iso: str = "") -> dict:
    now_iso = now_iso or _now_iso()
    store = SocialActionStore.load(data_dir)
    updated = 0
    per_action: list[dict] = []
    errors: list[str] = []

    end = date.today()
    start = end - timedelta(days=max(1, int(days)))
    pc = pinterest_client

    for act in store.all():
        metrics: dict = dict(act.metrics)

        # affiliate click economics (real redirect-server data only)
        if act.link_id:
            econ = link_economics(data_dir, act.link_id)
            if econ:
                metrics["affiliate_clicks"] = econ.get("click_count", 0)
                metrics["affiliate_recorded_clicks"] = econ.get("recorded_clicks", 0)

        # Pinterest analytics for a live pin
        pin_id = act.draft.get("pin_id", "")
        if act.platform == "pinterest" and act.state in (STATE_PUBLISHED, STATE_MEASURED) and pin_id:
            if pc is None:
                pc = PinterestClient(data_dir=data_dir, environ=environ)
            if pc.available:
                try:
                    an = pc.pin_analytics(pin_id, start_date=start.isoformat(),
                                          end_date=end.isoformat())
                    metrics["pinterest_analytics"] = an
                    metrics["pinterest_analytics_window"] = f"{start} .. {end}"
                except PinterestUnavailable as exc:
                    errors.append(f"{act.action_id}: {exc}")
            else:
                errors.append(f"{act.action_id}: Pinterest API not connected - "
                              "cannot pull pin analytics")

        if metrics != act.metrics:
            act.metrics = metrics
            act.updated_at = now_iso
            if act.state == STATE_PUBLISHED and (
                    metrics.get("affiliate_clicks") or metrics.get("pinterest_analytics")):
                act.state = STATE_MEASURED
            store.upsert(act)
            updated += 1

        if metrics:
            per_action.append({"action_id": act.action_id, "platform": act.platform,
                               "state": act.state, "link_id": act.link_id,
                               "metrics": metrics})

    store.save()

    # read-only revenue view per distinct link (real PartnerNet data only)
    revenue_by_opportunity: dict[str, dict] = {}
    for oid in sorted({a.opportunity_id for a in store.all() if a.opportunity_id}):
        try:
            revenue_by_opportunity[oid] = affiliate_revenue.opportunity_commission_summary(
                data_dir, oid)
        except Exception:  # noqa: BLE001 - revenue view is best-effort read-only
            pass

    return {
        "updated_actions": updated,
        "per_action": per_action,
        "revenue_by_opportunity": revenue_by_opportunity,
        "errors": errors,
        "note": ("Clicks are 0 until the /go redirect server is public "
                 "(AFFILIATE_TRACKING_BASE_URL). Revenue is only ever booked "
                 "from real Amazon PartnerNet reports."),
        "ran_at": now_iso,
    }
