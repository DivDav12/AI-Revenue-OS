"""Deterministic, honest marketing content for social distribution.

No LLM, no network. Every fact in the output traces back to the real
`AffiliateOffer` fields (evidence the human supplied, the stated price and
when it was observed) or to the poster's own words. Never a fabricated
review, star rating, testimonial, or "best" claim.

Two renderers:
  reddit_reply()  - a context-specific, genuinely-helpful comment draft
                    with an inline affiliate disclosure.
  pinterest_pin() - title / description / alt-text / suggested board /
                    dominant keyword for one Pin, disclosure included.

The affiliate URL is passed in already-resolved (see `social/links.py`) -
this module never builds one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# EN disclosure for Reddit (community language); matches the wording the
# WIP browser_response draft used, kept consistent across the codebase.
REDDIT_DISCLOSURE = (
    "Disclosure: the link is an Amazon affiliate link - if you buy through "
    "it I may earn a small commission, at no extra cost to you.")

# Pinterest descriptions are short; a compact, unambiguous disclosure.
PINTEREST_DISCLOSURE = "#ad - contains an Amazon affiliate link."

_MAX_PIN_TITLE = 100
_MAX_PIN_DESC = 480


def _price_clause(offer) -> str:
    if offer.product_price and offer.product_price > 0 and not offer.price_is_estimate:
        stamp = f" (as of {offer.price_observed_at.split('T')[0]})" if offer.price_observed_at else ""
        return f" It was about {offer.product_price:g} {offer.currency}{stamp} when last checked."
    if offer.product_price and offer.product_price > 0:
        return f" Roughly {offer.product_price:g} {offer.currency}, but check the current price."
    return " Check the current price on the product page."


def _subject(category_phrase: str, fallback: str) -> str:
    c = (category_phrase or "").strip()
    return c or (fallback or "what you described").strip()


@dataclass(frozen=True)
class ReplyDraft:
    platform: str
    body: str                     # the full comment text, disclosure included
    disclosure: str
    link_url: str
    link_mode: str
    product_name: str
    asin: str
    reasoning: str = ""           # why this was judged relevant (for the reviewer)
    community: str = ""

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "community": self.community,
            "body": self.body, "disclosure": self.disclosure,
            "link_url": self.link_url, "link_mode": self.link_mode,
            "product_name": self.product_name, "asin": self.asin,
            "reasoning": self.reasoning,
        }


def reddit_reply(*, offer, product_intent: dict, matched_terms=None,
                 assessment_reason: str = "", link_url: str, link_mode: str,
                 community: str = "") -> ReplyDraft:
    """One helpful Reddit comment draft. Speaks to the poster's stated
    need, gives real buying considerations, names the product as ONE
    option, and discloses the affiliate relationship inline."""
    matched_terms = [t for t in (matched_terms or []) if t]
    subject = _subject(product_intent.get("category_phrase", ""), offer.category)
    reason_bits = ", ".join(matched_terms[:4])

    # a real, verbatim spec detail from the offer's own evidence (not a
    # review, not a rating) - the first evidence line, lightly trimmed.
    spec_hint = ""
    for e in (offer.evidence or []):
        s = str(e).strip()
        if s:
            spec_hint = re.split(r"[.:]", s, maxsplit=1)[0].strip()
            break

    lead = (
        f"For {subject}, one option that lines up with what you're after"
        + (f" ({reason_bits})" if reason_bits else "")
        + f" is the {offer.product_name}."
    )
    body_mid = (
        (f" On paper: {spec_hint}." if spec_hint else "")
        + _price_clause(offer)
        + " I haven't tested every alternative, so it's worth comparing a "
        "couple in the same range before you decide - but it's a reasonable "
        "default for this use case."
    )
    if link_mode == "guide":
        link_line = f"\n\nI wrote up the considerations here: {link_url}"
    else:
        link_line = f"\n\nProduct page: {link_url}"

    body = f"{lead}{body_mid}{link_line}\n\n{REDDIT_DISCLOSURE}"
    return ReplyDraft(
        platform="reddit", community=community, body=body,
        disclosure=REDDIT_DISCLOSURE, link_url=link_url, link_mode=link_mode,
        product_name=offer.product_name, asin=offer.product_asin,
        reasoning=assessment_reason)


@dataclass(frozen=True)
class PinDraft:
    platform: str
    title: str
    description: str              # disclosure included
    alt_text: str
    dominant_keyword: str
    suggested_board: str
    link_url: str
    link_mode: str
    disclosure: str
    product_name: str
    asin: str
    creative_brief: str = ""      # what image to use / render
    keywords: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "title": self.title,
            "description": self.description, "alt_text": self.alt_text,
            "dominant_keyword": self.dominant_keyword,
            "suggested_board": self.suggested_board, "link_url": self.link_url,
            "link_mode": self.link_mode, "disclosure": self.disclosure,
            "product_name": self.product_name, "asin": self.asin,
            "creative_brief": self.creative_brief, "keywords": list(self.keywords),
        }


def _title_case_kw(kw: str) -> str:
    return " ".join(w.capitalize() for w in kw.split())


def pinterest_pin(*, offer, product_intent: dict, link_url: str,
                  link_mode: str) -> PinDraft:
    """One Pin draft. Keyword-led (Pinterest is a search engine), honest,
    disclosure in the description."""
    cat = _subject(product_intent.get("category_phrase", ""), offer.category)
    dominant = cat if cat != "what you described" else (offer.keywords[0] if offer.keywords else offer.category)
    kw_pool = [dominant] + [k for k in offer.keywords[:6] if k and k != dominant]

    title = f"{_title_case_kw(dominant)}: {offer.product_name}"
    if len(title) > _MAX_PIN_TITLE:
        title = title[:_MAX_PIN_TITLE - 1].rstrip() + "…"

    price = _price_clause(offer).strip()
    desc = (
        f"Looking for {dominant}? The {offer.product_name} is one option to "
        f"consider. {price} Not a tested 'best' pick - compare a few in the "
        f"same range. {PINTEREST_DISCLOSURE}"
    )
    if len(desc) > _MAX_PIN_DESC:
        desc = desc[:_MAX_PIN_DESC - 1].rstrip() + "…"

    alt = f"{offer.product_name} - {dominant}"
    board = _title_case_kw(offer.category.replace("-", " ").replace("_", " ")) or "Tech Picks"

    brief = (
        "Use a plain, honest product image or a simple text-on-solid-color "
        f"card reading '{_title_case_kw(dominant)}'. No fake lifestyle shot, "
        "no invented rating badge, no price burned into the image (price "
        "changes). 2:3 ratio (1000x1500)."
    )
    return PinDraft(
        platform="pinterest", title=title, description=desc, alt_text=alt,
        dominant_keyword=dominant, suggested_board=board, link_url=link_url,
        link_mode=link_mode, disclosure=PINTEREST_DISCLOSURE,
        product_name=offer.product_name, asin=offer.product_asin,
        creative_brief=brief, keywords=kw_pool)
