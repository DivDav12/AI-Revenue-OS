"""Affiliate asset generation + deployment (spec sections 4 + 9).

Template-rendered (no LLM in this pass - deterministic, testable, zero
API cost), reusing the existing, real deployment stack
(`deployment.DeploymentArtifact` / `default_deployment_adapter()` - the
SAME GitHub Pages adapter the PRODUCT chain already uses via
`acceptance.py`). Nothing here invents a product claim, a review, a
testimonial, or a price: every fact in the rendered page traces back to
`AffiliateOffer.evidence` / the offer's own stated fields, or is the
literal demand quote the asset addresses.

Quality gate (spec section 9): `check_quality()` runs BEFORE deploy and
fails closed - a thin, low-content, disclosure-missing, or CTA-missing
page is never published.
"""

from __future__ import annotations

import html
import re

from ..deployment import DeploymentArtifact, default_deployment_adapter
from .affiliate_matching import AffiliateMatch
from .affiliate_model import AffiliateAsset, AffiliateAssetStore, new_id
from .model import OpportunityDraft
from .site import category_breadcrumb, page_shell

#: exact wording requested for the German customer-facing site - shown
#: directly next to/under the CTA button, and reused in the FAQ.
DISCLOSURE_TEXT = (
    "Wenn du über diesen Link kaufst, erhalten wir möglicherweise eine "
    "Provision. Für dich entstehen dadurch keine zusätzlichen Kosten.")

#: minimum body word count before a page is even considered publishable -
#: a hard floor against "thin/valueless mass pages" (spec section 9).
_MIN_WORDS = 120


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "affiliate-guide"


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def render_comparison_page(*, draft: OpportunityDraft, match: AffiliateMatch,
                           cta_url: str, guide_title: str = "",
                           related_links: tuple = ()) -> tuple[str, dict]:
    """Render one German-language "problem -> solution" buying-guide page,
    wrapped in the shared `site.py` chrome (header/nav/footer/CSS/
    branding) so every rendered page looks like one coherent site. Returns
    (html, quality_checks) - the checks are computed against the RENDERED
    content, not guessed, so `check_quality()` and the renderer can never
    silently disagree. `guide_title=` overrides the default "<product>:
    passt das zu ...?" headline (e.g. for a roundup-style buying guide) -
    every other section stays evidence-grounded regardless. `related_links=`
    is an optional tuple of `(title, url)` pairs to OTHER REAL, already-
    deployed pages (content-cluster interlinking, spec: "connect pages
    into a content cluster") - never a fabricated/placeholder link, the
    caller supplies only URLs that are already real and live; empty by
    default (byte-identical output to before this parameter existed)."""
    offer = match.offer
    problem = _esc(draft.title)
    # a "Nutzer:innen haben in eigenen Worten beschrieben" framing is only
    # honest when a REAL, independently-arising evidence quote exists (spec:
    # no fabricated demand quotes) - an opportunity with no evidence, OR an
    # editorial pick (a human chose this topic proactively, not a captured
    # post - see ecosystem.editorial), gets the neutral "häufiger Bedarf"
    # statement instead, never dressing up our own editorial judgement as a
    # stranger's verbatim words. Checked via BOTH `raw.editorial_pick` (set
    # at build time) AND `source_meta.source_type` (still correct after a
    # persist -> pipeline.draft_from_record() round trip, which does not
    # currently reconstruct `raw` - see that function's own docstring/
    # tests) - a live-deploy path going through the persisted record must
    # never lose this distinction. Plain string literal (not an import) -
    # same convention `verification.py` already uses for
    # `source_type == "human_fed"`.
    is_editorial_pick = (bool((draft.raw or {}).get("editorial_pick"))
                        or bool(draft.source_meta
                                and draft.source_meta.source_type == "editorial_pick"))
    real_evidence = [e for e in (draft.evidence or []) if str(e).strip()]
    has_real_quote = bool(real_evidence) and not is_editorial_pick
    need_quote = _esc(real_evidence[0]) if has_real_quote else _esc(draft.title)
    product = _esc(offer.product_name)
    program = _esc(offer.program_name)
    price_line = (f"Listenpreis: {_esc(offer.currency)} {offer.product_price:.2f}"
                 f"{' (Schätzung - nicht direkt an der Quelle bestätigt)' if offer.price_is_estimate else ''}"
                 if offer.product_price > 0 else
                 "Preis: siehe Angebotsseite des Anbieters (hier nicht angegeben).")
    evidence_items = "".join(f"<li>{_esc(e)}</li>" for e in offer.evidence) or (
        "<li>Der Anbieter hat keine weiteren Angaben zur Verfügung gestellt.</li>")

    faq_items = (
        f"<dt>Ist das gesponsert?</dt><dd>{_esc(DISCLOSURE_TEXT)}</dd>"
        f"<dt>Welches Problem löst das?</dt><dd>{need_quote}</dd>"
    )
    problem_statement = (
        f'Nutzer:innen haben ihren Bedarf in eigenen Worten so beschrieben: &quot;{need_quote}&quot;'
        if has_real_quote else
        f"Ein häufiger Bedarf: {need_quote}"
    )
    headline = _esc(guide_title) if guide_title else f"{product}: passt das zu &quot;{problem}&quot;?"
    # PLAIN-TEXT (unescaped) title/description for page_shell(), which
    # escapes its own inputs exactly once - `headline`/`product`/
    # `need_quote` above are already HTML-escaped for direct embedding in
    # the body, and passing them to page_shell() too would double-escape
    # (e.g. "&amp;" -> "&amp;amp;").
    title_text = guide_title if guide_title else f'{offer.product_name}: passt das zu "{draft.title}"?'
    # a curated `guide_title` is already a proper description of the page's
    # topic - preferring it over the raw demand title avoids a redundant/
    # duplicated meta description when the demand title itself already
    # names the product (a real case found live: an editorial title that
    # already started with the product's own name).
    description_text = (f"{offer.product_name}: {guide_title}" if guide_title else
                        f"{offer.product_name}: {real_evidence[0] if has_real_quote else draft.title}")

    # "Worauf du achten solltest" / "Für wen (nicht)" / Einschätzung sind
    # generische, kategorie-bezogene Kaufhinweise - niemals eine konkrete
    # Leistungsbehauptung ("klingt super", ein Sternerating, ein
    # Bewertungszitat), die nicht tatsächlich belegt ist. Nur gerendert,
    # wenn der Anbieter echte Belege liefert (spec: keine erfundenen
    # Tests/Bewertungen).
    criteria_html = pros_html = who_html = budget_html = reco_html = ""
    if offer.evidence:
        category_label = _esc(offer.category.replace("-", " ").replace("_", " ")) or "diese Kategorie"
        keyword_list = ", ".join(offer.keywords[:8])
        criteria_html = f"""<section>
<h2>Worauf du achten solltest</h2>
<ul>
<li>Ob es wirklich das abdeckt, was du brauchst: {_esc(keyword_list) or category_label}</li>
<li>Was die eigenen Angaben des Anbieters sagen (unten) - im Vergleich zu deinem tatsächlichen Bedarf</li>
<li>Die Gesamtkosten im Verhältnis zum Nutzen, inklusive laufender Kosten</li>
<li>Wie leicht der Einstieg ist - und wie leicht du später wieder kündigen/wechseln kannst</li>
</ul>
</section>"""
        pros_html = f"""<section>
<h2>Vorteile laut Anbieter</h2>
<ul>{evidence_items}</ul>
<p class="note">Das sind die eigenen Angaben des Anbieters, kein eigener
Test - wir haben diese Angaben nicht selbst unabhängig nachgeprüft.</p>
</section>"""
        who_html = f"""<section>
<h2>Nicht ideal, wenn ...</h2>
<p>... dein Bedarf nicht zu {category_label}{f" ({_esc(keyword_list)})" if keyword_list else ""}
passt. Diese Einschätzung ersetzt nicht den Abgleich deiner eigenen,
konkreten Anforderungen mit den oben genannten Angaben des Anbieters.</p>
</section>"""
        price_ts = (f" Stand: {_esc(offer.price_observed_at)}." if offer.price_observed_at else "")
        price_note = f" {_esc(offer.price_source_note)}" if offer.price_source_note else ""
        budget_html = f"""<section>
<h2>Hinweis zum Preis</h2>
<p>Der oben genannte Preis wurde zuletzt geprüft.{price_ts}{price_note} Preise
bei Anbietern ändern sich - prüfe den aktuellen Preis immer direkt auf der
Angebotsseite, bevor du kaufst.</p>
</section>"""
        reco_html = f"""<section>
<h2>Unsere Einschätzung</h2>
<p>Basierend auf den eigenen Angaben des Anbieters oben (kein eigener Test)
ist {product} eine sinnvolle Option, wenn diese Angaben zu deinem Bedarf
passen. Wir haben es nicht selbst getestet und behaupten nicht, dass es
objektiv die "beste" Option ist - nur, dass es ein reales, aktuell
verfügbares Angebot aus einem Partnerprogramm ist, dem wir tatsächlich
beigetreten sind.</p>
</section>"""

    related_html = ""
    if related_links:
        items = "".join(f'<li><a href="{_esc(u)}">{_esc(t)}</a></li>' for t, u in related_links)
        related_html = f"""<nav aria-label="Weitere Ratgeber"><h2>Weitere Kaufberatungen</h2>
<ul>{items}</ul>
</nav>"""

    # internal linking (spec: "crawlable page structure") - a real link
    # back to the guide's own category page, using the SAME category
    # taxonomy the homepage/category grid already use.
    cat_label, cat_path = category_breadcrumb(offer.category)
    breadcrumb_html = (f'<nav aria-label="Breadcrumb" class="breadcrumb">'
                       f'<a href="/">Start</a> &rsaquo; <a href="{_esc(cat_path)}">{_esc(cat_label)}</a>'
                       f'</nav>')

    body_html = f"""<article>
{breadcrumb_html}
<h1>{headline}</h1>
<p class="disclosure">{_esc(DISCLOSURE_TEXT)}</p>
<section>
<h2>Worum geht es?</h2>
<p>{problem_statement}</p>
</section>
{criteria_html}
<section>
<h2>Unsere Empfehlung: {product} ({program})</h2>
<p>{price_line}</p>
</section>
{pros_html}
{who_html}
{budget_html}
{reco_html}
<section>
<h2>Häufige Fragen</h2>
<dl>{faq_items}</dl>
</section>
<p class="cta">
<a class="button" href="{_esc(cta_url)}" rel="sponsored nofollow">Zum Händler &rarr;</a><br>
<span class="disclosure">{_esc(DISCLOSURE_TEXT)}</span>
</p>
{related_html}
</article>"""

    page = page_shell(title=title_text, description=description_text, body_html=body_html)

    word_count = len(re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", body_html))
    checks = {
        "word_count": word_count,
        "meets_min_words": word_count >= _MIN_WORDS,
        "has_disclosure": DISCLOSURE_TEXT in page,
        # the href attribute renders the HTML-ESCAPED url (e.g. "&" ->
        # "&amp;") - a cta_url containing such a character would otherwise
        # never match a plain substring check against the rendered page.
        # `cta_url == ""` deliberately still passes (unchanged from
        # before this fix) - build_asset() calls this BEFORE the real
        # link/cta_url exists, purely to quality-gate everything else;
        # deploy_asset() re-renders with the real cta_url and is the
        # actual, authoritative check of it.
        "has_cta": cta_url in page or _esc(cta_url) in page,
        "has_evidence": bool(offer.evidence),
        "has_demand_quote": need_quote != "",
    }
    return page, checks


def check_quality(checks: dict) -> tuple[bool, list]:
    """Fail closed: every gate must pass. Returns (ok, reasons_if_not)."""
    reasons = []
    if not checks.get("meets_min_words"):
        reasons.append(f"only {checks.get('word_count', 0)} words "
                       f"(< {_MIN_WORDS} minimum) - too thin to publish")
    if not checks.get("has_disclosure"):
        reasons.append("affiliate disclosure text missing")
    if not checks.get("has_cta"):
        reasons.append("call-to-action link missing")
    if not checks.get("has_evidence"):
        reasons.append("no program evidence to back the product claims")
    return (len(reasons) == 0, reasons)


def build_asset(data_dir, *, opportunity_id: str, draft: OpportunityDraft,
                match: AffiliateMatch, cta_url: str, now_iso: str = "",
                guide_title: str = "", related_links: tuple = ()) -> tuple[AffiliateAsset, bool, list]:
    """Render + quality-gate one asset and persist its record (NOT yet
    deployed - `deploy_asset()` is the separate, explicit publish step, so
    a failed quality gate never reaches deployment). Idempotent per
    (opportunity_id, offer_id): re-running returns the existing asset
    record rather than minting a duplicate."""
    store = AffiliateAssetStore.load(data_dir)
    for existing in store.by_opportunity(opportunity_id):
        if existing.offer_id == match.offer.offer_id:
            checks = existing.quality_checks
            ok, reasons = check_quality(checks)
            return existing, ok, reasons

    page, checks = render_comparison_page(draft=draft, match=match, cta_url=cta_url,
                                          guide_title=guide_title, related_links=related_links)
    ok, reasons = check_quality(checks)
    slug = _slugify(f"{draft.title}-{match.offer.product_name}")
    asset = AffiliateAsset(
        asset_id=new_id("asset"), opportunity_id=opportunity_id,
        offer_id=match.offer.offer_id, asset_type="comparison_page",
        title=draft.title[:200], guide_title=guide_title, slug=slug, file_path="index.html",
        disclosure_included=checks["has_disclosure"], quality_checks=checks,
        related_links=tuple(related_links), created_at=now_iso)
    store.upsert(asset)
    store.save()
    # the rendered page itself is not persisted to JSON (large, derivable) -
    # deploy_asset() re-renders from the same inputs, byte-for-byte, right
    # before publishing.
    return asset, ok, reasons


def deploy_asset(*, asset: AffiliateAsset, draft: OpportunityDraft,
                 match: AffiliateMatch, cta_url: str, adapter=None) -> dict:
    """Publish the asset via the EXISTING, real deployment adapter (spec:
    reuse, no new deploy mechanism). Refuses to deploy anything that does
    not pass `check_quality()` right now, even if it did when built (an
    offer's evidence can change between build and deploy). `adapter=` lets
    a test/caller inject `deployment.FakeDeploymentAdapter()`; the default
    is the real, credential-gated GitHub Pages adapter."""
    page, checks = render_comparison_page(draft=draft, match=match, cta_url=cta_url,
                                          guide_title=asset.guide_title,
                                          related_links=asset.related_links)
    ok, reasons = check_quality(checks)
    if not ok:
        return {"deployed": False, "blocked": True, "reasons": reasons}

    artifact = DeploymentArtifact(opportunity_id=asset.opportunity_id,
                                  slug=asset.slug, files={"index.html": page})
    result = (adapter or default_deployment_adapter()).deploy(artifact)
    return {"deployed": result.success, "blocked": result.blocked,
           "live_url": result.live_url, "error": result.error,
           "provider": result.provider, "reasons": [] if result.success else [result.error]}
