"""Phase 11-real P1-8: `check-payments` CLI command.

Proves: the command wires a REAL PayPalPaymentAdapter into CHECK_REVENUE
(attribution/idempotency/state-transitions all still the real, unmodified
architecture); the global default (`default_payment_adapter()` /
`revenue_os worker`) is completely unaffected and stays NullPaymentAdapter;
missing/non-live PayPal configuration fails closed before anything is
constructed. No real network call in any test - only `paypal._http_json`
is patched (the external transport boundary), exactly like
test_paypal_readonly_guard.py / test_phase11_real_p1_1_smoke.py.
"""

import os
import tempfile
import unittest
from pathlib import Path

from revenue_os import paypal
from revenue_os.cli import main
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.payments import NullPaymentAdapter, default_payment_adapter
from revenue_os.revenue import RevenueLedger

_CLIENT_ID = "AbC-live_client_123"
_ENV_LIVE = {"PAYPAL_CLIENT_ID": _CLIENT_ID, "PAYPAL_ENV": "live",
            "PAYPAL_CLIENT_SECRET": "secret"}
_PRICE = 29.90
_CURRENCY = "EUR"


class _FakeHTTP:
    """Stands in for paypal._http_json only - the external transport
    boundary. No socket is ever opened."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, *, headers, data=None):
        self.calls.append((method, url))
        if url.endswith("/v1/oauth2/token"):
            return {"access_token": "tok", "expires_in": 3600}
        if "/v1/reporting/transactions" in url:
            return {"transaction_details": self.rows, "total_pages": 1}
        raise AssertionError(f"unexpected PayPal endpoint hit: {method} {url}")


def _txn(custom_id, amount, currency, txn_id="REAL-CAP-1", payer_email=None):
    row = {"transaction_info": {
        "transaction_id": txn_id,
        "transaction_status": "S",
        "transaction_amount": {"value": f"{amount:.2f}", "currency_code": currency},
        "custom_field": custom_id,
        "transaction_initiation_date": "2026-01-01T00:00:00-0000",
    }}
    if payer_email is not None:
        row["payer_info"] = {"email_address": payer_email}
    return row


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self._orig_http = paypal._http_json
        self._old_env = {k: os.environ.get(k) for k in
                         ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in self._old_env})

    def tearDown(self):
        paypal._http_json = self._orig_http
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._d.cleanup()

    def _live_opportunity_with_offer(self, *, price=_PRICE, currency=_CURRENCY):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Cold-email pack", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_succeeded(t.task_id, {"offer": {"price": price, "currency": currency}})
        q.create(oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        return oid

    def _run(self, *args):
        return main(["check-payments", "--data-dir", str(self.d), *args])


# ---------------------------------------------------------------------------
# A. the command really uses PayPalPaymentAdapter - real attribution works
# ---------------------------------------------------------------------------

class RealAdapterUsedTests(_Base):
    def test_A_real_transaction_is_attributed_booked_and_reaches_first_sale(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY, payer_email="buyer@example.test")])
        paypal._http_json = http
        os.environ.update(_ENV_LIVE)

        rc = self._run()
        self.assertEqual(rc, 0)

        # the real HTTP boundary was actually reached (proves the real
        # adapter, not a no-op, ran)
        self.assertTrue(any("reporting/transactions" in u for _, u in http.calls))

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(oid), _PRICE)
        self.assertEqual(led.entries()[0]["ref"], "paypal:REAL-CAP-1")
        self.assertEqual(led.entries()[0]["customer_ref"], "buyer@example.test")
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")

        # the existing DELIVER auto-spawn still fires - unmodified downstream
        deliver = next((t for t in load_tasks(self.d).all()
                        if t.task_type == "DELIVER"), None)
        self.assertIsNotNone(deliver)
        self.assertEqual(deliver.input.get("customer_ref"), "buyer@example.test")

    def test_G_idempotent_second_run_does_not_double_book(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ.update(_ENV_LIVE)

        self._run()
        q = load_tasks(self.d)
        q.create(oid, "CHECK_REVENUE", priority=9)
        q.resolve_dependencies()
        q.save()
        self._run()

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total(), _PRICE)          # not doubled
        self.assertEqual(len(led.entries()), 1)

    def test_amount_mismatch_still_rejected_no_fabricated_revenue(self):
        oid = self._live_opportunity_with_offer(price=_PRICE)
        http = _FakeHTTP([_txn(oid, _PRICE + 5, _CURRENCY)])   # wrong amount
        paypal._http_json = http
        os.environ.update(_ENV_LIVE)

        rc = self._run()
        self.assertEqual(rc, 0)   # the worker ran fine, just found nothing
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")

    def test_only_read_only_endpoints_are_ever_hit(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ.update(_ENV_LIVE)

        self._run()
        for method, url in http.calls:
            allowed = (url.endswith("/v1/oauth2/token")
                      or "/v1/reporting/transactions" in url)
            self.assertTrue(allowed, f"unexpected call: {method} {url}")
            if not url.endswith("/v1/oauth2/token"):
                self.assertEqual(method, "GET")


# ---------------------------------------------------------------------------
# B. the global default is completely unaffected
# ---------------------------------------------------------------------------

class GlobalDefaultUnaffectedTests(_Base):
    def test_default_payment_adapter_is_still_null(self):
        self.assertIsInstance(default_payment_adapter(), NullPaymentAdapter)

    def test_plain_worker_command_never_books_revenue_even_with_a_real_transaction_available(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ.update(_ENV_LIVE)   # even with real, live config present...

        rc = main(["worker", "--data-dir", str(self.d)])   # ...plain `worker`, not check-payments
        self.assertEqual(rc, 0)

        self.assertEqual(http.calls, [])   # PayPal was never even contacted
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")


# ---------------------------------------------------------------------------
# C. fail-closed on missing/non-live configuration
# ---------------------------------------------------------------------------

class FailClosedTests(_Base):
    def test_missing_client_id_fails_closed_before_any_network_call(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ["PAYPAL_ENV"] = "live"   # client id left unset

        rc = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(http.calls, [])
        self.assertFalse((self.d / "revenue.json").exists())

    def test_sandbox_env_fails_closed_before_any_network_call(self):
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ["PAYPAL_CLIENT_ID"] = _CLIENT_ID
        os.environ["PAYPAL_ENV"] = "sandbox"

        rc = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(http.calls, [])
        self.assertFalse((self.d / "revenue.json").exists())

    def test_missing_client_secret_fails_closed_at_the_task_not_the_cli(self):
        # CLI-level check only verifies client_id + env=live (matching the
        # existing P1-4/P1-5 pattern); a missing secret is still caught,
        # deeper, by the adapter itself - the CHECK_REVENUE task fails
        # cleanly, no revenue is ever fabricated.
        oid = self._live_opportunity_with_offer()
        http = _FakeHTTP([_txn(oid, _PRICE, _CURRENCY)])
        paypal._http_json = http
        os.environ["PAYPAL_CLIENT_ID"] = _CLIENT_ID
        os.environ["PAYPAL_ENV"] = "live"   # secret left unset

        rc = self._run()
        self.assertEqual(rc, 0)    # the worker itself ran fine
        self.assertEqual(http.calls, [])   # never even reached oauth
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")


if __name__ == "__main__":
    unittest.main()
