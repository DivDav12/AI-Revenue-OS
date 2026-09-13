"""TikTok slideshow content drafting - the same "the fleet drafts, a human
or a live browser session posts" discipline as pinterest_pins.py.

`draft_content()` never invents a product, price, or claim: every slide is
built from a real, already-verified `AffiliateOffer` (affiliate_model.py),
exactly like pin_images.render_product_pin. It also never invents a
trending sound: `sound_name`/`sound_source`/`sound_rationale` must be
supplied by the caller from real, current research (see cli.py's
`tiktok-draft` command) - a draft with no sound information is refused,
matching the hard rule that a TikTok is never posted without one.

Variation (spec: don't post the same format every time) is handled by
`_rotate()`, a deterministic least-recently/least-frequently-used pick
over the last N drafts already on file - never random, always
inspectable and testable.
"""

from __future__ import annotations

import re

from .affiliate_model import AffiliateOffer, TikTokContentDraft, TikTokContentStore, new_id

HOOKS = (
    "3 things that instantly upgrade your setup.",
    "You're probably missing this.",
    "These tech finds are actually worth knowing.",
    "Before you buy your next gadget, see this.",
    "Nobody's talking about these finds enough.",
    "Stop scrolling if you're building a desk setup.",
    "This is what actually improves your setup.",
    "A few finds worth knowing about before you buy.",
)

CTAS = (
    "Want to see more? Check our profile.",
    "More finds in our bio.",
    "See the products through our profile.",
    "Find the links in our bio.",
)

#: same substance as pinterest_pins.PIN_DISCLOSURE - TikTok's own branded
#: content / affiliate disclosure rules require this on every caption.
CONTENT_DISCLOSURE = "Werbung / enthält Affiliate-Links"

_BASE_HASHTAGS = ("smartfinds",)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TikTokDraftError(ValueError):
    """Raised when a draft cannot honestly be built (no real sound
    research supplied, or no real offers exist for the category)."""


def _rotate(options: tuple, recent: list[str]) -> str:
    """Deterministic least-recently/least-frequently-used pick. Never
    random: same history always yields the same next pick, so tests and
    a human reading `data/tiktok_content.json` can both predict it."""
    if not options:
        raise TikTokDraftError("no options to rotate through")
    counts: dict = {}
    last_seen: dict = {}
    for i, v in enumerate(recent):
        if v in options:
            counts[v] = counts.get(v, 0) + 1
            last_seen[v] = i
    return min(options, key=lambda o: (counts.get(o, 0), last_seen.get(o, -1), options.index(o)))


def _slug(text: str) -> str:
    return _SLUG_RE.sub("", (text or "").lower())


def _select_offers(offers_in_category: list[AffiliateOffer], recent_offer_ids: list[str],
                   num_products: int) -> list[AffiliateOffer]:
    if not offers_in_category:
        raise TikTokDraftError("no real offers exist for this category yet")
    n = max(1, min(num_products, len(offers_in_category)))
    counts: dict = {}
    last_seen: dict = {}
    for i, oid in enumerate(recent_offer_ids):
        counts[oid] = counts.get(oid, 0) + 1
        last_seen[oid] = i
    ranked = sorted(
        offers_in_category,
        key=lambda o: (counts.get(o.offer_id, 0), last_seen.get(o.offer_id, -1), o.offer_id),
    )
    return ranked[:n]


def _caption_and_hashtags(category: str, offers: list[AffiliateOffer]) -> tuple[str, tuple]:
    names = [o.product_name for o in offers]
    if len(names) == 1:
        lead = names[0]
    elif len(names) == 2:
        lead = f"{names[0]} & {names[1]}"
    else:
        lead = f"{', '.join(names[:-1])} & {names[-1]}"
    caption = f"{lead} - real finds, real prices. {CONTENT_DISCLOSURE}"

    tags = list(_BASE_HASHTAGS) + [_slug(category)]
    for o in offers:
        for kw in o.keywords:
            slug = _slug(kw)
            if slug and slug not in tags and len(slug) >= 3:
                tags.append(slug)
            if len(tags) >= 8:
                break
        if len(tags) >= 8:
            break
    return caption, tuple(f"#{t}" for t in dict.fromkeys(tags) if t)


def draft_content(data_dir, *, offers: list[AffiliateOffer], sound_name: str,
                  sound_source: str, sound_rationale: str, sound_researched_at: str,
                  category: str = "", num_products: int = 3, now_iso: str = "",
                  category_by_offer: dict | None = None) -> TikTokContentDraft:
    """Build one TikTok slideshow content draft from real, verified offers.

    `category_by_offer` (offer_id -> a broader content category, e.g. from
    products.py's `site_category`) groups offers for content purposes -
    AffiliateOffer.category is often a narrow, near-per-product slug and
    grouping by it alone would starve most drafts down to a single
    product. Falls back to `offer.category` when not supplied.

    Raises `TikTokDraftError` if no sound research was supplied, or if the
    chosen/given category has no real offers - a draft is never built
    against fabricated data or a fabricated trend.
    """
    if not (sound_name or "").strip() or not (sound_source or "").strip() \
            or not (sound_rationale or "").strip():
        raise TikTokDraftError(
            "no current trend-sound research supplied (sound_name/sound_source/"
            "sound_rationale) - research this week's trending sounds first, or "
            "queue this content for later rather than posting without one")

    category_by_offer = category_by_offer or {}

    def cat_of(o: AffiliateOffer) -> str:
        return category_by_offer.get(o.offer_id) or o.category

    store = TikTokContentStore.load(data_dir)
    history = store.all()
    recent = history[-30:]

    categories = tuple(dict.fromkeys(cat_of(o) for o in offers if cat_of(o)))
    if not categories:
        raise TikTokDraftError("no real offers with a category exist yet")
    chosen_category = category.strip() if category.strip() else _rotate(
        categories, [d.category for d in recent])

    offers_in_category = [o for o in offers if cat_of(o) == chosen_category]
    recent_offer_ids = [oid for d in recent for oid in d.offer_ids]
    chosen_offers = _select_offers(offers_in_category, recent_offer_ids, num_products)

    hook = _rotate(HOOKS, [d.hook for d in recent])
    cta = _rotate(CTAS, [d.cta for d in recent])
    caption, hashtags = _caption_and_hashtags(chosen_category, chosen_offers)

    draft = TikTokContentDraft(
        draft_id=new_id("tt"), category=chosen_category,
        offer_ids=tuple(o.offer_id for o in chosen_offers),
        hook=hook, cta=cta, caption=caption, hashtags=hashtags,
        sound_name=sound_name.strip(), sound_source=sound_source.strip(),
        sound_rationale=sound_rationale.strip(), sound_researched_at=sound_researched_at,
        created_at=now_iso,
    )
    store.upsert(draft)
    store.save()
    return draft


def attach_slides(data_dir, draft_id: str, slide_paths: list[str]) -> TikTokContentDraft:
    """Record the local slide image paths already rendered for a draft
    (see cli.py's `tiktok-draft` which calls pin_images render_*)."""
    store = TikTokContentStore.load(data_dir)
    draft = store.get(draft_id)
    if draft is None:
        raise ValueError(f"no TikTok content draft with id {draft_id!r}")
    draft.slide_paths = tuple(slide_paths)
    store.upsert(draft)
    store.save()
    return draft


def mark_posted(data_dir, draft_id: str, *, status: str = "posted",
                now_iso: str = "", note: str = "") -> TikTokContentDraft:
    """Human/live-session confirms what was actually posted. This module
    never posts anything itself - action_class.posting_permitted('tiktok')
    stays False, same as Pinterest."""
    from .affiliate_model import TT_POSTED, TT_SKIPPED
    if status not in (TT_POSTED, TT_SKIPPED):
        raise ValueError(f"status must be {TT_POSTED!r} or {TT_SKIPPED!r}, got {status!r}")
    store = TikTokContentStore.load(data_dir)
    draft = store.get(draft_id)
    if draft is None:
        raise ValueError(f"no TikTok content draft with id {draft_id!r}")
    draft.status = status
    draft.posted_at = now_iso
    if note:
        draft.note = note
    store.upsert(draft)
    store.save()
    return draft


def pending_drafts(data_dir) -> list[TikTokContentDraft]:
    return TikTokContentStore.load(data_dir).pending()
