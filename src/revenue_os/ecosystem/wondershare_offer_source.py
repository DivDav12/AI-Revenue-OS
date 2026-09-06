"""Wondershare DE / PDFelement curated affiliate offer source (Demand-First
Affiliate architecture, Real Offer Discovery step - Awin advertiser 20202).

WHY A SEPARATE, CURATED SOURCE (not `awin_offer_source.py`): that module
searches a real, official Awin *product data feed* - it needs an approved
`AWIN_DATAFEED_API_KEY` and Create-a-Feed configuration per advertiser.
The Wondershare DE program is instead being integrated from ONE real,
already-issued Awin tracking link (the publisher has been approved into
advertiser 20202 and copied the exact `awin1.com/cread.php` deep link).
That is the same shape `systeme_offer_source.py` already handles for
systeme.io: a single verified affiliate link, not an automated catalog
connector - so this mirrors that module exactly rather than forcing a
feed architecture onto a link that has no feed.

`network` stays `awin` (`affiliate_model.NETWORK_AWIN`) - this is the Awin
network, just a different, link-only access mode. Nothing about
`NETWORK_POLICY` changes.

Two independent relevance gates apply, deliberately not merged (identical
rationale to `systeme_offer_source.py`):
  1. `_is_relevant_category()` here - a small, transparent, static keyword
     check standing in for the server-side filtering a real per-network
     search API would do. It only decides whether THIS curated source's
     one candidate is even discoverable for a given demand.
  2. `affiliate_matching.match_offers()` - the EXISTING, unmodified
     keyword/category relevance gate that runs later against whatever
     `AffiliateOffer` a human eventually ingests from this candidate (via
     `affiliate_sources.offer_candidate_to_payload()`, unchanged). This
     file never touches, weakens, or bypasses that gate.

Never fabricates price, commission, availability, or conversion data - a
curated single-link source has none of those to report, so the returned
`OfferCandidate` leaves every such field at its honest default. Turning
the candidate into a USABLE offer still requires a human to supply the
real, evidenced commission terms and confirm the join
(`offer_candidate_to_payload()` omits exactly those fields, so
`parse_offer_json()` fails closed until then - no automatic accept).

Fails closed on missing/malformed configuration. Performs no network call
of any kind (verifiable from this module's own imports; asserted by
test_wondershare_offer_source.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import model
from .affiliate_model import NETWORK_AWIN
from .model import SourceMeta
from .offer_sources import OfferCandidate
from .product_intent import ProductIntent

#: the real Awin advertiser id for the Wondershare DE program - a fact the
#: publisher supplied, not a guess. The configured tracking link's
#: `awinmid` must equal this or the source refuses to load.
_ADVERTISER_ID = "20202"

#: the real landing page the Awin deep link resolves to - a fact the
#: publisher supplied. The configured link's decoded `ued` destination
#: must point at a wondershare.com https URL.
_DESTINATION = "https://pdf.wondershare.com/pdfelement.html"

_OFFER_TITLE = "Wondershare PDFelement"

#: only Awin's real tracking host - never a shortener/redirector that
#: could hide (or break attribution for) where the link actually goes
#: (same rule as systeme_offer_source._SYSTEME_IO_HOSTS).
_AWIN_HOSTS = frozenset({"www.awin1.com", "awin1.com"})

#: static, transparent stand-in for what a real per-network search API
#: would filter server-side. PHRASES, not bare single words (systeme.io's
#: hard-won lesson: a bare "pdf" or "editor" token produces false
#: positives against unrelated demand). Every phrase is a genuine,
#: specific PDFelement product area (PDF editing / conversion / forms /
#: e-signing / OCR / Acrobat alternative).
_RELEVANT_PHRASES: tuple[str, ...] = (
    "pdf editor", "pdf-editor", "edit pdf", "edit pdfs", "editing pdf",
    "pdf editing", "pdf software", "pdf tool", "pdf app",
    "pdf converter", "convert pdf", "pdf to word", "word to pdf",
    "pdf form", "fill pdf", "fillable pdf", "pdf forms",
    "sign pdf", "e-sign pdf", "esign pdf", "pdf signature",
    "pdf ocr", "ocr pdf", "scanned pdf",
    "merge pdf", "split pdf", "compress pdf", "annotate pdf",
    "acrobat alternative", "adobe acrobat alternative", "alternative to acrobat",
    "alternative to adobe acrobat",
)


def _is_relevant_category(category_phrase: str) -> bool:
    phrase = (category_phrase or "").lower()
    if not phrase:
        return False
    return any(p in phrase for p in _RELEVANT_PHRASES)


class ConfigError(ValueError):
    """WONDERSHARE_AWIN_PUBLISHER_ID / WONDERSHARE_AWIN_TRACKING_URL are
    missing or malformed - never guessed, never silently patched up."""


@dataclass
class WondershareConfig:
    """The real, human-supplied Awin publisher identity + the exact
    tracking link Awin issued for advertiser 20202 - never a default,
    never invented."""

    publisher_id: str
    tracking_url: str

    @classmethod
    def from_env(cls, environ=None) -> "WondershareConfig":
        import os

        environ = environ if environ is not None else os.environ
        publisher_id = environ.get("WONDERSHARE_AWIN_PUBLISHER_ID", "").strip()
        tracking_url = environ.get("WONDERSHARE_AWIN_TRACKING_URL", "").strip()
        if not publisher_id or not tracking_url:
            raise ConfigError(
                "set WONDERSHARE_AWIN_PUBLISHER_ID and "
                "WONDERSHARE_AWIN_TRACKING_URL in the environment - your real "
                "Awin publisher id and the exact cread.php tracking link Awin "
                "issued for advertiser 20202 (see .env.example)")
        _validate_tracking_url(tracking_url, publisher_id)
        return cls(publisher_id=publisher_id, tracking_url=tracking_url)


def _validate_tracking_url(url: str, publisher_id: str) -> None:
    """Fail closed. Never repairs, rewrites, or guesses at a fix - either
    the configured link is exactly a real Awin deep link for advertiser
    20202 owned by `publisher_id` and pointing at wondershare.com, or this
    source refuses to use it at all."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ConfigError(f"WONDERSHARE_AWIN_TRACKING_URL is not a valid URL: {exc}") from exc
    if parsed.scheme != "https":
        raise ConfigError("WONDERSHARE_AWIN_TRACKING_URL must be https")
    if parsed.netloc.lower() not in _AWIN_HOSTS:
        raise ConfigError(
            f"WONDERSHARE_AWIN_TRACKING_URL host {parsed.netloc!r} is not "
            "Awin's tracking host (www.awin1.com) - refusing a URL that could "
            "hide where the link actually goes")
    if parsed.path != "/cread.php":
        raise ConfigError(
            f"WONDERSHARE_AWIN_TRACKING_URL path {parsed.path!r} is not "
            "/cread.php - not a recognised Awin click-through link")
    q = parse_qs(parsed.query)
    mid = (q.get("awinmid") or [""])[0]
    aff = (q.get("awinaffid") or [""])[0]
    ued = (q.get("ued") or [""])[0]
    if mid != _ADVERTISER_ID:
        raise ConfigError(
            f"WONDERSHARE_AWIN_TRACKING_URL awinmid={mid!r} is not the "
            f"Wondershare DE advertiser id ({_ADVERTISER_ID})")
    if aff != publisher_id:
        raise ConfigError(
            "WONDERSHARE_AWIN_TRACKING_URL's awinaffid does not match "
            "WONDERSHARE_AWIN_PUBLISHER_ID - refusing to guess which is correct")
    dest = urlparse(ued)
    if dest.scheme != "https" or not dest.netloc.lower().endswith("wondershare.com"):
        raise ConfigError(
            f"WONDERSHARE_AWIN_TRACKING_URL ued destination {ued!r} does not "
            "resolve to a wondershare.com https page")


class WondershareOfferSource:
    """The one real Wondershare DE / PDFelement offer, gated on verified
    config - see the module docstring for the full access model."""

    meta = SourceMeta(
        source=NETWORK_AWIN, source_type="offer_discovery_curated",
        source_url="https://www.awin.com",
        access_method=model.ACCESS_CURATED_FILE,
        automation_allowed=False, requires_login=False, requires_human=True,
        policy_status=model.POLICY_OK)

    #: explicit, not inferred - this is a single curated link, not a
    #: catalog search (mirrors systeme_offer_source.py). The feed-based
    #: `awin_offer_source.AwinOfferSource` is the search-capable Awin path.
    discovery_mode = "curated"
    product_search_available = False

    def __init__(self, *, config: WondershareConfig | None = None, environ=None) -> None:
        self._config = config
        self._environ = environ

    @property
    def authorized(self) -> bool:
        """Pure config check, no network call - mirrors
        `SystemeIoOfferSource.authorized`."""
        if self._config is not None:
            return True
        try:
            WondershareConfig.from_env(self._environ)
            return True
        except ConfigError:
            return False

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        if limit <= 0:
            return []
        if not _is_relevant_category(intent.category_phrase):
            return []
        try:
            cfg = self._config or WondershareConfig.from_env(self._environ)
        except ConfigError:
            return []   # fail closed - no verified config yet

        from ..store import now_iso

        return [OfferCandidate(
            network=NETWORK_AWIN, title=_OFFER_TITLE,
            url=cfg.tracking_url,   # EXACT, unmodified - the Awin deep link
                                    # is itself the redirector; never wrap it
            observed_at=now_iso(),
            provenance="awin:wondershare_curated_affiliate_link", confidence=1.0)]
