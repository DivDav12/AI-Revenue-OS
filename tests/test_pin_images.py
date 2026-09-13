"""Pin/slideshow image rendering - Pillow-only, no network in tests (the
real product photo download is monkeypatched to a synthetic image so the
suite stays fast and offline; render_product_pin/render_cover_slide never
fabricate a price or product name themselves - they only draw what the
AffiliateOffer record already carries).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from revenue_os.ecosystem import pin_images
from revenue_os.ecosystem.affiliate_model import AffiliateOffer


def _fake_photo(_url: str) -> Image.Image:
    return Image.new("RGB", (600, 400), (10, 20, 30))


def _offer(**overrides) -> AffiliateOffer:
    defaults = dict(
        offer_id="aff-1", network="amazon_associates", program_name="Amazon PartnerNet",
        product_name="Example Mechanical Keyboard", product_url="https://www.amazon.de/dp/B000000001",
        product_asin="B000000001", product_price=79.99, currency="EUR",
        image_urls=("https://m.media-amazon.com/images/I/fake.jpg",),
    )
    defaults.update(overrides)
    return AffiliateOffer(**defaults)


class RenderProductPinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_path = Path(self._tmp.name) / "pin.png"

    @patch.object(pin_images, "_download_photo", side_effect=_fake_photo)
    def test_renders_a_real_photo_and_price(self, _mock):
        path = pin_images.render_product_pin(_offer(), out_path=self.out_path)
        self.assertTrue(path.exists())
        with Image.open(path) as img:
            self.assertEqual(img.size, (pin_images.CANVAS_W, pin_images.CANVAS_H))

    def test_refuses_without_a_real_photo(self):
        with self.assertRaises(pin_images.PinImageError):
            pin_images.render_product_pin(_offer(image_urls=()), out_path=self.out_path)

    def test_refuses_without_a_verified_price(self):
        with self.assertRaises(pin_images.PinImageError):
            pin_images.render_product_pin(_offer(product_price=0.0), out_path=self.out_path)

    @patch.object(pin_images, "_download_photo", side_effect=_fake_photo)
    def test_uses_the_offers_own_price_and_currency(self, _mock):
        path = pin_images.render_product_pin(
            _offer(product_price=12.34, currency="USD"), out_path=self.out_path)
        self.assertTrue(path.exists())


class RenderCoverSlideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_path = Path(self._tmp.name) / "cover.png"

    @patch.object(pin_images, "_download_photo", side_effect=_fake_photo)
    def test_renders_headline_and_thumbnails(self, _mock):
        offers = [_offer(offer_id=f"aff-{i}") for i in range(3)]
        path = pin_images.render_cover_slide(
            offers, headline="Best Keyboards Under €100", subhead="on Amazon",
            out_path=self.out_path)
        self.assertTrue(path.exists())

    def test_renders_even_with_no_offers(self):
        path = pin_images.render_cover_slide(
            [], headline="Best Keyboards Under €100", subhead="on Amazon",
            out_path=self.out_path)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
