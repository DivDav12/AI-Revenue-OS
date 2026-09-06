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
SITE_TAGLINE = "Finde das richtige Produkt – ohne stundenlang zu suchen."
SITE_ROOT_SLUG = ""   # deploys at the repo root, not a subfolder


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


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

#: (key, emoji label) - display order.
SITE_CATEGORIES: tuple[tuple[str, str], ...] = (
    (CATEGORY_KOPFHOERER, "\U0001F3A7 Kopfhörer & Earbuds"),
    (CATEGORY_MIKROFONE, "\U0001F399️ Mikrofone"),
    (CATEGORY_TASTATUREN, "⌨️ Tastaturen"),
    (CATEGORY_MAEUSE, "\U0001F5B1️ Mäuse"),
    (CATEGORY_MONITORE, "\U0001F5A5️ Monitore"),
    (CATEGORY_GAMING, "\U0001F3AE Gaming"),
    (CATEGORY_TECHNIK, "\U0001F4BB Technik"),
)

_CATEGORY_LABELS = dict(SITE_CATEGORIES) | {CATEGORY_SONSTIGES: "\U0001F4E6 Sonstiges"}

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


def category_breadcrumb(offer_category: str) -> tuple[str, str]:
    """(label, path) for the site category a real `AffiliateOffer.category`
    falls under - used by a guide page to link back to its own category
    (internal linking, spec: "crawlable page structure")."""
    key = classify_offer_category(offer_category)
    label = _CATEGORY_LABELS.get(key, key)
    return label, f"/kategorie/{key}/"


# ---------------------------------------------------------------------------
# shared chrome
# ---------------------------------------------------------------------------

_BASE_CSS = """
:root{--fg:#16181d;--muted:#5b6270;--bg:#ffffff;--bg-alt:#f6f7f9;--border:#e6e8ec;--accent:#1a56db;--accent-dark:#123f9e}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg);line-height:1.6}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
img{max-width:100%}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
header.site{border-bottom:1px solid var(--border);padding:16px 0}
header.site .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{font-weight:700;font-size:1.25rem;color:var(--fg)}
.brand:hover{text-decoration:none}
nav.site a{margin-left:18px;color:var(--fg);font-size:.95rem}
nav.site a:first-child{margin-left:0}
.hero{padding:56px 0 40px;text-align:center;background:var(--bg-alt)}
.hero h1{font-size:2rem;margin:0 0 12px;line-height:1.25}
.hero p.tagline{color:var(--muted);font-size:1.1rem;margin:0 0 28px}
.searchbox{max-width:520px;margin:0 auto;display:flex;gap:8px}
.searchbox input{flex:1;padding:12px 14px;border:1px solid var(--border);border-radius:8px;font-size:1rem}
.searchbox button{padding:12px 18px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:1rem;cursor:pointer}
.searchbox button:hover{background:var(--accent-dark)}
.categories{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;margin:28px 0}
.category-card{display:block;border:1px solid var(--border);border-radius:10px;padding:18px 14px;text-align:center;color:var(--fg)}
.category-card:hover{border-color:var(--accent);text-decoration:none}
.category-card .emoji{font-size:1.6rem;display:block;margin-bottom:8px}
.category-card .count{color:var(--muted);font-size:.85rem;display:block;margin-top:4px}
.guides{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin:20px 0}
.guide-card{border:1px solid var(--border);border-radius:10px;padding:18px;display:block;color:var(--fg)}
.guide-card:hover{border-color:var(--accent);text-decoration:none}
.guide-card .cat{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.02em}
.guide-card h3{margin:6px 0 0;font-size:1.05rem}
.empty-state{color:var(--muted);border:1px dashed var(--border);border-radius:10px;padding:24px;text-align:center}
section.block{padding:36px 0}
section.block h2{font-size:1.4rem;margin-bottom:14px}
.how-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px}
.how-steps div{border:1px solid var(--border);border-radius:10px;padding:16px}
.disclosure,p.disclosure{background:var(--bg-alt);border:1px solid var(--border);border-radius:8px;padding:12px 14px;font-size:.9rem;color:var(--muted)}
.cta a.button{display:inline-block;padding:14px 26px;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;font-size:1.05rem}
.cta a.button:hover{background:var(--accent-dark);text-decoration:none}
footer.site{border-top:1px solid var(--border);margin-top:48px;padding:28px 0;color:var(--muted);font-size:.9rem}
footer.site a{color:var(--muted)}
.breadcrumb{font-size:.85rem;color:var(--muted);margin-bottom:10px}
article h1{font-size:1.7rem;line-height:1.3}
article section{margin:26px 0}
article h2{font-size:1.2rem}
dl dt{font-weight:600;margin-top:10px}
dl dd{margin:2px 0 0}
@media (max-width:560px){.hero{padding:36px 0 28px}.hero h1{font-size:1.5rem}.searchbox{flex-direction:column}}
"""


def _nav_links() -> str:
    items = [("/", "Start")] + [(f"/kategorie/{key}/", label.split(" ", 1)[1] if " " in label else label)
                                for key, label in SITE_CATEGORIES[:4]]
    return "".join(f'<a href="{_esc(href)}">{_esc(label)}</a>' for href, label in items)


def render_header() -> str:
    return f"""<header class="site"><div class="wrap">
<a class="brand" href="/">{_esc(SITE_BRAND)}</a>
<nav class="site">{_nav_links()}</nav>
</div></header>"""


def render_footer() -> str:
    return f"""<footer class="site"><div class="wrap">
<p>&copy; {_esc(SITE_BRAND)}. Alle Preise und Angebote laut Angaben der jeweiligen Anbieter/Partnerprogramme, ohne Gewähr.</p>
<p>
<a href="/impressum/">Impressum</a> &middot;
<a href="/datenschutz/">Datenschutz</a> &middot;
<a href="/affiliate-erklaerung/">Wie wir Geld verdienen</a>
</p>
</div></footer>"""


def page_shell(*, title: str, description: str, body_html: str) -> str:
    """Wrap `body_html` (already-rendered, escaped-as-needed content) with
    the shared header/footer/CSS/branding - the ONE place every page on
    the site gets its look from."""
    full_title = SITE_BRAND if title == SITE_BRAND else f"{title} – {SITE_BRAND}"
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(full_title)}</title>
<meta name="description" content="{_esc(description)}">
<style>{_BASE_CSS}</style>
</head><body>
{render_header()}
{body_html}
{render_footer()}
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
        cards.append(GuideCard(title=asset.title or "Kaufberatung", live_url=asset.live_url,
                               site_category=site_cat))
    return cards


# ---------------------------------------------------------------------------
# homepage
# ---------------------------------------------------------------------------

def render_homepage(data_dir) -> str:
    cards = _real_guide_cards(data_dir)
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.site_category] = counts.get(c.site_category, 0) + 1

    category_html = "".join(
        f'<a class="category-card" href="/kategorie/{_esc(key)}/">'
        f'<span class="emoji">{label.split(" ", 1)[0]}</span>{_esc(label.split(" ", 1)[1])}'
        f'<span class="count">{counts.get(key, 0)} Ratgeber</span></a>'
        for key, label in SITE_CATEGORIES)

    if cards:
        guides_html = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}">'
            f'<span class="cat">{_esc(_CATEGORY_LABELS.get(c.site_category, ""))}</span>'
            f'<h3>{_esc(c.title)}</h3></a>' for c in cards) + "</div>"
    else:
        guides_html = ('<div class="empty-state">Noch keine Kaufberatung veröffentlicht - '
                       'schau bald wieder vorbei.</div>')

    body = f"""<section class="hero"><div class="wrap">
<h1>{_esc(SITE_TAGLINE)}</h1>
<p class="tagline">Ehrliche, unabhängige Kaufberatung auf Basis echter Angebote - keine erfundenen Tests, keine gefälschten Bewertungen.</p>
<form class="searchbox" id="site-search" onsubmit="return false;">
<input type="search" id="site-search-input" placeholder="Was möchtest du kaufen?" aria-label="Suche">
<button type="submit">Suchen</button>
</form>
</div></section>

<section class="block wrap">
<h2>Kategorien</h2>
<div class="categories">{category_html}</div>
</section>

<section class="block wrap" id="guides">
<h2>Aktuelle Kaufberatungen</h2>
{guides_html}
</section>

<section class="block wrap">
<h2>So funktioniert {_esc(SITE_BRAND)}</h2>
<div class="how-steps">
<div><strong>1. Echte Nachfrage erkennen</strong><p>Wir beobachten, wonach Menschen tatsächlich suchen und fragen - keine erfundenen Themen.</p></div>
<div><strong>2. Echtes Angebot prüfen</strong><p>Wir verlinken nur Partnerprogramme, denen wir selbst beigetreten sind und deren Konditionen wir geprüft haben.</p></div>
<div><strong>3. Ehrlich vergleichen</strong><p>Wir stellen die Fakten der Anbieter dar - ohne erfundene Tests, Sterne oder Kundenstimmen.</p></div>
<div><strong>4. Transparent verlinken</strong><p>Kaufst du über unseren Link, erhalten wir ggf. eine Provision - ohne Mehrkosten für dich. Siehe <a href="/affiliate-erklaerung/">Wie wir Geld verdienen</a>.</p></div>
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
    return page_shell(title=SITE_BRAND, description=SITE_TAGLINE, body_html=body)


# ---------------------------------------------------------------------------
# category pages
# ---------------------------------------------------------------------------

def render_category_page(category_key: str, data_dir) -> str:
    label = _CATEGORY_LABELS.get(category_key, category_key)
    cards = [c for c in _real_guide_cards(data_dir) if c.site_category == category_key]
    if cards:
        body_list = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}"><h3>{_esc(c.title)}</h3></a>'
            for c in cards) + "</div>"
    else:
        body_list = ('<div class="empty-state">Für diese Kategorie gibt es aktuell noch keine '
                    'Kaufberatung - bald verfügbar.</div>')
    body = f"""<section class="block wrap">
<h1>{_esc(label)}</h1>
{body_list}
</section>"""
    return page_shell(title=label, description=f"Kaufberatung: {label}", body_html=body)


def all_category_pages(data_dir) -> dict[str, str]:
    """{relative file path: html} for every site category - always all of
    them, even empty ones, so the nav/category grid never links to a 404."""
    return {f"kategorie/{key}/index.html": render_category_page(key, data_dir)
           for key, _label in SITE_CATEGORIES}


# ---------------------------------------------------------------------------
# legal pages - real facts only; a legally-required field we do not have
# configured is explicitly flagged as outstanding, never invented.
# ---------------------------------------------------------------------------

def _business_email() -> str:
    import os
    return (os.environ.get("BUSINESS_EMAIL") or "").strip()


def render_impressum() -> str:
    email = _business_email()
    contact = (f"<p>E-Mail: <a href=\"mailto:{_esc(email)}\">{_esc(email)}</a></p>" if email else "")
    body = f"""<section class="block wrap">
<h1>Impressum</h1>
<p><strong>Hinweis:</strong> Die vollständigen Pflichtangaben nach §&nbsp;5 TMG
(Name/Firma, ladungsfähige Anschrift, ggf. Handelsregister/USt-IdNr.) werden vom
Betreiber ergänzt und liegen aktuell noch nicht vor. Diese Seite ist daher noch
nicht vollständig.</p>
{contact}
</section>"""
    return page_shell(title="Impressum", description="Impressum", body_html=body)


def render_datenschutz() -> str:
    email = _business_email()
    contact = (f"<p>Kontakt für Datenschutzanfragen: <a href=\"mailto:{_esc(email)}\">{_esc(email)}</a></p>"
              if email else "")
    body = f"""<section class="block wrap">
<h1>Datenschutzerklärung</h1>
<p>Diese Website ist eine statische Seite. Wir setzen aktuell keine Cookies, keine
Analyse- oder Tracking-Skripte und keine Werbenetzwerke auf dieser Seite ein.</p>
<p>Wenn du über einen Link auf dieser Seite zu einem Partnerangebot (z. B.
systeme.io) wechselst, gilt ab diesem Zeitpunkt die Datenschutzerklärung des
jeweiligen Anbieters - wir haben keinen Einfluss darauf, welche Daten dieser
Anbieter erhebt.</p>
{contact}
<p>Diese Datenschutzerklärung beschreibt den aktuellen technischen Stand dieser
Seite; sie ersetzt keine individuelle Rechtsberatung.</p>
</section>"""
    return page_shell(title="Datenschutz", description="Datenschutzerklärung", body_html=body)


def render_affiliate_erklaerung() -> str:
    body = """<section class="block wrap">
<h1>Wie wir Geld verdienen</h1>
<p>Diese Seite veröffentlicht Kaufberatungen zu echten Produkten und Diensten.
Manche der verlinkten Angebote sind Partnerprogramme (Affiliate-Links): Kaufst
oder registrierst du dich über einen solchen Link, erhalten wir möglicherweise
eine Provision vom Anbieter. Dir entstehen dadurch keine zusätzlichen Kosten.</p>
<p>Wir verlinken ausschließlich Programme, denen wir selbst tatsächlich
beigetreten sind, und stellen nur Fakten dar, die der Anbieter selbst angibt
oder die wir direkt geprüft haben - niemals erfundene Testergebnisse, Sterne
oder Kundenstimmen.</p>
<p>Aktuell nutzen bzw. planen wir Partnerprogramme u. a. über systeme.io sowie
perspektivisch über Netzwerke wie Awin, CJ Affiliate und Amazon PartnerNet,
sobald die jeweilige Freischaltung vorliegt.</p>
</section>"""
    return page_shell(title="Wie wir Geld verdienen", description="Affiliate-Transparenz", body_html=body)


def render_legal_pages() -> dict[str, str]:
    return {
        "impressum/index.html": render_impressum(),
        "datenschutz/index.html": render_datenschutz(),
        "affiliate-erklaerung/index.html": render_affiliate_erklaerung(),
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
