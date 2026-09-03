"""Phase 11-real P1-1: end-to-end, non-money smoke verification.

Closes the loop between P1-1 (the opportunity checkout builder) and P0-1
(`PayPalPaymentAdapter`) that no prior test exercised together:

  persisted Opportunity + successful PLAN task (frozen offer)
    -> `build-opportunity-checkout <opp_id>` (the real CLI command)
    -> generated checkout.html
    -> parsed EXACTLY as PayPal would receive the order-creation payload
       (custom_id / amount / currency, extracted from the page - not
       recomputed from the store)
    -> that extracted payload is turned into a synthetic PayPal
       Transaction Search row (the only fake: the external transport)
    -> fed into the real, unmodified `PayPalPaymentAdapter.poll()`
    -> a matching `PaymentEvent`
    -> the existing, unmodified `process_payment_event()` books it into
       the real revenue ledger.

No network call, no PayPal SDK execution, no real payment, no secret
exposure. Nothing in P0-1 or P1-1 production code is touched by this
file - it only proves the two already-shipped pieces agree with each
other on the wire format they share.
"""

import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from revenue_os import opportunity_state as ostate
from revenue_os.cli import main
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore
from revenue_os.payments import process_payment_event
from revenue_os.paypal_payments import PayPalPaymentAdapter
from revenue_os.revenue import RevenueLedger

_CLIENT_ID = "AbC-live_client_123"
_SECRET = "sk_live_this_must_never_leak_anywhere"
_ENV = {"PAYPAL_CLIENT_ID": _CLIENT_ID, "PAYPAL_ENV": "live",
       "PAYPAL_CLIENT_SECRET": _SECRET}

_PRICE = 29.90
_CURRENCY = "EUR"


class _WellFormed(HTMLParser):
    """Just proves the page parses without throwing - a real DOM/browser
    would render it. No network, no execution of the embedded script."""


class _StubPayPalClient:
    """Stands in for the whole authenticated PayPalClient - the ONLY fake
    in this chain, and only at the external transport boundary."""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[str] = []

    def search_transactions(self, start, end):
        self.calls.append("search_transactions")
        return self._rows


def _paypal_row(custom_id: str, amount: str, currency: str,
               txn_id: str = "REAL-CAP-1") -> dict:
    """The shape a real PayPal Transaction Search response row has - built
    ONLY from values extracted from the generated checkout page, never
    recomputed from the store."""
    return {"transaction_info": {
        "transaction_id": txn_id,
        "transaction_status": "S",
        "transaction_amount": {"value": amount, "currency_code": currency},
        "custom_field": custom_id,
        "transaction_initiation_date": "2026-01-01T00:00:00-0000",
    }}


class P1_1_SmokeTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self._old_env = {k: os.environ.get(k) for k in
                         ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in self._old_env})
        os.environ.update(_ENV)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._d.cleanup()

    def _real_opportunity(self) -> str:
        """A persisted Opportunity, walked to LIVE (a real revenue-eligible
        state) through the legal state machine, with a real successful PLAN
        task carrying the frozen offer - exactly what the real pipeline
        (acceptance -> worker -> PLAN adapter) would have produced."""
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Smoke Test Pack", category="saas",
                                   target_customer="indie hackers"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()

        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.resolve_dependencies()
        q.claim(t.task_id, "smoke")
        q.mark_succeeded(t.task_id, {"offer": {
            "price": _PRICE, "currency": _CURRENCY,
            "what_is_sold": "Smoke Test Pack"}})
        q.save()
        return oid

    def test_full_non_money_chain_checkout_to_p0_1_attribution(self):
        oid = self._real_opportunity()

        # 1. the real CLI command builds the real checkout page
        rc = main(["build-opportunity-checkout", "--data-dir", str(self.d), oid])
        self.assertEqual(rc, 0)
        html_path = self.d / "deliverables" / oid / "checkout.html"
        self.assertTrue(html_path.exists())
        html = html_path.read_text(encoding="utf-8")

        # 2. the page is well-formed enough to be parsed (a browser would
        #    render it) - no network, no script execution
        _WellFormed().feed(html)   # raises on structurally broken markup
        self.assertIn("<script src=\"https://www.paypal.com/sdk/js?", html)
        self.assertIn("id='paypal-button-container'", html)

        # 3. extract EXACTLY what PayPal's order-creation call would carry -
        #    not recomputed from the store, read off the actual page
        m = re.search(
            r"actions\.order\.create\(\{.*?amount:\s*\{\s*value:\s*\"([\d.]+)\","
            r"\s*currency_code:\s*\"([A-Z]{3})\"\s*\},\s*custom_id:\s*\"([^\"]+)\"",
            html, re.S)
        self.assertIsNotNone(m, "could not find the order.create payload in the page")
        page_amount, page_currency, page_custom_id = m.groups()

        # 4a. custom_id is EXACTLY the persisted opportunity id
        self.assertEqual(page_custom_id, oid)
        self.assertRegex(page_custom_id, r"^opp_[0-9a-f]{12}$")

        # 4b. price/currency on the page are EXACTLY the frozen PLAN offer -
        #     the same fields P0-1's _frozen_offer() reads
        plan = next(t for t in load_tasks(self.d).by_opportunity(oid)
                   if t.task_type == "PLAN" and t.status == "SUCCEEDED")
        frozen_offer = plan.output["offer"]
        self.assertEqual(round(float(page_amount), 2),
                         round(float(frozen_offer["price"]), 2))
        self.assertEqual(page_currency, str(frozen_offer["currency"]).upper())

        # 4c. no secret anywhere on the page
        self.assertNotIn(_SECRET, html)
        for marker in ("CLIENT_SECRET", "client_secret", "PAYPAL_CLIENT_SECRET"):
            self.assertNotIn(marker, html)
        # the only external origin is PayPal's own SDK
        origins = set(re.findall(r'https?://[a-z0-9.\-]+', html))
        self.assertEqual(origins, {"https://www.paypal.com"})

        # 5. simulate PayPal having settled EXACTLY that order (only the
        #    external transport is fake) and poll with the real,
        #    unmodified P0-1 adapter
        row = _paypal_row(page_custom_id, page_amount, page_currency)
        stub = _StubPayPalClient([row])
        adapter = PayPalPaymentAdapter(self.d, client=stub)
        result = adapter.poll(opportunity_id=oid)

        self.assertTrue(result.ok)
        self.assertFalse(result.blocked)
        self.assertEqual(stub.calls, ["search_transactions"])   # read-only only
        self.assertEqual(len(result.events), 1)
        ev = result.events[0]
        self.assertEqual(ev.opportunity_id, oid)
        self.assertEqual(ev.amount, round(float(page_amount), 2))
        self.assertEqual(ev.currency, page_currency)
        self.assertEqual(ev.provider, "paypal")

        # 6. and the existing, unmodified revenue-booking path accepts it -
        #    closing the loop all the way to a booked (but never spent) EUR
        led_path = self.d / "revenue.json"
        booking = process_payment_event(RevenueLedger.load(led_path), ev)
        self.assertTrue(booking.success)
        self.assertFalse(booking.already_booked)
        self.assertEqual(RevenueLedger.load(led_path).total_for(oid),
                         round(float(page_amount), 2))
        self.assertEqual(len(RevenueLedger.load(led_path).entries()), 1)

        # a second, independent poll of the SAME still-open transaction
        # window must not double-book (idempotent by provider reference)
        adapter2 = PayPalPaymentAdapter(self.d, client=_StubPayPalClient([row]))
        result2 = adapter2.poll(opportunity_id=oid)
        booking2 = process_payment_event(RevenueLedger.load(led_path), result2.events[0])
        self.assertTrue(booking2.already_booked)
        self.assertEqual(RevenueLedger.load(led_path).total_for(oid),
                         round(float(page_amount), 2))
        self.assertEqual(len(RevenueLedger.load(led_path).entries()), 1)

    def test_ineligible_state_checkout_still_builds_but_p0_1_reports_no_event(self):
        """The checkout builder does not gate on opportunity state (Phase
        11-real P1-1 scope), but P0-1's own state gate still protects a
        not-yet-live opportunity from being attributed revenue even if a
        checkout page for it happens to exist."""
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Not Live Yet", category="saas")
                      )["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.resolve_dependencies()
        q.claim(t.task_id, "smoke")
        q.mark_succeeded(t.task_id, {"offer": {"price": _PRICE,
                                                "currency": _CURRENCY}})
        q.save()

        rc = main(["build-opportunity-checkout", "--data-dir", str(self.d), oid])
        self.assertEqual(rc, 0)
        html = (self.d / "deliverables" / oid / "checkout.html").read_text(
            encoding="utf-8")
        self.assertIn(f'custom_id: "{oid}"', html)

        row = _paypal_row(oid, f"{_PRICE:.2f}", _CURRENCY)
        adapter = PayPalPaymentAdapter(self.d, client=_StubPayPalClient([row]))
        result = adapter.poll(opportunity_id=oid)
        self.assertTrue(result.ok)
        self.assertEqual(result.events, [])   # BUILDING is not revenue-eligible


if __name__ == "__main__":
    unittest.main()
