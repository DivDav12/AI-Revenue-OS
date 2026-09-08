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
# base path - GitHub *project* Pages serve under "/<repo>/" (e.g.
# "/AI-Revenue-OS/"), not the domain root. Every internal link must carry
# that prefix or it 404s (Impressum/Datenschutz included). Resolved from
# the SAME real, already-configured deploy target `render_sitemap_xml()`
# uses - never a guessed path. Empty when no deploy config is present (so
# local renders / tests keep working root-relative and unchanged).
# ---------------------------------------------------------------------------

def site_base_path(environ=None) -> str:
    """"" or e.g. "/AI-Revenue-OS" (no trailing slash) - the path prefix
    every internal href on the deployed site needs."""
    from urllib.parse import urlparse

    base = _real_base_url(environ)
    if not base:
        return ""
    return urlparse(base).path.rstrip("/")


def _href(path: str, environ=None) -> str:
    """Prefix a root-relative internal path with the deploy base path."""
    if not path.startswith("/"):
        return path
    return f"{site_base_path(environ)}{path}"


def _split_emoji_label(label: str) -> tuple[str, str]:
    """('🎧', 'Kopfhörer & Earbuds') - so the emoji can be marked
    aria-hidden and the text stays the accessible name."""
    parts = label.split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", label


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
    (internal linking, spec: "crawlable page structure"). Label is the
    plain text (no emoji - a breadcrumb link's accessible name); path is
    already prefixed with the deploy base path so it resolves on GitHub
    *project* Pages (served under "/<repo>/")."""
    key = classify_offer_category(offer_category)
    label = _split_emoji_label(_CATEGORY_LABELS.get(key, key))[1]
    return label, _href(f"/kategorie/{key}/")


# ---------------------------------------------------------------------------
# shared chrome
# ---------------------------------------------------------------------------

_BASE_CSS = """
:root{--fg:#16181d;--muted:#5b6270;--bg:#ffffff;--bg-alt:#f6f7f9;--border:#e6e8ec;--accent:#1a56db;--accent-dark:#123f9e}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg);line-height:1.6}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:2px}
img{max-width:100%}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-40px;background:var(--accent);color:#fff;padding:8px 14px;border-radius:0 0 8px 8px;z-index:100;transition:top .15s}
.skip-link:focus{top:0}
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
.disclosure,p.disclosure{background:#fff8e6;border:1px solid #e5cf8f;border-left:4px solid #b8860b;border-radius:8px;padding:12px 14px;font-size:.95rem;color:var(--fg)}
.disclosure strong{color:#8a5a00}
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
    items = [("/", "Start")] + [(f"/kategorie/{key}/", _split_emoji_label(label)[1])
                                for key, label in SITE_CATEGORIES[:4]]
    return "".join(f'<a href="{_esc(_href(href))}">{_esc(label)}</a>' for href, label in items)


def render_header() -> str:
    return f"""<header class="site"><div class="wrap">
<a class="brand" href="{_esc(_href('/'))}">{_esc(SITE_BRAND)}</a>
<nav class="site" aria-label="Hauptnavigation">{_nav_links()}</nav>
</div></header>"""


def render_footer() -> str:
    return f"""<footer class="site"><div class="wrap">
<p>&copy; {_esc(SITE_BRAND)}. Alle Preise und Angebote laut Angaben der jeweiligen Anbieter/Partnerprogramme, ohne Gewähr.</p>
<nav aria-label="Rechtliche Hinweise">
<a href="{_esc(_href('/impressum/'))}">Impressum</a> &middot;
<a href="{_esc(_href('/datenschutz/'))}">Datenschutz</a> &middot;
<a href="{_esc(_href('/affiliate-erklaerung/'))}">Wie wir Geld verdienen</a>
</nav>
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
<a class="skip-link" href="#inhalt">Zum Inhalt springen</a>
{render_header()}
<main id="inhalt">
{body_html}
</main>
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

    def _cat_card(key: str, label: str) -> str:
        emoji, text = _split_emoji_label(label)
        return (f'<a class="category-card" href="{_esc(_href(f"/kategorie/{key}/"))}">'
                f'<span class="emoji" aria-hidden="true">{emoji}</span>{_esc(text)}'
                f'<span class="count">{counts.get(key, 0)} Ratgeber</span></a>')

    category_html = "".join(_cat_card(key, label) for key, label in SITE_CATEGORIES)

    if cards:
        guides_html = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}">'
            f'<span class="cat">{_esc(_split_emoji_label(_CATEGORY_LABELS.get(c.site_category, ""))[1])}</span>'
            f'<h3>{_esc(c.title)}</h3></a>' for c in cards) + "</div>"
    else:
        guides_html = ('<div class="empty-state">Noch keine Kaufberatung veröffentlicht - '
                       'schau bald wieder vorbei.</div>')

    body = f"""<section class="hero"><div class="wrap">
<h1>{_esc(SITE_TAGLINE)}</h1>
<p class="tagline">Ehrliche, transparent finanzierte Kaufberatung auf Basis echter Angebote - keine erfundenen Tests, keine gefälschten Bewertungen. Manche Links sind Affiliate-Links (siehe <a href="{_esc(_href('/affiliate-erklaerung/'))}">Wie wir Geld verdienen</a>).</p>
<form class="searchbox" id="site-search" onsubmit="return false;">
<label class="sr-only" for="site-search-input">Kaufberatungen durchsuchen</label>
<input type="search" id="site-search-input" placeholder="Was möchtest du kaufen?" aria-label="Kaufberatungen durchsuchen">
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
<div><strong>4. Transparent verlinken</strong><p>Kaufst du über unseren Link, erhalten wir ggf. eine Provision - ohne Mehrkosten für dich. Siehe <a href="{_esc(_href('/affiliate-erklaerung/'))}">Wie wir Geld verdienen</a>.</p></div>
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
    emoji, text = _split_emoji_label(label)
    cards = [c for c in _real_guide_cards(data_dir) if c.site_category == category_key]
    if cards:
        body_list = '<div class="guides">' + "".join(
            f'<a class="guide-card" href="{_esc(c.live_url)}"><h3>{_esc(c.title)}</h3></a>'
            for c in cards) + "</div>"
    else:
        body_list = ('<div class="empty-state">Für diese Kategorie gibt es aktuell noch keine '
                    'Kaufberatung - bald verfügbar.</div>')
    heading = (f'<span aria-hidden="true">{emoji}</span> {_esc(text)}' if emoji else _esc(text))
    body = f"""<section class="block wrap">
<h1>{heading}</h1>
{body_list}
</section>"""
    return page_shell(title=text, description=f"Kaufberatung: {text}", body_html=body)


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


#: shown at the bottom of every legal page - this pass improves accuracy and
#: completeness but is not a substitute for a qualified legal review.
_LEGAL_REVIEW_NOTE = (
    '<p class="disclosure"><strong>Hinweis:</strong> Diese Angaben beschreiben '
    'den tatsächlichen technischen und organisatorischen Stand dieser Website. '
    'Sie wurden sorgfältig erstellt, ersetzen aber keine individuelle '
    'Rechtsberatung und begründen keine Gewähr für Vollständigkeit oder '
    'rechtliche Wirksamkeit.</p>')

#: exactly the fields a natural person operating this site from Germany must
#: still supply for a complete Impressum (§ 5 DDG, § 18 Abs. 2 MStV). One
#: place, so the Impressum page and the HUMAN_REQUIRED checklist can't drift.
IMPRESSUM_REQUIRED_FIELDS: tuple[str, ...] = (
    "Vollständiger Name der verantwortlichen natürlichen Person "
    "(bzw. exakte Firmierung samt Rechtsform, falls ein Unternehmen)",
    "Ladungsfähige Anschrift (Straße, Hausnummer, PLZ, Ort - kein Postfach)",
    "Zweites, unmittelbar wirksames Kontaktmittel neben der E-Mail "
    "(z. B. Telefonnummer oder ein Kontaktformular mit Reaktionszusage)",
    "Umsatzsteuer-Identifikationsnummer nach § 27a UStG - nur falls vorhanden",
    "Handelsregister / Registergericht und Registernummer - nur falls "
    "eine Eintragung besteht",
    "Name und Anschrift der/des inhaltlich Verantwortlichen nach "
    "§ 18 Abs. 2 MStV",
    "Angabe, ob zur Teilnahme an einem Verbraucherschlichtungsverfahren "
    "bereit/verpflichtet (§ 36 VSBG)",
)


def render_impressum() -> str:
    email = _business_email()
    contact = (f'<p>E-Mail: <a href="mailto:{_esc(email)}">{_esc(email)}</a></p>'
               if email else '<p><strong>[BITTE ERGÄNZEN: E-Mail-Adresse]</strong></p>')
    todo = "".join(f"<li>{_esc(f)}</li>" for f in IMPRESSUM_REQUIRED_FIELDS)
    body = f"""<section class="block wrap">
<h1>Impressum</h1>
<p>Angaben gemäß § 5 Digitale-Dienste-Gesetz (DDG) sowie § 18 Abs. 2
Medienstaatsvertrag (MStV).</p>

<h2>Diensteanbieter</h2>
<p><strong>[BITTE ERGÄNZEN: Name bzw. Firmierung der verantwortlichen Person]</strong><br>
<strong>[BITTE ERGÄNZEN: Straße und Hausnummer]</strong><br>
<strong>[BITTE ERGÄNZEN: PLZ und Ort]</strong><br>
Deutschland</p>

<h2>Kontakt</h2>
{contact}
<p><strong>[BITTE ERGÄNZEN: zweites Kontaktmittel, z. B. Telefonnummer]</strong></p>

<h2>Verantwortlich für den Inhalt nach § 18 Abs. 2 MStV</h2>
<p><strong>[BITTE ERGÄNZEN: Name und Anschrift der verantwortlichen Person]</strong></p>

<h2>Umsatzsteuer-Identifikationsnummer</h2>
<p>[BITTE ERGÄNZEN: USt-IdNr. nach § 27a UStG, falls vorhanden - sonst diesen
Abschnitt streichen.]</p>

<h2>EU-Streitschlichtung / Verbraucherschlichtung</h2>
<p>Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung
bereit: <a href="https://ec.europa.eu/consumers/odr/" rel="nofollow noopener"
target="_blank">https://ec.europa.eu/consumers/odr/</a>.
[BITTE ERGÄNZEN/BESTÄTIGEN: Aussage zur Bereitschaft/Verpflichtung zur Teilnahme
an einem Verbraucherschlichtungsverfahren nach § 36 VSBG.]</p>

<h2>Noch offen</h2>
<p>Dieses Impressum ist unvollständig, solange die oben mit
<strong>[BITTE ERGÄNZEN]</strong> markierten gesetzlichen Pflichtangaben noch nicht
eingetragen sind. Die noch fehlenden Angaben:</p>
<ul>{todo}</ul>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="Impressum",
                      description="Impressum und Anbieterkennzeichnung", body_html=body)


def render_datenschutz() -> str:
    email = _business_email()
    resp_contact = (f'E-Mail: <a href="mailto:{_esc(email)}">{_esc(email)}</a>'
                    if email else "<strong>[BITTE ERGÄNZEN: Kontakt-E-Mail]</strong>")
    body = f"""<section class="block wrap">
<h1>Datenschutzerklärung</h1>

<h2>1. Verantwortlicher</h2>
<p>Verantwortlich im Sinne der Datenschutz-Grundverordnung (DSGVO):<br>
<strong>[BITTE ERGÄNZEN: Name / Firmierung]</strong><br>
<strong>[BITTE ERGÄNZEN: ladungsfähige Anschrift]</strong><br>
{resp_contact}</p>

<h2>2. Grundsätzliches zu dieser Website</h2>
<p>Diese Website ist eine statische Seite. Wir selbst setzen <strong>keine Cookies</strong>,
keine Analyse- oder Tracking-Skripte, keine Werbe-Pixel, keine externen
Schriftarten und keine eingebetteten Drittanbieter-Inhalte (z. B. Karten, Videos,
Social-Media-Widgets) ein. Es findet keine Reichweitenmessung und kein Profiling
statt. Ein Consent-Banner ist deshalb für diese Seiten nicht erforderlich.</p>

<h2>3. Hosting und Server-Logdaten (GitHub Pages)</h2>
<p>Diese Website wird über <strong>GitHub Pages</strong> gehostet, einen Dienst der
GitHub, Inc., 88 Colin P. Kelly Jr. Street, San Francisco, CA 94107, USA (ein
Unternehmen der Microsoft Corporation). Beim Aufruf dieser Seiten überträgt dein
Browser technisch notwendige Daten an GitHub als Hosting-Provider, insbesondere
die IP-Adresse, Datum und Uhrzeit des Zugriffs, die angeforderte Adresse, den
HTTP-Statuscode, die übertragene Datenmenge sowie ggf. Referrer und Browser-/
Betriebssystemkennung. GitHub kann diese Daten in Server-Logdateien zur
Sicherstellung von Betrieb, Sicherheit und Stabilität verarbeiten. Wir haben auf
diese Logdateien keinen Zugriff; uns liegen nur aggregierte, nicht personenbezogene
Zugriffszahlen vor. Rechtsgrundlage ist Art. 6 Abs. 1 lit. f DSGVO (berechtigtes
Interesse an einer sicheren, zuverlässigen und kostengünstigen Bereitstellung).
Die Verarbeitung erfolgt teils in den USA; GitHub/Microsoft stützt Übermittlungen
nach eigenen Angaben auf die EU-Standardvertragsklauseln bzw. das EU-U.S. Data
Privacy Framework. Einzelheiten:
<a href="https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement"
rel="nofollow noopener" target="_blank">GitHub Privacy Statement</a>.
[BITTE PRÜFEN: aktuellen Übermittlungsmechanismus und ggf. Auftragsverarbeitung
mit GitHub bestätigen.]</p>

<h2>4. Affiliate-/Partnerlinks (Amazon, systeme.io)</h2>
<p>Einige Links auf dieser Website sind Partner-/Affiliate-Links. Klickst du einen
solchen Link an, verlässt du diese Website und gelangst zum jeweiligen Anbieter
(z. B. amazon.de oder systeme.io). Erst der Anbieter verarbeitet dann deine Daten
nach <em>seiner</em> Datenschutzerklärung und setzt in der Regel ein Cookie bzw.
speichert eine Kennung, um einen späteren Kauf unserer Partnerkennung zuzuordnen
(bei Amazon der Partner-Tag <code>airevenue-21</code>). Darauf haben wir keinen
Einfluss. Wir erhalten vom Anbieter keine personenbezogenen Daten über dich,
sondern nur zusammengefasste, anonyme Statistiken zu Klicks und ggf. Provisionen.
Das Setzen der Links erfolgt auf Grundlage von Art. 6 Abs. 1 lit. f DSGVO
(berechtigtes Interesse an der Finanzierung des Angebots).</p>
<p>Datenschutzhinweise der Anbieter:
<a href="https://www.amazon.de/gp/help/customer/display.html?nodeId=201909010"
rel="nofollow noopener" target="_blank">Amazon</a> &middot;
<a href="https://systeme.io/privacy-policy" rel="nofollow noopener"
target="_blank">systeme.io</a>.</p>

<h2>5. Kontaktaufnahme per E-Mail</h2>
<p>Wenn du uns per E-Mail schreibst, verarbeiten wir deine E-Mail-Adresse und den
Inhalt deiner Nachricht ausschließlich zur Bearbeitung deines Anliegens
(Art. 6 Abs. 1 lit. b bzw. lit. f DSGVO). Die Daten werden gelöscht, sobald sie
nicht mehr erforderlich sind und keine Aufbewahrungspflichten entgegenstehen.</p>

<h2>6. Formulare, Newsletter, Nutzerkonten</h2>
<p>Auf dieser Website gibt es kein Kontaktformular, keinen Newsletter und kein
Nutzerkonto. Die Suchfunktion auf der Startseite arbeitet ausschließlich lokal in
deinem Browser; dabei werden keine Daten an uns oder Dritte übertragen.</p>

<h2>7. Deine Rechte</h2>
<p>Du hast nach der DSGVO das Recht auf Auskunft (Art. 15), Berichtigung
(Art. 16), Löschung (Art. 17), Einschränkung (Art. 18), Datenübertragbarkeit
(Art. 20) sowie ein Widerspruchsrecht gegen Verarbeitungen auf Grundlage von
Art. 6 Abs. 1 lit. f (Art. 21). Erteilte Einwilligungen kannst du jederzeit mit
Wirkung für die Zukunft widerrufen. Außerdem hast du das Recht, dich bei einer
Datenschutz-Aufsichtsbehörde zu beschweren (Art. 77 DSGVO).
[BITTE ERGÄNZEN: konkrete zuständige Landesdatenschutzbehörde nach Wohnsitz/Sitz
des Betreibers.]</p>

<h2>8. Keine Pflicht zur Bereitstellung, keine automatisierte Entscheidung</h2>
<p>Du bist nicht verpflichtet, uns personenbezogene Daten bereitzustellen. Eine
automatisierte Entscheidungsfindung einschließlich Profiling nach Art. 22 DSGVO
findet nicht statt.</p>

<h2>9. Stand und Änderungen</h2>
<p>Diese Erklärung gibt den aktuellen Stand wieder. Ändert sich die eingesetzte
Technik, passen wir sie an.</p>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="Datenschutz", description="Datenschutzerklärung",
                      body_html=body)


def render_affiliate_erklaerung() -> str:
    body = f"""<section class="block wrap">
<h1>Wie wir Geld verdienen</h1>
<p>Diese Website veröffentlicht Kaufberatungen zu echten Produkten und Diensten.
Ein Teil der verlinkten Angebote sind Partnerprogramme (Affiliate-Links): Kaufst
oder registrierst du dich über einen solchen Link, erhalten wir möglicherweise
eine Provision vom Anbieter. Dir entstehen dadurch keine zusätzlichen Kosten.
Diese Links sind Werbung.</p>

<p>Wir verlinken ausschließlich Programme, denen wir selbst tatsächlich
beigetreten sind, und stellen nur Fakten dar, die der Anbieter selbst angibt oder
die wir direkt geprüft haben - niemals erfundene Testergebnisse, Sterne oder
Kundenstimmen. Wir testen die Produkte nicht selbst und behaupten das auch nicht.</p>

<h2>Amazon</h2>
<p>Wir nehmen am Partnerprogramm <strong>Amazon PartnerNet</strong> teil; unser
Partner-Tag lautet <code>airevenue-21</code>.</p>
<p><strong>Als Amazon-Partner verdiene ich an qualifizierten Verkäufen.</strong></p>
<p>Amazon und das Amazon-Logo sind Marken von Amazon.com, Inc. oder seinen
verbundenen Unternehmen; eine Empfehlung, Prüfung oder Unterstützung dieser
Website durch Amazon ist damit nicht verbunden.</p>

<h2>Weitere Programme</h2>
<p>Zusätzlich nutzen wir das Partnerprogramm von <strong>systeme.io</strong>.
Weitere Netzwerke wie <strong>Awin</strong> und <strong>CJ Affiliate</strong> sind
vorbereitet, aber derzeit nicht aktiv.</p>
{_LEGAL_REVIEW_NOTE}
</section>"""
    return page_shell(title="Wie wir Geld verdienen",
                      description="Transparenz zu Affiliate-Links und Partnerprogrammen",
                      body_html=body)


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
    # serve the HTML exactly as written - no Jekyll processing (build speed,
    # and no surprise transforms of files/dirs that start with "_").
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
