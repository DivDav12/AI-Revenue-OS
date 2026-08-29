import re
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.deliverable import render_checkout_html
from revenue_os.offer import paid_offer
from revenue_os.store import Candidate, CandidateStore

_CAND = {"name": "ask-hn-how-do-you-find-your-first-paying-customers",
         "description": "a first-10-customers outreach plan"}
_OFFER = {
    "candidate_name": _CAND["name"],
    "what_is_sold": "a first-10-customers outreach plan",
    "price": 150.0, "currency": "EUR", "delivery": "manual",
    "call_to_action": "Book a paid pilot this week.",
    "price_is_estimate": False, "positioning": "for solo founders pre-revenue",
}
_CLIENT_ID = "AbC-live_client_123"


class RenderTests(unittest.TestCase):
    def test_has_price_currency_and_exact_custom_id(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        self.assertIn("150.00 EUR", h)
        # exact candidate name embedded as the JS custom_id string literal
        self.assertIn(
            'custom_id: "ask-hn-how-do-you-find-your-first-paying-customers"', h)
        self.assertIn('value: "150.00", currency_code: "EUR"', h)

    def test_sdk_is_live_and_carries_client_id_and_currency(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        m = re.search(r'<script src="([^"]+)"></script>', h)
        self.assertIsNotNone(m)
        url = m.group(1)
        self.assertTrue(url.startswith("https://www.paypal.com/sdk/js?"))
        self.assertNotIn("sandbox", url)
        self.assertIn("client-id=AbC-live_client_123", url)
        self.assertIn("currency=EUR", url)
        self.assertIn("intent=capture", url)

    def test_only_paypal_is_an_external_origin(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        origins = set(re.findall(r'https?://[a-z0-9.\-]+', h))
        self.assertEqual(origins, {"https://www.paypal.com"})

    def test_injected_text_is_escaped(self):
        h = render_checkout_html(
            {"name": "x", "description": "<script>alert(1)</script>"},
            {"price": 5.0, "currency": "EUR",
             "what_is_sold": "<script>alert(1)</script>"},
            client_id=_CLIENT_ID,
        )
        self.assertNotIn("<script>alert(1)</script>", h)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h)

    def test_bad_price_raises(self):
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, {"price": 0}, client_id=_CLIENT_ID)
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, {"price": "x"}, client_id=_CLIENT_ID)

    def test_missing_client_id_raises(self):
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, _OFFER, client_id="")


class PaidOfferTests(unittest.TestCase):
    def test_real_price_is_not_an_estimate(self):
        o = paid_offer(Candidate(name="c", description="d"), price=150,
                       currency="eur")
        self.assertEqual(o.price, 150.0)
        self.assertEqual(o.currency, "EUR")
        self.assertFalse(o.price_is_estimate)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            paid_offer(Candidate(name="c"), price=0)
        with self.assertRaises(ValueError):
            paid_offer(Candidate(name="c"), price=10, delivery="bogus")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name=_CAND["name"], description=_CAND["description"],
                            status="launched"))
        store.save()
        self._env = dict(
            PAYPAL_CLIENT_ID=_CLIENT_ID, PAYPAL_ENV="live",
            PAYPAL_CLIENT_SECRET="secret",
        )

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, *args, env=None):
        import os
        old = {k: os.environ.get(k) for k in
               ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in old})
        os.environ.update(env if env is not None else self._env)
        try:
            return main(["build-checkout", "--data-dir", str(self.d), *args])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_writes_file_and_persists_offer(self):
        rc = self._run(_CAND["name"], "--price", "150", "--currency", "EUR",
                       "--what", "a first-10-customers outreach plan")
        self.assertEqual(rc, 0)
        f = self.d / "deliverables" / _CAND["name"] / "checkout.html"
        self.assertTrue(f.exists())
        html = f.read_text(encoding="utf-8")
        self.assertIn(f'custom_id: "{_CAND["name"]}"', html)
        self.assertIn("150.00 EUR", html)
        got = CandidateStore.load(self.d / "candidates.json").get(_CAND["name"])
        self.assertEqual(got.offer["price"], 150.0)
        self.assertEqual(got.offer["currency"], "EUR")
        self.assertFalse(got.offer["price_is_estimate"])

    def test_reuses_stored_offer_without_price(self):
        self._run(_CAND["name"], "--price", "150")
        rc = self._run(_CAND["name"], "--out", str(self.d / "c2.html"))
        self.assertEqual(rc, 0)
        self.assertIn("150.00 EUR", (self.d / "c2.html").read_text(encoding="utf-8"))

    def test_errors_without_offer_or_price(self):
        self.assertEqual(self._run(_CAND["name"]), 1)

    def test_errors_when_env_not_live(self):
        env = {**self._env, "PAYPAL_ENV": "sandbox"}
        self.assertEqual(self._run(_CAND["name"], "--price", "150", env=env), 1)

    def test_errors_when_candidate_not_launched(self):
        store = CandidateStore.load(self.d / "candidates.json")
        store.put(Candidate(name="held", status="validated"))
        store.save()
        self.assertEqual(self._run("held", "--price", "150"), 1)


if __name__ == "__main__":
    unittest.main()
