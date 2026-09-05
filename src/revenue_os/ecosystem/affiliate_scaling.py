"""Affiliate optimization + winner scaling (spec sections 13-14).

Reuses the EXISTING generic learning store (`learning.OutcomeStore`,
already populated by `affiliate_revenue.confirm_commission`/
`reverse_commission` with `strategy="AFFILIATE"` rows) instead of
building a second learning system. This module only adds the
affiliate-specific READ side: ranking live links/assets by the same
"profitability first, then conversion, then traffic" order the spec
requires, and a simple, transparent scale/hold/stop verdict per asset.

Deterministic, no ML, no LLM - a plain, explainable sort + threshold,
exactly like `strategy.py`/`learning.priority_weights()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .affiliate_model import AffiliateLinkStore
from .learning import OutcomeStore

#: an asset/link needs at least this many clicks before a scale/stop
#: verdict is trusted - below this, "insufficient data" (avoid
#: over-fitting a single lucky/unlucky click, same spirit as
#: learning.py's _MIN_SETTLED floor).
_MIN_CLICKS_FOR_VERDICT = 20

SCALE = "SCALE"
HOLD = "HOLD"
STOP = "STOP"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class LinkPerformance:
    link_id: str
    opportunity_id: str
    asset_id: str
    offer_id: str
    source: str
    click_count: int
    conversion_count: int
    commission_eur: float
    cost_eur: float
    profit_eur: float
    conversion_rate: float
    verdict: str
    reason: str

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "link_id", "opportunity_id", "asset_id", "offer_id", "source",
            "click_count", "conversion_count", "commission_eur", "cost_eur",
            "profit_eur", "conversion_rate", "verdict", "reason")}


def _verdict(clicks: int, profit: float, conversion_rate: float) -> tuple:
    if clicks < _MIN_CLICKS_FOR_VERDICT:
        return (INSUFFICIENT_DATA,
                f"only {clicks} clicks (< {_MIN_CLICKS_FOR_VERDICT} minimum) - "
                "not enough data for a scale/stop call yet")
    if profit > 0 and conversion_rate > 0:
        return (SCALE, f"profitable (EUR {profit:.2f}) with a real conversion "
               f"rate ({conversion_rate:.1%}) - promote: more distribution, "
               "consider a similar asset/offer combination")
    if profit <= 0 and clicks >= _MIN_CLICKS_FOR_VERDICT:
        return (STOP, f"EUR {profit:.2f} profit after {clicks} clicks with no "
               "positive economics - stop further distribution spend/effort here")
    return (HOLD, "economics are marginal - keep running, re-check after more data")


def rank_links(data_dir) -> list[LinkPerformance]:
    """Primary sort: profitability. Secondary: conversion rate. Tertiary:
    clicks/traffic (spec section 13's explicit ordering) - never clicks
    alone."""
    out: list[LinkPerformance] = []
    for link in AffiliateLinkStore.load(data_dir).all():
        conv_rate = round(link.conversion_count / link.click_count, 4) if link.click_count else 0.0
        verdict, reason = _verdict(link.click_count, link.profit_eur, conv_rate)
        out.append(LinkPerformance(
            link_id=link.link_id, opportunity_id=link.opportunity_id,
            asset_id=link.asset_id, offer_id=link.offer_id, source=link.source,
            click_count=link.click_count, conversion_count=link.conversion_count,
            commission_eur=link.commission_eur, cost_eur=link.cost_eur,
            profit_eur=link.profit_eur, conversion_rate=conv_rate,
            verdict=verdict, reason=reason))
    out.sort(key=lambda p: (-p.profit_eur, -p.conversion_rate, -p.click_count))
    return out


def optimization_report(data_dir) -> dict:
    ranked = rank_links(data_dir)
    return {
        "ranked_links": [p.to_dict() for p in ranked],
        "scale": [p.to_dict() for p in ranked if p.verdict == SCALE],
        "stop": [p.to_dict() for p in ranked if p.verdict == STOP],
        "hold": [p.to_dict() for p in ranked if p.verdict == HOLD],
        "insufficient_data": [p.to_dict() for p in ranked if p.verdict == INSUFFICIENT_DATA],
        "priority_weights": OutcomeStore.load(data_dir).priority_weights(),
    }
