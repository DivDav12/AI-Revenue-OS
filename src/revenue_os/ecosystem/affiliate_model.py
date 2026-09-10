"""Affiliate data model + persisted stores (Affiliate Revenue Pipeline).

Reuses the existing ecosystem vocabulary (`model.py`'s POLICY_*/FACT/
ESTIMATED/UNKNOWN conventions, `learning.py`'s load/save-a-JSON-list
pattern) rather than inventing a parallel one. No network, no I/O beyond
plain JSON files under `data_dir` - exactly like `learning.OutcomeStore`
and `revenue.RevenueLedger`.

Design rule carried over from `model.py` (spec: no fabricated data): an
`AffiliateOffer` a human has not actually joined/confirmed stays
`POLICY_HUMAN_SETUP_REQUIRED` forever - nothing here upgrades a program's
status on its own. Every commission amount defaults to `is_estimate=True`
until a real settlement is recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import model
from .model import ORIGIN_REAL, ORIGIN_SYNTHETIC  # noqa: F401 - re-exported for callers

# ---------------------------------------------------------------------------
# commission shapes
# ---------------------------------------------------------------------------

COMMISSION_FIXED = "fixed"                    # a flat amount per conversion
COMMISSION_PERCENT = "percent"                # a % of sale price, one-off
COMMISSION_RECURRING_PERCENT = "recurring_percent"  # a % of sale price, repeating
COMMISSION_KINDS = (COMMISSION_FIXED, COMMISSION_PERCENT, COMMISSION_RECURRING_PERCENT)


@dataclass(frozen=True)
class CommissionModel:
    """What the program actually pays - never a guess dressed up as a fact.
    `is_estimate=False` is reserved for a source-stated, verifiable number
    (exactly PaymentEvidence's convention in model.py)."""
    kind: str = COMMISSION_PERCENT
    rate: float = 0.0                 # 0..1, used when kind is a percent kind
    fixed_amount: float = 0.0         # EUR, used when kind == COMMISSION_FIXED
    currency: str = "EUR"
    cookie_duration_days: float = 0.0  # 0 = unknown/unstated
    is_estimate: bool = True
    evidence: tuple = ()

    def expected_commission(self, sale_price: float) -> float:
        """One conversion's expected payout for a given sale price. Pure
        arithmetic - callers decide how to discount for probability."""
        price = max(0.0, float(sale_price or 0.0))
        if self.kind == COMMISSION_FIXED:
            return round(max(0.0, self.fixed_amount), 2)
        if self.kind in (COMMISSION_PERCENT, COMMISSION_RECURRING_PERCENT):
            return round(price * max(0.0, min(1.0, self.rate)), 2)
        return 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "rate": self.rate,
                "fixed_amount": round(float(self.fixed_amount), 2),
                "currency": self.currency,
                "cookie_duration_days": self.cookie_duration_days,
                "is_estimate": bool(self.is_estimate),
                "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, d: dict) -> "CommissionModel":
        d = d or {}
        return cls(kind=d.get("kind", COMMISSION_PERCENT), rate=float(d.get("rate", 0.0) or 0.0),
                   fixed_amount=float(d.get("fixed_amount", 0.0) or 0.0),
                   currency=d.get("currency", "EUR"),
                   cookie_duration_days=float(d.get("cookie_duration_days", 0.0) or 0.0),
                   is_estimate=bool(d.get("is_estimate", True)),
                   evidence=tuple(d.get("evidence") or ()))


# ---------------------------------------------------------------------------
# affiliate network / program policy table (spec section 1) - DATA, not code,
# same shape and same fail-closed default as human_fed.PLATFORM_POLICY.
# Every network defaults to HUMAN_SETUP_REQUIRED: none of these can be
# safely used without a human-owned account/API key, so no fake connector
# is built for any of them. "human_fed" is the one exception - it is not a
# network, it is the ingestion channel for an account a human has ALREADY
# set up and is now feeding real details for (see affiliate_sources.py).
# ---------------------------------------------------------------------------

NETWORK_AMAZON_ASSOCIATES = "amazon_associates"
NETWORK_SHAREASALE = "shareasale"
NETWORK_CJ_AFFILIATE = "cj_affiliate"
NETWORK_IMPACT = "impact"
NETWORK_AWIN = "awin"
NETWORK_GENERIC_SAAS = "generic_saas_program"
NETWORK_SYSTEME_IO = "systeme_io"
NETWORK_HUMAN_FED = "human_fed"

NETWORK_POLICY: dict[str, dict] = {
    NETWORK_AMAZON_ASSOCIATES: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Apply for an Amazon Associates account (requires a live, "
            "policy-compliant site/app - Amazon reviews the application).",
            "Once approved, generate a PA-API 5.0 access key + secret key "
            "in the Associates portal.",
            "Provide the access key, secret key, and Associate/Partner tag "
            "via environment variables (never commit them) so the fleet "
            "can look up real product price/availability through PA-API.",
        ],
        "note": "No fake PA-API connector is built - product data and "
                "commission rates are program-defined and require a live, "
                "authenticated API call. As of 2026, Amazon's Product "
                "Advertising API (PA-API 5.0) is deprecated in favour of "
                "the Creators API, which itself requires 10 qualifying "
                "affiliate sales in the trailing 30 days before API access "
                "is granted - not realistically reachable before real "
                "sales already exist through some other channel.",
    },
    NETWORK_SHAREASALE: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Register as a ShareASale affiliate and get approved into the "
            "specific merchant program(s) of interest.",
            "Generate an API token + secret in the ShareASale account.",
            "Provide the token/secret via environment variables.",
        ],
        "note": "Per-merchant approval is required before any real link "
                "or commission data exists.",
    },
    NETWORK_CJ_AFFILIATE: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Register as a CJ (Commission Junction) publisher and get "
            "approved into the target advertiser program(s).",
            "Generate a CJ Developer API personal access token.",
            "Provide the token via an environment variable.",
        ],
        "note": "Advertiser-level approval is required per program.",
    },
    NETWORK_IMPACT: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Register as an Impact.com partner and get approved into the "
            "target brand's program.",
            "Generate an Impact API account SID + auth token.",
            "Provide both via environment variables.",
        ],
        "note": "Per-brand approval is required per program.",
    },
    NETWORK_AWIN: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Register as an Awin publisher (real compliance review; a "
            "refundable deposit applies) and get approved into the target "
            "advertiser program(s) - many EU/DE consumer-electronics and "
            "home-office retailers run on Awin.",
            "In the Awin dashboard, use Create-a-Feed to configure and "
            "generate a product data feed for each approved advertiser, "
            "and copy the resulting datafeed API key.",
            "Provide the datafeed API key and the approved advertiser "
            "id(s) via AWIN_DATAFEED_API_KEY / AWIN_ADVERTISER_IDS "
            "environment variables (never guessed - the fleet only ever "
            "reads feeds for advertiser ids a human has already been "
            "approved for).",
        ],
        "note": "Awin has no live keyword-search API for products - "
                "publishers get official, documented BULK product data "
                "feeds (https://productdata.awin.com) per approved "
                "advertiser; see awin_offer_source.py, which downloads and "
                "searches the real feed content locally rather than "
                "fabricating a search endpoint that does not exist.",
    },
    NETWORK_GENERIC_SAAS: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Sign up for the specific SaaS/tool's own affiliate program "
            "(terms and API/dashboard access vary per vendor).",
            "Obtain the real affiliate/referral link + stated commission "
            "terms from the program's own dashboard or agreement.",
        ],
        "note": "Covers standalone software/tool/hosting affiliate "
                "programs not on a large network - always vendor-specific.",
    },
    NETWORK_SYSTEME_IO: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Sign up for the systeme.io affiliate program (public - no "
            "advertiser-specific approval step) and obtain your unique "
            "affiliate link (contains a `sa=<affiliate_id>` query "
            "parameter).",
            "Provide the affiliate id and the exact affiliate link via "
            "SYSTEME_IO_AFFILIATE_ID / SYSTEME_IO_AFFILIATE_URL environment "
            "variables (never commit them).",
        ],
        "note": "systeme.io has no official public product-search API or "
                "catalog for affiliates (verified against its own public "
                "affiliate/help pages) - this is a single-product, curated "
                "offer source (see systeme_offer_source.py), not an "
                "automated marketplace connector.",
    },
    NETWORK_HUMAN_FED: {
        "status": model.POLICY_OK,
        "setup_steps": [],
        "note": "A human has already joined a real affiliate program and "
                "is feeding its real, already-approved details in "
                "(affiliate_sources.ingest_affiliate_offer). The fleet "
                "never joins a program or requests credentials itself.",
    },
}


def network_policy(network: str) -> dict:
    """Fail closed: an unknown network is treated exactly like an unknown
    platform in human_fed.py - HUMAN_SETUP_REQUIRED, never OK by default."""
    return NETWORK_POLICY.get(network, {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": ["This network is not yet recognised - a human must "
                        "confirm how it is accessed before the fleet can "
                        "use it."],
        "note": "unknown network - failing closed",
    })


# ---------------------------------------------------------------------------
# AffiliateOffer - a catalog entry, independent of any one demand signal.
# Matched against demand opportunities by affiliate_matching.py.
# ---------------------------------------------------------------------------

@dataclass
class AffiliateOffer:
    offer_id: str
    network: str
    program_name: str
    product_name: str
    product_url: str = ""
    product_asin: str = ""            # marketplace-specific product id, if verified (e.g. Amazon ASIN)
    product_price: float = 0.0
    currency: str = "EUR"
    price_is_estimate: bool = True
    #: WHEN the price above was actually observed (ISO timestamp) - a
    #: marketplace price is a snapshot, never a durable fact (spec:
    #: "Preis nicht als dauerhaft/fest speichern"). "" = unknown/unset.
    price_observed_at: str = ""
    #: how the price was obtained (a direct scrape, a human's own
    #: observation, a third-party price-comparison corroboration, ...) -
    #: never silently implies "verified live from the marketplace" when
    #: it was not.
    price_source_note: str = ""
    commission: CommissionModel = field(default_factory=CommissionModel)
    category: str = "other"           # matched against demand category/keywords
    keywords: tuple = ()              # human-supplied match keywords
    terms_url: str = ""
    join_url: str = ""
    eligibility_note: str = ""
    evidence: tuple = ()              # verbatim facts the human supplied
    status: str = model.POLICY_HUMAN_SETUP_REQUIRED
    tracking_param: str = ""          # e.g. "tag" (Amazon), "subid" - if the
                                       # network supports one; "" = unknown
    #: the REAL, static value to put in `tracking_param` (e.g. a real,
    #: pre-registered Amazon Associates tag like "airevenue-21"). Amazon's
    #: own program rules do not allow inventing a new `tag=` value per
    #: click/link - it must be one the human actually registered. When
    #: set, `affiliate_links.create_link()` uses THIS static value for the
    #: outbound URL (never a per-link random id) - our own internal
    #: `tracking_id`/`redirect_path` still exists separately, purely for
    #: OUR OWN click counting on our own domain, one hop before the
    #: visitor reaches this URL. Empty (the default) preserves the older,
    #: generic behaviour: `tracking_id` itself is used as the network
    #: query-param value (fine for a network whose subid IS meant to be a
    #: fresh value per link, e.g. ShareASale's afftrack/CJ's sid).
    tracking_value: str = ""
    #: when True, `affiliate_links.create_link()` NEVER appends any query
    #: parameter to `product_url` - not even the generic `subid=` fallback
    #: it otherwise uses for a network with no `tracking_param` configured.
    #: Set this for a real, human-verified affiliate URL whose exact query
    #: string must never be modified (e.g. systeme.io's `sa=<id>` link,
    #: which is not a redirector - see systeme_offer_source.py). Click
    #: counting still works via our OWN separate `/go/<tracking_id>`
    #: redirect hop (affiliate_tracking_server.py); only the FINAL,
    #: outbound URL the visitor lands on stays byte-for-byte untouched.
    #: Defaults to False - every existing offer's behaviour is unchanged.
    preserve_exact_url: bool = False
    #: compliant product image URLs for the public product page (spec:
    #: product-first catalog). ONLY ever populated by a human from an
    #: Amazon-permitted source (SiteStripe image / PA-API / Creators API) -
    #: nothing in the fleet fabricates, scrapes or mirrors an image. Empty
    #: (the default) -> the product card/page renders a clean icon
    #: placeholder, never a fake image.
    image_urls: tuple = ()
    #: a short, honest one-line context string shown on the product card
    #: (e.g. "Budget USB condenser mic for podcasting and streaming").
    #: Empty -> the card falls back to the first sentence of `evidence`.
    short_context: str = ""
    #: product-identity verification record (spec: "ASIN/product identity
    #: must be sufficiently verified"). Free-text status + ISO timestamp.
    #: "" -> the human-supplied `evidence` on file is the verification of
    #: record (every offer here was added by a human with evidence).
    verification_status: str = ""
    verified_at: str = ""
    added_at: str = ""
    added_by: str = "human"
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "offer_id": self.offer_id, "network": self.network,
            "program_name": self.program_name, "product_name": self.product_name,
            "product_url": self.product_url, "product_asin": self.product_asin,
            "product_price": round(float(self.product_price), 2),
            "currency": self.currency, "price_is_estimate": bool(self.price_is_estimate),
            "price_observed_at": self.price_observed_at,
            "price_source_note": self.price_source_note,
            "commission": self.commission.to_dict(), "category": self.category,
            "keywords": list(self.keywords), "terms_url": self.terms_url,
            "join_url": self.join_url, "eligibility_note": self.eligibility_note,
            "evidence": list(self.evidence), "status": self.status,
            "tracking_param": self.tracking_param, "tracking_value": self.tracking_value,
            "preserve_exact_url": bool(self.preserve_exact_url),
            "image_urls": list(self.image_urls), "short_context": self.short_context,
            "verification_status": self.verification_status,
            "verified_at": self.verified_at,
            "added_at": self.added_at,
            "added_by": self.added_by, "active": bool(self.active),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AffiliateOffer":
        d = dict(d or {})
        d["commission"] = CommissionModel.from_dict(d.get("commission") or {})
        d["keywords"] = tuple(d.get("keywords") or ())
        d["evidence"] = tuple(d.get("evidence") or ())
        d["image_urls"] = tuple(d.get("image_urls") or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def usable(self) -> bool:
        """Only an OK-status, active offer may be matched/planned against.
        A HUMAN_SETUP_REQUIRED/BLOCKED offer stays visible (for the human
        setup checklist) but is never selected automatically."""
        return self.active and self.status == model.POLICY_OK


def new_offer_id() -> str:
    return f"aff-{uuid.uuid4().hex[:12]}"


def offer_candidate_id(network: str, ident: str) -> str:
    """Deterministic, reconstructable id for a discovered offer candidate,
    keyed ONLY on `(network, product_id-or-url)` - the dedup identity the
    Offer Discovery layer promises. The same real search result always
    maps to the same id, so a re-run never duplicates a candidate and the
    id can be recomputed from the candidate's own fields alone."""
    key = f"{(network or '').strip().lower()}\n{(ident or '').strip()}"
    return f"cand-{hashlib.blake2s(key.encode('utf-8'), digest_size=8).hexdigest()}"


# ---------------------------------------------------------------------------
# small shared JSON-list persistence base (same atomic-write pattern as
# learning.OutcomeStore / revenue.RevenueLedger) - factored once here so
# the four stores below don't each re-implement tmpfile+os.replace.
# ---------------------------------------------------------------------------

class _JsonListStore:
    _FILENAME = "affiliate_generic.json"

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []

    @classmethod
    def load(cls, data_dir) -> "_JsonListStore":
        s = cls(Path(data_dir) / cls._FILENAME)
        if s.path.exists():
            try:
                raw = json.loads(s.path.read_text(encoding="utf-8"))
                s._rows = [dict(r) for r in raw] if isinstance(raw, list) else []
            except json.JSONDecodeError:
                s._rows = []
        return s

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._rows, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def rows(self) -> list[dict]:
        return list(self._rows)


class AffiliateOfferStore(_JsonListStore):
    _FILENAME = "affiliate_offers.json"

    def all(self) -> list[AffiliateOffer]:
        return [AffiliateOffer.from_dict(r) for r in self._rows]

    def get(self, offer_id: str) -> AffiliateOffer | None:
        for r in self._rows:
            if r.get("offer_id") == offer_id:
                return AffiliateOffer.from_dict(r)
        return None

    def upsert(self, offer: AffiliateOffer) -> None:
        for i, r in enumerate(self._rows):
            if r.get("offer_id") == offer.offer_id:
                self._rows[i] = offer.to_dict()
                return
        self._rows.append(offer.to_dict())


# ---------------------------------------------------------------------------
# discovered-but-not-yet-usable affiliate offer candidates (Offer Discovery
# layer). A candidate is a REAL product search result from an authorized
# offer source - never an AffiliateOffer: it carries NO commission terms
# and NO join confirmation (a search result cannot state either), so it is
# never `usable` and is never matched/planned against. A human turns one
# into a real offer via `affiliate_sources.ingest_affiliate_offer` after
# supplying the commission evidence + join confirmation the schema demands.
# ---------------------------------------------------------------------------

CANDIDATE_NEEDS_COMPLETION = "needs_human_completion"
CANDIDATE_COMPLETED = "completed"


@dataclass
class OfferCandidateRecord:
    candidate_id: str
    opportunity_id: str
    network: str
    product_name: str
    category_phrase: str = ""          # the demand's extracted product category
    product_url: str = ""
    product_id: str = ""               # marketplace-specific id, if the source gave one
    price: float = 0.0
    currency: str = ""
    availability: str = ""
    provenance: str = ""               # which real source call produced this
    confidence: float = 0.0            # the SOURCE's own relevance score, if any
    observed_at: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    status: str = CANDIDATE_NEEDS_COMPLETION
    completed_offer_id: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id, "opportunity_id": self.opportunity_id,
            "network": self.network, "product_name": self.product_name,
            "category_phrase": self.category_phrase, "product_url": self.product_url,
            "product_id": self.product_id, "price": round(float(self.price), 2),
            "currency": self.currency, "availability": self.availability,
            "provenance": self.provenance, "confidence": round(float(self.confidence), 3),
            "observed_at": self.observed_at, "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at, "status": self.status,
            "completed_offer_id": self.completed_offer_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OfferCandidateRecord":
        d = dict(d or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class AffiliateOfferCandidateStore(_JsonListStore):
    _FILENAME = "affiliate_offer_candidates.json"

    def all(self) -> list[OfferCandidateRecord]:
        return [OfferCandidateRecord.from_dict(r) for r in self._rows]

    def get(self, candidate_id: str) -> OfferCandidateRecord | None:
        for r in self._rows:
            if r.get("candidate_id") == candidate_id:
                return OfferCandidateRecord.from_dict(r)
        return None

    def upsert(self, rec: OfferCandidateRecord) -> None:
        for i, r in enumerate(self._rows):
            if r.get("candidate_id") == rec.candidate_id:
                self._rows[i] = rec.to_dict()
                break
        else:
            self._rows.append(rec.to_dict())
        # keep the file deterministic regardless of discovery order
        self._rows.sort(key=lambda r: r.get("candidate_id", ""))


# ---------------------------------------------------------------------------
# attribution chain: Demand -> Opportunity -> Asset -> Offer -> Link -> Click
# -> Conversion -> Commission (spec section 5)
# ---------------------------------------------------------------------------

@dataclass
class AffiliateAsset:
    asset_id: str
    opportunity_id: str
    offer_id: str
    asset_type: str = "comparison_page"
    title: str = ""
    #: the rendered page's actual <h1>/<title> override, if any (spec:
    #: Amazon Affiliate Loop - a caller-specified roundup-style headline
    #: like "Best USB microphone for streaming & Discord"). Persisted so
    #: `deploy_asset()` re-renders byte-identical content to what
    #: `build_asset()` quality-gated, not the generic default headline.
    guide_title: str = ""
    slug: str = ""
    file_path: str = ""               # relative path within the deploy artifact
    live_url: str = ""
    disclosure_included: bool = True
    quality_checks: dict = field(default_factory=dict)
    #: (title, url) pairs to OTHER REAL, already-deployed pages (content-
    #: cluster interlinking) - persisted for the same reason `guide_title`
    #: is: `deploy_asset()` re-renders from THIS stored value, not a
    #: freshly-supplied one, so a re-deploy is byte-identical to what
    #: `build_asset()` quality-gated. Empty by default - no cluster is
    #: claimed unless real pages actually exist to link to.
    related_links: tuple = ()
    created_at: str = ""

    def to_dict(self) -> dict:
        return {"asset_id": self.asset_id, "opportunity_id": self.opportunity_id,
                "offer_id": self.offer_id, "asset_type": self.asset_type,
                "title": self.title, "guide_title": self.guide_title,
                "slug": self.slug, "file_path": self.file_path,
                "live_url": self.live_url, "disclosure_included": self.disclosure_included,
                "quality_checks": dict(self.quality_checks),
                "related_links": [list(p) for p in self.related_links],
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "AffiliateAsset":
        d = dict(d or {})
        d["quality_checks"] = dict(d.get("quality_checks") or {})
        d["related_links"] = tuple(tuple(p) for p in (d.get("related_links") or ()))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class AffiliateAssetStore(_JsonListStore):
    _FILENAME = "affiliate_assets.json"

    def all(self) -> list[AffiliateAsset]:
        return [AffiliateAsset.from_dict(r) for r in self._rows]

    def get(self, asset_id: str) -> AffiliateAsset | None:
        for r in self._rows:
            if r.get("asset_id") == asset_id:
                return AffiliateAsset.from_dict(r)
        return None

    def by_opportunity(self, opportunity_id: str) -> list[AffiliateAsset]:
        return [a for a in self.all() if a.opportunity_id == opportunity_id]

    def upsert(self, asset: AffiliateAsset) -> None:
        for i, r in enumerate(self._rows):
            if r.get("asset_id") == asset.asset_id:
                self._rows[i] = asset.to_dict()
                return
        self._rows.append(asset.to_dict())


@dataclass
class AffiliateLink:
    link_id: str
    opportunity_id: str
    asset_id: str
    offer_id: str
    source: str = ""                  # e.g. "own_blog"
    tracking_id: str = ""             # subid/tag passed to the network, if any
    target_url: str = ""              # the real, external affiliate URL
    redirect_path: str = ""           # e.g. "/go/<tracking_id>" - our own hop
    created_at: str = ""
    click_count: int = 0
    conversion_count: int = 0
    commission_eur: float = 0.0
    revenue_eur: float = 0.0
    cost_eur: float = 0.0

    @property
    def profit_eur(self) -> float:
        return round(self.commission_eur - self.cost_eur, 2)

    def to_dict(self) -> dict:
        return {"link_id": self.link_id, "opportunity_id": self.opportunity_id,
                "asset_id": self.asset_id, "offer_id": self.offer_id,
                "source": self.source, "tracking_id": self.tracking_id,
                "target_url": self.target_url, "redirect_path": self.redirect_path,
                "created_at": self.created_at, "click_count": self.click_count,
                "conversion_count": self.conversion_count,
                "commission_eur": round(self.commission_eur, 2),
                "revenue_eur": round(self.revenue_eur, 2),
                "cost_eur": round(self.cost_eur, 2),
                "profit_eur": self.profit_eur}

    @classmethod
    def from_dict(cls, d: dict) -> "AffiliateLink":
        d = dict(d or {})
        d.pop("profit_eur", None)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AffiliateLinkStore(_JsonListStore):
    _FILENAME = "affiliate_links.json"

    def all(self) -> list[AffiliateLink]:
        return [AffiliateLink.from_dict(r) for r in self._rows]

    def get(self, link_id: str) -> AffiliateLink | None:
        for r in self._rows:
            if r.get("link_id") == link_id:
                return AffiliateLink.from_dict(r)
        return None

    def get_by_tracking_id(self, tracking_id: str) -> AffiliateLink | None:
        for r in self._rows:
            if r.get("tracking_id") == tracking_id:
                return AffiliateLink.from_dict(r)
        return None

    def by_opportunity(self, opportunity_id: str) -> list[AffiliateLink]:
        return [l for l in self.all() if l.opportunity_id == opportunity_id]

    def upsert(self, link: AffiliateLink) -> None:
        for i, r in enumerate(self._rows):
            if r.get("link_id") == link.link_id:
                self._rows[i] = link.to_dict()
                return
        self._rows.append(link.to_dict())


# ---------------------------------------------------------------------------
# click tracking (spec section 6) - data-sparse by design: no IP, no user
# agent, no cookie, no cross-site identifier. Just: which link, when,
# and which channel referred it (a label the fleet itself assigned when it
# distributed the asset there, e.g. "own_blog" - never derived from the
# visitor).
# ---------------------------------------------------------------------------

@dataclass
class ClickEvent:
    click_id: str
    link_id: str
    ts: str
    channel: str = ""

    def to_dict(self) -> dict:
        return {"click_id": self.click_id, "link_id": self.link_id,
                "ts": self.ts, "channel": self.channel}


class ClickStore(_JsonListStore):
    _FILENAME = "affiliate_clicks.json"

    def record(self, click: ClickEvent) -> dict:
        row = click.to_dict()
        self._rows.append(row)
        return row

    def by_link(self, link_id: str) -> list[dict]:
        return [r for r in self._rows if r.get("link_id") == link_id]

    def count_by_link(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._rows:
            k = str(r.get("link_id") or "")
            out[k] = out.get(k, 0) + 1
        return out


# ---------------------------------------------------------------------------
# commission lifecycle (spec sections 5 + 11): PENDING -> CONFIRMED -> PAID,
# or -> REVERSED. Only CONFIRMED/PAID amounts are booked into the real
# revenue ledger (see affiliate_revenue.py) - PENDING/ESTIMATED numbers
# never touch it.
# ---------------------------------------------------------------------------

COMMISSION_PENDING = "PENDING"
COMMISSION_CONFIRMED = "CONFIRMED"
COMMISSION_REVERSED = "REVERSED"
COMMISSION_PAID = "PAID"
COMMISSION_STATUSES = (COMMISSION_PENDING, COMMISSION_CONFIRMED, COMMISSION_REVERSED,
                       COMMISSION_PAID)
#: only these represent money that actually happened - PENDING is a hope,
#: not a fact, and must never be summed into "revenue".
SETTLED_COMMISSION_STATUSES = frozenset({COMMISSION_CONFIRMED, COMMISSION_PAID})


@dataclass
class CommissionRecord:
    commission_id: str
    link_id: str
    opportunity_id: str
    offer_id: str
    status: str = COMMISSION_PENDING
    amount: float = 0.0
    currency: str = "EUR"
    is_estimate: bool = True
    ref: str = ""                     # provider reference, for ledger idempotency
    note: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict:
        return {"commission_id": self.commission_id, "link_id": self.link_id,
                "opportunity_id": self.opportunity_id, "offer_id": self.offer_id,
                "status": self.status, "amount": round(float(self.amount), 2),
                "currency": self.currency, "is_estimate": bool(self.is_estimate),
                "ref": self.ref, "note": self.note, "recorded_at": self.recorded_at}

    @classmethod
    def from_dict(cls, d: dict) -> "CommissionRecord":
        d = dict(d or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class CommissionStore(_JsonListStore):
    _FILENAME = "affiliate_commissions.json"

    def all(self) -> list[CommissionRecord]:
        return [CommissionRecord.from_dict(r) for r in self._rows]

    def get(self, commission_id: str) -> CommissionRecord | None:
        for r in self._rows:
            if r.get("commission_id") == commission_id:
                return CommissionRecord.from_dict(r)
        return None

    def has_ref(self, ref: str) -> bool:
        return bool(ref) and any(r.get("ref") == ref for r in self._rows)

    def by_opportunity(self, opportunity_id: str) -> list[CommissionRecord]:
        return [c for c in self.all() if c.opportunity_id == opportunity_id]

    def upsert(self, record: CommissionRecord) -> None:
        for i, r in enumerate(self._rows):
            if r.get("commission_id") == record.commission_id:
                self._rows[i] = record.to_dict()
                return
        self._rows.append(record.to_dict())


# ---------------------------------------------------------------------------
# Pinterest pin drafts (organic distribution layer, business-model research
# phase 1/2 - see docs/BUSINESS_MODEL_RESEARCH.md). A pin ALWAYS points at an
# already-deployed AffiliateAsset's real live_url - see pinterest_pins.py,
# which refuses to draft one otherwise. The fleet never posts a pin itself:
# `status` only ever moves draft -> posted/skipped via a human confirming
# what they actually did (mirrors outreach.py's draft/approved/posted
# lifecycle - the same "the fleet drafts, a human acts" invariant).
# ---------------------------------------------------------------------------

PIN_DRAFT = "draft"
PIN_POSTED = "posted"
PIN_SKIPPED = "skipped"
PIN_STATUSES = (PIN_DRAFT, PIN_POSTED, PIN_SKIPPED)


@dataclass
class PinterestPinDraft:
    pin_id: str
    asset_id: str
    opportunity_id: str
    dest_url: str
    title: str
    description: str
    alt_text: str
    board_suggestion: str = "Allgemein"
    status: str = PIN_DRAFT
    created_at: str = ""
    posted_at: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"pin_id": self.pin_id, "asset_id": self.asset_id,
                "opportunity_id": self.opportunity_id, "dest_url": self.dest_url,
                "title": self.title, "description": self.description,
                "alt_text": self.alt_text, "board_suggestion": self.board_suggestion,
                "status": self.status, "created_at": self.created_at,
                "posted_at": self.posted_at, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "PinterestPinDraft":
        d = dict(d or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class PinterestPinStore(_JsonListStore):
    _FILENAME = "pinterest_pins.json"

    def all(self) -> list[PinterestPinDraft]:
        return [PinterestPinDraft.from_dict(r) for r in self._rows]

    def get(self, pin_id: str) -> PinterestPinDraft | None:
        for r in self._rows:
            if r.get("pin_id") == pin_id:
                return PinterestPinDraft.from_dict(r)
        return None

    def by_asset(self, asset_id: str) -> PinterestPinDraft | None:
        for r in self._rows:
            if r.get("asset_id") == asset_id:
                return PinterestPinDraft.from_dict(r)
        return None

    def pending(self) -> list[PinterestPinDraft]:
        return [p for p in self.all() if p.status == PIN_DRAFT]

    def upsert(self, pin: PinterestPinDraft) -> None:
        for i, r in enumerate(self._rows):
            if r.get("pin_id") == pin.pin_id:
                self._rows[i] = pin.to_dict()
                return
        self._rows.append(pin.to_dict())
