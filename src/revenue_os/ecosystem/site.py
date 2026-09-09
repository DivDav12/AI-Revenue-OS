"""AI Revenue - the customer-facing website shell.

Everything here is real, generated data or hand-written honest copy -
never a fabricated review, testimonial, star rating, price, or fake
visitor/customer count. This module owns the site's shared chrome
(header/nav/footer/branding/CSS) and its non-guide pages (homepage,
category pages, legal pages); the individual buying-guide pages
themselves are still rendered by `affiliate_assets.render_comparison_page()`
(which imports this module's chrome so every page looks like one
coherent site) and deployed through the SAME, unmodified affiliate
pipeline (`affiliate_pipeline.run_affiliate_chain()` -> `build_asset()` /
`deploy_asset()`).

Architecture (spec: "muss dynamisch mit dem bestehenden System
funktionieren"): the homepage and category pages are built PURELY from
already-persisted, real data (`AffiliateAssetStore` / `AffiliateOfferStore`)
- there is no per-category hardcoded product list. When a new demand
signal produces a new deployed guide (via the existing
Demand -> ProductIntent -> Offer Discovery -> Product Selection ->
Content -> Website -> Affiliate Link chain, unchanged), re-running
`build_site_artifact()` / `deploy_site()` picks it up automatically -
nothing here needs to change per category or per product.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ..deployment import DeploymentArtifact, default_deployment_adapter
from .affiliate_model import AffiliateAssetStore, AffiliateOfferStore

SITE_BRAND = "AI Revenue"
SITE_TAGLINE = "Find the right product – without hours of searching."
SITE_ROOT_SLUG = ""   # deploys at the repo root, not a subfolder


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def site_base_path(environ=None) -> str:
    """"" or e.g. "/AI-Revenue-OS" (no trailing slash) - the URL path
    prefix the deployed GitHub *project* Pages site is served under.
    Derived from the SAME real, already-configured deploy target every
    internal link + the sitemap use (`_real_base_url()`); "" when no real
    deploy target is configured (local render / tests / a root-served
    custom domain), so nothing changes there."""
    from urllib.parse import urlparse

    base = _real_base_url(environ)
    return urlparse(base).path.rstrip("/") if base else ""


def _split_emoji_label(label: str) -> tuple[str, str]:
    """('\U0001F3A7', 'Kopfhörer & Earbuds') - so a leading decorative
    emoji can be marked aria-hidden while the text stays the accessible
    name. ('', label) when there is no leading emoji."""
    parts = label.split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", label)


# ---------------------------------------------------------------------------
# category taxonomy - a real, transparent mapping table (data, not a guess):
# which of the 8 site categories a real AffiliateOffer.category falls under.
# An offer whose category is not yet mapped here shows up under "Sonstiges"
# rather than being silently dropped or forced into the wrong bucket.
# ---------------------------------------------------------------------------

CATEGORY_KOPFHOERER = "kopfhoerer-earbuds"
CATEGORY_MIKROFONE = "mikrofone"
CATEGORY_TASTATUREN = "tastaturen"
CATEGORY_MAEUSE = "maeuse"
CATEGORY_MONITORE = "monitore"
CATEGORY_GAMING = "gaming"
CATEGORY_TECHNIK = "technik"
CATEGORY_SONSTIGES = "sonstiges"

#: (key, emoji label) - display order. Keys are URL slugs and never change;
#: only the human-readable labels are localised.
SITE_CATEGORIES: tuple[tuple[str, str], ...] = (
    (CATEGORY_KOPFHOERER, "\U0001F3A7 Headphones & Earbuds"),
    (CATEGORY_MIKROFONE, "\U0001F399️ Microphones"),
    (CATEGORY_TASTATUREN, "⌨️ Keyboards"),
    (CATEGORY_MAEUSE, "\U0001F5B1️ Mice"),
    (CATEGORY_MONITORE, "\U0001F5A5️ Monitors"),
    (CATEGORY_GAMING, "\U0001F3AE Gaming"),
    (CATEGORY_TECHNIK, "\U0001F4BB Tech"),
)

_CATEGORY_LABELS = dict(SITE_CATEGORIES) | {CATEGORY_SONSTIGES: "\U0001F4E6 Other"}

#: real `AffiliateOffer.category` values already used in this codebase,
#: mapped to a site category - substring match against the offer's own
#: category string, checked in order. Extend this table as new real
#: offer categories are ingested; never invent a category value here.
_OFFER_CATEGORY_MAP: tuple[tuple[str, str], ...] = (
    ("microphone", CATEGORY_MIKROFONE),
    ("mikrofon", CATEGORY_MIKROFONE),
    ("headphone", CATEGORY_KOPFHOERER),
    ("earbud", CATEGORY_KOPFHOERER),
    ("kopfhoerer", CATEGORY_KOPFHOERER),
    ("keyboard", CATEGORY_TASTATUREN),
    ("tastatur", CATEGORY_TASTATUREN),
    ("mouse", CATEGORY_MAEUSE),
    ("maus", CATEGORY_MAEUSE),
    ("monitor", CATEGORY_MONITORE),
    ("gaming", CATEGORY_GAMING),
    ("business-platform", CATEGORY_TECHNIK),
    ("hosting", CATEGORY_TECHNIK),
    ("software", CATEGORY_TECHNIK),
)


def classify_offer_category(offer_category: str) -> str:
    c = (offer_category or "").lower()
    for needle, site_cat in _OFFER_CATEGORY_MAP:
        if needle in c:
            return site_cat
    return CATEGORY_SONSTIGES


def category_breadcrumb(offer_category: str, environ=None) -> tuple[str, str]:
    """(label, path) for the site category a real `AffiliateOffer.category`
    falls under - used by a guide page to link back to its own category
    (internal linking, spec: "crawlable page structure"). `path` is
    prefixed with the real, already-configured GitHub Pages base
    (`_real_base_url()`) when one resolves - a GitHub Pages PROJECT site
    (e.g. https://owner.github.io/repo/) is served under a subpath, so a
    root-relative "/kategorie/..." link 404s there; '' (unchanged,
    root-relative) when no real deploy config resolves yet, e.g. tests or
    a custom domain served at the root. The label is the PLAIN text (no
    leading decorative emoji) - it is a breadcrumb link's accessible name."""
    key = classify_offer_category(offer_category)
    label = _split_emoji_label(_CATEGORY_LABELS.get(key, key))[1]
    return label, f"{_real_base_url(environ)}/kategorie/{key}/"


# ---------------------------------------------------------------------------
# shared chrome
# ---------------------------------------------------------------------------

_BASE_CSS = """
:root{--fg:#eef1f8;--muted:#98a2b8;--bg:#05070d;--bg-alt:#0a0e1a;--bg-elev:#10162a;--border:#232a42;--accent:#4f7cff;--accent-2:#9b6bff;--accent-grad:linear-gradient(90deg,var(--accent),var(--accent-2));--radius:14px}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg);line-height:1.6}
a{color:var(--fg);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:3px}
img{max-width:100%}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-48px;background:var(--accent);color:#fff;padding:9px 16px;border-radius:0 0 10px 10px;z-index:100;transition:top .15s}
.skip-link:focus{top:0}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.site{border-bottom:1px solid var(--border);padding:16px 0;background:var(--bg)}
header.site .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{display:inline-flex;align-items:center;font-weight:700;font-size:1.15rem;color:var(--fg)}
.brand:hover{text-decoration:none}
.brand-mark{display:inline-flex;gap:3px;align-items:flex-end;width:20px;height:18px;margin-right:9px}
.brand-mark i{display:block;width:6px;border-radius:3px;background:var(--accent-grad);font-style:normal}
.brand-mark i:first-child{height:65%}
.brand-mark i:last-child{height:100%}
nav.site{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
nav.site a{color:var(--muted);font-size:.92rem}
nav.site a:hover{color:var(--fg);text-decoration:none}
.searchbox{display:flex;align-items:center;gap:8px;background:var(--bg-elev);border:1px solid var(--border);border-radius:999px;padding:9px 16px 9px 14px}
.searchbox svg{flex:none;color:var(--muted)}
.searchbox input{flex:1;min-width:120px;background:transparent;border:0;color:var(--fg);font-size:.88rem;outline:0}
.searchbox input::placeholder{color:var(--muted)}
.searchbox button{border:0;background:transparent;color:var(--muted);cursor:pointer;padding:0;display:flex}
.badge-pill{display:inline-flex;align-items:center;gap:7px;background:var(--bg-elev);border:1px solid var(--border);border-radius:999px;padding:7px 16px;font-size:.82rem;color:var(--muted)}
.hero{padding:72px 0 56px;text-align:center;background:radial-gradient(120% 100% at 50% 0%,#101a3a 0%,var(--bg) 60%)}
.hero h1{font-size:2.6rem;margin:22px 0 16px;line-height:1.22;font-weight:800}
.grad{background:var(--accent-grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p.tagline{color:var(--muted);font-size:1.08rem;margin:0 auto 30px;max-width:640px}
.hero-cta{display:inline-flex;align-items:center;gap:8px;padding:15px 30px;border-radius:999px;background:var(--accent-grad);color:#fff;font-weight:600;font-size:1.02rem}
.hero-cta:hover{opacity:.92;text-decoration:none}
.hero-note{margin:16px 0 0;color:var(--muted);font-size:.85rem;display:flex;align-items:center;justify-content:center;gap:6px}
.feature-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px;margin:44px auto 0;max-width:920px;text-align:left}
.feature-row div{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.feature-row .icon{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:rgba(79,124,255,.14);color:var(--accent);margin-bottom:12px}
.feature-row strong{display:block;margin-bottom:4px;font-size:.98rem}
.feature-row p{margin:0;color:var(--muted);font-size:.88rem}
.categories{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;margin:28px 0}
.category-card{display:block;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px 14px;text-align:center;color:var(--fg);transition:border-color .15s,transform .15s}
.category-card:hover{border-color:var(--accent);transform:translateY(-2px);text-decoration:none}
.category-card .emoji{font-size:1.3rem;width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:rgba(155,107,255,.14);margin:0 auto 10px}
.category-card .count{color:var(--muted);font-size:.82rem;display:block;margin-top:5px}
.guides{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin:20px 0}
.guide-card{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px;display:block;color:var(--fg);transition:border-color .15s,transform .15s}
.guide-card:hover{border-color:var(--accent);transform:translateY(-2px);text-decoration:none}
.guide-card .cat{color:var(--accent);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.guide-card h3{margin:8px 0 0;font-size:1.05rem}
.guide-card .guide-link{display:block;margin-top:10px;color:var(--accent);font-size:.88rem;font-weight:600}
.cat-intro{color:var(--muted);font-size:1.02rem;margin:14px 0 26px;max-width:640px}
.empty-state{color:var(--muted);border:1px dashed var(--border);border-radius:var(--radius);padding:24px;text-align:center;background:var(--bg-elev)}
section.block{padding:40px 0}
section.block h2{font-size:1.4rem;margin-bottom:16px}
.how-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px}
.how-steps div{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:18px}
.disclosure,p.disclosure{background:var(--bg-elev);border:1px solid var(--border);border-left:4px solid var(--accent-2);border-radius:10px;padding:13px 15px;font-size:.88rem;color:var(--fg)}
.disclosure strong{color:var(--accent-2)}
.note{font-size:.85rem;color:var(--muted);font-style:italic}
.cta a.button{display:inline-block;padding:15px 28px;border-radius:999px;background:var(--accent-grad);color:#fff;font-weight:600;font-size:1.02rem}
.cta a.button:hover{opacity:.92;text-decoration:none}
footer.site{border-top:1px solid var(--border);margin-top:48px;padding:28px 0;color:var(--muted);font-size:.88rem;background:var(--bg-alt)}
footer.site a{color:var(--muted)}
footer.site a:hover{color:var(--fg)}
.breadcrumb{font-size:.85rem;color:var(--muted);margin-bottom:10px}
.breadcrumb a{color:var(--muted)}
.breadcrumb a:hover{color:var(--fg)}
article h1{font-size:1.8rem;line-height:1.3}
article section{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px 22px;margin:22px 0}
article h2{font-size:1.15rem;margin-top:0}
dl dt{font-weight:600;margin-top:10px}
dl dd{margin:2px 0 0;color:var(--muted)}
@media (max-width:640px){.hero{padding:48px 0 36px}.hero h1{font-size:1.9rem}nav.site{gap:12px}.searchbox{display:none}}
@media (prefers-reduced-motion:reduce){.category-card,.guide-card,.skip-link{transition:none}}
"""


def _nav_links(environ=None) -> str:
    base = _real_base_url(environ)
    items = [("/", "Home")] + [(f"/kategorie/{key}/", _split_emoji_label(label)[1])
                               for key, label in SITE_CATEGORIES[:4]]
    return "".join(f'<a href="{_esc(base + href)}">{_esc(label)}</a>' for href, label in items)


_SEARCH_ICON_SVG = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
                    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                    'aria-hidden="true"><circle cx="11" cy="11" r="7"/>'
                    '<line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>')


def render_header(environ=None) -> str:
    base = _real_base_url(environ)
    return f"""<header class="site"><div class="wrap">
<a class="brand" href="{_esc(base + '/')}"><span class="brand-mark" aria-hidden="true"><i></i><i></i></span>{_esc(SITE_BRAND)}</a>
<nav class="site" aria-label="Main navigation">{_nav_links(environ)}
<form class="searchbox" id="site-search" onsubmit="return false;" role="search">
<label class="sr-only" for="site-search-input">Search buying guides</label>
<button type="submit" aria-label="Search">{_SEARCH_ICON_SVG}</button>
<input type="search" id="site-search-input" placeholder="What are you looking to buy?" aria-label="Search buying guides">
</form>
</nav>
</div></header>"""


def render_footer(environ=None) -> str:
    base = _real_base_url(environ)
    return f"""<footer class="site"><div class="wrap">
<p><a class="brand" href="{_esc(base + '/')}"><span class="brand-mark" aria-hidden="true"><i></i><i></i></span>{_esc(SITE_BRAND)}</a></p>
<p>&copy; {_esc(SITE_BRAND)}. All prices and offers per the respective providers/affiliate programs, without guarantee.</p>
<nav aria-label="Legal">
<a href="{_esc(base + '/impressum/')}">Imprint</a> &middot;
<a href="{_esc(base + '/datenschutz/')}">Privacy</a> &middot;
<a href="{_esc(base + '/affiliate-erklaerung/')}">How we make money</a>
</nav>
</div></footer>"""


def page_shell(*, title: str, description: str, body_html: str, environ=None) -> str:
    """Wrap `body_html` (already-rendered, escaped-as-needed content) with
    the shared header/footer/CSS/branding - the ONE place every page on
    the site gets its look from. `environ=` (default None -> the real
    process environment) lets header/footer navigation links resolve the
    real GitHub Pages PROJECT base path (see `_real_base_url()`) - never a
    second, independently-guessed base."""
    full_title = SITE_BRAND if title == SITE_BRAND else f"{title} – {SITE_BRAND}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(full_title)}</title>
<meta name="description" content="{_esc(description)}">
<style>{_BASE_CSS}</style>
</head><body>
<a class="skip-link" href="#content">Skip to content</a>
{render_header(environ)}
<main id="content">
{body_html}
</main>
{render_footer(environ)}
</body></html>"""


# ---------------------------------------------------------------------------
# real-data read helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuideCard:
    title: str
    live_url: str
    site_category: str


def _real_guide_cards(data_dir) -> list[GuideCard]:
    offers = {o.offer_id: o for o in AffiliateOfferStore.load(data_dir).all()}
    cards = []
    for asset in AffiliateAssetStore.load(data_dir).all():
        if not asset.live_url:
            continue
        offer = offers.get(asset.offer_id)
        site_cat = classify_offer_category(offer.category if offer else "")
        cards.append(GuideCard(title=asset.title or "Buying guide", live_url=asset.live_url,
                               site_category=site_cat))
    return cards


# ---------------------------------------------------------------------------
# homepage
# ---------------------------------------------------------------------------

def render_homepage(data_dir, *, environ=None) -> str:
    base = _real_base_url(environ)
    cards = _real_guide_cards(data_dir)
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.site_category] = counts.get(c.site_category, 0) + 1

    def _cat_card(key: str, label: str) -> str:
        emoji, text = _split_emoji_label(label)
        n = counts.get(key, 0)
        return (f'<a class="category-card" href="{_esc(base + f"/kategorie/{key}/")}">'
                f'<span class="emoji" aria-hidden="true">{emoji}</span>{_esc(text)}'
                f'<span class="count">{n} guide{"" if n == 1 else "s"}</span></a>')

    category_html = "".join(_cat_card(key, label) for key, label in SITE_CATEGORIES)

    if cards:
        guides_html = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}">'
            f'<span class="cat">{_esc(_split_emoji_label(_CATEGORY_LABELS.get(c.site_category, ""))[1])}</span>'
            f'<h3>{_esc(c.title)}</h3><span class="guide-link">View guide &rarr;</span></a>'
            for c in cards) + "</div>"
    else:
        guides_html = ('<div class="empty-state">No buying guide published yet - '
                       'check back soon.</div>')

    body = f"""<section class="hero"><div class="wrap">
<span class="badge-pill">🔗 Transparent affiliate links</span>
<h1><span class="grad">{_esc(SITE_TAGLINE)}</span></h1>
<p class="tagline">Honest, transparently funded buying advice based on real offers - no made-up tests, no fake reviews. Some links are affiliate links (see <a href="{_esc(base + '/affiliate-erklaerung/')}">How we make money</a>).</p>
<a class="hero-cta" href="#guides">Explore the guides &rarr;</a>
<p class="hero-note">🛡️ Transparently funded &middot; No hidden cost</p>
<div class="feature-row">
<div><span class="icon">{_SEARCH_ICON_SVG}</span><strong>Real demand</strong><p>We watch what people are actually searching for - no made-up topics.</p></div>
<div><span class="icon">🛡️</span><strong>Compared honestly</strong><p>The providers' own facts, without made-up tests, stars or customer quotes.</p></div>
<div><span class="icon">🔗</span><strong>Transparent links</strong><p>Affiliate links clearly labelled, at no extra cost to you.</p></div>
</div>
</div></section>

<section class="block wrap">
<h2>Categories</h2>
<div class="categories">{category_html}</div>
</section>

<section class="block wrap" id="guides">
<h2>Latest buying guides</h2>
{guides_html}
</section>

<section class="block wrap">
<h2>How {_esc(SITE_BRAND)} works</h2>
<div class="how-steps">
<div><strong>1. Spot real demand</strong><p>We watch what people are actually searching for and asking - no made-up topics.</p></div>
<div><strong>2. Check a real offer</strong><p>We only link to affiliate programs we have joined ourselves and whose terms we have reviewed.</p></div>
<div><strong>3. Compare honestly</strong><p>We present the providers' facts - without made-up tests, stars or customer quotes.</p></div>
<div><strong>4. Link transparently</strong><p>If you buy through our link we may earn a commission - at no extra cost to you. See <a href="{_esc(base + '/affiliate-erklaerung/')}">How we make money</a>.</p></div>
</div>
</section>
<script>
(function(){{
  var input = document.getElementById('site-search-input');
  var cards = document.querySelectorAll('#guides .guide-card');
  if (!input || !cards.length) return;
  input.addEventListener('input', function(){{
    var q = input.value.trim().toLowerCase();
    cards.forEach(function(card){{
      var match = !q || card.textContent.toLowerCase().indexOf(q) !== -1;
      card.style.display = match ? '' : 'none';
    }});
  }});
}})();
</script>"""
    return page_shell(title=SITE_BRAND, description=SITE_TAGLINE, body_html=body, environ=environ)


# ---------------------------------------------------------------------------
# category pages
# ---------------------------------------------------------------------------

def render_category_page(category_key: str, data_dir, *, environ=None) -> str:
    base = _real_base_url(environ)
    emoji, name = _split_emoji_label(_CATEGORY_LABELS.get(category_key, category_key))
    cards = [c for c in _real_guide_cards(data_dir) if c.site_category == category_key]

    breadcrumb_html = (f'<nav aria-label="Breadcrumb" class="breadcrumb">'
                       f'<a href="{_esc(base + "/")}">Home</a> &rsaquo; {_esc(name)}</nav>')

    if cards:
        body_list = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}">'
            f'<span class="cat">{_esc(_split_emoji_label(_CATEGORY_LABELS.get(c.site_category, ""))[1])}</span>'
            f'<h3>{_esc(c.title)}</h3><span class="guide-link">View guide &rarr;</span></a>'
            for c in cards) + "</div>"
    else:
        body_list = ('<div class="empty-state">There are no buying guides in this category '
                    'yet - coming soon.</div>')

    badge = (f'<span class="badge-pill"><span aria-hidden="true">{emoji}</span> Category</span>'
             if emoji else '<span class="badge-pill">Category</span>')
    body = f"""<section class="block wrap">
{breadcrumb_html}
{badge}
<h1><span class="grad">{_esc(name)}</span></h1>
<p class="cat-intro">Buying guides on {_esc(name)} - based on real offers from affiliate
programs and real, publicly asked questions. No made-up tests, no fake reviews.</p>
{body_list}
</section>"""
    return page_shell(title=name, description=f"Buying guides: {name}", body_html=body, environ=environ)


def all_category_pages(data_dir, *, environ=None) -> dict[str, str]:
    """{relative file path: html} for every site category - always all of
    them, even empty ones, so the nav/category grid never links to a 404."""
    return {f"kategorie/{key}/index.html": render_category_page(key, data_dir, environ=environ)
           for key, _label in SITE_CATEGORIES}


# ---------------------------------------------------------------------------
# legal pages - real facts only; a legally-required field we do not have
# configured is explicitly flagged as outstanding, never invented.
# ---------------------------------------------------------------------------

def _business_email() -> str:
    import os
    return (os.environ.get("BUSINESS_EMAIL") or "").strip()


#: shown at the bottom of every legal page - this pass improves accuracy and
#: completeness but is not a substitute for a qualified legal review.
_LEGAL_REVIEW_NOTE = (
    '<p class="disclosure"><strong>Note:</strong> These statements describe the '
    'actual technical and organisational state of this website. They have been '
    'prepared with care but are not a substitute for individual legal advice '
    'and do not warrant completeness or legal validity.</p>')

#: exactly the fields a natural person operating this site from Germany must
#: still supply for a complete Impressum (§ 5 DDG, § 18(2) MStV).
IMPRESSUM_REQUIRED_FIELDS: tuple[str, ...] = (
    "Full name of the responsible natural person "
    "(or the exact company name and legal form, if a company)",
    "Address that can be served with legal process "
    "(street, number, postal code, city - no PO box)",
    "A second, immediately effective means of contact besides email "
    "(e.g. a phone number or a contact form with a response commitment)",
    "VAT identification number under § 27a UStG - only if one exists",
    "Commercial register / registering court and register number - only if "
    "a registration exists",
    "Name and address of the person responsible for content under "
    "§ 18(2) MStV",
    "Statement of whether willing/obliged to take part in a consumer "
    "dispute resolution procedure (§ 36 VSBG)",
)


def render_impressum(*, environ=None) -> str:
    email = _business_email()
    contact = (f'<p>Email: <a href="mailto:{_esc(email)}">{_esc(email)}</a></p>'
               if email else '<p><strong>[TO BE COMPLETED: email address]</strong></p>')
    todo = "".join(f"<li>{_esc(f)}</li>" for f in IMPRESSUM_REQUIRED_FIELDS)
    body = f"""<section class="block wrap">
<h1>Imprint</h1>
<p>Information pursuant to § 5 of the German Digital Services Act
(Digitale-Dienste-Gesetz, DDG) and § 18(2) of the German Interstate
Media Treaty (Medienstaatsvertrag, MStV).</p>

<h2>Service provider</h2>
<p><strong>[TO BE COMPLETED: name or company name of the responsible person]</strong><br>
<strong>[TO BE COMPLETED: street and house number]</strong><br>
<strong>[TO BE COMPLETED: postal code and city]</strong><br>
Germany</p>

<h2>Contact</h2>
{contact}
<p><strong>[TO BE COMPLETED: second means of contact, e.g. a phone number]</strong></p>

<h2>Responsible for content under § 18(2) MStV</h2>
<p><strong>[TO BE COMPLETED: name and address of the responsible person]</strong></p>

<h2>VAT identification number</h2>
<p>[TO BE COMPLETED: VAT ID under § 27a UStG, if one exists - otherwise
delete this section.]</p>

<h2>EU online dispute resolution / consumer arbitration</h2>
<p>The European Commission provides a platform for online dispute
resolution: <a href="https://ec.europa.eu/consumers/odr/" rel="nofollow noopener"
target="_blank">https://ec.europa.eu/consumers/odr/</a>.
[TO BE COMPLETED/CONFIRMED: statement on willingness/obligation to take part
in a consumer dispute resolution procedure under § 36 VSBG.]</p>

<h2>Still outstanding</h2>
<p>This imprint is incomplete as long as the statutory mandatory details
marked <strong>[TO BE COMPLETED]</strong> above have not yet been entered.
The details still missing:</p>
<ul>{todo}</ul>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="Imprint",
                      description="Imprint and provider identification", body_html=body,
                      environ=environ)


def render_datenschutz(*, environ=None) -> str:
    email = _business_email()
    resp_contact = (f'Email: <a href="mailto:{_esc(email)}">{_esc(email)}</a>'
                    if email else "<strong>[TO BE COMPLETED: contact email]</strong>")
    body = f"""<section class="block wrap">
<h1>Privacy Policy</h1>

<h2>1. Controller</h2>
<p>Controller within the meaning of the General Data Protection Regulation (GDPR):<br>
<strong>[TO BE COMPLETED: name / company name]</strong><br>
<strong>[TO BE COMPLETED: address that can be served with legal process]</strong><br>
{resp_contact}</p>

<h2>2. About this website</h2>
<p>This website is a static site. We ourselves set <strong>no cookies</strong>,
no analytics or tracking scripts, no advertising pixels, no external fonts and
no embedded third-party content (e.g. maps, videos, social-media widgets).
There is no reach measurement and no profiling. A consent banner is therefore
not required for these pages.</p>

<h2>3. Hosting and server log data (GitHub Pages)</h2>
<p>This website is hosted via <strong>GitHub Pages</strong>, a service of
GitHub, Inc., 88 Colin P. Kelly Jr. Street, San Francisco, CA 94107, USA (a
Microsoft Corporation company). When you open these pages, your browser
transmits technically necessary data to GitHub as the hosting provider, in
particular the IP address, the date and time of access, the requested
address, the HTTP status code, the amount of data transferred and, where
applicable, the referrer and browser/operating-system identifier. GitHub may
process this data in server log files to ensure operation, security and
stability. We have no access to these log files; only aggregated,
non-personal access figures are available to us. The legal basis is
Art. 6(1)(f) GDPR (legitimate interest in a secure, reliable and
cost-effective provision). Processing takes place partly in the USA;
according to its own statements, GitHub/Microsoft bases transfers on the EU
Standard Contractual Clauses or the EU-U.S. Data Privacy Framework. Details:
<a href="https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement"
rel="nofollow noopener" target="_blank">GitHub Privacy Statement</a>.
[TO BE VERIFIED: confirm the current transfer mechanism and, where
applicable, a data processing agreement with GitHub.]</p>

<h2>4. Affiliate / partner links (Amazon, systeme.io)</h2>
<p>Some links on this website are partner/affiliate links. When you click
such a link, you leave this website and are taken to the respective provider
(e.g. amazon.de or systeme.io). Only the provider then processes your data
under <em>its</em> privacy policy and, as a rule, sets a cookie or stores an
identifier in order to attribute a later purchase to our partner
identification (for Amazon, the partner tag <code>airevenue-21</code>). We
have no influence over this. We receive no personal data about you from the
provider, only aggregated, anonymous statistics on clicks and, where
applicable, commissions. Placing the links is based on Art. 6(1)(f) GDPR
(legitimate interest in funding the service).</p>
<p>Providers' privacy notices:
<a href="https://www.amazon.de/gp/help/customer/display.html?nodeId=201909010"
rel="nofollow noopener" target="_blank">Amazon</a> &middot;
<a href="https://systeme.io/privacy-policy" rel="nofollow noopener"
target="_blank">systeme.io</a>.</p>

<h2>5. Contact by email</h2>
<p>If you write to us by email, we process your email address and the content
of your message solely to handle your request (Art. 6(1)(b) or (f) GDPR). The
data is deleted as soon as it is no longer required and no retention
obligations apply.</p>

<h2>6. Forms, newsletter, user accounts</h2>
<p>This website has no contact form, no newsletter and no user account. The
search function on the home page runs entirely locally in your browser; no
data is transmitted to us or to third parties in the process.</p>

<h2>7. Your rights</h2>
<p>Under the GDPR you have the right of access (Art. 15), rectification
(Art. 16), erasure (Art. 17), restriction (Art. 18) and data portability
(Art. 20), and a right to object to processing based on Art. 6(1)(f)
(Art. 21). You can withdraw any consent given at any time with effect for the
future. You also have the right to lodge a complaint with a data protection
supervisory authority (Art. 77 GDPR).
[TO BE COMPLETED: the specific competent supervisory authority based on the
operator's residence/place of business.]</p>

<h2>8. No obligation to provide data, no automated decision-making</h2>
<p>You are not obliged to provide us with personal data. There is no
automated decision-making, including profiling, within the meaning of
Art. 22 GDPR.</p>

<h2>9. Status and changes</h2>
<p>This statement reflects the current state. If the technology used changes,
we will adapt it.</p>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="Privacy Policy", description="Privacy Policy",
                      body_html=body, environ=environ)


def render_affiliate_erklaerung(*, environ=None) -> str:
    body = f"""<section class="block wrap">
<h1>How we make money</h1>
<p>This website publishes buying guides for real products and services. Some
of the linked offers are affiliate programs (affiliate links): if you buy or
sign up through such a link, we may earn a commission from the provider. This
does not cost you anything extra. These links are advertising.</p>

<p>We only link to programs we have actually joined ourselves, and we present
only facts that the provider states itself or that we have checked directly -
never made-up test results, stars or customer quotes. We do not test the
products ourselves and do not claim to.</p>

<h2>Amazon</h2>
<p>We take part in the <strong>Amazon PartnerNet</strong> affiliate program;
our partner tag is <code>airevenue-21</code>.</p>
<p><strong>As an Amazon Associate I earn from qualifying purchases.</strong></p>
<p>Amazon and the Amazon logo are trademarks of Amazon.com, Inc. or its
affiliates; this does not imply any endorsement, review or support of this
website by Amazon.</p>

<h2>Other programs</h2>
<p>We also use the <strong>systeme.io</strong> affiliate program. Other
networks such as <strong>Awin</strong> and <strong>CJ Affiliate</strong> are
prepared but currently not represented with active links on this website.</p>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="How we make money",
                      description="Transparency about affiliate links and partner programs",
                      body_html=body, environ=environ)


def render_legal_pages(*, environ=None) -> dict[str, str]:
    return {
        "impressum/index.html": render_impressum(environ=environ),
        "datenschutz/index.html": render_datenschutz(environ=environ),
        "affiliate-erklaerung/index.html": render_affiliate_erklaerung(environ=environ),
    }


# ---------------------------------------------------------------------------
# crawlability - sitemap.xml / robots.txt (spec: "crawlable page
# structure"). A sitemap requires ABSOLUTE URLs by spec - `_real_base_url()`
# only ever returns a real, already-configured GitHub Pages base URL (pure
# config read, no network call, no guessed domain); without it,
# `render_sitemap_xml()` fails closed (returns None, never a fabricated
# domain) and `build_site_artifact()` simply omits sitemap.xml.
# ---------------------------------------------------------------------------

def _real_base_url(environ=None) -> str:
    from ..deploy import DeployError, GitHubPagesConfig

    try:
        return GitHubPagesConfig.from_env(environ).public_base()
    except DeployError:
        return ""


def render_robots_txt(environ=None) -> str:
    base = _real_base_url(environ)
    lines = ["User-agent: *", "Allow: /"]
    if base:
        lines.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(lines) + "\n"


def _sitemap_paths(data_dir) -> list[str]:
    paths = ["/", "/impressum/", "/datenschutz/", "/affiliate-erklaerung/"]
    paths += [f"/kategorie/{key}/" for key, _label in SITE_CATEGORIES]
    return paths


def render_sitemap_xml(data_dir, *, environ=None) -> str | None:
    base = _real_base_url(environ)
    if not base:
        return None
    locs = [base + p for p in _sitemap_paths(data_dir)]
    locs += [c.live_url for c in _real_guide_cards(data_dir)]
    body = "".join(f"<url><loc>{_esc(u)}</loc></url>" for u in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{body}</urlset>")


# ---------------------------------------------------------------------------
# assembly + deploy
# ---------------------------------------------------------------------------

def build_site_artifact(data_dir) -> DeploymentArtifact:
    files: dict[str, str] = {"index.html": render_homepage(data_dir)}
    files.update(all_category_pages(data_dir))
    files.update(render_legal_pages())
    # serve the HTML exactly as written - no Jekyll processing (build speed,
    # and no surprise transforms of files/dirs whose name starts with "_").
    files[".nojekyll"] = ""
    files["robots.txt"] = render_robots_txt()
    sitemap = render_sitemap_xml(data_dir)
    if sitemap:
        files["sitemap.xml"] = sitemap
    return DeploymentArtifact(opportunity_id="site", slug=SITE_ROOT_SLUG, files=files)


def deploy_site(data_dir, *, adapter=None) -> dict:
    """Deploy the homepage + category + legal pages as one artifact at the
    site ROOT (not a guide's own subfolder) - reuses the exact same
    deployment adapter contract every other deploy in this codebase uses,
    so it works identically against `FakeDeploymentAdapter` (tests) and
    the real, credential-gated GitHub Pages adapter."""
    artifact = build_site_artifact(data_dir)
    result = (adapter or default_deployment_adapter()).deploy(artifact)
    return {"deployed": result.success, "blocked": result.blocked,
           "live_url": result.live_url, "error": result.error,
           "provider": result.provider, "reasons": [] if result.success else [result.error]}
