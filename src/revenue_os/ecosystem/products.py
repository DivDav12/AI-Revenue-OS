"""Product-first public catalog (spec: "product discovery platform").

ONE source of truth for "which products does the public website show, and
what does each one link to". Pure and deterministic - reads only the
already-persisted, real `AffiliateOfferStore` / `AffiliateAssetStore`
data; never invents a product, an ASIN, a price, an image, or a
product<->guide relationship.

A public product == a *usable* (POLICY_OK, active) `AffiliateOffer` whose
network is not on `EXCLUDED_NETWORKS`. Amazon offers additionally must
carry a verified product-specific destination (a real `/dp/<ASIN>` URL
whose ASIN matches the offer) - a homepage / search / category / mismatched
URL is rejected, never published.

The outbound URL is taken from the EXISTING affiliate-link architecture
(`AffiliateLinkStore` - the same verified `target_url` the buying guides
already use, `tag=airevenue-21` already baked in). This module does not
mint links or guess URLs; if no link row exists yet it falls back to the
offer's own verified `product_url` run through the same
`affiliate_links._append_tracking` helper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import model
from .affiliate_model import (
    NETWORK_AMAZON_ASSOCIATES,
    NETWORK_SYSTEME_IO,
    AffiliateAssetStore,
    AffiliateLinkStore,
    AffiliateOffer,
    AffiliateOfferStore,
)
from .site import classify_offer_category

#: networks removed from the public product experience. systeme.io was the
#: prior product strategy - it is no longer recommended as a product (spec:
#: "Remove Systeme.io from the public product strategy"). The offer row and
#: its source module stay for internal/historical state; the public site
#: simply never selects it.
EXCLUDED_NETWORKS = frozenset({NETWORK_SYSTEME_IO})

#: at most this many tag badges on a card - keeps the grid clean.
_MAX_CARD_TAGS = 4

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# keyword -> the human-readable tag badge shown on cards / product pages.
# Only tags whose keyword the offer actually carries are ever shown; this
# table just gives a few of the existing raw keywords a nicer label.
_TAG_LABELS: dict[str, str] = {
    "usb-mikrofon": "USB",
    "usb microphone": "USB",
    "condenser microphone": "Condenser",
    "kondensatormikrofon": "Condenser",
    "budget microphone": "Budget",
    "podcast microphone": "Podcasting",
    "podcasting": "Podcasting",
    "streaming microphone": "Streaming",
    "streaming": "Streaming",
    "gaming microphone": "Gaming",
    "gaming": "Gaming",
    "discord": "Discord",
    "podcast": "Podcasting",
    "creator": "Creators",
    "home-office": "Home office",
    "pc": "PC",
    "pdf editor": "PDF editor",
    "edit pdf": "Edit PDF",
    "ocr": "OCR",
    "e-sign": "E-sign",
    "acrobat alternative": "Acrobat alternative",
}

#: order preferred on the card when several apply.
_TAG_PRIORITY = (
    "Budget", "USB", "Condenser", "Podcasting", "Streaming", "Gaming",
    "Discord", "PC", "Creators", "Home office", "PDF editor", "Edit PDF",
    "OCR", "E-sign", "Acrobat alternative",
)


def product_slug(offer: AffiliateOffer) -> str:
    """Deterministic, collision-safe English slug for `/product/<slug>/`.
    Derived from the product name; the last 6 chars of the offer id are
    appended only if the name alone would be empty or ambiguous is avoided
    by callers via `_dedupe_slugs`."""
    s = _SLUG_RE.sub("-", (offer.product_name or "").lower()).strip("-")
    s = re.sub(r"-+", "-", s)[:70].strip("-")
    return s or f"product-{offer.offer_id[-6:]}"


def _is_amazon(offer: AffiliateOffer) -> bool:
    return offer.network == NETWORK_AMAZON_ASSOCIATES


_ASIN_URL_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")


def verified_amazon_destination(offer: AffiliateOffer) -> str | None:
    """The offer's own product URL iff it is a real, product-specific
    Amazon destination whose ASIN matches the offer's verified ASIN.
    Returns None for a homepage / search / category / mismatched URL - such
    an offer must never be published (spec 13/16/21)."""
    url = (offer.product_url or "").strip()
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if not (host == "amazon.de" or host.endswith(".amazon.de")
            or host == "amazon.com" or host.endswith(".amazon.com")):
        return None
    m = _ASIN_URL_RE.search(url)
    if not m:
        return None
    if offer.product_asin and m.group(1).upper() != offer.product_asin.upper():
        return None
    return url


@dataclass(frozen=True)
class RelatedGuide:
    title: str
    url: str


@dataclass(frozen=True)
class PublicProduct:
    product_id: str                 # == offer_id (one canonical page per product)
    slug: str
    name: str
    network: str
    is_amazon: bool
    asin: str
    price: float
    currency: str
    price_is_estimate: bool
    price_observed_at: str
    price_source_note: str
    tags: tuple[str, ...]
    description: str
    short_context: str
    image_urls: tuple[str, ...]
    site_category: str
    outbound_url: str               # verified affiliate destination (tag baked in)
    tracking_id: str                # our own click-tracking id, if a link row exists
    verification_status: str
    related_guides: tuple[RelatedGuide, ...] = ()

    @property
    def has_price(self) -> bool:
        return self.price > 0

    @property
    def price_display(self) -> str:
        if not self.has_price:
            return "See current price on Amazon" if self.is_amazon else "See current price"
        amount = f"{self.currency} {self.price:.2f}"
        return amount

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id, "slug": self.slug, "name": self.name,
            "network": self.network, "is_amazon": self.is_amazon, "asin": self.asin,
            "price": self.price, "currency": self.currency,
            "price_is_estimate": self.price_is_estimate,
            "tags": list(self.tags), "site_category": self.site_category,
            "outbound_url": self.outbound_url,
            "related_guides": [g.url for g in self.related_guides],
        }


def _tags_for(offer: AffiliateOffer) -> tuple[str, ...]:
    labels: list[str] = []
    for kw in offer.keywords:
        label = _TAG_LABELS.get(kw.lower())
        if label and label not in labels:
            labels.append(label)
    labels.sort(key=lambda t: (_TAG_PRIORITY.index(t) if t in _TAG_PRIORITY else 99))
    return tuple(labels[:_MAX_CARD_TAGS])


def _description_for(offer: AffiliateOffer) -> str:
    """One honest sentence - the human-curated `short_context` if present,
    otherwise the first sentence of the provider's own stated evidence.
    Never a fabricated spec or claim."""
    if offer.short_context.strip():
        return offer.short_context.strip()
    for e in offer.evidence:
        text = str(e).strip()
        if not text:
            continue
        # take the first sentence, trimmed to a card-friendly length.
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        return (first[:240]).strip()
    return ""


def _short_context_for(offer: AffiliateOffer, tags: tuple[str, ...]) -> str:
    if offer.short_context.strip():
        return offer.short_context.strip()
    if tags:
        joined = ", ".join(t.lower() for t in tags[:3])
        return f"For {joined}."
    return ""


def _outbound_for(offer: AffiliateOffer, links_by_offer: dict) -> tuple[str, str]:
    """(url, tracking_id). Prefers an existing verified `AffiliateLink.target_url`
    (already carries `tag=airevenue-21`); otherwise builds the outbound URL
    from the offer's own verified `product_url` via the SAME helper
    `affiliate_links` uses - never a guessed URL."""
    rows = links_by_offer.get(offer.offer_id) or []
    if rows:
        row = rows[0]
        return row.target_url, row.tracking_id
    from .affiliate_links import _append_tracking

    if _is_amazon(offer):
        dest = verified_amazon_destination(offer) or ""
        if dest and offer.tracking_param and offer.tracking_value:
            dest = _append_tracking(dest, offer.tracking_param, offer.tracking_value)
        return dest, ""
    return offer.product_url, ""


def _related_guides_for(offer_id: str, assets) -> tuple[RelatedGuide, ...]:
    out: list[RelatedGuide] = []
    seen: set[str] = set()
    for a in assets:
        if a.offer_id != offer_id or not a.live_url or a.live_url in seen:
            continue
        seen.add(a.live_url)
        out.append(RelatedGuide(title=(a.guide_title or a.title or "Buying guide"),
                                url=a.live_url))
    return tuple(out)


def _dedupe_slugs(products: list[PublicProduct]) -> list[PublicProduct]:
    seen: dict[str, int] = {}
    out: list[PublicProduct] = []
    for p in products:
        if p.slug not in seen:
            seen[p.slug] = 1
            out.append(p)
            continue
        seen[p.slug] += 1
        suffix = p.product_id[-6:]
        from dataclasses import replace
        out.append(replace(p, slug=f"{p.slug}-{suffix}"))
    return out


def load_public_products(data_dir) -> list[PublicProduct]:
    """Every product eligible for a public product page, ASIN-deduped,
    deterministic order (Amazon first, then by name)."""
    offers = AffiliateOfferStore.load(data_dir).all()
    assets = AffiliateAssetStore.load(data_dir).all()
    links = AffiliateLinkStore.load(data_dir).all()
    links_by_offer: dict[str, list] = {}
    for l in links:
        links_by_offer.setdefault(l.offer_id, []).append(l)

    products: list[PublicProduct] = []
    seen_asin: set[str] = set()
    for offer in offers:
        if not offer.usable or offer.network in EXCLUDED_NETWORKS:
            continue
        if _is_amazon(offer):
            dest = verified_amazon_destination(offer)
            if not dest:
                continue                       # unverified/mismatched -> never published
            if offer.product_asin and offer.product_asin.upper() in seen_asin:
                continue                       # ASIN dedupe -> one canonical page
            if offer.product_asin:
                seen_asin.add(offer.product_asin.upper())
        outbound, tracking_id = _outbound_for(offer, links_by_offer)
        if not outbound:
            continue
        tags = _tags_for(offer)
        products.append(PublicProduct(
            product_id=offer.offer_id, slug=product_slug(offer),
            name=offer.product_name, network=offer.network, is_amazon=_is_amazon(offer),
            asin=offer.product_asin, price=float(offer.product_price or 0.0),
            currency=offer.currency, price_is_estimate=bool(offer.price_is_estimate),
            price_observed_at=offer.price_observed_at,
            price_source_note=offer.price_source_note,
            tags=tags, description=_description_for(offer),
            short_context=_short_context_for(offer, tags),
            image_urls=tuple(offer.image_urls),
            site_category=classify_offer_category(offer.category),
            outbound_url=outbound, tracking_id=tracking_id,
            verification_status=(offer.verification_status
                                 or "human-added, provider evidence on file"),
            related_guides=_related_guides_for(offer.offer_id, assets)))

    products.sort(key=lambda p: (0 if p.is_amazon else 1, p.name.lower()))
    return _dedupe_slugs(products)


def products_by_category(data_dir) -> dict[str, list[PublicProduct]]:
    out: dict[str, list[PublicProduct]] = {}
    for p in load_public_products(data_dir):
        out.setdefault(p.site_category, []).append(p)
    return out


def products_in_category(data_dir, category_key: str) -> list[PublicProduct]:
    return [p for p in load_public_products(data_dir) if p.site_category == category_key]


def related_products(product: PublicProduct, all_products: list[PublicProduct],
                     *, limit: int = 3) -> list[PublicProduct]:
    """Same site category first, then a shared tag - never an arbitrary
    recommendation, and never the product itself."""
    same_cat = [p for p in all_products
                if p.product_id != product.product_id
                and p.site_category == product.site_category]
    if len(same_cat) >= limit:
        return same_cat[:limit]
    ptags = set(product.tags)
    extra = [p for p in all_products
             if p.product_id != product.product_id and p not in same_cat
             and ptags & set(p.tags)]
    return (same_cat + extra)[:limit]


def find_product(data_dir, slug: str) -> PublicProduct | None:
    for p in load_public_products(data_dir):
        if p.slug == slug:
            return p
    return None
