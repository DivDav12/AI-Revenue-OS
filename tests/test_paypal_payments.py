"""Phase 11-real P0-1: PayPalPaymentAdapter (tests/test_paypal_payments.py).

Letters A-U below map 1:1 onto the acceptance checklist for this phase.
No network: every test injects either a lightweight stub at the
`PayPalClient` boundary (business-logic tests) or a real `PayPalClient` /
`PayPalConfig` with `paypal._http_json` patched (the two security tests
that must exercise the real `guard_paypal()` call sites, S and T).
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from revenue_os import action_class as ac
from revenue_os import paypal
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore
from revenue_os.paypal import PayPalClient, PayPalConfig
from revenue_os.paypal_payments import PayPalPaymentAdapter

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

_PRICE = 29.90
_CURRENCY = "EUR"

# state -> the legal transition path from DISCOVERED (opportunity_state._FORWARD)
_PATH = {
    "DISCOVERED": [],
    "SCORED": ["SCORED"],
    "SELECTED": ["SCORED", "SELECTED"],
    "BUILDING": ["SCORED", "SELECTED", "PLANNING", "BUILDING"],
    "FAILED": ["SCORED", "SELECTED", "PLANNING", "BUILDING", "FAILED"],
    "LIVE": ["SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
             "READY_TO_DEPLOY", "DEPLOYING", "LIVE"],
    "ABANDONED": ["ABANDONED"],
}


def _txn(custom_id="opp_x", amount="29.90", currency="EUR", status="S",
         txn_id="CAP1", ts="2026-01-01T00:00:00-0000", payer_email=None):
    row = {"transaction_info": {
        "transaction_id": txn_id,
        "transaction_status": status,
        "transaction_amount": {"value": amount, "currency_code": currency},
        "custom_field": custom_id,
        "transaction_initiation_date": ts,
    }}
    if payer_email is not None:
        row["payer_info"] = {"email_address": payer_email}
    return row


class _StubClient:
    """Stands in for the whole authenticated PayPalClient - the external
    boundary. Records every method invoked so tests can prove no write
    method is ever called."""

    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows if rows is not None else []
        self._raise = raise_exc
        self.calls: list[str] = []

    def search_transactions(self, start, end):
        self.calls.append("search_transactions")
        if self._raise is not None:
            raise self._raise
        return self._rows

    def get_order(self, order_id):     # pragma: no cover - must never be called
        self.calls.append("get_order")
        raise AssertionError("PayPalPaymentAdapter must not call get_order()")


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _opportunity(self, *, state="LIVE", price=_PRICE, currency=_CURRENCY,
                     with_plan=True, second=False, title=None):
        s = OpportunityStore.load(self.d / "opportunities.json")
        if title is None:
            title = "pack-2" if second else "pack"
        oid = s.upsert(Opportunity(title=title, category="saas"))["id"]
        for st in _PATH[state]:
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        if with_plan:
            q = load_tasks(self.d)
            t = q.create(oid, "PLAN")
            q.resolve_dependencies()
            q.claim(t.task_id, "test")
            q.mark_succeeded(t.task_id, {"offer": {"price": price,
                                                    "currency": currency}})
            q.save()
        return oid

    def _adapter(self, rows=None, raise_exc=None, **kw):
        return PayPalPaymentAdapter(self.d, client=_StubClient(rows, raise_exc), **kw)


# ---------------------------------------------------------------------------
# A. valid payment
# ---------------------------------------------------------------------------

class ValidPayment(_Base):
    def test_A_valid_payment_produces_exactly_one_event(self):
        oid = self._opportunity()
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertTrue(r.ok)
        self.assertFalse(r.blocked)
        self.assertEqual(len(r.events), 1)
        ev = r.events[0]
        self.assertEqual(ev.opportunity_id, oid)
        self.assertEqual(ev.amount, _PRICE)
        self.assertEqual(ev.currency, _CURRENCY)
        self.assertEqual(ev.reference, "CAP1")
        self.assertEqual(ev.provider, "paypal")
        self.assertEqual(ev.customer_ref, "")
        self.assertIn("transaction_info", ev.raw)


# ---------------------------------------------------------------------------
# Phase 11-real P1-2: buyer email -> customer_ref
# ---------------------------------------------------------------------------

class BuyerEmail(_Base):
    def test_valid_payer_email_becomes_customer_ref(self):
        oid = self._opportunity()
        r = self._adapter([_txn(custom_id=oid, payer_email="buyer@example.test")]
                          ).poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)
        self.assertEqual(r.events[0].customer_ref, "buyer@example.test")

    def test_missing_payer_info_yields_empty_customer_ref_not_rejection(self):
        oid = self._opportunity()
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)          # still attributed / booked
        self.assertEqual(r.events[0].customer_ref, "")

    def test_malformed_email_is_dropped_not_passed_through_and_not_rejected(self):
        oid = self._opportunity()
        for bad in ("not-an-email", "<script>alert(1)</script>",
                    "@missing-local.test", "trailing-dot@example.", "",
                    "two@@at.test"):
            with self.subTest(bad=bad):
                r = self._adapter([_txn(custom_id=oid, payer_email=bad)]
                                  ).poll(opportunity_id=oid)
                self.assertEqual(len(r.events), 1)   # a bad email never blocks the sale
                self.assertEqual(r.events[0].customer_ref, "")

    def test_email_is_stripped(self):
        oid = self._opportunity()
        r = self._adapter([_txn(custom_id=oid, payer_email="  buyer@example.test  ")]
                          ).poll(opportunity_id=oid)
        self.assertEqual(r.events[0].customer_ref, "buyer@example.test")

    def test_non_dict_payer_info_is_ignored(self):
        oid = self._opportunity()
        row = _txn(custom_id=oid)
        row["payer_info"] = "not-a-dict"
        r = self._adapter([row]).poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)
        self.assertEqual(r.events[0].customer_ref, "")

    def test_email_of_a_non_matching_row_never_leaks_into_the_attributed_event(self):
        oid_a = self._opportunity()
        oid_b = self._opportunity(second=True)
        rows = [_txn(custom_id=oid_a, payer_email="buyer-a@example.test"),
                _txn(custom_id=oid_b, amount=str(_PRICE), txn_id="CAP2",
                    payer_email="buyer-b@example.test")]
        stub = _StubClient(rows)
        adapter = PayPalPaymentAdapter(self.d, client=stub)
        ra = adapter.poll(opportunity_id=oid_a)
        self.assertEqual(len(ra.events), 1)
        self.assertEqual(ra.events[0].customer_ref, "buyer-a@example.test")
        self.assertNotIn("buyer-b@example.test",
                         [e.customer_ref for e in ra.events])


# ---------------------------------------------------------------------------
# B. wrong transaction status
# ---------------------------------------------------------------------------

class WrongStatus(_Base):
    def test_B_non_success_statuses_yield_no_event(self):
        oid = self._opportunity()
        for status in ("P", "D", "R", "", "V", "UNKNOWN"):
            r = self._adapter([_txn(custom_id=oid, status=status)]
                              ).poll(opportunity_id=oid)
            self.assertTrue(r.ok)
            self.assertEqual(r.events, [], status)


# ---------------------------------------------------------------------------
# C. missing custom_id
# ---------------------------------------------------------------------------

class MissingCustomId(_Base):
    def test_C_missing_custom_id_no_event(self):
        oid = self._opportunity()
        r = self._adapter([_txn(custom_id="")]).poll(opportunity_id=oid)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# D. malformed custom_id
# ---------------------------------------------------------------------------

class MalformedCustomId(_Base):
    def test_D_malformed_custom_id_no_event(self):
        oid = self._opportunity()
        for bad in ("candidate", "opp_123", "opp_zzzzzzzzzzzz",
                    "foo_123456789abc"):
            r = self._adapter([_txn(custom_id=bad)]).poll(opportunity_id=oid)
            self.assertEqual(r.events, [], bad)


# ---------------------------------------------------------------------------
# E. unknown (but syntactically valid) opportunity id
# ---------------------------------------------------------------------------

class UnknownOpportunity(_Base):
    def test_E_syntactically_valid_but_unknown_opportunity_no_event(self):
        r = self._adapter([]).poll(opportunity_id="opp_" + "a" * 12)
        self.assertTrue(r.ok)
        self.assertFalse(r.blocked)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# F. custom_id belongs to a different opportunity
# ---------------------------------------------------------------------------

class CrossAttribution(_Base):
    def test_F_custom_id_of_a_different_opportunity_no_event(self):
        oid_a = self._opportunity()
        oid_b = self._opportunity(second=True)
        r = self._adapter([_txn(custom_id=oid_b)]).poll(opportunity_id=oid_a)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# G. amount mismatch
# ---------------------------------------------------------------------------

class AmountMismatch(_Base):
    def test_G_amount_mismatch_no_event(self):
        oid = self._opportunity(price=29.90)
        for bad_amount in ("29.89", "30.00", "29.91"):
            r = self._adapter([_txn(custom_id=oid, amount=bad_amount)]
                              ).poll(opportunity_id=oid)
            self.assertEqual(r.events, [], bad_amount)


# ---------------------------------------------------------------------------
# H. currency mismatch
# ---------------------------------------------------------------------------

class CurrencyMismatch(_Base):
    def test_H_currency_mismatch_no_event(self):
        oid = self._opportunity(currency="EUR")
        r = self._adapter([_txn(custom_id=oid, currency="USD")]
                          ).poll(opportunity_id=oid)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# I. zero / negative amounts
# ---------------------------------------------------------------------------

class ZeroNegative(_Base):
    def test_I_zero_or_negative_amount_no_event(self):
        oid = self._opportunity()
        for amt in ("0", "0.00", "-29.90"):
            r = self._adapter([_txn(custom_id=oid, amount=amt)]
                              ).poll(opportunity_id=oid)
            self.assertEqual(r.events, [], amt)


# ---------------------------------------------------------------------------
# J / K / L / M. PLAN task / offer problems
# ---------------------------------------------------------------------------

class OfferProblems(_Base):
    def test_J_missing_plan_task_no_event(self):
        oid = self._opportunity(with_plan=False)
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(r.events, [])

    def test_K_plan_task_not_succeeded_no_event(self):
        oid = self._opportunity(with_plan=False)
        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.save()   # left PENDING - never SUCCEEDED
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(r.events, [])

    def test_L_plan_output_without_offer_no_event(self):
        oid = self._opportunity(with_plan=False)
        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_succeeded(t.task_id, {"hypothesis": "no offer key here"})
        q.save()
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(r.events, [])

    def test_M_invalid_offer_no_event(self):
        for bad_offer in ({"price": 29.90}, {"currency": "EUR"},
                          {"price": "not-a-number", "currency": "EUR"},
                          {"price": 0, "currency": "EUR"},
                          {"price": -5, "currency": "EUR"}):
            with self.subTest(bad_offer=bad_offer):
                d2 = tempfile.TemporaryDirectory()
                try:
                    dd = Path(d2.name)
                    s = OpportunityStore(dd / "opportunities.json")
                    oid = s.upsert(Opportunity(title="x", category="saas"))["id"]
                    for st in _PATH["LIVE"]:
                        s.transition(oid, st, reason="setup", source="test")
                    s.save()
                    q = load_tasks(dd)
                    t = q.create(oid, "PLAN")
                    q.resolve_dependencies()
                    q.claim(t.task_id, "test")
                    q.mark_succeeded(t.task_id, {"offer": bad_offer})
                    q.save()
                    r = PayPalPaymentAdapter(
                        dd, client=_StubClient([_txn(custom_id=oid)])
                    ).poll(opportunity_id=oid)
                    self.assertEqual(r.events, [])
                finally:
                    d2.cleanup()


# ---------------------------------------------------------------------------
# N. ineligible opportunity state
# ---------------------------------------------------------------------------

class IneligibleState(_Base):
    def test_N_early_and_terminal_states_no_event(self):
        for state in ("DISCOVERED", "SELECTED", "BUILDING", "FAILED", "ABANDONED"):
            oid = self._opportunity(state=state, title=f"pack-{state.lower()}",
                                    with_plan=(state not in
                                    ("DISCOVERED", "SELECTED", "ABANDONED")))
            r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
            self.assertEqual(r.events, [], state)

    def test_N_live_or_later_is_possible(self):
        oid = self._opportunity(state="LIVE")
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)


# ---------------------------------------------------------------------------
# O. multiple opportunities / multiple transactions - no cross-attribution
# ---------------------------------------------------------------------------

class MultipleOpportunities(_Base):
    def test_O_poll_returns_only_the_polled_opportunitys_events(self):
        oid_a = self._opportunity(price=10.0)
        oid_b = self._opportunity(price=20.0, second=True)
        rows = [_txn(custom_id=oid_a, amount="10.00", txn_id="CAP-A"),
                _txn(custom_id=oid_b, amount="20.00", txn_id="CAP-B")]
        stub = _StubClient(rows)
        adapter = PayPalPaymentAdapter(self.d, client=stub)

        ra = adapter.poll(opportunity_id=oid_a)
        self.assertEqual([e.reference for e in ra.events], ["CAP-A"])

        rb = adapter.poll(opportunity_id=oid_b)
        self.assertEqual([e.reference for e in rb.events], ["CAP-B"])


# ---------------------------------------------------------------------------
# P. duplicate capture - de-duped downstream, not by the adapter itself
# ---------------------------------------------------------------------------

class DuplicateCapture(_Base):
    def test_P_same_transaction_id_twice_downstream_books_once(self):
        from revenue_os.payments import process_payment_event
        from revenue_os.revenue import RevenueLedger

        oid = self._opportunity()
        r = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)

        led_path = self.d / "revenue.json"
        res1 = process_payment_event(RevenueLedger.load(led_path), r.events[0])
        self.assertTrue(res1.success)
        self.assertFalse(res1.already_booked)
        # poll again (e.g. next CHECK_REVENUE cycle) - same transaction
        r2 = self._adapter([_txn(custom_id=oid)]).poll(opportunity_id=oid)
        res2 = process_payment_event(RevenueLedger.load(led_path), r2.events[0])
        self.assertTrue(res2.already_booked)
        self.assertEqual(RevenueLedger.load(led_path).total_for(oid), _PRICE)
        self.assertEqual(len(RevenueLedger.load(led_path).entries()), 1)


# ---------------------------------------------------------------------------
# Q. API failure -> no revenue, fail-closed retryable
# ---------------------------------------------------------------------------

class ApiFailure(_Base):
    def test_Q_client_exception_no_event_not_blocked(self):
        oid = self._opportunity()
        r = self._adapter(raise_exc=ValueError("PayPal API 503: upstream")
                          ).poll(opportunity_id=oid)
        self.assertFalse(r.ok)
        self.assertFalse(r.blocked)
        self.assertEqual(r.events, [])

    def test_Q_unexpected_exception_type_also_fails_closed(self):
        oid = self._opportunity()
        r = self._adapter(raise_exc=RuntimeError("boom")
                          ).poll(opportunity_id=oid)
        self.assertFalse(r.ok)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# R. missing credentials -> fail closed, blocked
# ---------------------------------------------------------------------------

class MissingCredentials(_Base):
    def test_R_no_credentials_is_blocked_not_a_retryable_error(self):
        oid = self._opportunity()
        adapter = PayPalPaymentAdapter(
            self.d, config_factory=lambda: PayPalConfig.from_env({}))
        r = adapter.poll(opportunity_id=oid)
        self.assertFalse(r.ok)
        self.assertTrue(r.blocked)
        self.assertEqual(r.events, [])


# ---------------------------------------------------------------------------
# S. runs inside autonomous_context() via the real guard_paypal() path
# ---------------------------------------------------------------------------

class _RealHTTP:
    """Same shape as test_paypal_readonly_guard._FakeHTTP - stands in for
    paypal._http_json only. Everything above it (PayPalConfig, PayPalClient,
    guard_paypal) is the REAL production code."""

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


class AutonomousContextTests(_Base):
    def setUp(self):
        super().setUp()
        self._orig = paypal._http_json

    def tearDown(self):
        paypal._http_json = self._orig
        ac._local.__dict__.pop("depth", None)
        ac._local.__dict__.pop("ppr_depth", None)
        super().tearDown()

    def _real_adapter(self, rows):
        http = _RealHTTP(rows)
        paypal._http_json = http
        cfg = PayPalConfig(client_id="id", client_secret="secret", env="sandbox")
        client = PayPalClient(cfg)
        return PayPalPaymentAdapter(self.d, client=client), http

    def test_S_allowed_read_search_succeeds_inside_autonomous_context(self):
        oid = self._opportunity()
        adapter, http = self._real_adapter([_txn(custom_id=oid)])
        with ac.autonomous_context():
            r = adapter.poll(opportunity_id=oid)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.events), 1)
        self.assertTrue(any("reporting/transactions" in u for _, u in http.calls))

    def test_S_no_money_guard_violation(self):
        oid = self._opportunity()
        adapter, _ = self._real_adapter([_txn(custom_id=oid)])
        with ac.autonomous_context():
            adapter.poll(opportunity_id=oid)   # must not raise ActionBlocked
        # the global money firewall is unaffected by any of this
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy("spend money")

    # -- P1-12: the Transaction Search request must ask for payer_info -----
    def test_B_request_asks_for_transaction_info_and_payer_info(self):
        oid = self._opportunity()
        adapter, http = self._real_adapter(
            [_txn(custom_id=oid, payer_email="buyer@example.test")])
        r = adapter.poll(opportunity_id=oid)
        self.assertEqual(len(r.events), 1)
        self.assertEqual(r.events[0].customer_ref, "buyer@example.test")
        search_urls = [u for _, u in http.calls if "reporting/transactions" in u]
        self.assertTrue(search_urls)
        for u in search_urls:
            # urlencoded "transaction_info,payer_info"
            self.assertIn("payer_info", u)
            self.assertIn("transaction_info", u)


# ---------------------------------------------------------------------------
# T. write prohibition
# ---------------------------------------------------------------------------

class WriteProhibition(_Base):
    def test_T_only_search_transactions_is_ever_called(self):
        oid = self._opportunity()
        stub = _StubClient([_txn(custom_id=oid)])
        adapter = PayPalPaymentAdapter(self.d, client=stub)
        adapter.poll(opportunity_id=oid)
        self.assertEqual(stub.calls, ["search_transactions"])

    def test_T_guard_allowlist_still_excludes_every_write_op(self):
        # documents the boundary this adapter relies on: guard_paypal()'s
        # allow-list is unchanged by this phase and still rejects every
        # money-moving PayPal operation inside the autonomous loop.
        with ac.autonomous_context(), ac.paypal_read_context():
            for money_op in ("capture_order", "capture_payment", "create_order",
                             "refund", "payout", "void", "send_money"):
                with self.assertRaises(ac.ActionBlocked):
                    ac.guard_paypal(money_op)
        ac._local.__dict__.pop("depth", None)
        ac._local.__dict__.pop("ppr_depth", None)


if __name__ == "__main__":
    unittest.main()
