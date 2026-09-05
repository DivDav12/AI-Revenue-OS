"""Affiliate-specific profitability engine (spec section 3).

Deterministic, pure - mirrors `profitability.py`'s shape (every output
wrapped by `model.estimate()`, one comparator `decision_value` to sort
on) but models the axes unique to affiliate economics: commission shape,
click-through and conversion funnel, and content/distribution cost -
rather than reusing the generic per-opportunity-type table in
`profitability.py` (that module still runs unmodified for the strategy
engine's own AFFILIATE row; this is a MORE DETAILED, additive projection
used once a real offer has been matched).

Key property the spec calls out explicitly: a high commission does not
automatically win. `expected_revenue` is `traffic * ctr * conversion_rate
* commission_per_sale` - multiplicative, so a high commission with a
near-zero conversion rate can score below a modest commission with a
healthy conversion rate. No branch anywhere special-cases "commission is
high" to boost the result.
"""

from __future__ import annotations

from dataclasses import dataclass

from .affiliate_matching import AffiliateMatch
from .model import estimate

#: fleet time is not cash, but it is scarce - same price as profitability.py
_FLEET_HOUR_EUR = 2.0
#: hours to research + write one comparison/buying-guide asset (template-
#: rendered, not LLM-authored in this pass - see affiliate_assets.py).
_ASSET_BUILD_HOURS = 1.5

#: baseline monthly organic visits a single well-targeted, freshly-published
#: asset can plausibly reach with zero paid distribution - a conservative,
#: clearly-labelled T-shirt-size estimate (spec 7 + 20: never a fact),
#: scaled by how strong the underlying demand signal is (0.2x .. 1.0x).
_BASE_MONTHLY_TRAFFIC = 40.0

#: click-through from an asset's CTA to the affiliate offer - a properly
#: targeted comparison/buying-guide page, not a generic banner.
_DEFAULT_CTR = 0.12
#: conversion rate on the MERCHANT's site after the click - the fleet has
#: no control over this; a conservative cross-category default.
_DEFAULT_CONVERSION_RATE = 0.02

#: risk rises with: an estimated (not source-confirmed) price, a very short
#: or unstated cookie window (less time for the click to convert), and a
#: recurring-percent commission (payout depends on the buyer staying
#: subscribed, which the fleet cannot influence or verify from outside).
_BASE_RISK = 0.35


@dataclass
class AffiliateProfitability:
    expected_traffic: dict
    expected_clicks: dict
    expected_conversions: dict
    expected_commission_per_sale: dict
    expected_revenue: dict
    content_cost: dict
    distribution_cost: dict
    paid_ad_cost: dict
    expected_cost: dict
    expected_profit: dict
    confidence: dict
    risk: dict
    automation_level: dict
    time_to_revenue_days: dict
    decision_value: dict
    is_estimate: bool = True

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "expected_traffic", "expected_clicks", "expected_conversions",
            "expected_commission_per_sale", "expected_revenue", "content_cost",
            "distribution_cost", "paid_ad_cost", "expected_cost", "expected_profit",
            "confidence", "risk", "automation_level", "time_to_revenue_days",
            "decision_value")} | {"is_estimate": True}


def evaluate(match: AffiliateMatch, *, distribution_cost_eur: float = 0.0,
            paid_ad_cost_eur: float = 0.0,
            ctr: float | None = None,
            conversion_rate: float | None = None) -> AffiliateProfitability:
    """Project the economics of one demand<->offer match. `distribution_cost_eur`
    / `paid_ad_cost_eur` default to 0 (organic-only, spec section 21's
    "small first test") - a caller planning a paid campaign passes the
    real budgeted numbers (see affiliate_scaling.py / a future campaign
    model, spec section 10)."""
    offer = match.offer

    traffic = round(_BASE_MONTHLY_TRAFFIC * max(0.2, min(1.0, 0.2 + 0.8 * match.demand_strength)), 1)
    ctr_v = float(ctr) if ctr is not None else _DEFAULT_CTR
    conv_v = float(conversion_rate) if conversion_rate is not None else _DEFAULT_CONVERSION_RATE
    clicks = round(traffic * ctr_v, 2)
    conversions = round(clicks * conv_v, 4)

    commission_per_sale = offer.commission.expected_commission(offer.product_price)
    revenue = round(conversions * commission_per_sale, 2)

    content_cost = round(_ASSET_BUILD_HOURS * _FLEET_HOUR_EUR, 2)
    cost = round(content_cost + max(0.0, distribution_cost_eur) + max(0.0, paid_ad_cost_eur), 2)
    profit = round(revenue - cost, 2)

    # confidence: how much of this projection rests on real, human-verified
    # facts vs. defaults - match strength, a non-estimated price, and a
    # stated cookie window each raise it; nothing here can push it above 0.9
    # (this is still a projection, never a guarantee).
    confidence = 0.25
    confidence += 0.30 * match.match_score
    confidence += 0.15 if not offer.price_is_estimate else 0.0
    confidence += 0.10 if not offer.commission.is_estimate else 0.0
    confidence += 0.10 if offer.commission.cookie_duration_days > 0 else 0.0
    confidence = round(min(0.90, confidence), 3)

    risk = _BASE_RISK
    risk += 0.15 if offer.price_is_estimate else 0.0
    risk += 0.10 if offer.commission.cookie_duration_days <= 0 else 0.0
    risk += 0.10 if offer.commission.kind == "recurring_percent" else 0.0
    risk = round(min(0.95, risk), 3)

    # automation_level: everything up to "distribute on owned channels" is
    # fully autonomous (template asset + link + deploy); only paid spend or
    # a non-owned distribution channel would need a human, tracked
    # separately by distribution.py / action_class.py, not folded in here.
    automation = 0.90

    time_to_revenue_days = 30.0 if match.demand_strength >= 0.5 else 45.0

    dv = round(profit * confidence * (1.0 - 0.5 * risk) * (0.5 + 0.5 * automation), 3)

    return AffiliateProfitability(
        expected_traffic=estimate(traffic, "base monthly traffic x demand-strength scaling"),
        expected_clicks=estimate(clicks, f"traffic x CTR {ctr_v}"),
        expected_conversions=estimate(conversions, f"clicks x conversion-rate {conv_v}"),
        expected_commission_per_sale=estimate(commission_per_sale,
                                              "offer.commission.expected_commission(product_price)"),
        expected_revenue=estimate(revenue, "conversions x commission_per_sale"),
        content_cost=estimate(content_cost, f"{_ASSET_BUILD_HOURS}h @ EUR {_FLEET_HOUR_EUR}/h"),
        distribution_cost=estimate(round(max(0.0, distribution_cost_eur), 2), "caller-supplied, default 0 (organic only)"),
        paid_ad_cost=estimate(round(max(0.0, paid_ad_cost_eur), 2), "caller-supplied, default 0 (no paid spend)"),
        expected_cost=estimate(cost, "content + distribution + paid-ad cost"),
        expected_profit=estimate(profit, "revenue - cost"),
        confidence=estimate(confidence, "match strength + fact-vs-estimate bonuses, capped 0.90"),
        risk=estimate(risk, "base risk + price/cookie/commission-shape penalties"),
        automation_level=estimate(automation, "asset+link+deploy are autonomous; distribution/paid-spend are not"),
        time_to_revenue_days=estimate(time_to_revenue_days, "faster for strongly-demanded categories"),
        decision_value=estimate(dv, "profit x confidence x (1-0.5*risk) x (0.5+0.5*automation)"),
    )
