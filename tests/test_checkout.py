import re
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.deliverable import render_checkout_html, render_intake_html
from revenue_os.intake import FIELD_KEYS
from revenue_os.offer import paid_offer
from revenue_os.store import Candidate, CandidateStore

_CAND = {"name": "ask-hn-how-do-you-find-your-first-paying-customers",
         "description": "Customer Launch Plan"}
_INCLUDES = [
    "Business & product analysis - what you sell and the problem it solves",
    "Ideal customer profile - who is most likely to buy and where to reach them",
    "5-10 specific acquisition opportunities, each with why it fits you",
    "Prioritized strategy - what to try first and the reasoning",
    "14-day action plan - concrete day-by-day steps",
    "2-3 ready-to-use outreach templates adapted to your business",
    "Next-steps checklist",
]
_DISCLAIMER = ("You receive a personalized research and strategy document within "
               "3 business days - not guaranteed customers, revenue, or results.")
_OFFER = {
    "candidate_name": _CAND["name"],
    "what_is_sold": "Customer Launch Plan",
    "price": 29.9, "currency": "EUR", "delivery": "digital",
    "call_to_action": "Get your Customer Launch Plan",
    "price_is_estimate": False,
    "positioning": "A personalized strategy to help you find your first paying customers.",
    "includes": _INCLUDES,
    "delivery_note": "Delivered as a personalized PDF within 3 business days.",
    "disclaimer": _DISCLAIMER,
}
_CLIENT_ID = "AbC-live_client_123"


class RenderTests(unittest.TestCase):
    def test_price_currency_and_exact_custom_id(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        self.assertIn("29.90 EUR", h)
        self.assertIn(
            'custom_id: "ask-hn-how-do-you-find-your-first-paying-customers"', h)
        self.assertIn('value: "29.90", currency_code: "EUR"', h)

    def test_promise_includes_and_disclaimer_are_rendered(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        self.assertIn("first paying customers", h)          # promise / subheadline
        self.assertIn("What you get", h)
        self.assertIn("14-day action plan", h)              # an includes bullet
        self.assertEqual(h.count("<li>"), len(_INCLUDES))
        self.assertIn("not guaranteed customers, revenue, or results", h)
        self.assertIn("Delivered as a personalized PDF within 3 business days", h)

    def test_no_fake_scarcity_or_testimonial_language(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID).lower()
        for bad in ("limited time", "only today", "spots left", "act now",
                    "testimonial", "customers served", "5-star"):
            self.assertNotIn(bad, h)
        # every mention of a guarantee is a negation ("not guaranteed ...")
        for m in re.finditer(r"guarantee", h):
            self.assertIn("not guarantee", h[max(0, m.start() - 8):m.end()])

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
             "what_is_sold": "<script>alert(1)</script>",
             "includes": ["<img src=x onerror=alert(1)>"]},
            client_id=_CLIENT_ID,
        )
        self.assertNotIn("<script>alert(1)</script>", h)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h)
        self.assertNotIn("<img src=x onerror=alert(1)>", h)

    def test_embeds_hidden_intake_form_with_all_fields(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        self.assertIn("id='intake' class='hidden'", h)
        self.assertIn("id='intake-form'", h)
        self.assertIn("name='order_id'", h)
        self.assertIn("name='capture_id'", h)
        self.assertIn(f"name='candidate' value='{_CAND['name']}'", h)
        for key in FIELD_KEYS:
            self.assertIn(f"name='{key}'", h)
        # capture id is pulled from the capture response in onApprove
        self.assertIn("payments.captures[0].id", h)
        # placeholder action until the operator wires a provider
        self.assertIn("REPLACE_WITH_YOUR_FORM_ENDPOINT", h)

    def test_form_action_is_used_when_given(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 form_action="https://formprovider.example/f/abc")
        self.assertIn("action='https://formprovider.example/f/abc'", h)
        self.assertNotIn("REPLACE_WITH_YOUR_FORM_ENDPOINT", h)

    def test_business_email_appears_when_given(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 business_email="divdav12support@gmail.com")
        # the "form failed" fallback names the address
        self.assertIn("to divdav12support@gmail.com.", h)
        # footer contact + post-payment JS suffix
        self.assertIn("Contact: divdav12support@gmail.com.", h)
        self.assertIn("Questions: divdav12support@gmail.com.", h)
        self.assertNotIn("the address that sold you this plan", h)

    def test_generic_wording_without_business_email(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        self.assertIn("the address that sold you this plan", h)
        self.assertNotIn("Contact:", h)

    def test_bogus_business_email_is_ignored(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 business_email="not an email <x>")
        self.assertIn("the address that sold you this plan", h)
        self.assertNotIn("not an email", h)
        # still only paypal as an external origin
        origins = set(re.findall(r'https?://[a-z0-9.\-]+', h))
        self.assertEqual(origins, {"https://www.paypal.com"})

    def test_standalone_intake_page_shows_business_email(self):
        h = render_intake_html(_CAND["name"],
                               form_action="https://formprovider.example/f/abc",
                               business_email="divdav12support@gmail.com")
        self.assertIn("to divdav12support@gmail.com.", h)
        # the email is not a URL - external origins unchanged
        origins = set(re.findall(r'https?://[a-z0-9.\-/]+', h))
        self.assertEqual(origins, {"https://formprovider.example/f/abc"})

    def test_standalone_intake_page(self):
        h = render_intake_html(_CAND["name"],
                               form_action="https://formprovider.example/f/abc")
        for key in FIELD_KEYS:
            self.assertIn(f"name='{key}'", h)
        self.assertIn("URLSearchParams", h)          # reads ?order=&capture=
        self.assertIn("action='https://formprovider.example/f/abc'", h)
        origins = set(re.findall(r'https?://[a-z0-9.\-/]+', h))
        self.assertEqual(origins, {"https://formprovider.example/f/abc"})

    def test_bad_price_raises(self):
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, {"price": 0}, client_id=_CLIENT_ID)
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, {"price": "x"}, client_id=_CLIENT_ID)

    def test_missing_client_id_raises(self):
        with self.assertRaises(ValueError):
            render_checkout_html(_CAND, _OFFER, client_id="")


# ---------------------------------------------------------------------------
# Phase 11-real P1-1: opportunity_id -> custom_id attribution
# ---------------------------------------------------------------------------

_OPP_ID = "opp_0123456789ab"


class OpportunityAttributionTests(unittest.TestCase):
    """A. a valid opportunity id becomes the PayPal custom_id.
    B. the exact same id reaches the order-creation payload.
    C. invalid opportunity ids are rejected (fail closed).
    D. no opportunity_id -> no silent fallback to any other identifier.
    """

    def test_A_valid_opportunity_id_becomes_custom_id(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 opportunity_id=_OPP_ID)
        self.assertIn(f'custom_id: "{_OPP_ID}"', h)
        # the candidate name must NOT leak into custom_id when an
        # opportunity_id is given
        self.assertNotIn(f'custom_id: "{_CAND["name"]}"', h)

    def test_B_same_id_reaches_the_order_create_payload(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 opportunity_id=_OPP_ID)
        m = re.search(r"actions\.order\.create\(\{.*?custom_id:\s*\"([^\"]+)\"",
                      h, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), _OPP_ID)
        # and the footer's human-visible order reference agrees with it
        self.assertIn(f"Order reference: <code>{_OPP_ID}</code>", h)

    def test_C_invalid_opportunity_ids_are_rejected(self):
        for bad in ("candidate", "opp_123", "opp_zzzzzzzzzzzz",
                    "foo_0123456789ab", "opp_0123456789ab ", " opp_0123456789ab",
                    "OPP_0123456789AB", "opp_0123456789abx"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                         opportunity_id=bad)

    def test_C_empty_opportunity_id_is_a_noop_not_an_error(self):
        # empty means "no opportunity_id given" - falls back to the
        # existing candidate-name behaviour, not a validation error
        # (a real whitespace-padded id is still rejected - no leniency, see
        # test_C_invalid_opportunity_ids_are_rejected)
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 opportunity_id="")
        self.assertIn(f'custom_id: "{_CAND["name"]}"', h)

    def test_D_missing_opportunity_id_never_falls_back_to_another_identifier(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        # the ONLY identifier that can ever appear as custom_id here is the
        # candidate name explicitly passed in - never a guessed opportunity id
        self.assertNotRegex(h, r'custom_id:\s*"opp_[0-9a-f]{12}"')
        self.assertIn(f'custom_id: "{_CAND["name"]}"', h)

    def test_E_existing_candidate_checkout_is_byte_identical_without_the_param(self):
        with_default = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID)
        without_param = render_checkout_html(
            _CAND, _OFFER, client_id=_CLIENT_ID, currency="EUR",
            form_action="", business_email="")
        self.assertEqual(with_default, without_param)

    def test_F_no_paypal_secret_appears_on_the_opportunity_checkout(self):
        h = render_checkout_html(_CAND, _OFFER, client_id=_CLIENT_ID,
                                 opportunity_id=_OPP_ID)
        for secret_marker in ("CLIENT_SECRET", "client_secret", "PAYPAL_CLIENT_SECRET"):
            self.assertNotIn(secret_marker, h)
        # only the (public, by design) client id and the opportunity id are
        # embedded - no other credential-shaped token
        origins = set(re.findall(r'https?://[a-z0-9.\-]+', h))
        self.assertEqual(origins, {"https://www.paypal.com"})


class PaidOfferTests(unittest.TestCase):
    def test_real_price_is_not_an_estimate(self):
        o = paid_offer(Candidate(name="c", description="d"), price=29.9,
                       currency="eur", includes=("a", " ", "b"),
                       disclaimer="no results promised")
        self.assertEqual(o.price, 29.9)
        self.assertEqual(o.currency, "EUR")
        self.assertFalse(o.price_is_estimate)
        self.assertEqual(o.includes, ("a", "b"))          # blanks dropped
        self.assertEqual(o.to_dict()["disclaimer"], "no results promised")

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

    def test_writes_file_and_persists_full_offer(self):
        rc = self._run(
            _CAND["name"], "--price", "29.90", "--currency", "EUR",
            "--what", "Customer Launch Plan", "--delivery", "digital",
            "--promise", "A personalized strategy to find your first paying customers.",
            "--delivery-note", "Delivered as a personalized PDF within 3 business days.",
            "--disclaimer", _DISCLAIMER,
            "--include", "Ideal customer profile",
            "--include", "14-day action plan",
        )
        self.assertEqual(rc, 0)
        f = self.d / "deliverables" / _CAND["name"] / "checkout.html"
        self.assertTrue(f.exists())
        html = f.read_text(encoding="utf-8")
        self.assertIn(f'custom_id: "{_CAND["name"]}"', html)
        self.assertIn("29.90 EUR", html)
        self.assertIn("14-day action plan", html)
        self.assertIn("not guaranteed customers", html)
        got = CandidateStore.load(self.d / "candidates.json").get(_CAND["name"])
        self.assertEqual(got.offer["price"], 29.9)
        self.assertEqual(got.offer["currency"], "EUR")
        self.assertFalse(got.offer["price_is_estimate"])
        self.assertEqual(got.offer["includes"],
                         ["Ideal customer profile", "14-day action plan"])
        self.assertIn("3 business days", got.offer["delivery_note"])

    def test_business_email_flag_lands_on_both_pages(self):
        rc = self._run(_CAND["name"], "--price", "29.90",
                       "--business-email", "divdav12support@gmail.com")
        self.assertEqual(rc, 0)
        d = self.d / "deliverables" / _CAND["name"]
        for fn in ("checkout.html", "intake.html"):
            html = (d / fn).read_text(encoding="utf-8")
            self.assertIn("divdav12support@gmail.com", html)

    def test_business_email_from_env(self):
        import os
        old = os.environ.get("BUSINESS_EMAIL")
        os.environ["BUSINESS_EMAIL"] = "divdav12support@gmail.com"
        try:
            self._run(_CAND["name"], "--price", "29.90")
        finally:
            if old is None:
                os.environ.pop("BUSINESS_EMAIL", None)
            else:
                os.environ["BUSINESS_EMAIL"] = old
        html = (self.d / "deliverables" / _CAND["name"] / "checkout.html").read_text(
            encoding="utf-8")
        self.assertIn("Contact: divdav12support@gmail.com.", html)

    def test_reuses_stored_offer_without_price(self):
        self._run(_CAND["name"], "--price", "29.90")
        rc = self._run(_CAND["name"], "--out", str(self.d / "c2.html"))
        self.assertEqual(rc, 0)
        self.assertIn("29.90 EUR", (self.d / "c2.html").read_text(encoding="utf-8"))

    def test_errors_without_offer_or_price(self):
        self.assertEqual(self._run(_CAND["name"]), 1)

    def test_errors_when_env_not_live(self):
        env = {**self._env, "PAYPAL_ENV": "sandbox"}
        self.assertEqual(self._run(_CAND["name"], "--price", "29.90", env=env), 1)

    def test_errors_when_candidate_not_launched(self):
        store = CandidateStore.load(self.d / "candidates.json")
        store.put(Candidate(name="held", status="validated"))
        store.save()
        self.assertEqual(self._run("held", "--price", "29.90"), 1)


if __name__ == "__main__":
    unittest.main()
