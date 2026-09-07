"""Pinterest pin drafting - a human-review-only distribution layer on top
of the affiliate asset pipeline (affiliate_assets.py).

Pinterest was selected in the Phase 1/2 business-model research
(docs/BUSINESS_MODEL_RESEARCH.md) as the fastest zero-cost organic
traffic channel available: no ad spend, no watch-hour/subscriber gate,
no KYC to start, and pins can start surfacing in Pinterest search within
days rather than the months an SEO page needs to rank.

Same discipline as the rest of the affiliate pipeline: template-rendered,
no LLM call, $0 API cost. `draft_pin()` refuses to draft a pin for an
asset that has no real `live_url` - a pin is never pointed at a
fabricated or not-yet-deployed page. Posting a pin is a human action:
`action_class.posting_permitted()` fails closed for any platform that is
not an owned channel, and Pinterest is a third party the fleet never
logs into - see `mark_posted()`, which only records what a human already
did.
"""

from __future__ import annotations

from .affiliate_model import (
    PIN_POSTED,
    PIN_SKIPPED,
    AffiliateAsset,
    PinterestPinDraft,
    PinterestPinStore,
)

#: Pinterest's own Merchant/branded-content guidelines require an affiliate
#: relationship to be disclosed - same substance as the on-site
#: `affiliate_assets.DISCLOSURE_TEXT`, shortened to fit the pin description.
PIN_DISCLOSURE = "(Affiliate-Link/Werbung)"

_TITLE_MAX = 100   # Pinterest pin title hard limit
_DESC_MAX = 500    # Pinterest pin description hard limit
_ALT_MAX = 500     # Pinterest alt-text hard limit


class PinDraftError(ValueError):
    """Raised when an asset is not eligible to be drafted into a pin."""


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def draft_pin(data_dir, *, asset: AffiliateAsset, product_name: str = "",
             category_label: str = "", board_suggestion: str = "",
             now_iso: str = "") -> PinterestPinDraft:
    """Turn one already-deployed, quality-passed affiliate asset into a
    Pinterest pin draft. Idempotent per asset_id - re-running returns the
    existing draft rather than minting a duplicate (same convention as
    `affiliate_assets.build_asset`).

    `product_name` / `category_label` are read from the caller's own real
    records (the asset, the matched offer) - never invented here. Raises
    `PinDraftError` when `asset.live_url` is empty: a pin must point at a
    page that actually exists.
    """
    store = PinterestPinStore.load(data_dir)
    existing = store.by_asset(asset.asset_id)
    if existing is not None:
        return existing

    if not (asset.live_url or "").strip():
        raise PinDraftError(
            f"asset {asset.asset_id!r} has no live_url - deploy it first; "
            "a pin is never drafted against a page that is not actually live")

    name = (product_name or asset.title or "Produkt").strip()
    topic = (asset.guide_title or asset.title or name).strip()
    cat = (category_label or "").strip()

    title = _clip(f"{name} – {topic}" if topic and topic != name else name, _TITLE_MAX)
    desc_core = (f"{topic}. Ehrlicher Vergleich, echte Anbieterangaben, keine erfundenen Tests."
                if topic else f"Ehrlicher Vergleich für {cat or 'diese Kategorie'}.")
    description = _clip(f"{desc_core} {PIN_DISCLOSURE}", _DESC_MAX)
    alt_text = _clip(f"{name}{f' – {cat}' if cat else ''}", _ALT_MAX)

    pin = PinterestPinDraft(
        pin_id=_new_pin_id(), asset_id=asset.asset_id,
        opportunity_id=asset.opportunity_id, dest_url=asset.live_url,
        title=title, description=description, alt_text=alt_text,
        board_suggestion=(board_suggestion or cat or "Allgemein"),
        created_at=now_iso,
    )
    store.upsert(pin)
    store.save()
    return pin


def mark_posted(data_dir, pin_id: str, *, status: str = PIN_POSTED,
                now_iso: str = "", note: str = "") -> PinterestPinDraft:
    """Human confirms what they actually did with a draft (posted it to
    Pinterest themselves, or decided to skip it). The fleet never calls
    this on its own - see cli.py's `pinterest-mark-posted`."""
    if status not in (PIN_POSTED, PIN_SKIPPED):
        raise ValueError(f"status must be {PIN_POSTED!r} or {PIN_SKIPPED!r}, got {status!r}")
    store = PinterestPinStore.load(data_dir)
    pin = store.get(pin_id)
    if pin is None:
        raise ValueError(f"no pin draft with id {pin_id!r}")
    pin.status = status
    pin.posted_at = now_iso
    if note:
        pin.note = note
    store.upsert(pin)
    store.save()
    return pin


def pending_pins(data_dir) -> list[PinterestPinDraft]:
    """Every pin draft still waiting on a human to post or skip it."""
    return PinterestPinStore.load(data_dir).pending()


def _new_pin_id() -> str:
    from .affiliate_model import new_id
    return new_id("pin")
