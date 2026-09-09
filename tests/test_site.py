"""AI Revenue - customer-facing website shell (Phase: Website Redesign).

Covers: homepage built purely from real, persisted data (never a
hardcoded product), category taxonomy mapping (with a safe "Sonstiges"
fallback for anything unmapped), category pages that always exist (never
a 404 from the nav/category grid) with an honest empty state, legal pages
(Impressum honestly flags missing mandatory fields rather than inventing
an address; Datenschutz states the real, current no-tracking fact;
Affiliate-Erklärung is transparent), mobile-friendly viewport meta on
every page, no fake stars/reviews/testimonials/customer counts anywhere
in the shell, `page_shell()` title de-duplication, and
`build_site_artifact()`/`deploy_site()` producing the right file layout
at the site root (not a guide's own subfolder).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.deployment import DeploymentArtifact, FakeDeploymentAdapter
from revenue_os.ecosystem import site
from revenue_os.ecosystem.affiliate_model import (
    AffiliateAsset,
    AffiliateAssetStore,
    AffiliateOffer,
    AffiliateOfferStore,
)
from revenue_os.ecosystem.model import POLICY_OK


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _seed_real_guide(d, *, category: str, title: str, live_url: str,
                     offer_id: str = "aff-1") -> None:
    offers = AffiliateOfferStore.load(d)
    offers.upsert(AffiliateOffer(offer_id=offer_id, network="generic_saas_program",
                                 program_name="P", product_name="X", category=category,
                                 status=POLICY_OK))
    offers.save()
    assets = AffiliateAssetStore.load(d)
    assets.upsert(AffiliateAsset(asset_id=f"asset-{offer_id}", opportunity_id=f"op-{offer_id}",
                                 offer_id=offer_id, title=title, live_url=live_url))
    assets.save()


class CategoryTaxonomyTests(unittest.TestCase):
    def test_known_categories_map_correctly(self):
        cases = {
            "usb-microphone-streaming": site.CATEGORY_MIKROFONE,
            "online-business-platform": site.CATEGORY_TECHNIK,
            "wireless-earbuds": site.CATEGORY_KOPFHOERER,
            "mechanical-keyboard": site.CATEGORY_TASTATUREN,
            "gaming-mouse": site.CATEGORY_MAEUSE,
            "4k-monitor": site.CATEGORY_MONITORE,
            "gaming-headset": site.CATEGORY_GAMING,
        }
        for offer_category, expected in cases.items():
            with self.subTest(offer_category=offer_category):
                self.assertEqual(site.classify_offer_category(offer_category), expected)

    def test_unknown_category_falls_back_to_sonstiges_never_dropped(self):
        self.assertEqual(site.classify_offer_category("some-totally-new-category"),
                         site.CATEGORY_SONSTIGES)

    def test_empty_category_falls_back_to_sonstiges(self):
        self.assertEqual(site.classify_offer_category(""), site.CATEGORY_SONSTIGES)


class HomepageTests(unittest.TestCase):
    def test_empty_state_shows_no_guides_but_all_categories_navigable(self):
        d = _tmp()
        html = site.render_homepage(d)
        self.assertIn("Noch keine Kaufberatung veröffentlicht", html)
        for key, _label in site.SITE_CATEGORIES:
            self.assertIn(f"/kategorie/{key}/", html)
            self.assertIn("0 Ratgeber", html)

    def test_real_deployed_guide_appears_with_correct_category_count(self):
        d = _tmp()
        _seed_real_guide(d, category="usb-microphone-streaming",
                         title="Bestes USB-Mikrofon für Streaming",
                         live_url="https://example.test/mikrofon-guide/")
        html = site.render_homepage(d)
        self.assertIn("Bestes USB-Mikrofon für Streaming", html)
        self.assertIn("https://example.test/mikrofon-guide/", html)
        self.assertIn(f'/kategorie/{site.CATEGORY_MIKROFONE}/">'
                      f'<span class="emoji" aria-hidden="true">\U0001F399️</span>Mikrofone'
                      f'<span class="count">1 Ratgeber',
                      html)

    def test_undeployed_asset_never_shown_on_homepage(self):
        # only assets with a real live_url are real, published guides.
        d = _tmp()
        offers = AffiliateOfferStore.load(d)
        offers.upsert(AffiliateOffer(offer_id="o1", network="generic_saas_program",
                                     program_name="P", product_name="X",
                                     category="mikrofone", status=POLICY_OK))
        offers.save()
        assets = AffiliateAssetStore.load(d)
        assets.upsert(AffiliateAsset(asset_id="a1", opportunity_id="op1", offer_id="o1",
                                     title="Not yet live", live_url=""))
        assets.save()
        html = site.render_homepage(d)
        self.assertNotIn("Not yet live", html)

    def test_has_mobile_viewport_meta(self):
        html = site.render_homepage(_tmp())
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)

    def test_has_search_box_and_hero_and_branding(self):
        html = site.render_homepage(_tmp())
        self.assertIn("Was möchtest du kaufen?", html)
        self.assertIn(site.SITE_TAGLINE, html)
        self.assertIn(">AI Revenue<", html)

    def test_footer_links_to_all_legal_pages(self):
        html = site.render_homepage(_tmp())
        for path in ("/impressum/", "/datenschutz/", "/affiliate-erklaerung/"):
            self.assertIn(f'href="{path}"', html)

    def test_no_fake_reviews_stars_or_customer_counts_anywhere(self):
        d = _tmp()
        _seed_real_guide(d, category="usb-microphone-streaming", title="Guide",
                         live_url="https://example.test/g/")
        html = site.render_homepage(d).lower()
        for forbidden in ("★", "5 sterne", "5/5", "kundenmeinung", "kundenbewertung",
                         "verifizierter kauf", "tausende kunden", "unsere kunden sagen"):
            self.assertNotIn(forbidden, html)

    def test_title_is_not_duplicated_for_the_brand_itself(self):
        html = site.render_homepage(_tmp())
        self.assertIn("<title>AI Revenue</title>", html)
        self.assertNotIn("AI Revenue – AI Revenue", html)


class CategoryPageTests(unittest.TestCase):
    def test_every_category_page_exists_even_with_no_guides(self):
        d = _tmp()
        pages = site.all_category_pages(d)
        self.assertEqual(len(pages), len(site.SITE_CATEGORIES))
        for key, _label in site.SITE_CATEGORIES:
            self.assertIn(f"kategorie/{key}/index.html", pages)
            self.assertIn("bald verfügbar", pages[f"kategorie/{key}/index.html"])

    def test_category_page_lists_only_matching_real_guides(self):
        d = _tmp()
        _seed_real_guide(d, category="usb-microphone-streaming", title="Mikrofon-Guide",
                         live_url="https://example.test/mik/", offer_id="o1")
        _seed_real_guide(d, category="gaming-mouse", title="Maus-Guide",
                         live_url="https://example.test/maus/", offer_id="o2")
        mikrofone_page = site.render_category_page(site.CATEGORY_MIKROFONE, d)
        self.assertIn("Mikrofon-Guide", mikrofone_page)
        self.assertNotIn("Maus-Guide", mikrofone_page)

    def test_category_page_has_viewport_meta(self):
        html = site.render_category_page(site.CATEGORY_GAMING, _tmp())
        self.assertIn('<meta name="viewport"', html)


class LegalPageTests(unittest.TestCase):
    def test_impressum_flags_missing_mandatory_fields_never_invents_an_address(self):
        html = site.render_impressum()
        self.assertIn("§", html)
        self.assertIn("noch nicht", html.lower())
        # never a fabricated street/city - only real, configured contact
        # info (business email) may appear.
        for fake_marker in ("Musterstraße", "Musterstadt", "GmbH & Co", "12345 "):
            self.assertNotIn(fake_marker, html)

    def test_impressum_shows_real_business_email_when_configured(self):
        import os
        old = os.environ.get("BUSINESS_EMAIL")
        os.environ["BUSINESS_EMAIL"] = "kontakt@example.test"
        try:
            html = site.render_impressum()
            self.assertIn("kontakt@example.test", html)
        finally:
            if old is None:
                os.environ.pop("BUSINESS_EMAIL", None)
            else:
                os.environ["BUSINESS_EMAIL"] = old

    def test_impressum_omits_contact_block_when_not_configured(self):
        import os
        old = os.environ.pop("BUSINESS_EMAIL", None)
        try:
            html = site.render_impressum()
            self.assertNotIn("mailto:", html)
        finally:
            if old is not None:
                os.environ["BUSINESS_EMAIL"] = old

    def test_datenschutz_states_the_real_no_tracking_fact(self):
        html = site.render_datenschutz()
        self.assertIn("keine Cookies", html)
        self.assertIn("Tracking-Skripte", html)

    def test_affiliate_erklaerung_is_transparent_and_names_real_networks_only(self):
        html = site.render_affiliate_erklaerung()
        self.assertIn("Provision", html)
        self.assertIn("systeme.io", html)
        # only real, documented networks in this codebase - never a
        # network invented for this page.
        for name in ("Awin", "CJ Affiliate", "Amazon PartnerNet"):
            self.assertIn(name, html)

    def test_all_legal_pages_have_viewport_meta(self):
        for html in site.render_legal_pages().values():
            self.assertIn('<meta name="viewport"', html)


class PageShellTests(unittest.TestCase):
    def test_non_brand_title_gets_brand_suffix(self):
        html = site.page_shell(title="Mikrofone", description="x", body_html="<p>x</p>")
        self.assertIn("<title>Mikrofone – AI Revenue</title>", html)

    def test_title_is_escaped(self):
        html = site.page_shell(title='<script>alert(1)</script>', description="x", body_html="<p>x</p>")
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_lang_is_german(self):
        html = site.page_shell(title="x", description="x", body_html="<p>x</p>")
        self.assertIn('<html lang="de">', html)


class BuildAndDeploySiteTests(unittest.TestCase):
    def test_build_site_artifact_has_the_right_file_layout(self):
        d = _tmp()
        artifact = site.build_site_artifact(d)
        self.assertIsInstance(artifact, DeploymentArtifact)
        self.assertEqual(artifact.slug, "")
        self.assertIn("index.html", artifact.files)
        self.assertIn("impressum/index.html", artifact.files)
        self.assertIn("datenschutz/index.html", artifact.files)
        self.assertIn("affiliate-erklaerung/index.html", artifact.files)
        for key, _label in site.SITE_CATEGORIES:
            self.assertIn(f"kategorie/{key}/index.html", artifact.files)
        self.assertIn("robots.txt", artifact.files)

    def test_deploy_site_uses_the_injected_adapter_and_lands_at_root(self):
        d = _tmp()
        out = site.deploy_site(d, adapter=FakeDeploymentAdapter())
        self.assertTrue(out["deployed"])
        self.assertEqual(out["live_url"], "https://fake.pages.test/index.html")

    def test_cli_deploy_site_runs(self):
        from unittest import mock

        from revenue_os.cli import main
        d = _tmp()
        with mock.patch("revenue_os.ecosystem.site.default_deployment_adapter",
                       return_value=FakeDeploymentAdapter()):
            rc = main(["--data-dir", str(d), "deploy-site"])
        self.assertEqual(rc, 0)

    def test_deploy_site_reflects_a_real_guide_once_seeded(self):
        d = _tmp()
        _seed_real_guide(d, category="gaming-mouse", title="Beste Gaming-Maus unter 50€",
                         live_url="https://example.test/maus-guide/")
        artifact = site.build_site_artifact(d)
        self.assertIn("Beste Gaming-Maus unter 50€", artifact.files["index.html"])
        self.assertIn("Beste Gaming-Maus unter 50€",
                      artifact.files[f"kategorie/{site.CATEGORY_MAEUSE}/index.html"])


class SitemapAndRobotsTests(unittest.TestCase):
    _REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "someone/site"}

    def test_robots_always_present_even_without_config(self):
        txt = site.render_robots_txt(environ={})
        self.assertIn("User-agent: *", txt)
        self.assertIn("Allow: /", txt)
        self.assertNotIn("Sitemap:", txt)   # no real base URL to point at

    def test_robots_references_sitemap_when_config_is_real(self):
        txt = site.render_robots_txt(environ=self._REAL_ENV)
        self.assertIn("Sitemap: https://someone.github.io/site/sitemap.xml", txt)

    def test_sitemap_is_none_without_real_config_never_fabricates_a_domain(self):
        self.assertIsNone(site.render_sitemap_xml(_tmp(), environ={}))

    def test_sitemap_lists_real_pages_with_absolute_urls(self):
        d = _tmp()
        xml = site.render_sitemap_xml(d, environ=self._REAL_ENV)
        self.assertIsNotNone(xml)
        self.assertIn("<loc>https://someone.github.io/site/</loc>", xml)
        self.assertIn("<loc>https://someone.github.io/site/impressum/</loc>", xml)
        for key, _label in site.SITE_CATEGORIES:
            self.assertIn(f"<loc>https://someone.github.io/site/kategorie/{key}/</loc>", xml)

    def test_sitemap_includes_real_deployed_guides_not_undeployed_ones(self):
        d = _tmp()
        _seed_real_guide(d, category="usb-microphone-streaming", title="Mikrofon-Guide",
                         live_url="https://divdav12.github.io/AI-Revenue-OS/mik/")
        xml = site.render_sitemap_xml(d, environ=self._REAL_ENV)
        self.assertIn("<loc>https://divdav12.github.io/AI-Revenue-OS/mik/</loc>", xml)

    def test_build_site_artifact_omits_sitemap_without_real_config(self):
        import os
        from unittest import mock

        d = _tmp()
        with mock.patch.dict(os.environ, {}, clear=True):
            artifact = site.build_site_artifact(d)
        self.assertNotIn("sitemap.xml", artifact.files)
        self.assertIn("robots.txt", artifact.files)


class InternalNavigationBasePathTests(unittest.TestCase):
    """Regression guard for a real bug found live: a GitHub Pages PROJECT
    site (served at https://owner.github.io/<repo>/, not the domain root)
    needs every INTERNAL nav link prefixed with that repo subpath - a
    plain root-relative "/kategorie/..." link 404s there. Reuses the
    SAME real, already-configured base `_real_base_url()` the sitemap
    already computes - never a second, independently-guessed domain."""
    _REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
    _BASE = "https://DivDav12.github.io/AI-Revenue-OS"

    def test_homepage_nav_links_are_prefixed_under_a_real_project_deploy(self):
        html = site.render_homepage(_tmp(), environ=self._REAL_ENV)
        self.assertIn(f'href="{self._BASE}/"', html)
        self.assertIn(f'href="{self._BASE}/impressum/"', html)
        self.assertIn(f'href="{self._BASE}/datenschutz/"', html)
        self.assertIn(f'href="{self._BASE}/affiliate-erklaerung/"', html)
        for key, _label in site.SITE_CATEGORIES:
            self.assertIn(f'href="{self._BASE}/kategorie/{key}/"', html)
        # never a bare, unprefixed internal link once a real base resolves
        self.assertNotIn('href="/kategorie/', html)
        self.assertNotIn('href="/impressum/"', html)

    def test_homepage_nav_links_stay_root_relative_without_real_config(self):
        # unchanged behaviour - a custom domain served at the root, or no
        # deploy config yet (e.g. every other existing test in this file).
        html = site.render_homepage(_tmp())
        self.assertIn('href="/"', html)
        self.assertIn('href="/impressum/"', html)
        self.assertIn('href="/kategorie/mikrofone/"', html)

    def test_category_page_nav_and_breadcrumb_prefixed_under_real_deploy(self):
        html = site.render_category_page(site.CATEGORY_MIKROFONE, _tmp(), environ=self._REAL_ENV)
        self.assertIn(f'href="{self._BASE}/"', html)
        self.assertIn(f'href="{self._BASE}/kategorie/kopfhoerer-earbuds/"', html)
        self.assertNotIn('href="/"', html)

    def test_category_breadcrumb_path_is_prefixed_under_real_deploy(self):
        label, path = site.category_breadcrumb("usb-microphone-streaming", self._REAL_ENV)
        self.assertEqual(path, f"{self._BASE}/kategorie/{site.CATEGORY_MIKROFONE}/")

    def test_category_breadcrumb_path_stays_root_relative_without_real_config(self):
        label, path = site.category_breadcrumb("usb-microphone-streaming")
        self.assertEqual(path, f"/kategorie/{site.CATEGORY_MIKROFONE}/")

    def test_build_site_artifact_has_no_dangling_internal_link_under_real_deploy(self):
        # a full crawl of every generated internal href: none may be a
        # bare root-relative path once a real base is configured - it
        # would 404 on a GitHub Pages PROJECT site.
        import os
        import re
        from unittest import mock

        d = _tmp()
        with mock.patch.dict(os.environ, self._REAL_ENV, clear=True):
            artifact = site.build_site_artifact(d)
        for path, html in artifact.files.items():
            if not html.strip().startswith("<!doctype") and "<a " not in html:
                continue
            for href in re.findall(r'href="([^"]*)"', html):
                if href.startswith("#") or href.startswith("mailto:"):
                    continue
                self.assertTrue(href.startswith(self._BASE) or href.startswith("https://"),
                               f"dangling unprefixed internal link {href!r} in {path}")


if __name__ == "__main__":
    unittest.main()
