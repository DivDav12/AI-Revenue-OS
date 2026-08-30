"""Ads Manager (#14, marketing cluster, HUMAN-GATED) - campaign PLANNING.

Produces a campaign plan and ad drafts from the offer, audience and
landing page. It never launches anything and never spends: every output
is a draft for a human to run. `launched` is always False and
`spent` is always 0.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_STAGES = ("reach", "click", "landing view", "checkout start", "paid")


def _variants(headline: str, positioning: str, what: str, price, currency: str) -> list:
    price_txt = f"{price} {currency}" if price is not None else ""
    return [
        {"angle": "problem-led",
         "primary_text": f"Struggling with {positioning or 'this problem'}? "
                         f"{headline}".strip(),
         "headline": headline or what, "cta": "Learn more"},
        {"angle": "outcome-led",
         "primary_text": f"{headline}. {what}".strip(". "),
         "headline": what or headline, "cta": "Get started"},
        {"angle": "offer-led",
         "primary_text": f"{what} - {price_txt}".strip(" -"),
         "headline": (f"{what} for {price_txt}").strip(" for"), "cta": "See the offer"},
    ]


def build_campaign_plan(offer: dict, *, audience: dict | None = None,
                        landing_page: str = "", positioning: str = "") -> dict:
    offer = offer or {}
    audience = audience or {}
    what = str(offer.get("what_is_sold") or "")
    headline = str(offer.get("headline") or offer.get("call_to_action") or what)
    positioning = positioning or str(offer.get("positioning") or "")
    price = offer.get("price")
    currency = str(offer.get("currency") or "EUR")

    channels = list(audience.get("channels") or ["search", "one relevant community"])
    daily = float(audience.get("suggested_daily_budget") or 0.0)
    test_days = int(audience.get("test_days") or 7)

    return {
        "campaign_plan": {
            "objective": "conversions (paid checkouts)",
            "channels": channels,
            "funnel_stages": list(_STAGES),
            "landing_page": landing_page or "<the checkout page from Store Builder>",
            "measurement": "one UTM-tagged link per channel; compare CPA to price",
            "kill_criteria": "pause a channel with 0 checkouts after the test spend",
        },
        "ad_variants": _variants(headline, positioning, what, price, currency),
        "targeting_hypotheses": [
            {"hypothesis": h, "test": "separate ad set / link"}
            for h in (audience.get("hypotheses")
                      or ["people actively searching for a solution",
                          "members of the niche community",
                          "followers of adjacent tools"])
        ],
        "estimated_budget": {
            "daily": daily, "test_duration_days": test_days,
            "total": round(daily * test_days, 2), "currency": currency,
            "basis": "planning estimate only - NOT authorized spend",
        },
        "launched": False,
        "spent": 0,
        "human_gate_required": True,
        "note": "draft campaign - a human launches and funds it, or does not",
    }


class AdsManagerAgent(Agent):
    role = "ads_manager"
    objective = "Draft a campaign plan and ad variants; never launch or spend."
    capabilities = ("run_ads",)

    def run(self, task: Task) -> Result:
        offer = task.payload.get("offer")
        if not isinstance(offer, dict) or not offer:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['offer'] must be a non-empty dict")
        aud = task.payload.get("audience")
        if aud is not None and not isinstance(aud, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['audience'] must be a dict when given")
        plan = build_campaign_plan(
            offer, audience=aud,
            landing_page=str(task.payload.get("landing_page", "")),
            positioning=str(task.payload.get("positioning", "")),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=plan)
