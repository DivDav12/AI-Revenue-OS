"""Affiliate Offer Discovery (Demand-First Affiliate architecture).

The missing orchestration layer: the real offer-source connectors
(`cj_offer_source`, `awin_offer_source`, `wondershare_offer_source`,
`systeme_offer_source`, resolved by `offer_sources.build_offer_source`)
already existed and were tested, but nothing outside the test suite ever
called them. `discover_offer_candidates()` closes that gap - and ONLY
that gap:

    PLANNABLE opportunity with a valid ProductIntent
      -> build_offer_source(network)          (only if actually authorized)
      -> source.search(intent, limit)          (real, read-only product search)
      -> OfferCandidate[]                       (verbatim search results)
      -> AffiliateOfferCandidateStore          (staged for human completion)

What this module deliberately does NOT do (unchanged fail-closed gates):
  * It never creates an `AffiliateOffer`. A candidate carries no
    commission terms and no join confirmation - a product search cannot
    state either - so it is never `usable` and is never matched, planned,
    or deployed against. A human completes it via
    `affiliate_sources.ingest_affiliate_offer` (CLI `affiliate-complete-
    offer`), supplying the verbatim commission evidence + join
    confirmation the schema already demands.
  * It never applies to, joins, or authenticates against any network.
  * It makes NO network call for a network that is not authorized/
    configured right now: `build_offer_source` returns a
    `HumanSetupRequiredOfferSource` for those, and this module skips it
    without calling `.search()` at all.
  * It never raises: a bad source, a transport error, or a malformed
    result is caught, recorded under `errors`, and the loop continues
    (same contract as `discovery.DiscoveryEngine` /
    `affiliate_pipeline.run_affiliate_tick`).

Pure orchestration - no new scoring, no new policy, no LLM, no money.
"""

from __future__ import annotations

from .affiliate_model import (
    NETWORK_HUMAN_FED,
    NETWORK_POLICY,
    AffiliateOfferCandidateStore,
    OfferCandidateRecord,
    offer_candidate_id,
)
from .offer_sources import HumanSetupRequiredOfferSource, build_offer_source
from .product_intent import ProductIntent

#: every network with an offer-source connector - `human_fed` is the
#: manual ingestion channel, not a searchable network (mirrors
#: `offer_sources._offer_source_networks`).
_ALL_OFFER_NETWORKS: tuple[str, ...] = tuple(
    n for n in NETWORK_POLICY if n != NETWORK_HUMAN_FED)


def _intent_from_record(rec: dict) -> ProductIntent | None:
    """Reconstruct the ProductIntent the demand-discovery layer already
    persisted on `discovery.product_intent`. Returns None (skip) unless a
    concrete product category was extracted - the exact bar
    `product_intent.extract_product_intent` itself uses."""
    pin = ((rec.get("discovery") or {}).get("product_intent")) or {}
    category = str(pin.get("category_phrase") or "").strip()
    if not category:
        return None
    return ProductIntent(
        category_phrase=category,
        intent=str(pin.get("intent") or ""),
        constraints=tuple(pin.get("constraints") or ()),
        provenance=str(pin.get("provenance") or ""))


def _resolve_networks(networks) -> list[str]:
    if not networks:
        return list(_ALL_OFFER_NETWORKS)
    known = set(_ALL_OFFER_NETWORKS)
    out: list[str] = []
    for n in networks:
        nn = (n or "").strip().lower()
        if nn in known and nn not in out:
            out.append(nn)
    return out


def discover_offer_candidates(data_dir, *, networks=None, limit: int = 10,
                              now_iso: str = "", environ=None) -> dict:
    """Search every AUTHORIZED offer source for products matching each
    PLANNABLE opportunity's ProductIntent, and stage the results for human
    completion. Deterministic and idempotent: candidates dedupe on
    `(network, product_id|url)` via `offer_candidate_id`, so a re-run only
    refreshes `last_seen_at` and never duplicates a row.

    `networks=` restricts the search to the named connectors (default:
    all). `environ=` overrides `os.environ` for the per-network
    credential check (used by tests) - never a real credential source.
    """
    from ..opportunity_store import load_opportunities
    from ..store import now_iso as _now_iso
    from .model import PLANNABLE

    ts = now_iso or _now_iso()
    lim = max(1, int(limit))
    requested = _resolve_networks(networks)

    # Resolve each network's source ONCE, before touching any opportunity -
    # a network that is not authorized right now never has `.search()`
    # called on it at all.
    live: dict[str, object] = {}
    skipped: list[dict] = []
    # surface a mistyped/unknown network name rather than silently ignoring it
    for raw in (networks or []):
        nn = (raw or "").strip().lower()
        if nn and nn not in requested:
            skipped.append({"network": nn, "reason": "unknown offer-source network"})
    for n in requested:
        try:
            src = build_offer_source(n, environ=environ)
        except Exception as exc:                     # noqa: BLE001 - isolate a bad factory call
            skipped.append({"network": n, "reason": f"build_offer_source error: {exc!r}"})
            continue
        if isinstance(src, HumanSetupRequiredOfferSource):
            skipped.append({"network": n, "reason": "not authorized / not configured"})
            continue
        live[n] = src

    store = AffiliateOfferCandidateStore.load(data_dir)
    scanned = 0
    new_ids: list[str] = []
    refreshed_ids: list[str] = []
    errors: list[str] = []

    if live:
        for rec in load_opportunities(data_dir).all():
            vstatus = ((rec.get("discovery") or {}).get("verification") or {}).get("status")
            if vstatus not in PLANNABLE:
                continue
            intent = _intent_from_record(rec)
            if intent is None:
                continue
            scanned += 1
            oid = rec.get("id", "")
            for n, src in live.items():
                try:
                    results = src.search(intent, lim) or []
                except Exception as exc:             # noqa: BLE001 - fail closed per source
                    errors.append(f"{n} search for {oid!r}: {exc!r}")
                    continue
                for c in results:
                    ident = (getattr(c, "product_id", "") or "").strip() \
                        or (getattr(c, "url", "") or "").strip()
                    title = (getattr(c, "title", "") or "").strip()
                    if not ident or not title:
                        continue   # never stage a candidate with no real identity/title
                    cid = offer_candidate_id(n, ident)
                    existing = store.get(cid)
                    if existing is not None:
                        existing.last_seen_at = ts
                        store.upsert(existing)
                        if cid not in refreshed_ids:
                            refreshed_ids.append(cid)
                        continue
                    store.upsert(OfferCandidateRecord(
                        candidate_id=cid, opportunity_id=oid, network=n,
                        product_name=title, category_phrase=intent.category_phrase,
                        product_url=getattr(c, "url", "") or "",
                        product_id=getattr(c, "product_id", "") or "",
                        price=float(getattr(c, "price", 0.0) or 0.0),
                        currency=getattr(c, "currency", "") or "",
                        availability=getattr(c, "availability", "") or "",
                        provenance=getattr(c, "provenance", "") or "",
                        confidence=float(getattr(c, "confidence", 0.0) or 0.0),
                        observed_at=getattr(c, "observed_at", "") or "",
                        first_seen_at=ts, last_seen_at=ts))
                    new_ids.append(cid)
        store.save()

    return {
        "ran_at": ts,
        "networks_requested": requested,
        "networks_live": sorted(live),
        "networks_skipped": skipped,
        "opportunities_scanned": scanned,
        "new_candidates": new_ids,
        "refreshed_candidates": refreshed_ids,
        "errors": errors,
    }
