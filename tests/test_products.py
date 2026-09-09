"""Product-first public catalog (spec: "product discovery platform").

Covers: which offers become public products (usable-only, systeme.io
excluded, Amazon must have a verified product-specific destination),
ASIN dedupe, deterministic collision-safe slugs, product card / product
page rendering (image/name/price/tags, missing price + missing image
handled safely), the "Find on Amazon" CTA (correct product, airevenue-21
preserved, rel="sponsored nofollow"), rejection of generic/placeholder
Amazon URLs, category->product and product->guide navigation, SEO
(canonical title, meta description, breadcrumb, sitemap, no duplicate
product URLs), the Amazon Associates disclosure on Amazon product pages,
and that no public surface mentions systeme.io.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import products, site
from revenue_os.ecosystem.affiliate_model import (
    AffiliateAsset,
    AffiliateAssetStore,
    AffiliateLink,
    AffiliateLinkStore,
    AffiliateOffer,
    AffiliateOfferStore,
)
from revenue_os.ecosystem.model import POLICY_HUMAN_SETUP_REQUIRED, POLICY_OK

_REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
_BASE = "https://DivDav12.github.io/AI-Revenue-OS"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _amazon_offer(**kw) -> AffiliateOffer:
    d = dict(
        offer_id="aff-amz-1", network="amazon_associates", program_name="Amazon PartnerNet",
        product_name="Amazon Basics Mini USB Microphone",
        product_url="https://www.amazon.de/Amazon-Basics-Mini/dp/B0CL9BTQRF/",
        product_asin="B0CL9BTQRF", product_price=26.24, currency="EUR",
        price_is_estimate=False, category="mikrofone",
        keywords=("usb-mikrofon", "budget microphone", "podcasting"),
        evidence=("Amazon.de product page: plug and play condenser mic.",),
        status=POLICY_OK, tracking_param="tag", tracking_value="airevenue-21")
    d.update(kw)
    return AffiliateOffer(**d)


def _seed(d, offers=(), assets=(), links=()):
    os_ = AffiliateOfferStore.load(d)
    for o in offers:
        os_.upsert(o)
    os_.save()
    as_ = AffiliateAssetStore.load(d)
    for a in assets:
        as_.upsert(a)
    as_.save()
    ls_ = AffiliateLinkStore.load(d)
    for l in links:
        ls_.upsert(l)
    ls_.save()


class EligibilityTests(unittest.TestCase):
    def test_usable_amazon_offer_becomes_a_public_product(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer()])
        ps = products.load_public_products(d)
        self.assertEqual([p.product_id for p in ps], ["aff-amz-1"])
        self.assertTrue(ps[0].is_amazon)

    def test_unusable_offer_is_never_published(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer(status=POLICY_HUMAN_SETUP_REQUIRED)])
        self.assertEqual(products.load_public_products(d), [])

    def test_inactive_offer_is_never_published(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer(active=False)])
        self.assertEqual(products.load_public_products(d), [])

    def test_systeme_io_offer_is_excluded_from_public_products(self):
        d = _tmp()
        sy = AffiliateOffer(offer_id="aff-sy", network="systeme_io",
                            program_name="systeme.io", product_name="systeme.io",
                            product_url="https://systeme.io/de?sa=abc",
                            status=POLICY_OK, preserve_exact_url=True,
                            evidence=("x",), category="online-business-platform")
        _seed(d, offers=[sy, _amazon_offer()])
        ps = products.load_public_products(d)
        self.assertEqual([p.product_id for p in ps], ["aff-amz-1"])

    def test_amazon_homepage_or_search_url_is_rejected(self):
        for bad in ("https://www.amazon.de/",
                    "https://www.amazon.de/s?k=microphone",
                    "https://www.amazon.de/b?node=571860"):
            self.assertIsNone(
                products.verified_amazon_destination(
                    _amazon_offer(product_url=bad, product_asin="")))

    def test_amazon_url_with_mismatched_asin_is_rejected(self):
        self.assertIsNone(products.verified_amazon_destination(
            _amazon_offer(product_url="https://www.amazon.de/x/dp/B00WRONGXX/",
                          product_asin="B0CL9BTQRF")))

    def test_amazon_offer_without_a_valid_product_url_is_not_published(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer(product_url="https://www.amazon.de/")])
        self.assertEqual(products.load_public_products(d), [])

    def test_duplicate_asin_yields_one_canonical_product(self):
        d = _tmp()
        _seed(d, offers=[
            _amazon_offer(offer_id="a1"),
            _amazon_offer(offer_id="a2",
                          product_url="https://www.amazon.de/other/dp/B0CL9BTQRF")])
        ps = products.load_public_products(d)
        self.assertEqual(len(ps), 1)


class SlugTests(unittest.TestCase):
    def test_slug_is_deterministic_and_english_from_the_name(self):
        self.assertEqual(products.product_slug(_amazon_offer()),
                         "amazon-basics-mini-usb-microphone")

    def test_colliding_names_get_distinct_slugs(self):
        d = _tmp()
        _seed(d, offers=[
            _amazon_offer(offer_id="a1", product_asin="B0CL9BTQRF",
                          product_url="https://www.amazon.de/x/dp/B0CL9BTQRF"),
            _amazon_offer(offer_id="a2", product_asin="B0CQP5NL72",
                          product_url="https://www.amazon.de/y/dp/B0CQP5NL72")])
        slugs = [p.slug for p in products.load_public_products(d)]
        self.assertEqual(len(slugs), len(set(slugs)))


class CardAndPageRenderTests(unittest.TestCase):
    def _one(self, d, **kw):
        _seed(d, offers=[_amazon_offer(**kw)])
        return products.load_public_products(d)[0]

    def test_product_card_shows_name_price_tags_and_internal_link(self):
        d = _tmp()
        p = self._one(d)
        card = site.render_product_card(p, _BASE)
        self.assertIn("Amazon Basics Mini USB Microphone", card)
        self.assertIn("EUR 26.24", card)
        self.assertIn("Budget", card)
        self.assertIn(f'href="{_BASE}/product/{p.slug}/"', card)
        self.assertIn("View product", card)
        # a card never carries the outbound Amazon button
        self.assertNotIn("sponsored nofollow", card)

    def test_missing_price_is_handled_without_a_fake_number(self):
        d = _tmp()
        p = self._one(d, product_price=0.0, price_is_estimate=True)
        card = site.render_product_card(p, _BASE)
        self.assertIn("See current price", card)
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn("See current price", page)
        self.assertNotIn("EUR 0.00", page)

    def test_missing_image_renders_a_placeholder_not_a_fabricated_image(self):
        d = _tmp()
        p = self._one(d)
        self.assertEqual(p.image_urls, ())
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        body = page.split('<main id="content">', 1)[1]
        self.assertIn('role="img"', body)      # accessible placeholder
        self.assertNotIn("<img", body)         # never a fabricated image tag

    def test_compliant_image_is_used_when_present(self):
        d = _tmp()
        img = "https://m.media-amazon.com/images/I/abc._AC_SL1500_.jpg"
        p = self._one(d, image_urls=(img,))
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn(f'src="{img}"', page)
        self.assertIn("alt=", page)

    def test_product_page_has_find_on_amazon_cta_to_the_correct_product(self):
        d = _tmp()
        p = self._one(d)
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn("Find on Amazon", page)
        self.assertIn("B0CL9BTQRF", page)
        self.assertIn("tag=airevenue-21", page)
        self.assertIn('rel="sponsored nofollow"', page)
        # never a bare amazon homepage / search link
        self.assertNotIn('href="https://www.amazon.de/"', page)
        self.assertNotIn("/s?k=", page)

    def test_product_page_reuses_an_existing_verified_affiliate_link(self):
        d = _tmp()
        off = _amazon_offer()
        link = AffiliateLink(link_id="l1", opportunity_id="op1", asset_id="as1",
                             offer_id=off.offer_id, source="own_site", tracking_id="trk-x",
                             target_url="https://www.amazon.de/x/dp/B0CL9BTQRF/?tag=airevenue-21")
        _seed(d, offers=[off], links=[link])
        p = products.load_public_products(d)[0]
        self.assertEqual(p.outbound_url, link.target_url)
        self.assertEqual(p.tracking_id, "trk-x")

    def test_amazon_associate_disclosure_on_amazon_product_pages(self):
        d = _tmp()
        p = self._one(d)
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn("As an Amazon Associate I earn from qualifying purchases.", page)
        self.assertIn("Advertisement / affiliate link.", page)

    def test_standalone_non_amazon_program_is_not_a_public_product(self):
        # the public catalogue is an Amazon-affiliate product catalogue - a
        # standalone Awin advertiser never gets a public product page.
        d = _tmp()
        off = AffiliateOffer(
            offer_id="aff-awin", network="awin", program_name="Awin advertiser",
            product_name="Wondershare PDFelement",
            product_url="https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&ued=https%3A%2F%2Fx",
            preserve_exact_url=True, status=POLICY_OK, category="pdf-editor",
            keywords=("pdf editor",), evidence=("Wondershare page: edit PDF like Word.",))
        _seed(d, offers=[off, _amazon_offer()])
        ps = products.load_public_products(d)
        self.assertEqual([p.product_id for p in ps], ["aff-amz-1"])


class NavigationTests(unittest.TestCase):
    def _seed_full(self, d):
        off = _amazon_offer()
        asset = AffiliateAsset(asset_id="as1", opportunity_id="op1", offer_id=off.offer_id,
                               title="Looking for a budget microphone",
                               guide_title="Budget USB microphone: what to look for",
                               slug="looking-for-a-budget-microphone",
                               live_url=f"{_BASE}/looking-for-a-budget-microphone/index.html")
        _seed(d, offers=[off], assets=[asset])
        return off, asset

    def test_category_page_lists_the_product_first_then_guides(self):
        d = _tmp()
        _off, asset = self._seed_full(d)
        page = site.render_category_page(site.CATEGORY_MIKROFONE, d, environ=_REAL_ENV)
        body = page.split('<main id="content">', 1)[1]
        p = products.load_public_products(d)[0]
        self.assertIn(f'href="{_BASE}/product/{p.slug}/"', page)
        self.assertLess(body.index("product-card"), body.index("guide-card"))
        self.assertIn(asset.live_url, page)

    def test_product_page_links_to_its_related_guides(self):
        d = _tmp()
        _off, asset = self._seed_full(d)
        p = products.load_public_products(d)[0]
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn(asset.live_url, page)
        self.assertIn("Looking for a budget microphone?", page)  # grounded context question

    def test_guide_page_links_back_to_the_internal_product_page(self):
        from revenue_os.ecosystem import affiliate_assets
        from revenue_os.ecosystem.affiliate_matching import AffiliateMatch
        from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta

        draft = OpportunityDraft(title="Looking for a budget microphone", description="",
                                 opportunity_type="affiliate_product",
                                 evidence=["Looking for a budget microphone"],
                                 source_meta=SourceMeta(source="t", source_type="demand_forum"),
                                 category="mikrofone")
        match = AffiliateMatch(offer=_amazon_offer(), match_score=1.0, demand_strength=0.5)
        page, _ = affiliate_assets.render_comparison_page(
            draft=draft, match=match, cta_url="https://www.amazon.de/x/dp/B0CL9BTQRF/?tag=airevenue-21",
            environ=_REAL_ENV)
        self.assertIn(f"{_BASE}/product/amazon-basics-mini-usb-microphone/", page)

    def test_homepage_features_products_and_links_to_the_catalog(self):
        d = _tmp()
        self._seed_full(d)
        html = site.render_homepage(d, environ=_REAL_ENV)
        self.assertIn(f'href="{_BASE}/product/"', html)
        self.assertIn("product-card", html)


class SeoTests(unittest.TestCase):
    def test_product_page_title_and_meta_description(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer()])
        p = products.load_public_products(d)[0]
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn("<title>Amazon Basics Mini USB Microphone – AI Revenue</title>", page)
        self.assertRegex(page, r'<meta name="description" content="[^"]+"')
        self.assertIn('"@type": "Product"', page)
        self.assertIn('"@type": "BreadcrumbList"', page)
        # no fabricated structured data
        for forbidden in ("aggregateRating", '"@type": "Review"', "ratingValue",
                          '"offers"', '"sku"'):
            self.assertNotIn(forbidden, page)

    def test_sitemap_includes_product_index_and_each_product_no_duplicates(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer()])
        xml = site.render_sitemap_xml(d, environ=_REAL_ENV)
        p = products.load_public_products(d)[0]
        self.assertIn(f"<loc>{_BASE}/product/</loc>", xml)
        self.assertIn(f"<loc>{_BASE}/product/{p.slug}/</loc>", xml)
        locs = re.findall(r"<loc>([^<]+)</loc>", xml)
        self.assertEqual(len(locs), len(set(locs)))

    def test_build_artifact_generates_one_page_per_product_and_no_thin_pages(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer()])
        art = site.build_site_artifact(d)
        p = products.load_public_products(d)[0]
        self.assertIn("product/index.html", art.files)
        self.assertIn(f"product/{p.slug}/index.html", art.files)

    def test_no_generated_page_mentions_systeme_io(self):
        d = _tmp()
        sy = AffiliateOffer(offer_id="aff-sy", network="systeme_io", program_name="systeme.io",
                            product_name="systeme.io", product_url="https://systeme.io/de?sa=abc",
                            status=POLICY_OK, preserve_exact_url=True, evidence=("x",),
                            category="online-business-platform")
        asset = AffiliateAsset(asset_id="as-sy", opportunity_id="op-sy", offer_id="aff-sy",
                               title="systeme.io guide", slug="systeme-io-guide",
                               live_url=f"{_BASE}/systeme-io-guide/index.html")
        _seed(d, offers=[sy, _amazon_offer()], assets=[asset])
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, _REAL_ENV, clear=True):
            art = site.build_site_artifact(d)
        for path, html in art.files.items():
            self.assertNotIn("systeme", str(html).lower(), f"systeme.io leaked into {path}")


class EmptyCategoryTests(unittest.TestCase):
    def test_empty_category_shows_a_clean_english_state(self):
        d = _tmp()
        _seed(d, offers=[_amazon_offer()])   # only a microphone
        page = site.render_category_page(site.CATEGORY_MONITORE, d, environ=_REAL_ENV)
        body = page.split('<main id="content">', 1)[1]
        self.assertIn("More products coming soon.", body)
        self.assertNotIn('class="product-card"', body)


if __name__ == "__main__":
    unittest.main()
