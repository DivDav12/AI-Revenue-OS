import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from revenue_os import paypal
from revenue_os.paypal import (
    PayPalConfig,
    extract_capture,
    sync_transactions,
    verify_and_book_order,
)
from revenue_os.revenue import RevenueLedger
from revenue_os.store import Candidate, CandidateStore


def _order(status="COMPLETED", cap_status="COMPLETED", value="150.00",
           currency="EUR", custom="alpha", cap_id="CAP999"):
    return {
        "id": "ORDER1", "status": status, "create_time": "2026-08-01T10:00:00Z",
        "purchase_units": [{
            "custom_id": custom,
            "payments": {"captures": [{
                "id": cap_id, "status": cap_status,
                "amount": {"currency_code": currency, "value": value},
                "custom_id": custom, "create_time": "2026-08-01T10:00:05Z",
            }]},
        }],
    }


def _txn(tid="TXN1", status="S", value="150.00", currency="EUR", custom="alpha"):
    return {"transaction_info": {
        "transaction_id": tid, "transaction_status": status,
        "transaction_amount": {"currency_code": currency, "value": value},
        "custom_field": custom,
        "transaction_initiation_date": "2026-08-01T10:00:05+0000",
    }}


class _FakeClient:
    def __init__(self, order=None, txns=None):
        self._order = order
        self._txns = txns or []
        self.orders_fetched = []

    def get_order(self, order_id):
        self.orders_fetched.append(order_id)
        return self._order

    def search_transactions(self, start, end):
        return self._txns


class ConfigTests(unittest.TestCase):
    def test_missing_credentials_raise(self):
        with self.assertRaisesRegex(ValueError, "PAYPAL_CLIENT_ID"):
            PayPalConfig.from_env({})

    def test_sandbox_is_the_default(self):
        c = PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y"})
        self.assertEqual(c.env, "sandbox")
        self.assertIn("sandbox", c.base_url)

    def test_live_env(self):
        c = PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y",
                                   "PAYPAL_ENV": "live"})
        self.assertEqual(c.base_url, "https://api-m.paypal.com")

    def test_bad_env_rejected(self):
        with self.assertRaises(ValueError):
            PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y",
                                   "PAYPAL_ENV": "prod"})


class TokenTests(unittest.TestCase):
    def test_token_is_fetched_once_and_cached(self):
        calls = []

        def fake_http(method, url, *, headers, data=None):
            calls.append(url)
            return {"access_token": "tok", "expires_in": 3600}

        orig = paypal._http_json
        paypal._http_json = fake_http
        try:
            client = paypal.PayPalClient(PayPalConfig(client_id="a", client_secret="b"))
            self.assertEqual(client._access_token(), "tok")
            self.assertEqual(client._access_token(), "tok")
            self.assertEqual(len(calls), 1)
        finally:
            paypal._http_json = orig


class ExtractCaptureTests(unittest.TestCase):
    def test_completed_order(self):
        cap = extract_capture(_order())
        self.assertEqual(cap["amount"], 150.0)
        self.assertEqual(cap["currency"], "EUR")
        self.assertEqual(cap["capture_id"], "CAP999")
        self.assertEqual(cap["custom_id"], "alpha")

    def test_incomplete_order_raises(self):
        with self.assertRaisesRegex(ValueError, "not COMPLETED"):
            extract_capture(_order(status="APPROVED"))

    def test_no_completed_capture_raises(self):
        with self.assertRaisesRegex(ValueError, "no completed capture"):
            extract_capture(_order(cap_status="PENDING"))

    def test_bad_amount_raises(self):
        with self.assertRaises(ValueError):
            extract_capture(_order(value="not-a-number"))


class _Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")
        self.store.put(Candidate(name="alpha", status="launched"))
        self.store.save()
        self.ledger = RevenueLedger(self.d / "revenue.json")

    def tearDown(self):
        self._dir.cleanup()

    def _reload_ledger(self):
        return RevenueLedger.load(self.d / "revenue.json")


class VerifyOrderTests(_Base):
    def test_books_a_verified_order_through_record_payment(self):
        r = verify_and_book_order(
            self.store, self.ledger, candidate="alpha", order_id="ORDER1",
            client=_FakeClient(order=_order()),
        )
        self.assertEqual(r["outcome"], "booked")
        led = self._reload_ledger()
        self.assertEqual(led.total_for("alpha"), 150.0)
        self.assertTrue(led.has_ref("paypal:CAP999"))
        # candidate advanced launched -> earning by record_payment
        self.assertEqual(
            CandidateStore.load(self.d / "candidates.json").get("alpha").status,
            "earning")

    def test_second_verify_is_idempotent(self):
        c = _FakeClient(order=_order())
        verify_and_book_order(self.store, self.ledger, candidate="alpha",
                              order_id="ORDER1", client=c)
        r2 = verify_and_book_order(self.store, self._reload_ledger(), candidate="alpha",
                                   order_id="ORDER1", client=c)
        self.assertEqual(r2["outcome"], "already booked")
        self.assertEqual(len(self._reload_ledger().entries()), 1)

    def test_custom_id_mismatch_refuses_without_force(self):
        with self.assertRaisesRegex(ValueError, "custom_id"):
            verify_and_book_order(self.store, self.ledger, candidate="beta",
                                  order_id="ORDER1",
                                  client=_FakeClient(order=_order(custom="alpha")))

    def test_force_overrides_mismatch(self):
        self.store.put(Candidate(name="beta", status="launched"))
        self.store.save()
        r = verify_and_book_order(self.store, self.ledger, candidate="beta",
                                  order_id="ORDER1", force=True,
                                  client=_FakeClient(order=_order(custom="alpha")))
        self.assertEqual(r["outcome"], "booked")

    def test_candidate_not_launched_surfaces_the_gate_error(self):
        self.store.put(Candidate(name="alpha", status="validated"))
        self.store.save()
        with self.assertRaisesRegex(ValueError, "must be launched or earning"):
            verify_and_book_order(self.store, self.ledger, candidate="alpha",
                                  order_id="ORDER1",
                                  client=_FakeClient(order=_order()))


class SyncTests(_Base):
    def _now(self):
        return datetime(2026, 8, 2, tzinfo=timezone.utc)

    def test_books_matching_transactions(self):
        r = sync_transactions(
            self.store, self.ledger, actor="paypal",
            client=_FakeClient(txns=[_txn("T1", custom="alpha"),
                                     _txn("T2", value="49.00", custom="alpha")]),
            now=self._now(),
        )
        self.assertEqual(len(r["booked"]), 2)
        self.assertEqual(r["total_booked"], 199.0)
        self.assertEqual(self._reload_ledger().total_for("alpha"), 199.0)

    def test_skips_with_reasons(self):
        self.store.put(Candidate(name="held", status="validated"))
        self.store.save()
        r = sync_transactions(
            self.store, self.ledger,
            client=_FakeClient(txns=[
                _txn("T1", custom=""),              # no custom_field
                _txn("T2", custom="ghost"),         # unknown candidate
                _txn("T3", custom="held"),          # wrong status
                _txn("T4", status="P", custom="alpha"),  # not success -> filtered
            ]),
            now=self._now(),
        )
        self.assertEqual(r["booked"], [])
        reasons = " ".join(s["reason"] for s in r["skipped"])
        self.assertIn("no custom_field", reasons)
        self.assertIn("unknown candidate", reasons)
        self.assertIn("not launched/earning", reasons)

    def test_dry_run_books_nothing(self):
        r = sync_transactions(
            self.store, self.ledger, dry_run=True,
            client=_FakeClient(txns=[_txn("T1", custom="alpha")]), now=self._now(),
        )
        self.assertEqual(r["booked"][0]["outcome"], "would book")
        self.assertEqual(self._reload_ledger().entries(), [])

    def test_already_booked_transactions_are_silently_skipped(self):
        c = _FakeClient(txns=[_txn("T1", custom="alpha")])
        sync_transactions(self.store, self.ledger, client=c, now=self._now())
        r = sync_transactions(self.store, self._reload_ledger(), client=c,
                              now=self._now())
        self.assertEqual(r["booked"], [])
        self.assertEqual(r["skipped"], [])
        self.assertEqual(len(self._reload_ledger().entries()), 1)


class RecordPaymentRefTests(_Base):
    def test_ref_is_stored_and_double_ref_refused(self):
        from revenue_os.revenue import record_payment
        record_payment(self.store, self.ledger, "alpha", 10.0, actor="x",
                       ref="paypal:abc")
        self.assertTrue(self._reload_ledger().has_ref("paypal:abc"))
        with self.assertRaisesRegex(ValueError, "already recorded"):
            record_payment(self.store, self._reload_ledger(), "alpha", 10.0,
                           actor="x", ref="paypal:abc")


if __name__ == "__main__":
    unittest.main()
