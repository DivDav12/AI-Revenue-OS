"""systeme.io real, curated affiliate offer source (Demand-First Affiliate
architecture, Real Offer Discovery step).

RESEARCH FINDING (verified against systeme.io's own public affiliate/help
pages before writing this file - no scraping, no private/internal
endpoint inspection, no login): systeme.io's affiliate program is a
single-product "refer systeme.io itself" program - there is no official
public marketplace/catalog of many third-party products to search, and no
documented public API for affiliates to query products/offers
programmatically (unlike CJ Affiliate's GraphQL `products` query - see
`cj_offer_source.py`). An affiliate is given ONE tracked link
(`https://systeme.io/<path>?sa=<affiliate_id>`) that always points at
systeme.io's own signup/landing pages; the `sa=` query parameter is how
systeme.io attributes the referral - it is not a redirector and must
never be altered or wrapped in a way that would change what systeme.io
itself observes as the destination.

CONSEQUENCE for this adapter (spec: "do not force systeme.io into an
architecture it does not support"): `SystemeIoOfferSource.search()` never
makes a network call and never returns more than the ONE real, configured
offer - systeme.io the platform itself. It is a curated source, not a
search API client - `discovery_mode = "curated"` and
`product_search_available = False` make that explicit rather than
pretending an automated catalog exists (mirrors the module-level
`systeme_io:` shape sketched in the architecture review).

Two independent relevance gates apply, deliberately not merged into one:
  1. `_is_relevant_category()` here - a small, transparent, static keyword
     check standing in for what a real per-network search API would do
     server-side (exactly the role `intent.category_phrase` plays as the
     literal query string sent to CJ's API in `cj_offer_source.py`) - it
     only decides whether THIS curated source's one candidate is even
     discoverable for a given demand.
  2. `affiliate_matching.match_offers()` - the EXISTING, unmodified,
     keyword/category relevance gate that runs later against whatever
     `AffiliateOffer` a human actually ingests from this candidate (via
     `affiliate_sources.offer_candidate_to_payload()`, unchanged). This
     file never touches, weakens, or bypasses that gate.

Never fabricates price, commission, availability, EPC, or conversion
data - a curated single-link source has none of those to report, so the
returned `OfferCandidate` leaves every such field at its honest default.
Fails closed on missing or malformed configuration; performs no network
call of any kind (verifiable from this module's own imports, same
structural guarantee `test_offer_sources.py` already checks for
`offer_sources.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import model
from .model import SourceMeta
from .offer_sources import OfferCandidate
from .product_intent import ProductIntent

NETWORK_SYSTEME_IO = "systeme_io"

#: real Amazon-style host allowlist, but for systeme.io - never a
#: redirector/shortener that could hide (or break attribution for) where
#: the "verified" affiliate link actually goes.
_SYSTEME_IO_HOSTS = frozenset({"systeme.io", "www.systeme.io"})

#: the one product this source represents - systeme.io the platform
#: itself. Not a guess: this IS the entire affiliate program.
_OFFER_TITLE = "systeme.io - all-in-one funnel, email & online business platform"

#: static, transparent stand-in for what a real per-network search API
#: would filter server-side (see module docstring, gate 1 of 2). Every
#: token here is a genuine systeme.io product area (funnels, funnel
#: builder, funnel software, marketing automation, email marketing,
#: website/landing-page builder, membership/course platform, CRM,
#: creator/online-business software) - deliberately NOT generic hardware/
#: consumer-product words, so an unrelated demand (microphones,
#: headphones, gaming gear, VPNs, ...) never matches.
_RELEVANT_TOKENS = frozenset({
    "funnel", "funnels", "automation", "website", "builder", "crm",
    "landing", "membership", "course", "marketing", "creator", "business",
    "platform",
})


def _is_relevant_category(category_phrase: str) -> bool:
    import re

    tokens = set(re.findall(r"[a-z0-9]+", (category_phrase or "").lower()))
    return bool(tokens & _RELEVANT_TOKENS)


class ConfigError(ValueError):
    """SYSTEME_IO_AFFILIATE_ID / SYSTEME_IO_AFFILIATE_URL are missing or
    malformed - never guessed, never silently patched up."""


@dataclass
class SystemeIoConfig:
    """The real, human-supplied systeme.io affiliate identity - never a
    default, never invented. `affiliate_url` must be the exact link
    systeme.io issued (host + `sa=` query param matching `affiliate_id`
    exactly) - see `_validate_affiliate_url()`."""

    affiliate_id: str
    affiliate_url: str

    @classmethod
    def from_env(cls, environ=None) -> "SystemeIoConfig":
        import os

        environ = environ if environ is not None else os.environ
        affiliate_id = environ.get("SYSTEME_IO_AFFILIATE_ID", "").strip()
        affiliate_url = environ.get("SYSTEME_IO_AFFILIATE_URL", "").strip()
        if not affiliate_id or not affiliate_url:
            raise ConfigError(
                "set SYSTEME_IO_AFFILIATE_ID and SYSTEME_IO_AFFILIATE_URL in "
                "the environment - the real affiliate id and link systeme.io "
                "issued you (see .env.example)")
        _validate_affiliate_url(affiliate_url, affiliate_id)
        return cls(affiliate_id=affiliate_id, affiliate_url=affiliate_url)


def _validate_affiliate_url(url: str, affiliate_id: str) -> None:
    """Fail closed (spec: "malformed affiliate URL fails closed"). Never
    repairs, rewrites, or guesses at a fix - either the configured link is
    exactly the real systeme.io affiliate link, or this source refuses to
    use it at all."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ConfigError(f"SYSTEME_IO_AFFILIATE_URL is not a valid URL: {exc}") from exc
    if parsed.scheme != "https":
        raise ConfigError("SYSTEME_IO_AFFILIATE_URL must be https")
    host = parsed.netloc.lower()
    if host not in _SYSTEME_IO_HOSTS:
        raise ConfigError(
            f"SYSTEME_IO_AFFILIATE_URL host {host!r} is not systeme.io - "
            "refusing a URL that could hide where the link actually goes "
            "(no shorteners/redirectors)")
    sa_values = parse_qs(parsed.query).get("sa") or []
    if not sa_values or sa_values[0] != affiliate_id:
        raise ConfigError(
            "SYSTEME_IO_AFFILIATE_URL's sa= query parameter does not match "
            "SYSTEME_IO_AFFILIATE_ID - refusing to guess which one is correct")


class SystemeIoOfferSource:
    """The one real systeme.io offer, gated on verified config - see the
    module docstring for the full access model."""

    meta = SourceMeta(
        source=NETWORK_SYSTEME_IO, source_type="offer_discovery_curated",
        source_url="https://systeme.io",
        access_method=model.ACCESS_CURATED_FILE,
        automation_allowed=False, requires_login=False, requires_human=True,
        policy_status=model.POLICY_OK)

    #: explicit, not inferred - see module docstring. A future official
    #: catalog/API for systeme.io would change this to True and add a real
    #: network call; nothing else about this class's contract would need
    #: to change.
    discovery_mode = "curated"
    product_search_available = False

    def __init__(self, *, config: SystemeIoConfig | None = None, environ=None) -> None:
        self._config = config
        self._environ = environ

    @property
    def authorized(self) -> bool:
        """Pure config check, no network call - mirrors
        `CjOfferSource.authorized`."""
        if self._config is not None:
            return True
        try:
            SystemeIoConfig.from_env(self._environ)
            return True
        except ConfigError:
            return False

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        if limit <= 0:
            return []
        if not _is_relevant_category(intent.category_phrase):
            return []
        try:
            cfg = self._config or SystemeIoConfig.from_env(self._environ)
        except ConfigError:
            return []   # fail closed - no verified config yet

        from ..store import now_iso

        return [OfferCandidate(
            network=NETWORK_SYSTEME_IO, title=_OFFER_TITLE,
            url=cfg.affiliate_url,   # EXACT, unmodified - preserves sa= attribution
            observed_at=now_iso(),
            provenance="systeme_io:curated_affiliate_link", confidence=1.0)]
