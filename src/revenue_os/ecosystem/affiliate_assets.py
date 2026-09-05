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

DISCLOSURE_TEXT = (
    "Affiliate disclosure: this page may contain affiliate links. If you "
    "buy through one, we may earn a commission at no extra cost to you. "
    "We only link to products we have researched and can back with real "
    "evidence.")

#: minimum body word count before a page is even considered publishable -
#: a hard floor against "thin/valueless mass pages" (spec section 9).
_MIN_WORDS = 120


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "affiliate-guide"


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def render_comparison_page(*, draft: OpportunityDraft, match: AffiliateMatch,
                           cta_url: str, guide_title: str = "") -> tuple[str, dict]:
    """Render one self-contained HTML "problem -> solution" page. Returns
    (html, quality_checks) - the checks are computed against the RENDERED
    content, not guessed, so `check_quality()` and the renderer can never
    silently disagree. `guide_title=` overrides the default "<product>:
    does it solve ...?" headline (e.g. for a roundup-style buying guide) -
    every other section stays evidence-grounded regardless."""
    offer = match.offer
    problem = _esc(draft.title)
    # a "people have said, in their own words" framing is only honest when
    # a REAL evidence quote exists (spec: no fabricated demand quotes) -
    # an opportunity with no evidence at all gets a neutral problem
    # statement instead, never a quote dressed up as verbatim.
    real_evidence = [e for e in (draft.evidence or []) if str(e).strip()]
    has_real_quote = bool(real_evidence)
    need_quote = _esc(real_evidence[0]) if has_real_quote else _esc(draft.title)
    product = _esc(offer.product_name)
    program = _esc(offer.program_name)
    price_line = (f"Listed price: {_esc(offer.currency)} {offer.product_price:.2f}"
                 f"{' (estimated - not source-confirmed)' if offer.price_is_estimate else ''}"
                 if offer.product_price > 0 else "Price: see the offer page (not stated here).")
    evidence_items = "".join(f"<li>{_esc(e)}</li>" for e in offer.evidence) or (
        "<li>No additional program evidence was supplied.</li>")

    faq_items = (
        f"<dt>Is this sponsored?</dt><dd>{_esc(DISCLOSURE_TEXT)}</dd>"
        f"<dt>What problem does this solve?</dt><dd>{need_quote}</dd>"
    )
    problem_statement = (
        f'People looking for a solution have said, in their own words: &quot;{need_quote}&quot;'
        if has_real_quote else
        f"This is a common need: {need_quote}"
    )
    headline = _esc(guide_title) if guide_title else f"{product}: does it solve &quot;{problem}&quot;?"

    # "What to look for" / "who it's for" / recommendation are generic,
    # category-level buying guidance - never a specific performance claim
    # ("great sound", a star rating, a review quote) that was not actually
    # sourced. Only rendered when the offer has real evidence to ground
    # them in (spec: no invented reviews/testimonials).
    criteria_html = pros_html = who_html = budget_html = reco_html = ""
    if offer.evidence:
        criteria_html = """<section>
<h2>What to look for</h2>
<ul>
<li>Pickup pattern and how well it isolates your voice from room/keyboard noise</li>
<li>Plug-and-play compatibility with your OS and streaming/call software</li>
<li>Physical controls (mute, gain) you can reach without opening software</li>
<li>Price relative to what you actually need it for</li>
</ul>
</section>"""
        pros_html = f"""<section>
<h2>What the program/listing states</h2>
<ul>{evidence_items}</ul>
<p class="note">These are the program's own stated facts, not a first-hand
test result - we have not independently benchmarked audio quality.</p>
</section>"""
        who_html = f"""<section>
<h2>Who this is for</h2>
<p>Someone who wants a straightforward, single-microphone setup for voice
chat, streaming, or recording - not someone who already needs a
multi-microphone studio setup or is chasing a specific, verified
audio-quality benchmark we have not tested ourselves.</p>
</section>"""
        price_ts = (f" as of {_esc(offer.price_observed_at)}" if offer.price_observed_at else "")
        price_note = f" {_esc(offer.price_source_note)}" if offer.price_source_note else ""
        budget_html = f"""<section>
<h2>Budget context</h2>
<p>The price shown above was last checked{price_ts}.{price_note} Marketplace
prices change - always check the current price on the product page before
buying.</p>
</section>"""
        reco_html = f"""<section>
<h2>Our take</h2>
<p>Based on the program's own stated facts above (not a first-hand review),
{product} is a reasonable option if those facts match what you need. We
have not tested it ourselves and are not claiming it is the objectively
"best" option - only that it is a real, currently-available product from a
program we have actually joined.</p>
</section>"""

    body_html = f"""<article>
<h1>{headline}</h1>
<p class="disclosure">{_esc(DISCLOSURE_TEXT)}</p>
<section>
<h2>The problem</h2>
<p>{problem_statement}</p>
</section>
{criteria_html}
<section>
<h2>{product} ({program})</h2>
<p>{price_line}</p>
</section>
{pros_html}
{who_html}
{budget_html}
{reco_html}
<section>
<h2>FAQ</h2>
<dl>{faq_items}</dl>
</section>
<p class="cta"><a href="{_esc(cta_url)}" rel="sponsored nofollow">Check {product} &rarr;</a></p>
</article>"""

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{headline}</title>
<meta name="description" content="{product} evaluated against a real, stated need: {need_quote}">
</head><body>
{body_html}
</body></html>"""

    word_count = len(re.findall(r"[A-Za-z0-9]+", body_html))
    checks = {
        "word_count": word_count,
        "meets_min_words": word_count >= _MIN_WORDS,
        "has_disclosure": DISCLOSURE_TEXT in page,
        "has_cta": cta_url in page,
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
                guide_title: str = "") -> tuple[AffiliateAsset, bool, list]:
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
                                          guide_title=guide_title)
    ok, reasons = check_quality(checks)
    slug = _slugify(f"{draft.title}-{match.offer.product_name}")
    asset = AffiliateAsset(
        asset_id=new_id("asset"), opportunity_id=opportunity_id,
        offer_id=match.offer.offer_id, asset_type="comparison_page",
        title=draft.title[:200], guide_title=guide_title, slug=slug, file_path="index.html",
        disclosure_included=checks["has_disclosure"], quality_checks=checks,
        created_at=now_iso)
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
                                          guide_title=asset.guide_title)
    ok, reasons = check_quality(checks)
    if not ok:
        return {"deployed": False, "blocked": True, "reasons": reasons}

    artifact = DeploymentArtifact(opportunity_id=asset.opportunity_id,
                                  slug=asset.slug, files={"index.html": page})
    result = (adapter or default_deployment_adapter()).deploy(artifact)
    return {"deployed": result.success, "blocked": result.blocked,
           "live_url": result.live_url, "error": result.error,
           "provider": result.provider, "reasons": [] if result.success else [result.error]}
