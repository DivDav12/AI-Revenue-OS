"""Phase 11-real P0-2: the narrow read-only-PayPal exception for the
autonomous loop.

Rule under test:
  * outside autonomous_context()            -> guard_paypal is a no-op
  * inside autonomous_context():
      - inside paypal_read_context() AND a known read-only op   -> allowed
      - anything else (no read scope / unknown op / money op)   -> ActionBlocked

No network: paypal._http_json is fully mocked.
"""

import unittest
from datetime import datetime, timezone

from revenue_os import action_class as ac
from revenue_os import paypal
from revenue_os.paypal import PayPalClient, PayPalConfig


def _cfg():
    return PayPalConfig(client_id="id", client_secret="secret", env="sandbox")


class _FakeHTTP:
    """Stand-in for paypal._http_json - records calls, returns canned bodies,
    never opens a socket."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers, data=None):
        self.calls.append((method, url))
        if url.endswith("/v1/oauth2/token"):
            return {"access_token": "tok", "expires_in": 3600}
        if "/v2/checkout/orders/" in url:
            return {"id": "ORDER1", "status": "COMPLETED",
                    "purchase_units": [{"payments": {"captures": [
                        {"id": "CAP1", "status": "COMPLETED", "custom_id": "opp_x",
                         "amount": {"value": "29.00", "currency_code": "EUR"}}]}}]}
        if "/v1/reporting/transactions" in url:
            return {"transaction_details": [], "total_pages": 1}
        return {}


class GuardParaMixin(unittest.TestCase):
    def setUp(self):
        self._orig = paypal._http_json
        self.http = _FakeHTTP()
        paypal._http_json = self.http

    def tearDown(self):
        paypal._http_json = self._orig
        # never leak a stuck context between tests
        ac._local.__dict__.pop("depth", None)
        ac._local.__dict__.pop("ppr_depth", None)


class ReadOnlyAllowed(GuardParaMixin):
    def test_A_read_only_get_order_works_in_autonomous_context(self):
        with ac.autonomous_context():
            with ac.paypal_read_context():
                cfg = PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x",
                                             "PAYPAL_CLIENT_SECRET": "y",
                                             "PAYPAL_ENV": "sandbox"})
                order = PayPalClient(cfg).get_order("ORDER1")
        self.assertEqual(order["status"], "COMPLETED")
        self.assertTrue(any("orders/ORDER1" in u for _, u in self.http.calls))

    def test_A_read_only_search_transactions_works_in_autonomous_context(self):
        s = datetime(2026, 1, 1, tzinfo=timezone.utc)
        e = datetime(2026, 1, 20, tzinfo=timezone.utc)
        with ac.autonomous_context(), ac.paypal_read_context():
            rows = PayPalClient(_cfg()).search_transactions(s, e)
        self.assertEqual(rows, [])
        self.assertTrue(any("reporting/transactions" in u for _, u in self.http.calls))

    def test_outside_autonomy_guard_is_a_noop_for_any_op(self):
        for op in ("get_order", "capture_order", "refund", "wat", ""):
            ac.guard_paypal(op)   # must not raise
        # and a real client call still works with no context at all
        PayPalClient(_cfg()).get_order("ORDER1")


class MoneyAndUnknownBlocked(GuardParaMixin):
    def test_B_get_order_blocked_without_read_context(self):
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                PayPalClient(_cfg()).get_order("ORDER1")
        # nothing hit the transport
        self.assertEqual(self.http.calls, [])

    def test_B_capture_style_op_blocked_even_inside_read_context(self):
        with ac.autonomous_context(), ac.paypal_read_context():
            for money_op in ("capture_order", "capture_payment", "create_order"):
                with self.assertRaises(ac.ActionBlocked):
                    ac.guard_paypal(money_op)

    def test_C_refund_payout_void_are_blocked_in_autonomy(self):
        # NOTE: paypal.py contains NO refund / payout / void / send-money
        # code - these ops do not exist in production. This asserts the
        # guard's allow-list would reject them if such a call were ever added.
        with ac.autonomous_context(), ac.paypal_read_context():
            for op in ("refund", "refund_capture", "payout", "create_payout",
                       "void", "send_money", "disburse"):
                with self.assertRaises(ac.ActionBlocked):
                    ac.guard_paypal(op)

    def test_F_unknown_paypal_op_fails_closed(self):
        with ac.autonomous_context(), ac.paypal_read_context():
            for op in ("", "  ", "wat", "get_orderX", "orders", "GET",
                       "search_transactions_v2", "oauth"):
                with self.assertRaises(ac.ActionBlocked):
                    ac.guard_paypal(op)

    def test_B_http_write_backstop_blocks_any_non_get_in_autonomy(self):
        # exercise the REAL _http_json backstop (the mixin patched it out).
        # The guard fires BEFORE any socket - no network is opened.
        real = self._orig
        import urllib.request as _u
        hit = []
        orig_urlopen = _u.urlopen
        _u.urlopen = lambda *a, **k: hit.append(1) or (_ for _ in ()).throw(
            RuntimeError("sentinel: reached the socket"))
        try:
            with ac.autonomous_context(), ac.paypal_read_context():
                for verb in ("POST", "PUT", "PATCH", "DELETE"):
                    with self.assertRaises(ac.ActionBlocked):
                        real(verb, "https://api-m.paypal.com/v2/checkout/"
                             "orders/O1/capture", headers={})
                self.assertEqual(hit, [])   # never reached a socket

                # the oauth token POST (the only legitimate non-GET) is NOT
                # blocked by the backstop - it proceeds to the transport
                # (our sentinel), i.e. no ActionBlocked.
                with self.assertRaises(RuntimeError):
                    real("POST", "https://api-m.paypal.com/v1/oauth2/token",
                         headers={}, data=b"x")
        finally:
            _u.urlopen = orig_urlopen

    def test_config_blocked_without_read_context(self):
        # exactly the existing test_money_firewall::test_paypal_config_refuses
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x",
                                       "PAYPAL_CLIENT_SECRET": "y",
                                       "PAYPAL_ENV": "live"})


class FirewallStillIntact(GuardParaMixin):
    def test_D_non_paypal_money_paths_still_blocked(self):
        import tempfile
        from revenue_os.revenue import RevenueLedger, record_payment
        from revenue_os.store import CandidateStore
        from revenue_os.llm_normalize import build_client
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                record_payment(CandidateStore(d / "c.json"),
                               RevenueLedger(d / "r.json"), "x", 10.0, actor="t")
            with self.assertRaises(ac.ActionBlocked):
                build_client()
        # even inside a paypal_read_context, the non-PayPal guards are untouched
        with ac.autonomous_context(), ac.paypal_read_context():
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy("spend money")

    def test_D_guard_no_money_in_autonomy_semantics_unchanged(self):
        self.assertIsNone(ac.guard_no_money_in_autonomy("x"))     # outside: no-op
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy("anything")

    def test_E_autonomous_context_stays_active_inside_read_context(self):
        with ac.autonomous_context():
            self.assertTrue(ac.in_autonomous_context())
            with ac.paypal_read_context():
                self.assertTrue(ac.in_autonomous_context())       # NOT disabled
                self.assertTrue(ac.in_paypal_read_context())
                ac.guard_paypal("get_order")                      # allowed here
            self.assertFalse(ac.in_paypal_read_context())         # scope closed
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_paypal("get_order")                      # blocked again

    def test_E_read_context_is_a_noop_on_its_own(self):
        # a read context WITHOUT an autonomous context grants nothing special
        # and blocks nothing
        with ac.paypal_read_context():
            self.assertFalse(ac.in_autonomous_context())
            ac.guard_paypal("get_order")
            ac.guard_paypal("capture_order")   # no-op: not in autonomy

    def test_read_context_nests(self):
        with ac.autonomous_context():
            with ac.paypal_read_context():
                with ac.paypal_read_context():
                    self.assertTrue(ac.in_paypal_read_context())
                self.assertTrue(ac.in_paypal_read_context())      # still one level
                ac.guard_paypal("search_transactions")
            self.assertFalse(ac.in_paypal_read_context())


if __name__ == "__main__":
    unittest.main()
