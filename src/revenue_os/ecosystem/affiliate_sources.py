"""Affiliate offer ingestion (Affiliate Revenue Pipeline, spec section 1 + 20).

Mirrors `human_fed.py`'s pattern exactly: a human has ALREADY joined a real
affiliate program (the fleet never signs up for one, never requests API
credentials, never solves a CAPTCHA to get in) and is now typing in its
real, already-approved details. This is a bridge, not a discovery
mechanism - `ingest_affiliate_offer()` validates a strict schema and
writes one `AffiliateOffer` row; everything downstream (matching,
profitability, asset generation, linking) reuses the existing, unmodified
pipeline.

No fabricated offers, no fabricated commissions, no invented product
prices. `UNKNOWN` stays `UNKNOWN` (offer.status stays
POLICY_HUMAN_SETUP_REQUIRED for anything not explicitly human-confirmed as
joined/approved).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import model
from .affiliate_model import (
    AffiliateOffer,
    AffiliateOfferStore,
    CommissionModel,
    NETWORK_POLICY,
    network_policy,
    new_offer_id,
)

SCHEMA_VERSION = 1

_REQUIRED_FIELDS = frozenset({
    "schema_version", "network", "program_name", "product_name",
    "commission_kind", "human_confirmed_joined",
})
_OPTIONAL_FIELDS = frozenset({
    "product_url", "product_price", "currency", "price_is_estimate",
    "commission_rate", "commission_fixed_amount", "cookie_duration_days",
    "commission_evidence", "category", "keywords", "terms_url", "join_url",
    "eligibility_note", "evidence", "tracking_param",
})
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


class IngestionError(ValueError):
    """The ingested affiliate-offer JSON does not conform to the schema, or
    is otherwise structurally unsafe. Never creates an AffiliateOffer."""


def parse_offer_json(raw: dict) -> dict:
    """Strict schema validation (spec: no invented offers/commissions)."""
    if not isinstance(raw, dict):
        raise IngestionError("offer must be a JSON object")

    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        raise IngestionError(f"missing required fields: {sorted(missing)}")
    unknown = set(raw.keys()) - _ALL_FIELDS
    if unknown:
        raise IngestionError(f"unknown fields: {sorted(unknown)}")

    if raw.get("schema_version") != SCHEMA_VERSION:
        raise IngestionError(
            f"unsupported schema_version {raw.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})")

    network = str(raw.get("network") or "").strip()
    if not network:
        raise IngestionError("network must be non-empty")

    program_name = str(raw.get("program_name") or "").strip()
    product_name = str(raw.get("product_name") or "").strip()
    if not program_name or not product_name:
        raise IngestionError("program_name and product_name must be non-empty")

    kind = str(raw.get("commission_kind") or "").strip()
    if kind not in ("fixed", "percent", "recurring_percent"):
        raise IngestionError(
            f"commission_kind must be one of fixed/percent/recurring_percent, "
            f"got {kind!r}")

    if not isinstance(raw.get("human_confirmed_joined"), bool):
        raise IngestionError(
            "human_confirmed_joined must be a boolean - true only if a "
            "human has ALREADY been accepted into this real program")

    rate = raw.get("commission_rate", 0.0)
    fixed = raw.get("commission_fixed_amount", 0.0)
    try:
        rate = float(rate or 0.0)
        fixed = float(fixed or 0.0)
    except (TypeError, ValueError) as exc:
        raise IngestionError("commission_rate/commission_fixed_amount must be numeric") from exc
    if kind in ("percent", "recurring_percent") and not (0.0 < rate <= 1.0):
        raise IngestionError(
            "commission_rate must be a fraction in (0, 1] for a percent-based "
            "commission (e.g. 0.10 for 10%)")
    if kind == "fixed" and fixed <= 0.0:
        raise IngestionError("commission_fixed_amount must be > 0 for a fixed commission")

    evidence = raw.get("commission_evidence") or []
    if not isinstance(evidence, list) or not evidence or not all(
            isinstance(e, str) and e.strip() for e in evidence):
        raise IngestionError(
            "commission_evidence must be a non-empty list of verbatim quotes "
            "from the program's own terms/dashboard - no invented commission "
            "may be recorded without a stated source")

    price = raw.get("product_price", 0.0)
    try:
        price = float(price or 0.0)
    except (TypeError, ValueError) as exc:
        raise IngestionError("product_price must be numeric") from exc
    if price < 0:
        raise IngestionError("product_price cannot be negative")

    return dict(raw)


def _offer_status(network: str, human_confirmed_joined: bool) -> str:
    """Fail closed: only a network the human explicitly confirmed joining
    AND that our own policy table does not BLOCK becomes usable. A program
    on a network we've never heard of, or one the human has not confirmed
    joining, stays HUMAN_SETUP_REQUIRED."""
    policy = network_policy(network)
    if policy["status"] == model.POLICY_BLOCKED:
        return model.POLICY_BLOCKED
    if not human_confirmed_joined:
        return model.POLICY_HUMAN_SETUP_REQUIRED
    # a human-confirmed join on a recognised (or unrecognised-but-not-
    # blocked) network makes the offer usable - the human already did the
    # KYC/signup step the fleet is never allowed to do itself.
    return model.POLICY_OK


def ingest_affiliate_offer(data_dir, payload: dict, *, actor: str = "human") -> dict:
    """Validate + upsert one real affiliate offer. Idempotent on
    (network, program_name, product_name): a second ingest of the same
    triple updates the existing row rather than duplicating it."""
    from ..store import now_iso

    parsed = parse_offer_json(payload)
    status = _offer_status(parsed["network"], bool(parsed["human_confirmed_joined"]))

    store = AffiliateOfferStore.load(data_dir)
    existing = next(
        (o for o in store.all()
         if o.network == parsed["network"] and o.program_name == parsed["program_name"]
         and o.product_name == parsed["product_name"]), None)
    offer_id = existing.offer_id if existing else new_offer_id()

    commission = CommissionModel(
        kind=parsed["commission_kind"],
        rate=float(parsed.get("commission_rate", 0.0) or 0.0),
        fixed_amount=float(parsed.get("commission_fixed_amount", 0.0) or 0.0),
        currency=str(parsed.get("currency", "EUR")),
        cookie_duration_days=float(parsed.get("cookie_duration_days", 0.0) or 0.0),
        is_estimate=False,   # a human-supplied, evidenced commission is a fact
        evidence=tuple(parsed.get("commission_evidence") or ()))

    offer = AffiliateOffer(
        offer_id=offer_id, network=parsed["network"], program_name=parsed["program_name"],
        product_name=parsed["product_name"], product_url=str(parsed.get("product_url", "")),
        product_price=float(parsed.get("product_price", 0.0) or 0.0),
        currency=str(parsed.get("currency", "EUR")),
        price_is_estimate=bool(parsed.get("price_is_estimate", True)),
        commission=commission, category=str(parsed.get("category", "other")),
        keywords=tuple(parsed.get("keywords") or ()),
        terms_url=str(parsed.get("terms_url", "")), join_url=str(parsed.get("join_url", "")),
        eligibility_note=str(parsed.get("eligibility_note", "")),
        evidence=tuple(parsed.get("evidence") or ()), status=status,
        tracking_param=str(parsed.get("tracking_param", "")),
        added_at=now_iso(), added_by=actor, active=True)

    store.upsert(offer)
    store.save()

    policy = network_policy(parsed["network"])
    return {
        "offer_id": offer.offer_id, "network": offer.network,
        "program_name": offer.program_name, "product_name": offer.product_name,
        "status": offer.status, "usable": offer.usable,
        "updated_existing": existing is not None,
        "setup_steps": [] if offer.usable else policy.get("setup_steps", []),
        "note": policy.get("note", ""),
    }


def ingest_affiliate_offer_file(data_dir, path: str, *, actor: str = "human") -> dict:
    p = Path(path)
    if not p.exists():
        raise IngestionError(f"file not found: {path}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IngestionError(f"invalid JSON in {path}: {exc}") from exc
    return ingest_affiliate_offer(data_dir, raw, actor=actor)


def setup_required_networks(data_dir) -> list[dict]:
    """Every configured network that is not yet usable, with its exact
    remaining setup steps (spec section 20/26: "Setup required: X Y Z").
    Also lists the well-known networks that have NO offer on file yet at
    all, so the human sees the full picture, not just what was tried."""
    store = AffiliateOfferStore.load(data_dir)
    offers = store.all()
    seen_networks = {o.network for o in offers}
    out: list[dict] = []
    for o in offers:
        if not o.usable:
            out.append({"network": o.network, "program_name": o.program_name,
                        "product_name": o.product_name, "status": o.status,
                        "setup_steps": network_policy(o.network).get("setup_steps", [])})
    for net, policy in NETWORK_POLICY.items():
        if net == "human_fed" or net in seen_networks:
            continue
        out.append({"network": net, "program_name": "", "product_name": "",
                    "status": policy["status"], "setup_steps": policy.get("setup_steps", [])})
    return out
