"""Product images (spec: "Use real Amazon product imagery ... compliant
with Amazon Associates / Amazon content rules").

No image is ever fetched, scraped or guessed. `AffiliateOffer.image_urls`
is a human-populated field; this suite proves the gate on that field:

* a valid Amazon-CDN image URL renders on cards and the product page
* a missing image renders the accessible icon fallback
* an invalid / placeholder / third-party (Google, imgur, ...) URL is
  rejected and never reaches a product page
* each product only ever shows its OWN offer's image - no cross-ASIN leak
* the affiliate URL, tag=airevenue-21, rel="sponsored nofollow" and the
  Amazon disclosure are all unchanged by adding an image
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import products, site
from revenue_os.ecosystem.affiliate_model import (
    AffiliateAssetStore,
    AffiliateOffer,
    AffiliateOfferStore,
)
from revenue_os.ecosystem.model import POLICY_OK

_REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
_BASE = "https://DivDav12.github.io/AI-Revenue-OS"

# a real-shaped Amazon media-CDN image URL (the shape PA-API returns)
_GOOD = "https://m.media-amazon.com/images/I/71AbCdEf12._AC_SL1500_.jpg"
_GOOD2 = "https://images-na.ssl-images-amazon.com/images/I/61XyZ98._AC_SX300_SY300_.png"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _amz(offer_id="aff-amz-1", asin="B0CL9BTQRF", name="Amazon Basics Mini USB Microphone",
         image_urls=(), **kw) -> AffiliateOffer:
    d = dict(
        offer_id=offer_id, network="amazon_associates", program_name="Amazon PartnerNet",
        product_name=name,
        product_url=f"https://www.amazon.de/x/dp/{asin}/", product_asin=asin,
        product_price=26.24, currency="EUR", price_is_estimate=False, category="mikrofone",
        keywords=("usb-mikrofon", "budget microphone"),
        evidence=("Amazon.de product page: plug and play.",), status=POLICY_OK,
        tracking_param="tag", tracking_value="airevenue-21", image_urls=tuple(image_urls))
    d.update(kw)
    return AffiliateOffer(**d)


def _seed(d, offers):
    s = AffiliateOfferStore.load(d)
    for o in offers:
        s.upsert(o)
    s.save()
    AffiliateAssetStore.load(d).save()


class UrlComplianceTests(unittest.TestCase):
    def test_accepts_amazon_media_cdn_https_image_urls(self):
        for u in (_GOOD, _GOOD2,
                  "https://m.media-amazon.com/images/I/51abc.jpg",
                  "https://images-eu.ssl-images-amazon.com/images/I/41abc._SL1000_.webp"):
            self.assertTrue(products.compliant_amazon_image_url(u), u)

    def test_rejects_google_and_third_party_hosts(self):
        for u in ("https://lh3.googleusercontent.com/abc.jpg",
                  "https://www.google.com/imgres?imgurl=x.jpg",
                  "https://i.imgur.com/abc.jpg",
                  "https://cdn.shopify.com/x/mic.png",
                  "https://example.com/placeholder.png",
                  "https://media-amazon.com.evil.example/x.jpg"):
            self.assertFalse(products.compliant_amazon_image_url(u), u)

    def test_rejects_non_https_non_image_and_empty(self):
        for u in ("http://m.media-amazon.com/images/I/51abc.jpg",   # plain http
                  "https://m.media-amazon.com/gp/product/B0CL9BTQRF",  # not an image
                  "https://m.media-amazon.com/images/I/51abc.svg",     # not a raster type
                  "data:image/png;base64,AAAA", "", "   ", "not-a-url"):
            self.assertFalse(products.compliant_amazon_image_url(u), repr(u))

    def test_compliant_images_for_filters_and_dedupes(self):
        off = _amz(image_urls=(_GOOD, "https://evil.example/x.jpg", _GOOD, _GOOD2))
        self.assertEqual(products.compliant_images_for(off), (_GOOD, _GOOD2))

    def test_non_amazon_offer_carries_no_public_images(self):
        off = AffiliateOffer(offer_id="aff-awin", network="awin", program_name="Awin",
                             product_name="X", product_url="https://www.awin1.com/cread.php?x",
                             status=POLICY_OK, image_urls=(_GOOD,))
        self.assertEqual(products.compliant_images_for(off), ())


class RenderTests(unittest.TestCase):
    def _product(self, d, **kw):
        _seed(d, [_amz(**kw)])
        return products.load_public_products(d)[0]

    def test_missing_image_renders_accessible_fallback_on_card_and_page(self):
        d = _tmp()
        p = self._product(d)
        self.assertEqual(p.image_urls, ())
        card = site.render_product_card(p, _BASE)
        self.assertIn('role="img"', card)
        self.assertNotIn("<img", card)
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        body = page.split('<main id="content">', 1)[1]
        self.assertIn('role="img"', body)
        self.assertNotIn("<img", body)

    def test_valid_image_renders_on_card_index_and_product_page(self):
        d = _tmp()
        p = self._product(d, image_urls=(_GOOD,))
        self.assertEqual(p.image_urls, (_GOOD,))
        card = site.render_product_card(p, _BASE)
        self.assertIn(f'src="{_GOOD}"', card)
        self.assertIn('alt="Amazon Basics Mini USB Microphone product image"', card)
        self.assertIn('loading="lazy"', card)

        idx = site.render_products_index(d, environ=_REAL_ENV)
        self.assertIn(f'src="{_GOOD}"', idx)

        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn(f'src="{_GOOD}"', page)
        self.assertIn('"@type": "Product"', page)
        self.assertIn(f'"image": ["{_GOOD}"]', page)

    def test_invalid_image_url_is_dropped_and_page_falls_back(self):
        d = _tmp()
        p = self._product(d, image_urls=("https://lh3.googleusercontent.com/x.jpg",))
        self.assertEqual(p.image_urls, ())
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertNotIn("googleusercontent", page)
        self.assertIn('role="img"', page.split('<main id="content">', 1)[1])

    def test_multi_image_gallery_has_accessible_button_controls(self):
        d = _tmp()
        p = self._product(d, image_urls=(_GOOD, _GOOD2))
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn('id="pd-main-img"', page)
        self.assertIn('<button type="button" class="thumb"', page)
        self.assertIn('aria-label="Show image 1 of 2"', page)
        self.assertIn('aria-pressed="true"', page)
        self.assertIn(f'data-src="{_GOOD2}"', page)

    def test_related_product_cards_use_each_products_own_image(self):
        d = _tmp()
        _seed(d, [
            _amz(offer_id="a1", asin="B0CL9BTQRF", name="Mic One", image_urls=(_GOOD,)),
            _amz(offer_id="a2", asin="B0CQP5NL72", name="Mic Two", image_urls=(_GOOD2,))])
        p1 = products.find_product(d, products.load_public_products(d)[0].slug)
        page = site.render_product_page(p1.slug, d, environ=_REAL_ENV)
        related = page.split("You may also like", 1)[1]
        # the related card shows the OTHER product's own image, not p1's
        other_img = _GOOD2 if p1.image_urls == (_GOOD,) else _GOOD
        self.assertIn(f'src="{other_img}"', related)


class NoCrossAsinLeakTests(unittest.TestCase):
    def test_image_on_one_asin_never_appears_on_another(self):
        d = _tmp()
        _seed(d, [
            _amz(offer_id="a1", asin="B0CL9BTQRF", name="Has Image", image_urls=(_GOOD,)),
            _amz(offer_id="a2", asin="B0CQP5NL72", name="No Image")])
        by_asin = {p.asin: p for p in products.load_public_products(d)}
        self.assertEqual(by_asin["B0CL9BTQRF"].image_urls, (_GOOD,))
        self.assertEqual(by_asin["B0CQP5NL72"].image_urls, ())
        page_no_img = site.render_product_page(by_asin["B0CQP5NL72"].slug, d, environ=_REAL_ENV)
        self.assertNotIn(_GOOD, page_no_img.split("You may also like")[0])


class AffiliateInvariantsUnaffectedByImagesTests(unittest.TestCase):
    def test_adding_an_image_leaves_the_affiliate_cta_untouched(self):
        d = _tmp()
        _seed(d, [_amz(image_urls=(_GOOD,))])
        p = products.load_public_products(d)[0]
        self.assertEqual(p.outbound_url,
                         "https://www.amazon.de/x/dp/B0CL9BTQRF/?tag=airevenue-21")
        page = site.render_product_page(p.slug, d, environ=_REAL_ENV)
        self.assertIn("Find on Amazon", page)
        self.assertIn("tag=airevenue-21", page)
        self.assertIn('rel="sponsored nofollow"', page)
        self.assertIn("As an Amazon Associate I earn from qualifying purchases.", page)
        self.assertIn("Advertisement / affiliate link.", page)


class ModelBackCompatTests(unittest.TestCase):
    def test_image_urls_round_trips_and_defaults_empty(self):
        off = _amz(image_urls=(_GOOD,))
        self.assertEqual(AffiliateOffer.from_dict(off.to_dict()).image_urls, (_GOOD,))
        self.assertEqual(AffiliateOffer.from_dict({"offer_id": "x", "network": "amazon_associates",
                                                  "program_name": "p", "product_name": "n"}
                                                 ).image_urls, ())


if __name__ == "__main__":
    unittest.main()
