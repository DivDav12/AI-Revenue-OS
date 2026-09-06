"""Awin real physical-product offer source (Demand-First Affiliate
architecture, Real Offer Discovery step - Germany/EU physical goods).

WHY AWIN, for physical consumer products (headphones, earbuds, keyboards,
monitors, cameras, home-office/smart-home gear, ...): Awin is Europe's
largest affiliate network and the dominant one in the DACH region -
mainstream EU/DE consumer-electronics and home-office retailers run
advertiser programs on it. It clears the bar Amazon's PA-API no longer
does (PA-API 5.0 is deprecated as of 2026; its Creators API replacement
requires 10 qualifying affiliate sales in a trailing 30-day window before
it grants access at all - unreachable before real sales already exist
through some other channel, see `affiliate_model.NETWORK_POLICY
[NETWORK_AMAZON_ASSOCIATES]`), and offers materially deeper EU/DE
physical-goods coverage than CJ Affiliate (`cj_offer_source.py`, still a
real, live, keyword-searchable connector for whichever advertiser
programs a human joins there - the two are complementary, not
either/or).

ACCESS MODEL, unautomatable, matches the `affiliate_model.NETWORK_POLICY
[NETWORK_AWIN]` entry exactly:
  1. A human applies for and is approved as an Awin publisher (real
     compliance review - the fleet never does this).
  2. A human is approved into >=1 specific Awin advertiser program(s) -
     a feed only exists for a program you have actually joined. The
     fleet never applies to join a program.
  3. A human uses Awin's own "Create-a-Feed" tool to configure a real
     product data feed for each approved advertiser and copies the
     resulting datafeed API key into AWIN_DATAFEED_API_KEY /
     AWIN_ADVERTISER_IDS - never in code, tests, or git.

SEARCH CAPABILITY - THE IMPORTANT ARCHITECTURAL DIFFERENCE FROM CJ: Awin
does not offer a live keyword-search API for products - see Awin's own
Product Feed Publisher Guide (help.awin.com/developers/docs/product-feed-
publisher-guide-intro), built for exactly this pattern ("price comparison
publishers... search, filter and display" against a downloaded feed).
`AwinOfferSource.search()` therefore (1) fetches the real, official Feed
List Download endpoint (`https://productdata.awin.com/datafeed/list/
apikey/<key>` - documented at help.awin.com/docs/product-feed-list-
download), which itself returns, per feed you are approved for, a ready
download URL; (2) for each feed whose advertiser id is in
`AWIN_ADVERTISER_IDS`, downloads that REAL feed file and searches its
REAL rows locally by keyword overlap against `intent.category_phrase`.
This is a real product search built on an official, documented feed -
never a fabricated live-search endpoint, never scraping, never a private
API. `discovery_mode = "feed_search"` names this explicitly (contrast
with `systeme_offer_source.py`'s `"curated"` - this source DOES scan a
real, changing catalog, it just does the filtering locally instead of
server-side).

SCHEMA CAVEAT (read before ever pointing this at a live account, exactly
the same caveat `cj_offer_source.py` carries): the exact column names in
an Awin feed are configured per advertiser via Create-a-Feed (a human
picks which mapped columns to include) - there is no single fixed global
schema. `_resolve_column()` therefore accepts several well-documented,
industry-standard Awin column name aliases per logical field (the same
names used across Awin's own docs and the wider ecosystem of Awin feed
importers) and SKIPS a row/feed it cannot map rather than guessing -
never a crash, never a fabricated field. Verify against a real downloaded
feed before the first live run and extend the alias lists in
`_COLUMN_ALIASES` if a specific advertiser's feed differs.

Standard library only (csv + urllib), same as every other real network
source in this codebase. No network call of any kind happens without a
verified `AwinConfig` (missing/invalid credentials degrade to `[]`,
exactly like `CjOfferSource`/`HumanSetupRequiredSource` - a source that
cannot act yields nothing, it never raises out of `search()`).
"""

from __future__ import annotations

import csv
import io
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import model
from .model import SourceMeta
from .offer_sources import OfferCandidate
from .product_intent import ProductIntent

_FEED_LIST_URL = "https://productdata.awin.com/datafeed/list/apikey/{api_key}"
_TIMEOUT = 15.0
#: hard ceiling on total product rows scanned across all matched feeds per
#: `search()` call - a real Awin feed can have hundreds of thousands of
#: rows; this bounds worst-case latency/memory without ever fabricating a
#: result, it simply stops looking once enough real candidates are found
#: or this many real rows have been examined.
_MAX_ROWS_SCANNED = 20_000
#: conservative, generic throttle between feed downloads - same
#: "err slow, not bursty" philosophy as `cj_offer_source.py`.
_MIN_INTERVAL = 0.5


def _http_get(url: str) -> bytes:
    """The one network primitive. Injected in tests - never called with a
    real key/network in this repo's test suite."""
    req = urllib.request.Request(url, headers={"Accept": "text/csv, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
        return resp.read()


class ConfigError(ValueError):
    """AWIN_DATAFEED_API_KEY / AWIN_ADVERTISER_IDS are missing - never
    guessed."""


@dataclass
class AwinConfig:
    """Real, human-supplied Awin datafeed access - never a default.
    `advertiser_ids` must be explicit: the fleet has no way to know which
    Awin advertiser programs a human has actually been approved for, and
    guessing one would risk reading a feed for a program that was never
    joined (same rule as `CjConfig.advertiser_ids`)."""

    api_key: str
    advertiser_ids: tuple[str, ...]

    @classmethod
    def from_env(cls, environ=None) -> "AwinConfig":
        import os

        environ = environ if environ is not None else os.environ
        api_key = environ.get("AWIN_DATAFEED_API_KEY", "").strip()
        raw_ids = environ.get("AWIN_ADVERTISER_IDS", "").strip()
        if not api_key:
            raise ConfigError("set AWIN_DATAFEED_API_KEY in the environment")
        advertiser_ids = tuple(a.strip() for a in raw_ids.split(",") if a.strip())
        if not advertiser_ids:
            raise ConfigError(
                "set AWIN_ADVERTISER_IDS - a comma-separated list of Awin "
                "advertiser ids a human has ALREADY been approved for (the "
                "fleet never joins a program itself - see "
                "NETWORK_POLICY['awin'])")
        return cls(api_key=api_key, advertiser_ids=advertiser_ids)


# ---------------------------------------------------------------------------
# column-name resolution - see module docstring's SCHEMA CAVEAT
# ---------------------------------------------------------------------------

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    # feed-list columns
    "advertiser_id": ("Advertiser ID", "AdvertiserId", "Merchant ID", "MerchantId"),
    "feed_url": ("URL", "Feed URL", "FeedURL", "Url"),
    # product-row columns
    "title": ("product_name", "Product Name", "title", "Title", "name"),
    "url": ("aw_deep_link", "merchant_deep_link", "deep_link", "aw_deeplink", "url", "URL"),
    "description": ("description", "Description", "product_short_description"),
    "category": ("merchant_category", "category_name", "Category", "category"),
    "price": ("search_price", "display_price", "price", "Price", "base_price_amount"),
    "currency": ("currency", "Currency"),
    "availability": ("in_stock", "stock_status", "InStock"),
    "product_id": ("aw_product_id", "merchant_product_id", "product_id", "ean", "EAN"),
}


def _resolve(row: dict, field: str) -> str:
    for name in _COLUMN_ALIASES[field]:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel   # comma-delimited default


def _parse_rows(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except (UnicodeDecodeError, LookupError):
            return []
    if not text.strip():
        return []
    dialect = _sniff_dialect(text[:4096])
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        return [dict(r) for r in reader if isinstance(r, dict)]
    except csv.Error:
        return []


def _price_within_budget(price_text: str, currency: str, intent: ProductIntent) -> bool:
    """Only filters when a REAL, currency-matched budget constraint exists
    (spec: never guess an FX conversion). No budget constraint, no
    parseable price, or a currency mismatch -> never excludes the row."""
    budget = None
    budget_ccy = ""
    for c in intent.constraints:
        m = re.match(r"^budget:([0-9.]+)([A-Z]{3})$", c)
        if m:
            budget, budget_ccy = float(m.group(1)), m.group(2)
            break
    if budget is None:
        return True
    if not currency or currency.upper() != budget_ccy:
        return True   # cannot safely compare across currencies - don't guess
    try:
        price = float(re.sub(r"[^0-9.]", "", price_text or ""))
    except ValueError:
        return True   # unparseable price - never exclude on a guess
    return price <= budget


class AwinOfferSource:
    """Real Awin product-feed search - see the module docstring for the
    full access model, search mechanism, and schema caveat."""

    meta = SourceMeta(
        source="awin", source_type="offer_search",
        source_url="https://developer.awin.com",
        access_method=model.ACCESS_OFFICIAL_API,
        automation_allowed=True, requires_login=False, requires_human=False,
        policy_status=model.POLICY_OK)

    discovery_mode = "feed_search"
    product_search_available = True

    _last_call_at: float = 0.0   # class-level, process-wide throttle clock

    def __init__(self, *, config: AwinConfig | None = None, fetch=None, environ=None) -> None:
        self._config = config
        self._environ = environ
        self._fetch = fetch or _http_get

    @property
    def authorized(self) -> bool:
        """Pure config check, no network call - mirrors
        `CjOfferSource.authorized`."""
        if self._config is not None:
            return True
        try:
            AwinConfig.from_env(self._environ)
            return True
        except ConfigError:
            return False

    def _throttled_fetch(self, url: str) -> bytes:
        now = time.monotonic()
        wait = AwinOfferSource._last_call_at + _MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        AwinOfferSource._last_call_at = time.monotonic()
        return self._fetch(url)

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        if limit <= 0 or not intent.category_phrase:
            return []
        try:
            cfg = self._config or AwinConfig.from_env(self._environ)
        except ConfigError:
            return []   # fail closed - no verified config yet

        try:
            feed_list_raw = self._throttled_fetch(_FEED_LIST_URL.format(api_key=cfg.api_key))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            return []   # fail closed - transport error

        feeds = _parse_rows(feed_list_raw)
        wanted = set(cfg.advertiser_ids)
        matched_feeds = [f for f in feeds if _resolve(f, "advertiser_id") in wanted]
        if not matched_feeds:
            return []   # none of the configured, human-approved ids are live feeds yet

        n = max(1, min(int(limit), 50))
        query_tokens = set(re.findall(r"[a-z0-9]+", intent.category_phrase.lower()))
        out: list[OfferCandidate] = []
        seen: set[tuple[str, str]] = set()
        scanned = 0

        from ..store import now_iso

        for feed in matched_feeds:
            if len(out) >= n:
                break
            feed_url = _resolve(feed, "feed_url")
            if not feed_url:
                continue
            try:
                product_raw = self._throttled_fetch(feed_url)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue   # one bad feed never kills the whole search
            for row in _parse_rows(product_raw):
                if scanned >= _MAX_ROWS_SCANNED or len(out) >= n:
                    break
                scanned += 1
                title = _resolve(row, "title")
                url = _resolve(row, "url")
                if not title or not url:
                    continue   # never invent a candidate with no real title/link
                haystack = f"{title} {_resolve(row, 'description')} {_resolve(row, 'category')}".lower()
                hay_tokens = set(re.findall(r"[a-z0-9]+", haystack))
                overlap = query_tokens & hay_tokens
                if not overlap:
                    continue
                price_text = _resolve(row, "price")
                currency = _resolve(row, "currency")
                if not _price_within_budget(price_text, currency, intent):
                    continue
                dedup_key = (title, url)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                try:
                    price = float(re.sub(r"[^0-9.]", "", price_text)) if price_text else 0.0
                except ValueError:
                    price = 0.0
                confidence = round(len(overlap) / max(1, len(query_tokens)), 3)
                out.append(OfferCandidate(
                    network="awin", title=title, url=url,
                    product_id=_resolve(row, "product_id"), price=price,
                    currency=currency, availability=_resolve(row, "availability"),
                    observed_at=now_iso(), provenance="awin:datafeed",
                    confidence=confidence))
        return out
