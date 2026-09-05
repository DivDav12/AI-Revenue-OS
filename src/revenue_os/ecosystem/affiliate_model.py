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
                "authenticated API call.",
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
            "Register as an Awin publisher and get approved into the "
            "target advertiser program.",
            "Generate an Awin API OAuth token from the Awin account.",
            "Provide the token via an environment variable.",
        ],
        "note": "Per-advertiser approval is required per program.",
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
    product_price: float = 0.0
    currency: str = "EUR"
    price_is_estimate: bool = True
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
    added_at: str = ""
    added_by: str = "human"
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "offer_id": self.offer_id, "network": self.network,
            "program_name": self.program_name, "product_name": self.product_name,
            "product_url": self.product_url, "product_price": round(float(self.product_price), 2),
            "currency": self.currency, "price_is_estimate": bool(self.price_is_estimate),
            "commission": self.commission.to_dict(), "category": self.category,
            "keywords": list(self.keywords), "terms_url": self.terms_url,
            "join_url": self.join_url, "eligibility_note": self.eligibility_note,
            "evidence": list(self.evidence), "status": self.status,
            "tracking_param": self.tracking_param, "added_at": self.added_at,
            "added_by": self.added_by, "active": bool(self.active),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AffiliateOffer":
        d = dict(d or {})
        d["commission"] = CommissionModel.from_dict(d.get("commission") or {})
        d["keywords"] = tuple(d.get("keywords") or ())
        d["evidence"] = tuple(d.get("evidence") or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def usable(self) -> bool:
        """Only an OK-status, active offer may be matched/planned against.
        A HUMAN_SETUP_REQUIRED/BLOCKED offer stays visible (for the human
        setup checklist) but is never selected automatically."""
        return self.active and self.status == model.POLICY_OK


def new_offer_id() -> str:
    return f"aff-{uuid.uuid4().hex[:12]}"


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
    slug: str = ""
    file_path: str = ""               # relative path within the deploy artifact
    live_url: str = ""
    disclosure_included: bool = True
    quality_checks: dict = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {"asset_id": self.asset_id, "opportunity_id": self.opportunity_id,
                "offer_id": self.offer_id, "asset_type": self.asset_type,
                "title": self.title, "slug": self.slug, "file_path": self.file_path,
                "live_url": self.live_url, "disclosure_included": self.disclosure_included,
                "quality_checks": dict(self.quality_checks), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "AffiliateAsset":
        d = dict(d or {})
        d["quality_checks"] = dict(d.get("quality_checks") or {})
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
