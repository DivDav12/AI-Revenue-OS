"""Regression guards for the legal / privacy / accessibility hardening pass
(ported from branch feat/social-affiliate-distribution onto the current
dark-theme + base-path-fixed site).

Covers only the behaviour that pass ADDED or CHANGED:

* every page carries a `<main id="content">` landmark + a skip link
* internal links get the real, already-configured deploy base when - and
  only when - a real deploy target is configured, so Impressum/Datenschutz
  are actually reachable on the deployed GitHub *project* Pages site
* the Amazon participant-identification disclosure (English wording) appears
  on (and only on) pages that link to an Amazon program
* no cookie / consent banner is added (there is nothing to consent to)
* the § 5 DDG reference replaced the repealed § 5 TMG
* every legal page flags that it is not individual legal advice
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revenue_os.ecosystem import affiliate_assets, site
from revenue_os.ecosystem.affiliate_matching import AffiliateMatch
from revenue_os.ecosystem.affiliate_model import AffiliateOffer
from revenue_os.ecosystem.model import POLICY_OK, OpportunityDraft, SourceMeta

_REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
_BASE = "https://DivDav12.github.io/AI-Revenue-OS"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _draft() -> OpportunityDraft:
    return OpportunityDraft(
        title="Looking for a budget microphone", description="",
        opportunity_type="affiliate_product",
        evidence=["Looking for a budget microphone"],
        source_meta=SourceMeta(source="t", source_type="demand_forum"),
        category="mikrofone")


def _amazon_match() -> AffiliateMatch:
    offer = AffiliateOffer(
        offer_id="o", network="amazon_associates", program_name="Amazon PartnerNet",
        product_name="Amazon Basics Mini-USB-Kondensatormikrofon", product_price=26.24,
        currency="EUR", category="mikrofone",
        evidence=("Amazon.de product page: plug and play",), status=POLICY_OK,
        tracking_param="tag", tracking_value="airevenue-21")
    return AffiliateMatch(offer=offer, match_score=1.0, demand_strength=0.5)


def _systeme_match() -> AffiliateMatch:
    offer = AffiliateOffer(
        offer_id="s", network="systeme_io", program_name="systeme.io Affiliate",
        product_name="systeme.io", product_price=17.0, currency="USD",
        category="online-business-platform",
        evidence=("systeme.io homepage: the only tool you need",), status=POLICY_OK)
    return AffiliateMatch(offer=offer, match_score=1.0, demand_strength=0.5)


class LandmarkAndSkipLinkTests(unittest.TestCase):
    def test_shell_has_main_landmark_and_skip_link(self):
        html = site.page_shell(title="x", description="x", body_html="<p>x</p>")
        self.assertIn('<a class="skip-link" href="#content">', html)
        self.assertIn('<main id="content">', html)

    def test_homepage_and_guide_have_the_landmark(self):
        self.assertIn('<main id="content">', site.render_homepage(_tmp()))
        page, _ = affiliate_assets.render_comparison_page(
            draft=_draft(), match=_amazon_match(), cta_url="https://x.test/go")
        self.assertIn('<main id="content">', page)


class BasePathTests(unittest.TestCase):
    def test_no_config_keeps_links_root_relative(self):
        self.assertEqual(site.site_base_path(environ={}), "")
        html = site.render_homepage(_tmp())
        self.assertIn('href="/impressum/"', html)

    def test_real_config_prefixes_every_internal_link(self):
        self.assertEqual(site.site_base_path(environ=_REAL_ENV), "/AI-Revenue-OS")
        with mock.patch.dict(os.environ, _REAL_ENV, clear=True):
            html = site.render_homepage(_tmp())
        self.assertIn(f'href="{_BASE}/impressum/"', html)
        self.assertIn(f'href="{_BASE}/datenschutz/"', html)
        self.assertIn(f'href="{_BASE}/kategorie/mikrofone/"', html)
        self.assertNotIn('href="/impressum/"', html)   # the broken form is gone

    def test_guide_breadcrumb_is_prefixed_on_the_deployed_site(self):
        with mock.patch.dict(os.environ, _REAL_ENV, clear=True):
            page, _ = affiliate_assets.render_comparison_page(
                draft=_draft(), match=_amazon_match(), cta_url="https://x.test/go")
        self.assertIn(f'href="{_BASE}/"', page)
        self.assertIn(f'href="{_BASE}/kategorie/mikrofone/"', page)


class AmazonDisclosureTests(unittest.TestCase):
    _AMZ = "As an Amazon Associate I earn from qualifying purchases."

    def test_amazon_page_carries_the_required_participation_statement(self):
        page, _ = affiliate_assets.render_comparison_page(
            draft=_draft(), match=_amazon_match(), cta_url="https://x.test/go")
        self.assertIn(self._AMZ, page)
        # near the top AND by the CTA (and in the FAQ)
        self.assertGreaterEqual(page.count(self._AMZ), 2)

    def test_non_amazon_page_does_not_carry_the_amazon_statement(self):
        page, _ = affiliate_assets.render_comparison_page(
            draft=_draft(), match=_systeme_match(), cta_url="https://x.test/go")
        self.assertNotIn(self._AMZ, page)
        self.assertIn(affiliate_assets.DISCLOSURE_TEXT, page)

    def test_both_pages_carry_the_generic_advertising_label(self):
        for m in (_amazon_match(), _systeme_match()):
            page, _ = affiliate_assets.render_comparison_page(
                draft=_draft(), match=m, cta_url="https://x.test/go")
            self.assertIn("Advertisement / affiliate link.", page)


class NoConsentBannerTests(unittest.TestCase):
    def test_no_cookie_or_consent_banner_was_added(self):
        pages = [site.render_homepage(_tmp()), site.render_datenschutz(),
                 site.render_impressum(), site.render_affiliate_erklaerung()]
        for html in pages:
            low = html.lower()
            for marker in ("cookiebanner", "cookiebot", "cookieconsent",
                           "usercentrics", "borlabs", "onetrust", "klaro"):
                self.assertNotIn(marker, low)

    def test_datenschutz_states_no_consent_needed_and_no_own_cookies(self):
        html = site.render_datenschutz()
        self.assertIn("no cookies", html)
        self.assertIn("consent banner is therefore", html)   # ...not required

    def test_impressum_cites_ddg_not_the_repealed_tmg(self):
        html = site.render_impressum()
        self.assertIn("Digitale-Dienste-Gesetz", html)
        self.assertNotIn("TMG", html)

    def test_affiliate_page_carries_exact_amazon_wording(self):
        html = site.render_affiliate_erklaerung()
        self.assertIn(
            "As an Amazon Associate I earn from qualifying purchases.", html)
        self.assertIn("airevenue-21", html)


class LegalReviewNoteTests(unittest.TestCase):
    def test_every_legal_page_flags_it_is_not_legal_advice(self):
        for html in site.render_legal_pages().values():
            self.assertIn("not a substitute for individual legal advice", html)

    def test_no_generated_page_has_a_dangling_internal_link_under_real_deploy(self):
        with mock.patch.dict(os.environ, _REAL_ENV, clear=True):
            artifact = site.build_site_artifact(_tmp())
        for path, html in artifact.files.items():
            if "<a " not in html:
                continue
            for href in re.findall(r'href="([^"]*)"', html):
                if href.startswith(("#", "mailto:", "https://")):
                    continue
                self.fail(f"dangling unprefixed internal link {href!r} in {path}")


if __name__ == "__main__":
    unittest.main()
