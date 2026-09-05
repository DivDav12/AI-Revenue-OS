"""CJ Affiliate (Commission Junction) real product search adapter (Demand-
First Affiliate architecture, Real Offer Discovery step).

WHY CJ, not Amazon PA-API / Awin / Impact (see the accompanying research):
Amazon's PA-API access is currently blocked by a qualifying-sales
threshold we do not meet. Awin's publisher API is bulk-datafeed-only (no
live keyword search endpoint). Impact.com's product catalogs are gated
per-brand with no documented cross-catalog search for a new partner. CJ
Affiliate's GraphQL API (https://ads.api.cj.com/query, v1.0 as of
2026-05) is the only one of the three researched alternatives that
exposes a genuine live, keyword-searchable `products` query - the
closest fit to this codebase's `OfferSource.search(intent, limit)`
contract.

ACCESS MODEL (unchanged, unautomatable, matches
`affiliate_model.NETWORK_POLICY[NETWORK_CJ_AFFILIATE]` exactly - this
file introduces no new policy, it only implements what that policy entry
already described):
  1. A human applies for and is approved as a CJ publisher (real site
     review - the fleet never does this, see action_class.py's
     `join_affiliate_program` = HUMAN_REQUIRED, unchanged).
  2. A human is approved into >=1 specific CJ advertiser program(s) -
     `products`/`shoppingProducts` only ever return items from
     advertisers you have already joined. The fleet never requests to
     join a program.
  3. A human generates a Personal Access Token (PAT) in their CJ account
     and sets it via environment variables - never in code, tests, or
     git (spec: credentials strictly via env/secret config).

Once (and only once) all three exist, `CjOfferSource.search()` performs
a real, read-only GraphQL query - never a mutation, never a purchase,
never a program-join call. Missing/invalid credentials, any transport
error, a timeout, or an empty result all degrade to `[]` - this class
NEVER raises out of `search()` (same "a source that cannot act yields
nothing" contract as `sources.HumanSetupRequiredSource` and every real
demand source in `acquisition_sources.py`).

SCHEMA CAVEAT (read before ever pointing this at a live account): the
exact GraphQL field names in `_PRODUCTS_QUERY`/`_parse_products()` are
based on CJ's public developer documentation (keywords +
advertiserIds arguments, a `resultList` wrapper) - they were NOT
executed against a live CJ account in this session (no real CJ
credentials exist yet, and none were created or requested to write this
file). Before the first real, credentialed run, verify the query in
CJ's GraphiQL explorer (developers.cj.com) and adjust field names in
`_parse_products()` if the live schema differs - nothing else in this
file (credential gating, retry/backoff, the `OfferCandidate` shape, the
fail-closed contract) needs to change either way.

Standard library only (json + urllib), same as every other real network
source in this codebase.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import model
from .model import SourceMeta
from .offer_sources import OfferCandidate
from .product_intent import ProductIntent

_GRAPHQL_URL = "https://ads.api.cj.com/query"
_TIMEOUT = 10.0
#: conservative, generic throttle - CJ's exact published rate limit was
#: not verified in this session (no live account to check against); this
#: errs on the side of being slower than necessary rather than risking a
#: burst, same philosophy as acquisition_sources.StackExchangeSource.
_MIN_INTERVAL = 1.0
_MAX_RETRIES = 1   # one retry after a 429/backoff, then fail closed


def _http_post_graphql(query: str, variables: dict, *, token: str):
    """The one network primitive. Injected in tests - never called with
    a real token/network in this repo's test suite."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        _GRAPHQL_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "{}")


@dataclass
class CjConfig:
    """Real, human-supplied CJ credentials - never a default, never
    guessed. `advertiser_ids` must be explicit: the fleet has no way to
    know which CJ advertiser programs a human has actually been approved
    for, and guessing one would risk querying a program that was never
    joined."""

    personal_access_token: str
    company_id: str
    advertiser_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ=None) -> "CjConfig":
        import os

        environ = environ if environ is not None else os.environ
        pat = environ.get("CJ_PERSONAL_ACCESS_TOKEN", "").strip()
        cid = environ.get("CJ_COMPANY_ID", "").strip()
        raw_ids = environ.get("CJ_ADVERTISER_IDS", "").strip()
        if not pat or not cid:
            raise ValueError(
                "set CJ_PERSONAL_ACCESS_TOKEN and CJ_COMPANY_ID in the environment")
        advertiser_ids = tuple(a.strip() for a in raw_ids.split(",") if a.strip())
        if not advertiser_ids:
            raise ValueError(
                "set CJ_ADVERTISER_IDS - a comma-separated list of CJ advertiser "
                "program ids a human has ALREADY been approved for (the fleet "
                "never joins a program itself - see NETWORK_POLICY['cj_affiliate'])")
        return cls(personal_access_token=pat, company_id=cid, advertiser_ids=advertiser_ids)


_PRODUCTS_QUERY = """
query OfferDiscoverySearch($companyId: ID!, $keywords: String!, $advertiserIds: [ID!], $limit: Int) {
  products(companyId: $companyId, keywords: $keywords, advertiserIds: $advertiserIds, limit: $limit) {
    resultList {
      id
      title
      advertiserId
      advertiserName
      price { amount currency }
      buyUrl
      availability
    }
  }
}
""".strip()


class CjOfferSource:
    """Real CJ Affiliate product search - see the module docstring for
    the full access model and the schema caveat."""

    meta = SourceMeta(
        source="cj_affiliate", source_type="offer_search",
        source_url="https://developers.cj.com",
        access_method=model.ACCESS_OFFICIAL_API,
        automation_allowed=True, requires_login=False, requires_human=False,
        policy_status=model.POLICY_OK)

    _last_call_at: float = 0.0   # class-level, process-wide throttle clock

    def __init__(self, *, config: CjConfig | None = None, fetch=None, environ=None) -> None:
        self._config = config
        self._environ = environ
        self._fetch = fetch or _http_post_graphql

    @property
    def authorized(self) -> bool:
        """Pure config check, no network call - mirrors
        deployment.GitHubPagesDeploymentAdapter.authorized."""
        if self._config is not None:
            return True
        try:
            CjConfig.from_env(self._environ)
            return True
        except ValueError:
            return False

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = CjOfferSource._last_call_at + _MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        CjOfferSource._last_call_at = time.monotonic()

    def search(self, intent: ProductIntent, limit: int) -> list[OfferCandidate]:
        if not intent.category_phrase:
            return []
        try:
            cfg = self._config or CjConfig.from_env(self._environ)
        except ValueError:
            return []   # fail closed - no credentials / no approved advertiser yet

        n = max(1, min(int(limit), 25))
        variables = {"companyId": cfg.company_id, "keywords": intent.category_phrase,
                    "advertiserIds": list(cfg.advertiser_ids), "limit": n}

        body = None
        for attempt in range(_MAX_RETRIES + 1):
            self._throttle()
            try:
                body = self._fetch(_PRODUCTS_QUERY, variables, token=cfg.personal_access_token)
                break
            except urllib.error.HTTPError as exc:
                if getattr(exc, "code", None) == 429 and attempt < _MAX_RETRIES:
                    time.sleep(2.0)
                    continue
                return []   # fail closed - HTTP error, including repeated 429
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                return []   # fail closed - transport error / malformed JSON

        if body is None:
            return []
        return _parse_products(body)


def _parse_products(body) -> list[OfferCandidate]:
    from ..store import now_iso

    if not isinstance(body, dict):
        return []
    rows = (((body.get("data") or {}).get("products") or {}).get("resultList") or [])
    if not isinstance(rows, list):
        return []
    out: list[OfferCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("buyUrl") or "").strip()
        if not title or not url:
            continue   # never invent a candidate with no real title/link
        price_obj = row.get("price") or {}
        try:
            price = float(price_obj.get("amount") or 0.0) if isinstance(price_obj, dict) else 0.0
        except (TypeError, ValueError):
            price = 0.0
        out.append(OfferCandidate(
            network="cj_affiliate", title=title, url=url,
            product_id=str(row.get("id") or ""), price=price,
            currency=str(price_obj.get("currency") or "") if isinstance(price_obj, dict) else "",
            availability=str(row.get("availability") or ""),
            observed_at=now_iso(), provenance="cj_affiliate:products", confidence=0.0))
    return out
