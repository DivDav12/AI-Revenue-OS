"""Offer Discovery architecture (Demand-First Affiliate architecture,
Offer Discovery MVP).

The second bridge, after `product_intent.py`:

    ProductIntent -> OfferSource -> OfferCandidate[] -> (affiliate_sources
    bridge) -> AffiliateOffer[] -> the EXISTING, unmodified
    affiliate_matching.match_offers()/affiliate_profitability.evaluate()

This module ONLY defines the search-side abstraction and its result
shape. It does not touch matching, profitability, assets, links, or the
affiliate pipeline in any way - see `offer_selection.py` for the new
Multi-Offer selection step that consumes the EXISTING
`affiliate_matching.AffiliateMatch` unchanged.

Mirrors, deliberately, the exact two proven patterns already in this
codebase:

  - `sources.OpportunitySource`/`demand_sources.AcqSearchable` - a
    `.meta: SourceMeta` + one search method, real network sources opt-in
    by name via a `build_*` factory.
  - `sources.HumanSetupRequiredSource` - a source that needs an account/
    API key a human has not yet provided returns an EMPTY result and a
    `POLICY_HUMAN_SETUP_REQUIRED` status - it never "figures out" a
    login, never simulates data, never guesses.

No network call of any kind lives in this file. No Amazon PA-API
request, no scraping, no credentials are read here - `build_offer_source`
returns only `HumanSetupRequiredOfferSource` today, for every network in
`affiliate_model.NETWORK_POLICY` (all of which are, as of this step,
HUMAN_SETUP_REQUIRED - see affiliate_model.py's own module docstring:
"No fake PA-API connector is built"). A real, credentialed connector for
any network is an explicit, separate, later step.

`OfferCandidate` is deliberately NOT an `AffiliateOffer` and carries no
`usable`/`status` concept at all - it represents only what a real search
call actually returned (or would return), nothing else. See
`affiliate_sources.offer_candidate_to_payload()` for the one-way,
non-fabricating bridge into the existing, unmodified offer-ingestion
schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import model
from .affiliate_model import network_policy
from .model import SourceMeta
from .product_intent import ProductIntent

# ---------------------------------------------------------------------------
# OfferCandidate - a raw, verbatim search result. Never invents a field:
# a candidate is only ever built from what a real source call actually
# returned (or, until a real connector exists, never built at all).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferCandidate:
    """One real (or, in tests, fixture-real) product search result.

    `product_id` is the marketplace-specific identifier (e.g. an Amazon
    ASIN) - never invented, empty if the source did not provide one.
    `confidence` is the SOURCE's OWN relevance ranking for this result
    against the query (if it provides one) - NOT the same thing as
    `affiliate_matching.AffiliateMatch.match_score`, which is computed
    independently, later, from the demand text. `provenance` names which
    real call/source produced this (e.g. "amazon_associates:SearchItems")
    - always a fact about origin, never a judgement call."""

    network: str
    title: str
    url: str = ""
    product_id: str = ""
    price: float = 0.0
    currency: str = ""
    availability: str = ""
    observed_at: str = ""
    provenance: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "network": self.network, "title": self.title, "url": self.url,
            "product_id": self.product_id, "price": self.price,
            "currency": self.currency, "availability": self.availability,
            "observed_at": self.observed_at, "provenance": self.provenance,
            "confidence": self.confidence,
        }


class OfferSource(Protocol):
    """The uniform interface any real (or HUMAN_SETUP_REQUIRED) offer
    search provider implements - mirrors `sources.OpportunitySource`
    exactly, one level down (offers, not opportunities)."""

    meta: SourceMeta

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        ...


# ---------------------------------------------------------------------------
# the ONLY implementation this step ships: a source that needs a human to
# set an account/API key up first. Never simulates PA-API, never invents
# a product - see the module docstring.
# ---------------------------------------------------------------------------


class HumanSetupRequiredOfferSource:
    """`search()` always returns `[]`. No network call, no credentials
    read, no product ever fabricated. Reuses
    `affiliate_model.network_policy()` - the SAME policy table
    `affiliate_sources.py`'s ingestion path already consults - so this
    source's `policy_status`/setup guidance can never drift from what the
    rest of the system already says about that network."""

    def __init__(self, network: str) -> None:
        self.network = (network or "").strip().lower()
        policy = network_policy(self.network)
        self.meta = SourceMeta(
            source=self.network, source_type="offer_discovery_requires_setup",
            access_method=model.ACCESS_OFFICIAL_API,
            automation_allowed=False, requires_login=True, requires_human=True,
            policy_status=policy.get("status", model.POLICY_HUMAN_SETUP_REQUIRED))
        self.setup_steps: list = list(policy.get("setup_steps") or [])
        self.note: str = str(policy.get("note", ""))

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        return []


#: every network `affiliate_model.NETWORK_POLICY` already knows about,
#: minus "human_fed" (not a searchable network - it is the manual
#: ingestion channel for an account a human has already set up, see
#: affiliate_sources.py). Every one of these is HUMAN_SETUP_REQUIRED
#: today (spec: "No fake PA-API connector is built" applies to all of
#: them, not just Amazon) - registering them here costs nothing and
#: keeps this factory honest about what networks EXIST versus what is
#: actually WIRED UP for real search.
def _offer_source_networks() -> tuple[str, ...]:
    from .affiliate_model import NETWORK_HUMAN_FED, NETWORK_POLICY

    return tuple(n for n in NETWORK_POLICY if n != NETWORK_HUMAN_FED)


def build_offer_source(network: str, **kw) -> OfferSource:
    """Factory mirroring `sources.build_source()`/
    `demand_sources.build_demand_source()`. Every currently-known network
    (amazon_associates, shareasale, cj_affiliate, impact, awin,
    generic_saas_program) returns a `HumanSetupRequiredOfferSource` UNLESS
    a real, credentialed connector exists AND is actually authorized
    right now (env vars resolve cleanly, no network call made to check) -
    today that is `cj_affiliate` only, via `cj_offer_source.CjOfferSource`
    (spec: Real Offer Discovery step - see that module for the full
    access model). Every other network stays exactly as before: a
    real connector for it is a separate, later, explicitly-approved
    step. `environ=` (optional) overrides `os.environ` for the
    credential check - used by tests, never by real callers."""
    n = (network or "").strip().lower()
    known = _offer_source_networks()
    if n not in known:
        raise ValueError(
            f"unknown offer source network {network!r} - one of: " + ", ".join(sorted(known)))
    if n == "cj_affiliate":
        from .cj_offer_source import CjOfferSource

        src = CjOfferSource(environ=kw.get("environ"))
        if src.authorized:
            return src
    return HumanSetupRequiredOfferSource(n)
