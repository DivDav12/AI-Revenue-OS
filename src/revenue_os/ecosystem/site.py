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
    ("pdf", CATEGORY_TECHNIK),
    # generic tech / PC accessories (power banks, USB-C hubs, chargers,
    # portable SSDs, webcams, streaming sticks, smart-home gear ...) - the
    # real offer category for these is prefixed "tech-" on ingest.
    ("tech", CATEGORY_TECHNIK),
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
:root{--fg:#1a1a1a;--muted:#666460;--bg:#ffffff;--bg-page:#efe4cc;--bg-alt:#f7f5f0;--bg-elev:#ffffff;--border:#ddd6c4;--accent:#2c3e50;--accent-2:#8b2e2e;--radius:4px;--serif:"Playfair Display",Georgia,"Times New Roman",serif}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg-page);line-height:1.65}
h1,h2,h3,.brand{font-family:var(--serif)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
img{max-width:100%}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-48px;background:var(--accent);color:#fff;padding:9px 16px;border-radius:0 0 4px 4px;z-index:100;transition:top .15s}
.skip-link:focus{top:0}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.site{border-bottom:2px solid var(--fg);padding:20px 0;background:var(--bg)}
header.site .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{display:inline-flex;align-items:center;font-weight:800;font-size:1.4rem;color:var(--fg);letter-spacing:.01em}
.brand:hover{text-decoration:none}
.brand-mark{display:inline-flex;gap:3px;align-items:flex-end;width:20px;height:18px;margin-right:10px}
.brand-mark i{display:block;width:6px;background:var(--accent);font-style:normal}
.brand-mark i:first-child{height:65%}
.brand-mark i:last-child{height:100%}
nav.site{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
nav.site a{color:var(--fg);font-size:.92rem}
nav.site a:hover{color:var(--accent);text-decoration:none}
.searchbox{position:relative;display:flex;align-items:center;gap:8px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:9px 16px 9px 14px}
.searchbox svg{flex:none;color:var(--muted)}
.searchbox input{flex:1;min-width:120px;background:transparent;border:0;color:var(--fg);font-size:.88rem;outline:0;font-family:inherit}
.searchbox input::placeholder{color:var(--muted)}
.searchbox button{border:0;background:transparent;color:var(--muted);cursor:pointer;padding:0;display:flex}
.search-results{position:absolute;top:calc(100% + 8px);right:0;left:0;min-width:300px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:6px;z-index:120;max-height:min(70vh,440px);overflow-y:auto;box-shadow:0 10px 28px rgba(26,26,26,.14)}
.search-results a{display:block;padding:9px 12px;border-radius:2px;color:var(--fg)}
.search-results a:hover,.search-results a[aria-selected="true"]{background:var(--bg-alt);text-decoration:none}
.search-results .k{display:inline-block;font-size:.66rem;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);border:1px solid var(--border);border-radius:2px;padding:1px 7px;margin-right:8px;vertical-align:1px}
.search-results .st{display:block;color:var(--muted);font-size:.8rem;margin-top:2px}
.search-results .empty{padding:12px;color:var(--muted);font-size:.85rem}
.badge-pill{display:inline-flex;align-items:center;gap:7px;color:var(--accent);font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.hero{padding:72px 0 56px;text-align:center;background:var(--bg);border-bottom:1px solid var(--border)}
.hero h1{font-size:2.7rem;margin:18px 0 16px;line-height:1.2;font-weight:800}
.grad{color:var(--accent)}
.hero p.tagline{color:var(--muted);font-size:1.08rem;margin:0 auto 30px;max-width:640px}
.hero-cta{display:inline-flex;align-items:center;gap:8px;padding:14px 30px;border-radius:var(--radius);background:var(--accent);color:#fff;font-weight:700;font-size:1.02rem;border:1px solid var(--accent)}
.hero-cta:hover{opacity:.88;text-decoration:none}
.hero-note{margin:16px 0 0;color:var(--muted);font-size:.85rem;display:flex;align-items:center;justify-content:center;gap:6px}
.feature-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px;margin:44px auto 0;max-width:920px;text-align:left}
.feature-row div{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.feature-row .icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:var(--accent);margin-bottom:10px;font-size:1.3rem}
.feature-row strong{display:block;margin-bottom:4px;font-size:.98rem;font-family:var(--serif)}
.feature-row p{margin:0;color:var(--muted);font-size:.88rem}
.categories{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;margin:28px 0}
.category-card{display:block;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px 14px;text-align:center;color:var(--fg);transition:border-color .15s}
.category-card:hover{border-color:var(--accent);text-decoration:none}
.category-card .emoji{font-size:1.3rem;width:44px;height:44px;display:flex;align-items:center;justify-content:center;margin:0 auto 10px}
.category-card .count{color:var(--muted);font-size:.82rem;display:block;margin-top:5px}
.guides{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin:20px 0}
.guide-card{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px;display:block;color:var(--fg);transition:border-color .15s}
.guide-card:hover{border-color:var(--accent);text-decoration:none}
.guide-card .cat{color:var(--accent);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.guide-card h3{margin:8px 0 0;font-size:1.05rem}
.guide-card .guide-link{display:block;margin-top:10px;color:var(--accent);font-size:.88rem;font-weight:700}
.cat-intro{color:var(--muted);font-size:1.02rem;margin:14px 0 26px;max-width:640px}
.empty-state{color:var(--muted);border:1px dashed var(--border);border-radius:var(--radius);padding:24px;text-align:center;background:var(--bg-elev)}
section.block{padding:40px 0}
section.block h2{font-size:1.5rem;margin-bottom:16px;font-weight:700}
.how-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px}
.how-steps div{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:18px}
.disclosure,p.disclosure{background:var(--bg-alt);border:1px solid var(--border);border-left:3px solid var(--accent-2);border-radius:2px;padding:13px 15px;font-size:.88rem;color:var(--fg)}
.disclosure strong{color:var(--accent-2)}
.note{font-size:.85rem;color:var(--muted);font-style:italic}
.cta a.button{display:inline-block;padding:14px 28px;border-radius:var(--radius);background:var(--accent);color:#fff;font-weight:700;font-size:1.02rem;border:1px solid var(--accent)}
.cta a.button:hover{opacity:.88;text-decoration:none}
footer.site{border-top:2px solid var(--fg);margin-top:48px;padding:28px 0;color:var(--muted);font-size:.88rem;background:var(--bg-alt)}
footer.site a{color:var(--muted)}
footer.site a:hover{color:var(--accent)}
.breadcrumb{font-size:.85rem;color:var(--muted);margin-bottom:10px}
.breadcrumb a{color:var(--muted)}
.breadcrumb a:hover{color:var(--accent)}
article h1{font-size:1.9rem;line-height:1.3}
article section{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:20px 22px;margin:22px 0}
article h2{font-size:1.2rem;margin-top:0}
dl dt{font-weight:700;margin-top:10px}
dl dd{margin:2px 0 0;color:var(--muted)}
.product-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:18px;margin:22px 0}
.product-card{display:flex;flex-direction:column;background:var(--bg-elev);border:1px solid var(--border);border-top:3px solid var(--accent);border-radius:var(--radius);overflow:hidden;color:var(--fg);transition:border-color .15s}
.product-card:hover{border-color:var(--accent);text-decoration:none}
.product-card .shot{aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;background:var(--bg-alt);border-bottom:1px solid var(--border)}
.product-card .shot img{width:100%;height:100%;object-fit:contain;padding:10px}
.product-card .shot .ph{width:48px;height:48px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:1.4rem}
.product-card .body{padding:16px 16px 18px;display:flex;flex-direction:column;gap:8px;flex:1}
.product-card .pname{font-size:1rem;font-weight:700;line-height:1.35;margin:0;font-family:var(--serif)}
.product-card .price{font-size:1.05rem;font-weight:700;color:var(--fg)}
.product-card .price .muted{font-size:.82rem;font-weight:400;color:var(--muted)}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{display:inline-block;background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:2px;padding:3px 10px;font-size:.74rem;letter-spacing:.02em;text-transform:uppercase}
.product-card .ctx{color:var(--muted);font-size:.85rem;margin:0}
.product-card .view{margin-top:auto;color:var(--accent);font-size:.88rem;font-weight:700}
.product-detail{display:grid;grid-template-columns:minmax(0,420px) 1fr;gap:32px;margin:18px 0 8px}
.product-detail .gallery{display:flex;flex-direction:column;gap:10px}
.product-detail .gallery .main{aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;background:var(--bg-alt);border:1px solid var(--border);border-radius:var(--radius)}
.product-detail .gallery .main img{width:100%;height:100%;object-fit:contain;padding:16px}
.product-detail .gallery .main .ph{width:72px;height:72px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:2rem}
.product-detail .thumbs{display:flex;gap:8px;flex-wrap:wrap}
.product-detail .thumbs .thumb{padding:0;margin:0;border:1px solid var(--border);background:var(--bg-elev);border-radius:2px;cursor:pointer;line-height:0;transition:border-color .15s}
.product-detail .thumbs .thumb:hover{border-color:var(--accent)}
.product-detail .thumbs .thumb[aria-pressed="true"]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.product-detail .thumbs .thumb img{width:64px;height:64px;object-fit:contain;padding:4px;display:block}
@media (prefers-reduced-motion:reduce){.product-detail .thumbs .thumb{transition:none}}
.product-detail h1{margin:0 0 12px}
.product-detail .price{font-size:1.5rem;font-weight:800;margin:6px 0}
.product-detail .price .muted{display:block;font-size:.82rem;font-weight:400;color:var(--muted);margin-top:2px}
.buy-cta{display:inline-flex;align-items:center;gap:8px;margin:18px 0 8px;padding:14px 30px;border-radius:var(--radius);background:var(--accent);color:#fff;font-weight:700;font-size:1.03rem;border:1px solid var(--accent)}
.buy-cta:hover{opacity:.88;text-decoration:none}
.ctx-question{background:var(--bg-alt);border:1px solid var(--border);border-left:3px solid var(--accent-2);border-radius:2px;padding:16px 18px;margin:16px 0}
.ctx-question h2{margin:0 0 6px;font-size:1.05rem}
.ctx-question p{margin:0 0 8px;color:var(--muted)}
.ctx-question a{color:var(--accent);font-weight:700}
@media (max-width:760px){.product-detail{grid-template-columns:1fr;gap:20px}.product-detail .gallery{max-width:360px}}
@media (max-width:640px){.hero{padding:48px 0 36px}.hero h1{font-size:1.9rem}nav.site{gap:12px}.searchbox{display:none}}
@media (prefers-reduced-motion:reduce){.category-card,.guide-card,.product-card,.skip-link{transition:none}}
"""


def _nav_links(environ=None) -> str:
    base = _real_base_url(environ)
    items = [("/", "Home"), ("/product/", "Products")] + [
        (f"/kategorie/{key}/", _split_emoji_label(label)[1])
        for key, label in SITE_CATEGORIES[:3]]
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
<label class="sr-only" for="site-search-input">Search products and buying guides</label>
<button type="submit" aria-label="Search">{_SEARCH_ICON_SVG}</button>
<input type="search" id="site-search-input" placeholder="Search products and guides" autocomplete="off"
 role="combobox" aria-expanded="false" aria-controls="site-search-results" aria-autocomplete="list"
 aria-label="Search products and buying guides">
<div class="search-results" id="site-search-results" role="listbox" aria-label="Search results" hidden></div>
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


#: the shared, site-wide search behaviour - present on EVERY page (via
#: page_shell). It lazily fetches the static `search-index.json`
#: (products + guides + categories + legal pages, real data only) the
#: first time the box is used, then filters it into an accessible
#: results dropdown (arrow-key + Enter navigation, click-outside to
#: close). No backend, no external call - just one same-origin GET of a
#: file this site generates itself. Fails silent if the index can't be
#: loaded (e.g. a file:// preview).
_SEARCH_JS = r"""<script>
(function(){
  var box=document.getElementById('site-search'),
      input=document.getElementById('site-search-input'),
      panel=document.getElementById('site-search-results');
  if(!box||!input||!panel)return;
  var base=window.__SITE_BASE__||'',idx=null,loading=false,items=[],sel=-1,t;
  function load(cb){
    if(idx){cb();return;}
    if(loading)return; loading=true;
    fetch(base+'/search-index.json',{credentials:'omit'})
      .then(function(r){return r.ok?r.json():[];})
      .then(function(d){idx=Array.isArray(d)?d:[];loading=false;cb();})
      .catch(function(){idx=[];loading=false;cb();});
  }
  function score(it,words){
    var hay=(it.t+' '+(it.s||'')+' '+it.k).toLowerCase(),title=it.t.toLowerCase(),s=0;
    for(var i=0;i<words.length;i++){
      var w=words[i];
      if(hay.indexOf(w)===-1)return -1;
      s+=title.indexOf(w)===0?3:title.indexOf(w)!==-1?2:1;
    }
    if(it.k==='Product')s+=1;
    return s;
  }
  function clear(){while(panel.firstChild)panel.removeChild(panel.firstChild);}
  function close(){panel.hidden=true;clear();sel=-1;
    input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');}
  function render(){
    var words=input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if(!words.length){close();return;}
    items=(idx||[]).map(function(it){return{it:it,s:score(it,words)};})
      .filter(function(x){return x.s>=0;})
      .sort(function(a,b){return b.s-a.s;}).slice(0,8).map(function(x){return x.it;});
    sel=-1; clear();
    if(!items.length){
      var e=document.createElement('div'); e.className='empty';
      e.textContent='No matches for “'+input.value.trim()+'”';
      panel.appendChild(e);
    }else{
      items.forEach(function(it,i){
        var a=document.createElement('a');
        a.setAttribute('role','option'); a.id='ssr-'+i;
        a.setAttribute('href', it.u);
        var k=document.createElement('span'); k.className='k'; k.textContent=it.k;
        a.appendChild(k); a.appendChild(document.createTextNode(it.t));
        if(it.s){var st=document.createElement('span'); st.className='st';
          st.textContent=it.s; a.appendChild(st);}
        panel.appendChild(a);
      });
    }
    panel.hidden=false;input.setAttribute('aria-expanded','true');
  }
  function move(d){
    if(panel.hidden||!items.length)return;
    sel=(sel+d+items.length)%items.length;
    var as=panel.querySelectorAll('a');
    for(var i=0;i<as.length;i++)as[i].removeAttribute('aria-selected');
    if(as[sel]){as[sel].setAttribute('aria-selected','true');
      as[sel].scrollIntoView({block:'nearest'});
      input.setAttribute('aria-activedescendant','ssr-'+sel);}
  }
  function go(){var a=panel.querySelectorAll('a')[sel>=0?sel:0];
    if(a){window.location.href=a.getAttribute('href');}}
  input.addEventListener('input',function(){clearTimeout(t);
    t=setTimeout(function(){load(render);},120);});
  input.addEventListener('focus',function(){if(input.value.trim())load(render);});
  input.addEventListener('keydown',function(e){
    if(e.key==='ArrowDown'){e.preventDefault();move(1);}
    else if(e.key==='ArrowUp'){e.preventDefault();move(-1);}
    else if(e.key==='Enter'){if(!panel.hidden){e.preventDefault();go();}}
    else if(e.key==='Escape'){close();}
  });
  box.addEventListener('submit',function(e){e.preventDefault();if(!panel.hidden)go();});
  document.addEventListener('click',function(e){if(!box.contains(e.target))close();});
})();
</script>"""


def page_shell(*, title: str, description: str, body_html: str, environ=None) -> str:
    """Wrap `body_html` (already-rendered, escaped-as-needed content) with
    the shared header/footer/CSS/branding - the ONE place every page on
    the site gets its look from. `environ=` (default None -> the real
    process environment) lets header/footer navigation links resolve the
    real GitHub Pages PROJECT base path (see `_real_base_url()`) - never a
    second, independently-guessed base. Every page also gets the shared,
    site-wide search (see `_SEARCH_JS` / `search-index.json`)."""
    import json

    full_title = SITE_BRAND if title == SITE_BRAND else f"{title} – {SITE_BRAND}"
    base_js = json.dumps(_real_base_url(environ))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(full_title)}</title>
<meta name="description" content="{_esc(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800;900&display=swap" rel="stylesheet">
<style>{_BASE_CSS}</style>
<script>window.__SITE_BASE__={base_js};</script>
</head><body>
<a class="skip-link" href="#content">Skip to content</a>
{render_header(environ)}
<main id="content">
{body_html}
</main>
{render_footer(environ)}
{_SEARCH_JS}
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
    from .products import EXCLUDED_NETWORKS

    offers = {o.offer_id: o for o in AffiliateOfferStore.load(data_dir).all()}
    cards = []
    for asset in AffiliateAssetStore.load(data_dir).all():
        if not asset.live_url:
            continue
        offer = offers.get(asset.offer_id)
        # a guide for a product the public site no longer recommends (e.g.
        # the retired systeme.io strategy) is dropped from every public
        # surface - homepage, category pages and the sitemap.
        if offer is not None and offer.network in EXCLUDED_NETWORKS:
            continue
        site_cat = classify_offer_category(offer.category if offer else "")
        cards.append(GuideCard(title=asset.guide_title or asset.title or "Buying guide",
                               live_url=asset.live_url, site_category=site_cat))
    return cards


# ---------------------------------------------------------------------------
# product-first catalog rendering (spec: "product discovery platform").
# Every product/tag/price/image fact comes from ecosystem.products, which
# reads only real persisted offer/asset/link data - nothing here invents a
# product, a price, an image, or a product<->guide relationship.
# ---------------------------------------------------------------------------

_PRODUCT_ICON = "\U0001F4E6"   # 📦 - the neutral placeholder when no compliant image exists

#: generic advertising label + (Amazon-only) the required participant
#: identification statement - imported from affiliate_assets so the exact
#: approved wording is defined in exactly one place.
def _product_disclosure_html(is_amazon: bool) -> str:
    from .affiliate_assets import AMAZON_ASSOCIATE_DISCLOSURE, DISCLOSURE_TEXT

    text = f"<strong>Advertisement / affiliate link.</strong> {_esc(DISCLOSURE_TEXT)}"
    if is_amazon:
        text += f" {_esc(AMAZON_ASSOCIATE_DISCLOSURE)}"
    return text


def _product_url(base: str, slug: str) -> str:
    return f"{base}/product/{slug}/"


def _tags_html(tags) -> str:
    if not tags:
        return ""
    return '<div class="tags">' + "".join(
        f'<span class="tag">{_esc(t)}</span>' for t in tags) + "</div>"


def _product_image_html(product, *, big: bool) -> str:
    """A real product image (from a compliant source stored on the offer)
    or a clean icon placeholder - never a fabricated image."""
    alt = _esc(f"{product.name} product image")
    if product.image_urls:
        first = _esc(product.image_urls[0])
        return (f'<img src="{first}" alt="{alt}" loading="lazy" decoding="async">')
    return (f'<span class="ph" role="img" aria-label="{alt}">'
            f'{_PRODUCT_ICON}</span>')


def render_product_card(product, base: str) -> str:
    href = _esc(_product_url(base, product.slug))
    price = (f'{_esc(product.price_display)}'
             if product.has_price else
             f'<span class="muted">{_esc(product.price_display)}</span>')
    ctx = f'<p class="ctx">{_esc(product.short_context)}</p>' if product.short_context else ""
    return (
        f'<a class="product-card" href="{href}">'
        f'<span class="shot">{_product_image_html(product, big=False)}</span>'
        f'<span class="body">'
        f'<span class="pname">{_esc(product.name)}</span>'
        f'<span class="price">{price}</span>'
        f'{_tags_html(product.tags)}'
        f'{ctx}'
        f'<span class="view">View product &rarr;</span>'
        f'</span></a>')


def _product_grid_html(products, base: str, *, empty_msg: str) -> str:
    if not products:
        return f'<div class="empty-state">{_esc(empty_msg)}</div>'
    return ('<div class="product-grid">'
            + "".join(render_product_card(p, base) for p in products)
            + "</div>")


def render_products_index(data_dir, *, environ=None) -> str:
    from . import products as products_mod

    base = _real_base_url(environ)
    all_products = products_mod.load_public_products(data_dir)
    by_cat: dict[str, list] = {}
    for p in all_products:
        by_cat.setdefault(p.site_category, []).append(p)

    breadcrumb = (f'<nav aria-label="Breadcrumb" class="breadcrumb">'
                  f'<a href="{_esc(base + "/")}">Home</a> &rsaquo; Products</nav>')
    if all_products:
        sections = []
        for key, label in SITE_CATEGORIES:
            items = by_cat.get(key)
            if not items:
                continue
            _emoji, name = _split_emoji_label(label)
            sections.append(
                f'<section class="block"><h2>{_esc(name)}</h2>'
                f'{_product_grid_html(items, base, empty_msg="")}'
                f'<p><a class="guide-link" href="{_esc(base + f"/kategorie/{key}/")}">'
                f'Browse {_esc(name)} &rarr;</a></p></section>')
        # anything under "Sonstiges" (unmapped category) still gets shown.
        other = by_cat.get(CATEGORY_SONSTIGES)
        if other:
            sections.append(f'<section class="block"><h2>More</h2>'
                            f'{_product_grid_html(other, base, empty_msg="")}</section>')
        grid_html = "".join(sections)
    else:
        grid_html = ('<div class="empty-state">More products coming soon. In the '
                     'meantime, browse our buying guides below.</div>')

    body = f"""<section class="block wrap">
{breadcrumb}
<span class="badge-pill">\U0001F4E6 Product catalog</span>
<h1><span class="grad">Browse products</span></h1>
<p class="cat-intro">Real products from affiliate programs we have joined
ourselves. Each product page shows the key facts and links straight to the
correct product - no made-up tests, no fake reviews.</p>
{grid_html}
</section>"""
    return page_shell(title="Products", description="Browse curated products by category",
                      body_html=body, environ=environ)


def _context_question(product) -> str:
    """A real, grounded 'Looking for ...?' line built only from the
    product's own category + tags - never an invented claim."""
    cat_label = _split_emoji_label(_CATEGORY_LABELS.get(product.site_category, ""))[1]
    noun = cat_label[:-1].lower() if cat_label.endswith("s") else cat_label.lower()
    lead = product.tags[0].lower() if product.tags else ""
    if lead and noun:
        return f"Looking for a {lead} {noun}?"
    if noun:
        return f"Looking for a {noun}?"
    return "Looking for a product like this?"


def render_product_page(slug: str, data_dir, *, environ=None) -> str:
    from . import products as products_mod

    base = _real_base_url(environ)
    all_products = products_mod.load_public_products(data_dir)
    product = next((p for p in all_products if p.slug == slug), None)
    if product is None:
        raise KeyError(f"no public product with slug {slug!r}")

    cat_label = _split_emoji_label(_CATEGORY_LABELS.get(product.site_category,
                                                       product.site_category))[1]
    cat_path = f"{base}/kategorie/{product.site_category}/"
    breadcrumb = (
        f'<nav aria-label="Breadcrumb" class="breadcrumb">'
        f'<a href="{_esc(base + "/")}">Home</a> &rsaquo; '
        f'<a href="{_esc(base + "/product/")}">Products</a> &rsaquo; '
        f'<a href="{_esc(cat_path)}">{_esc(cat_label)}</a> &rsaquo; '
        f'{_esc(product.name)}</nav>')

    # gallery - a real Amazon-CDN image (first = primary) or the accessible
    # icon placeholder. When more than one compliant image exists the
    # thumbnails are real <button>s that swap the main image (keyboard
    # operable, labelled); with JS off every image stays reachable.
    gallery_js = ""
    if product.image_urls:
        main_alt = _esc(product.name + " product image")
        main_img = (f'<img id="pd-main-img" src="{_esc(product.image_urls[0])}" '
                    f'alt="{main_alt}" decoding="async">')
        thumbs = ""
        if len(product.image_urls) > 1:
            btns = "".join(
                f'<button type="button" class="thumb" data-src="{_esc(u)}" '
                f'aria-label="{_esc(f"Show image {i + 1} of {len(product.image_urls)}")}"'
                f'{" aria-pressed=\"true\"" if i == 0 else ""}>'
                f'<img src="{_esc(u)}" alt="{_esc(f"{product.name} view {i + 1}")}" '
                f'loading="lazy" decoding="async"></button>'
                for i, u in enumerate(product.image_urls))
            thumbs = f'<div class="thumbs" role="group" aria-label="Product images">{btns}</div>'
            gallery_js = """<script>
(function(){
  var main=document.getElementById('pd-main-img');
  var btns=document.querySelectorAll('.thumbs .thumb');
  if(!main||!btns.length)return;
  btns.forEach(function(b){b.addEventListener('click',function(){
    main.src=b.getAttribute('data-src');
    btns.forEach(function(x){x.removeAttribute('aria-pressed')});
    b.setAttribute('aria-pressed','true');
  });});
})();
</script>"""
        gallery = f'<div class="main">{main_img}</div>{thumbs}'
    else:
        gallery = f'<div class="main">{_product_image_html(product, big=True)}</div>'

    price_block = ""
    if product.has_price:
        est = " (estimate - not confirmed at the source)" if product.price_is_estimate else ""
        checked = (f" Last checked {_esc(product.price_observed_at)}."
                   if product.price_observed_at else "")
        price_block = (
            f'<p class="price">{_esc(product.price_display)}{_esc(est)}'
            f'<span class="muted">Provider prices change - check the current price on '
            f'{"Amazon" if product.is_amazon else "the provider page"} before you buy.'
            f'{checked}</span></p>')
    else:
        price_block = (f'<p class="price">{_esc(product.price_display)}'
                       f'<span class="muted">We do not show a price we cannot verify.</span></p>')

    cta_label = "Find on Amazon" if product.is_amazon else "View the offer"
    cta = (f'<a class="buy-cta" href="{_esc(product.outbound_url)}" '
           f'rel="sponsored nofollow" target="_blank">{cta_label} &rarr;</a>')

    desc_html = f'<p>{_esc(product.description)}</p>' if product.description else ""

    # context + guide section - only real, already-deployed guides.
    guide_html = ""
    if product.related_guides:
        q = _context_question(product)
        first = product.related_guides[0]
        links = "".join(
            f'<li><a href="{_esc(g.url)}">{_esc(g.title)}</a></li>'
            for g in product.related_guides)
        guide_html = f"""<div class="ctx-question">
<h2>{_esc(q)}</h2>
<p>Before choosing, our buying guide explains the main things to check.</p>
<a href="{_esc(first.url)}">Read the buying guide &rarr;</a>
<ul>{links}</ul>
</div>"""

    # related products
    rel = products_mod.related_products(product, all_products, limit=3)
    related_html = ""
    if rel:
        related_html = (f'<section class="block"><h2>You may also like</h2>'
                        f'{_product_grid_html(rel, base, empty_msg="")}</section>')

    disclosure = _product_disclosure_html(product.is_amazon)

    ld = _product_jsonld(product, base)

    body = f"""<article>
{breadcrumb}
<div class="product-detail">
<div class="gallery">{gallery}</div>
<div class="info">
<h1>{_esc(product.name)}</h1>
<p class="disclosure" role="note">{disclosure}</p>
{price_block}
{_tags_html(product.tags)}
{desc_html}
{cta}
<p class="note">Verification: {_esc(product.verification_status)}.</p>
</div>
</div>
{guide_html}
<section>
<h2>About this product</h2>
<p>These are the provider's own stated details, shown here without a test
or a review of our own. We link to the correct product; the price and
availability are always as shown by {"Amazon" if product.is_amazon else "the provider"}
at the time you click.</p>
</section>
{related_html}
<p class="cta">
{cta}<br>
<span class="disclosure" role="note">{disclosure}</span>
</p>
</article>
{ld}{(chr(10) + gallery_js) if gallery_js else ""}"""

    title = f"{product.name}"
    description = (product.short_context or product.description
                   or f"{product.name} - product details and where to buy.")
    return page_shell(title=title, description=description[:200], body_html=body,
                      environ=environ)


def _product_jsonld(product, base: str) -> str:
    """BreadcrumbList + a minimal Product node - only facts the project
    actually knows (name, description, image, url). No reviews, ratings,
    aggregate ratings, brand, sku or availability are ever emitted, and a
    price is emitted only when it is source-confirmed (not an estimate)."""
    import json

    prod: dict = {"@context": "https://schema.org", "@type": "Product",
                  "name": product.name, "url": _product_url(base, product.slug)}
    if product.description:
        prod["description"] = product.description
    if product.image_urls:
        prod["image"] = list(product.image_urls)
    cat_label = _split_emoji_label(_CATEGORY_LABELS.get(product.site_category, ""))[1]
    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{base}/"},
            {"@type": "ListItem", "position": 2, "name": "Products",
             "item": f"{base}/product/"},
            {"@type": "ListItem", "position": 3, "name": cat_label or "Category",
             "item": f"{base}/kategorie/{product.site_category}/"},
            {"@type": "ListItem", "position": 4, "name": product.name,
             "item": _product_url(base, product.slug)},
        ],
    }
    return (f'<script type="application/ld+json">{json.dumps(prod)}</script>'
            f'<script type="application/ld+json">{json.dumps(crumbs)}</script>')


def all_product_pages(data_dir, *, environ=None) -> dict[str, str]:
    """{relative file path: html} - the products index plus one detail page
    per eligible product. Empty of detail pages (index only) when no
    product qualifies - never a thin fake page."""
    from . import products as products_mod

    pages = {"product/index.html": render_products_index(data_dir, environ=environ)}
    for p in products_mod.load_public_products(data_dir):
        pages[f"product/{p.slug}/index.html"] = render_product_page(
            p.slug, data_dir, environ=environ)
    return pages


# ---------------------------------------------------------------------------
# site-wide search index (spec: the header search must actually return
# results). A flat, static list the client-side search (`_SEARCH_JS`)
# fetches once. Every entry is real, already-persisted data - a product,
# a deployed buying guide, a real site category, or a real page. Nothing
# fabricated.
# ---------------------------------------------------------------------------

def search_index(data_dir, *, environ=None) -> list[dict]:
    """[{t: title, u: url, k: kind, s: subtitle}] for every product,
    deployed guide, category and standing page on the site."""
    from . import products as products_mod

    base = _real_base_url(environ)
    entries: list[dict] = []

    prods = products_mod.load_public_products(data_dir)
    prod_counts: dict[str, int] = {}
    for p in prods:
        prod_counts[p.site_category] = prod_counts.get(p.site_category, 0) + 1
        price = p.price_display if p.has_price else (
            "See price on Amazon" if p.is_amazon else "See current price")
        subtitle = " · ".join(x for x in (price, ", ".join(p.tags)) if x)
        entries.append({"t": p.name, "u": f"{base}/product/{p.slug}/",
                        "k": "Product", "s": subtitle})

    guide_cards = _real_guide_cards(data_dir)
    guide_counts: dict[str, int] = {}
    for c in guide_cards:
        guide_counts[c.site_category] = guide_counts.get(c.site_category, 0) + 1

    for key, label in SITE_CATEGORIES:
        name = _split_emoji_label(label)[1]
        n, g = prod_counts.get(key, 0), guide_counts.get(key, 0)
        bits = []
        if n:
            bits.append(f"{n} product{'' if n == 1 else 's'}")
        if g:
            bits.append(f"{g} guide{'' if g == 1 else 's'}")
        entries.append({"t": name, "u": f"{base}/kategorie/{key}/",
                        "k": "Category", "s": ", ".join(bits) or "coming soon"})

    # the same buying question often has two deployed guide pages (one per
    # matched product) - the search shows it once, pointing at the first.
    seen_guides: set[str] = set()
    for c in guide_cards:
        key = c.title.strip().lower()
        if key in seen_guides:
            continue
        seen_guides.add(key)
        cat = _split_emoji_label(_CATEGORY_LABELS.get(c.site_category, ""))[1]
        entries.append({"t": c.title, "u": c.live_url, "k": "Guide", "s": cat})

    entries.append({"t": "All products", "u": f"{base}/product/",
                    "k": "Page", "s": "Browse the full catalogue"})
    entries.append({"t": "Imprint", "u": f"{base}/impressum/", "k": "Page", "s": ""})
    entries.append({"t": "Privacy Policy", "u": f"{base}/datenschutz/",
                    "k": "Page", "s": ""})
    entries.append({"t": "How we make money", "u": f"{base}/affiliate-erklaerung/",
                    "k": "Page", "s": "Affiliate disclosure"})
    return entries


def render_search_index_json(data_dir, *, environ=None) -> str:
    import json

    return json.dumps(search_index(data_dir, environ=environ),
                      ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# homepage
# ---------------------------------------------------------------------------

def render_homepage(data_dir, *, environ=None) -> str:
    from . import products as products_mod

    base = _real_base_url(environ)
    cards = _real_guide_cards(data_dir)
    all_products = products_mod.load_public_products(data_dir)
    prod_counts: dict[str, int] = {}
    for p in all_products:
        prod_counts[p.site_category] = prod_counts.get(p.site_category, 0) + 1
    guide_counts: dict[str, int] = {}
    for c in cards:
        guide_counts[c.site_category] = guide_counts.get(c.site_category, 0) + 1

    def _cat_card(key: str, label: str) -> str:
        emoji, text = _split_emoji_label(label)
        n = prod_counts.get(key, 0)
        g = guide_counts.get(key, 0)
        if n:
            meta = f'{n} product{"" if n == 1 else "s"}'
        elif g:
            meta = f'{g} guide{"" if g == 1 else "s"}'
        else:
            meta = "coming soon"
        return (f'<a class="category-card" href="{_esc(base + f"/kategorie/{key}/")}">'
                f'<span class="emoji" aria-hidden="true">{emoji}</span>{_esc(text)}'
                f'<span class="count">{_esc(meta)}</span></a>')

    category_html = "".join(_cat_card(key, label) for key, label in SITE_CATEGORIES)
    featured = all_products[:6]
    products_html = _product_grid_html(
        featured, base,
        empty_msg="More products coming soon - browse our buying guides below.")

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
<span class="badge-pill">\U0001F4E6 Curated product discovery</span>
<h1><span class="grad">{_esc(SITE_TAGLINE)}</span></h1>
<p class="tagline">Browse real products from affiliate programs we have joined ourselves - each with the key facts and a link straight to the right product. No made-up tests, no fake reviews. Some links are affiliate links (see <a href="{_esc(base + '/affiliate-erklaerung/')}">How we make money</a>).</p>
<a class="hero-cta" href="{_esc(base + '/product/')}">Browse products &rarr;</a>
<p class="hero-note">🛡️ Transparently funded &middot; No hidden cost</p>
<div class="feature-row">
<div><span class="icon">\U0001F4E6</span><strong>Products first</strong><p>See the actual products for a category before any buying guide.</p></div>
<div><span class="icon">🛡️</span><strong>Compared honestly</strong><p>The providers' own facts, without made-up tests, stars or customer quotes.</p></div>
<div><span class="icon">🔗</span><strong>Transparent links</strong><p>Affiliate links clearly labelled, at no extra cost to you.</p></div>
</div>
</div></section>

<section class="block wrap" id="products">
<h2>Featured products</h2>
{products_html}
<p><a class="guide-link" href="{_esc(base + '/product/')}">See all products &rarr;</a></p>
</section>

<section class="block wrap">
<h2>Browse by category</h2>
<div class="categories">{category_html}</div>
</section>

<section class="block wrap" id="guides">
<h2>Buying guides</h2>
<p class="cat-intro">Background reading to help you choose - secondary to the product pages above.</p>
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
"""
    return page_shell(title=SITE_BRAND, description=SITE_TAGLINE, body_html=body, environ=environ)


# ---------------------------------------------------------------------------
# category pages
# ---------------------------------------------------------------------------

def render_category_page(category_key: str, data_dir, *, environ=None) -> str:
    from . import products as products_mod

    base = _real_base_url(environ)
    emoji, name = _split_emoji_label(_CATEGORY_LABELS.get(category_key, category_key))
    products = products_mod.products_in_category(data_dir, category_key)
    cards = [c for c in _real_guide_cards(data_dir) if c.site_category == category_key]

    breadcrumb_html = (f'<nav aria-label="Breadcrumb" class="breadcrumb">'
                       f'<a href="{_esc(base + "/")}">Home</a> &rsaquo; '
                       f'<a href="{_esc(base + "/product/")}">Products</a> &rsaquo; '
                       f'{_esc(name)}</nav>')

    products_html = _product_grid_html(
        products, base, empty_msg="More products coming soon.")

    if cards:
        guides_html = ('<section class="block"><h2>Related buying guides</h2>'
                       '<div class="guides">' + "".join(
                           f'<a class="guide-card" href="{_esc(c.live_url)}">'
                           f'<span class="cat">{_esc(name)}</span>'
                           f'<h3>{_esc(c.title)}</h3>'
                           f'<span class="guide-link">View guide &rarr;</span></a>'
                           for c in cards) + "</div></section>")
    else:
        guides_html = ""

    badge = (f'<span class="badge-pill"><span aria-hidden="true">{emoji}</span> Category</span>'
             if emoji else '<span class="badge-pill">Category</span>')
    body = f"""<section class="block wrap">
{breadcrumb_html}
{badge}
<h1><span class="grad">{_esc(name)}</span></h1>
<p class="cat-intro">{_esc(name)} from affiliate programs we have joined ourselves -
each product links straight to the correct product. No made-up tests, no fake reviews.</p>
{products_html}
{guides_html}
</section>"""
    return page_shell(title=name, description=f"{name} - products and buying guides",
                      body_html=body, environ=environ)


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

<h2>4. Affiliate / partner links (Amazon)</h2>
<p>Some links on this website are partner/affiliate links. When you click
such a link, you leave this website and are taken to the respective provider
(currently amazon.de). Only the provider then processes your data
under <em>its</em> privacy policy and, as a rule, sets a cookie or stores an
identifier in order to attribute a later purchase to our partner
identification (for Amazon, the partner tag <code>airevenue-21</code>). We
have no influence over this. We receive no personal data about you from the
provider, only aggregated, anonymous statistics on clicks and, where
applicable, commissions. Placing the links is based on Art. 6(1)(f) GDPR
(legitimate interest in funding the service).</p>
<p>Provider privacy notice:
<a href="https://www.amazon.de/gp/help/customer/display.html?nodeId=201909010"
rel="nofollow noopener" target="_blank">Amazon</a>.</p>

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
<p>We are also set up with the affiliate networks <strong>Awin</strong> and
<strong>CJ Affiliate</strong>, but neither is currently represented with
active links on this website.</p>
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
    from . import products as products_mod

    paths = ["/", "/product/", "/impressum/", "/datenschutz/", "/affiliate-erklaerung/"]
    paths += [f"/kategorie/{key}/" for key, _label in SITE_CATEGORIES]
    paths += [f"/product/{p.slug}/" for p in products_mod.load_public_products(data_dir)]
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
    files.update(all_product_pages(data_dir))
    files.update(render_legal_pages())
    # serve the HTML exactly as written - no Jekyll processing (build speed,
    # and no surprise transforms of files/dirs whose name starts with "_").
    files[".nojekyll"] = ""
    files["robots.txt"] = render_robots_txt()
    files["search-index.json"] = render_search_index_json(data_dir)
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
