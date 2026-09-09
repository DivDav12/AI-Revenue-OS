"""Site-wide header search (spec: the search must actually return results).

The search is a static, client-side filter over `search-index.json` -
a flat list of every product, deployed buying guide, real site category
and standing page. This suite covers the index (real data only, correct
shape, no fabrication) and that the search UI + behaviour ship on every
generated page, not just the homepage.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revenue_os.ecosystem import site
from revenue_os.ecosystem.affiliate_model import (
    AffiliateAsset,
    AffiliateAssetStore,
    AffiliateOffer,
    AffiliateOfferStore,
)
from revenue_os.ecosystem.model import POLICY_OK

_REAL_ENV = {"GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "DivDav12/AI-Revenue-OS"}
_BASE = "https://DivDav12.github.io/AI-Revenue-OS"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _seed(d):
    offers = AffiliateOfferStore.load(d)
    offers.upsert(AffiliateOffer(
        offer_id="aff-amz", network="amazon_associates", program_name="Amazon PartnerNet",
        product_name="JBL Quantum Stream Talk",
        product_url="https://www.amazon.de/x/dp/B0CQP5NL72/", product_asin="B0CQP5NL72",
        product_price=39.99, currency="EUR", category="mikrofone",
        keywords=("usb-mikrofon", "streaming", "gaming"),
        evidence=("Amazon.de: USB microphone.",), status=POLICY_OK,
        tracking_param="tag", tracking_value="airevenue-21"))
    offers.save()
    assets = AffiliateAssetStore.load(d)
    assets.upsert(AffiliateAsset(
        asset_id="as1", opportunity_id="op1", offer_id="aff-amz",
        title="Looking for a budget microphone",
        guide_title="Budget USB microphone: what to look for",
        slug="budget-usb-mic", live_url=f"{_BASE}/budget-usb-mic/index.html"))
    assets.save()


class SearchIndexTests(unittest.TestCase):
    def test_index_lists_products_guides_categories_and_pages(self):
        d = _tmp()
        _seed(d)
        idx = site.search_index(d, environ=_REAL_ENV)
        kinds = {e["k"] for e in idx}
        self.assertEqual(kinds, {"Product", "Guide", "Category", "Page"})
        prod = next(e for e in idx if e["k"] == "Product")
        self.assertEqual(prod["t"], "JBL Quantum Stream Talk")
        self.assertEqual(prod["u"], f"{_BASE}/product/jbl-quantum-stream-talk/")
        self.assertIn("EUR 39.99", prod["s"])
        guide = next(e for e in idx if e["k"] == "Guide")
        self.assertEqual(guide["t"], "Budget USB microphone: what to look for")
        self.assertEqual(guide["u"], f"{_BASE}/budget-usb-mic/index.html")
        # every category is present, even empty ones
        cats = [e["t"] for e in idx if e["k"] == "Category"]
        self.assertEqual(len(cats), len(site.SITE_CATEGORIES))
        pages = {e["t"] for e in idx if e["k"] == "Page"}
        self.assertEqual(pages, {"All products", "Imprint", "Privacy Policy",
                                 "How we make money"})

    def test_index_is_compact_valid_json_with_the_right_shape(self):
        d = _tmp()
        _seed(d)
        raw = site.render_search_index_json(d, environ=_REAL_ENV)
        data = json.loads(raw)
        self.assertIsInstance(data, list)
        for e in data:
            self.assertEqual(set(e), {"t", "u", "k", "s"})
            self.assertTrue(e["u"].startswith(("http://", "https://", "/")))

    def test_index_excludes_systeme_io_and_non_amazon_products(self):
        d = _tmp()
        _seed(d)
        offers = AffiliateOfferStore.load(d)
        offers.upsert(AffiliateOffer(offer_id="aff-sy", network="systeme_io",
                                     program_name="systeme.io", product_name="systeme.io",
                                     product_url="https://systeme.io/de?sa=x",
                                     status=POLICY_OK, preserve_exact_url=True,
                                     evidence=("x",), category="online-business-platform"))
        offers.save()
        idx = site.search_index(d, environ=_REAL_ENV)
        self.assertNotIn("systeme.io", json.dumps(idx).lower())

    def test_empty_site_still_produces_a_valid_index(self):
        idx = site.search_index(_tmp(), environ=_REAL_ENV)
        self.assertTrue(all(set(e) == {"t", "u", "k", "s"} for e in idx))
        self.assertGreaterEqual(len([e for e in idx if e["k"] == "Category"]), 7)


class SearchUiOnEveryPageTests(unittest.TestCase):
    def _artifact(self, d):
        with mock.patch.dict(os.environ, _REAL_ENV, clear=True):
            return site.build_site_artifact(d)

    def test_search_index_file_is_in_the_artifact(self):
        d = _tmp()
        _seed(d)
        art = self._artifact(d)
        self.assertIn("search-index.json", art.files)
        json.loads(art.files["search-index.json"])

    def test_every_html_page_ships_the_search_ui_and_behaviour(self):
        d = _tmp()
        _seed(d)
        art = self._artifact(d)
        html_pages = [name for name, h in art.files.items()
                      if str(h).lstrip().startswith("<!doctype")]
        self.assertGreater(len(html_pages), 5)
        for name in html_pages:
            h = art.files[name]
            with self.subTest(page=name):
                self.assertIn('id="site-search-results"', h)
                self.assertIn('role="combobox"', h)
                self.assertIn("window.__SITE_BASE__", h)
                self.assertIn("search-index.json", h)
                self.assertIn("aria-activedescendant", h)   # keyboard nav wired

    def test_old_homepage_only_guide_filter_script_is_gone(self):
        d = _tmp()
        _seed(d)
        art = self._artifact(d)
        for h in art.files.values():
            self.assertNotIn("#guides .guide-card')", str(h))

    def test_base_is_injected_so_fetch_resolves_from_any_depth(self):
        d = _tmp()
        _seed(d)
        art = self._artifact(d)
        # a deep page (product detail) must still know the site root
        deep = art.files["product/jbl-quantum-stream-talk/index.html"]
        self.assertIn(f'window.__SITE_BASE__="{_BASE}"', deep)

    def test_search_ui_present_without_deploy_config_too(self):
        d = _tmp()
        _seed(d)
        with mock.patch.dict(os.environ, {}, clear=True):
            art = site.build_site_artifact(d)
        self.assertIn("search-index.json", art.files)
        self.assertIn("window.__SITE_BASE__", art.files["index.html"])
        self.assertIn('window.__SITE_BASE__=""', art.files["index.html"])


if __name__ == "__main__":
    unittest.main()
