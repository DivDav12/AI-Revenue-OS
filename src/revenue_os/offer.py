"""First paid offer proposal for a validated candidate.

propose_offer() is pure and deterministic (template-driven, no LLM).
The price is a non-binding default band by delivery type: the human
owner sets the real price. Nothing here charges anyone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Candidate, now_iso

# non-binding default price bands (USD) by delivery type
_PRICE_BY_DELIVERY = {
    "digital": 29.0,
    "manual": 250.0,
    "subscription": 19.0,
}

_CTA_BY_DELIVERY = {
    "digital": "Buy now and download instantly.",
    "manual": "Book a paid pilot this week.",
    "subscription": "Start a paid subscription.",
}


@dataclass(frozen=True)
class Offer:
    candidate_name: str
    what_is_sold: str
    price: float
    delivery: str  # "digital" | "manual" | "subscription"
    call_to_action: str
    currency: str = "USD"
    price_is_estimate: bool = True
    created_at: str = ""
    positioning: str = ""  # one line: who it's for + the pain (LLM path only)

    def to_dict(self) -> dict:
        return {
            "candidate_name": self.candidate_name,
            "what_is_sold": self.what_is_sold,
            "price": self.price,
            "delivery": self.delivery,
            "call_to_action": self.call_to_action,
            "currency": self.currency,
            "price_is_estimate": self.price_is_estimate,
            "created_at": self.created_at,
            "positioning": self.positioning,
        }


def _delivery_for(candidate: Candidate) -> str:
    test = str(candidate.plan.get("cheapest_test", "")).lower()
    if "landing" in test:
        return "digital"
    return "manual"


def paid_offer(
    candidate: Candidate,
    *,
    price: float,
    currency: str = "EUR",
    what_is_sold: str = "",
    delivery: str = "manual",
    call_to_action: str = "",
    positioning: str = "",
) -> Offer:
    """A real, human-priced first offer (price_is_estimate=False).

    Unlike propose_offer() this is not a template guess: the human owner
    passes the actual price and terms. Nothing here charges anyone; it
    only records what is being sold so the checkout page and the revenue
    ledger agree.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if delivery not in _CTA_BY_DELIVERY:
        raise ValueError(f"delivery must be one of {sorted(_CTA_BY_DELIVERY)}")
    return Offer(
        candidate_name=candidate.name,
        what_is_sold=what_is_sold or candidate.description or candidate.name,
        price=round(float(price), 2),
        delivery=delivery,
        call_to_action=call_to_action or _CTA_BY_DELIVERY[delivery],
        currency=currency.upper(),
        price_is_estimate=False,
        created_at=now_iso(),
        positioning=positioning,
    )


def propose_offer(candidate: Candidate) -> Offer:
    delivery = _delivery_for(candidate)
    return Offer(
        candidate_name=candidate.name,
        what_is_sold=candidate.description or candidate.name,
        price=_PRICE_BY_DELIVERY[delivery],
        delivery=delivery,
        call_to_action=_CTA_BY_DELIVERY[delivery],
        currency="USD",
        price_is_estimate=True,
        created_at=now_iso(),
    )
