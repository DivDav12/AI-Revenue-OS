"""PHASE 11-REAL P0-1 - targeted E2E: LIVE -> real PayPalPaymentAdapter ->
CHECK_REVENUE -> FIRST_SALE.

Same shape as test_e2e_phase11.py, but the payment leg uses the REAL
`PayPalPaymentAdapter` (real attribution / amount / currency / state
verification, real `payments.process_payment_event`, real revenue
ledger, real state machine) instead of `FakePaymentAdapter`. The ONLY
fake sits at the external PayPal transport boundary (a stub standing in
for the whole authenticated `PayPalClient`) - the adapter, the worker,
the queue, the event log, and the opportunity state machine are all the
real production code.

Phase 11-real still ends at FIRST_SALE (Phase 12 delivery is unaffected
and untouched by this phase).
"""

import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import load_opportunities
from revenue_os.paypal_payments import PayPalPaymentAdapter
from revenue_os.revenue import RevenueLedger
from revenue_os.task_adapters import CheckRevenueAdapter, DeployTaskAdapter, default_registry
from revenue_os.worker import Worker


class _StubPayPalClient:
    """Stands in for the authenticated PayPalClient - the external
    boundary. No network, no real credentials."""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[str] = []

    def search_transactions(self, start, end):
        self.calls.append("search_transactions")
        return self._rows

    def get_order(self, order_id):     # pragma: no cover - must never fire
        raise AssertionError("the real adapter must not call get_order()")


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


class Phase11RealE2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _registry(self, payment_adapter, *, deploy=None):
        reg = default_registry()
        reg.register(DeployTaskAdapter(deploy or FakeDeploymentAdapter(
            base_url="https://e2e-real.pages.test")))
        reg.register(CheckRevenueAdapter(payment_adapter))
        return reg

    def _discover_accept(self):
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, OID, actor="founder")
        return OID

    def _release_deploy(self, OID):
        deploy_id = next(t.task_id for t in load_tasks(self.d).by_opportunity(OID)
                         if t.task_type == "DEPLOY")
        release_task(self.d, deploy_id, actor="founder")

    def _frozen_offer(self, OID):
        plan = next(t for t in load_tasks(self.d).by_opportunity(OID)
                    if t.task_type == "PLAN" and t.status == "SUCCEEDED")
        offer = plan.output["offer"]
        return round(float(offer["price"]), 2), str(offer["currency"]).upper()

    def test_live_to_first_sale_through_the_real_paypal_adapter(self):
        OID = self._discover_accept()

        # nothing to poll yet - no PLAN output frozen, no live opportunity
        early_adapter = PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        reg = self._registry(early_adapter)
        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=100)
        self.assertNotEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")

        price, currency = self._frozen_offer(OID)
        self.assertGreater(price, 0)
        self.assertEqual(currency, "EUR")

        # a real, correctly attributed, correctly priced PayPal transaction
        stub = _StubPayPalClient([_txn(OID, price, currency)])
        real_adapter = PayPalPaymentAdapter(self.d, client=stub)
        reg = self._registry(real_adapter)

        seq_before_deploy = load_events(self.d).last_seq()
        self._release_deploy(OID)
        # one drain: DEPLOY -> LIVE, then CHECK_REVENUE -> the real adapter
        # -> real Transaction Search -> FIRST_SALE
        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=100)

        after = [e for e in load_events(self.d).all() if e["seq"] > seq_before_deploy]
        live_seq = next(e["seq"] for e in after
                        if e["type"] == "OPPORTUNITY_TRANSITIONED"
                        and e["data"].get("to") == "LIVE")
        rr_seq = next(e["seq"] for e in after if e["type"] == "REVENUE_RECORDED")
        fs_seq = next(e["seq"] for e in after
                      if e["type"] == "OPPORTUNITY_TRANSITIONED"
                      and e["data"].get("to") == "FIRST_SALE")
        self.assertLess(live_seq, rr_seq)
        self.assertLess(rr_seq, fs_seq)

        # the stub PayPal transport was actually exercised, and ONLY its
        # read-only search method was ever called
        self.assertEqual(stub.calls, ["search_transactions"])

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(OID), price)
        self.assertEqual(len(led.entries()), 1)
        entry = led.entries()[0]
        self.assertEqual(entry["ref"], "paypal:REAL-CAP-1")
        self.assertEqual(entry["opportunity_id"], OID)
        self.assertEqual(entry["currency"], currency)

        new_events = [e for e in load_events(self.d).all() if e["seq"] > seq_before_deploy]
        types = [e["type"] for e in new_events]
        self.assertEqual(types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(types.count("REVENUE_RECORDED"), 1)
        rr = next(e for e in new_events if e["type"] == "REVENUE_RECORDED")
        self.assertEqual(rr["data"]["reference"], "REAL-CAP-1")
        self.assertEqual(rr["data"]["amount"], price)

        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_SALE")
        fs = [e for e in new_events
              if e["type"] == "OPPORTUNITY_TRANSITIONED"
              and e["data"].get("to") == "FIRST_SALE"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["data"]["from"], "LIVE")

        cr = next(t for t in load_tasks(self.d).all() if t.task_type == "CHECK_REVENUE")
        self.assertEqual(cr.status, "SUCCEEDED")
        self.assertTrue(cr.output["first_sale"])

        # idempotent across a fresh CHECK_REVENUE + "restart" (brand new
        # worker / registry / adapter instances re-polling the SAME PayPal
        # transaction id)
        q = load_tasks(self.d)
        cr2 = q.create(OID, "CHECK_REVENUE", priority=9,
                       depends_on=list(cr.depends_on))
        q.resolve_dependencies()
        q.save()
        stub2 = _StubPayPalClient([_txn(OID, price, currency)])
        reg2 = self._registry(PayPalPaymentAdapter(self.d, client=stub2))
        Worker(self.d, registry=reg2, name="e2e-real-restarted").run(max_ticks=50)

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total(), price)          # not doubled
        self.assertEqual(len(led.entries()), 1)
        all_types = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(all_types.count("REVENUE_RECORDED"), 1)
        self.assertEqual(all_types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "FIRST_SALE"]), 1)
        self.assertEqual(load_tasks(self.d).get(cr2.task_id).status, "SUCCEEDED")
        self.assertFalse(load_tasks(self.d).get(cr2.task_id).output.get("first_sale"))

        # never touched an outgoing-spend / candidate-lifecycle artefact
        for artefact in ("llm_spend.json", "spend.json", "candidates.json",
                         "approvals.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)

    def test_unattributed_paypal_transactions_never_book_revenue(self):
        """A PayPal account can carry transactions for other, unrelated
        opportunities / candidates. None of them may ever be attributed to
        OID without an exact custom_id match."""
        OID = self._discover_accept()
        self._release_deploy(OID)
        Worker(self.d, registry=self._registry(
            PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        ), name="e2e-real").run(max_ticks=100)
        price, currency = self._frozen_offer(OID)

        noise = [
            _txn("candidate-name", price, currency, txn_id="N1"),
            _txn("opp_ffffffffffff", price, currency, txn_id="N2"),   # unknown opp
            _txn("", price, currency, txn_id="N3"),                    # no custom_id
            _txn(OID, price + 0.01, currency, txn_id="N4"),           # amount off
            _txn(OID, price, "USD", txn_id="N5"),                      # currency off
        ]
        stub = _StubPayPalClient(noise)
        reg = self._registry(PayPalPaymentAdapter(self.d, client=stub))
        q = load_tasks(self.d)
        q.create(OID, "CHECK_REVENUE", priority=9)
        q.resolve_dependencies()
        q.save()
        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=50)

        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")
        self.assertNotIn("REVENUE_RECORDED",
                         [e["type"] for e in load_events(self.d).all()])

    def test_buyer_email_from_paypal_reaches_the_ledger_and_the_deliver_task(self):
        """Phase 11-real P1-2: the buyer email PayPal returns alongside a
        matched transaction is not just parsed - it survives the real
        worker -> process_payment_event -> ledger path, and the DELIVER
        task the worker auto-spawns on the confirmed payment carries it as
        input. This is the concrete gap P1-2 closes: before it, a real
        PayPal-sourced sale's DELIVER task always failed with "no customer
        reference"; now it fails only because no DeliveryAdapter is wired
        (fail-closed, unchanged, not part of this phase)."""
        OID = self._discover_accept()
        Worker(self.d, registry=self._registry(
            PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        ), name="e2e-real").run(max_ticks=100)   # -> VALIDATE, before DEPLOY release
        price, currency = self._frozen_offer(OID)

        stub = _StubPayPalClient([_txn(OID, price, currency,
                                       payer_email="buyer@example.test")])
        reg = self._registry(PayPalPaymentAdapter(self.d, client=stub))
        self._release_deploy(OID)
        # one drain: DEPLOY -> LIVE, then CHECK_REVENUE -> FIRST_SALE
        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=100)

        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.entries()[0]["customer_ref"], "buyer@example.test")

        deliver = next(t for t in load_tasks(self.d).all()
                       if t.task_type == "DELIVER")
        self.assertEqual(deliver.input.get("customer_ref"), "buyer@example.test")
        # no DeliveryAdapter is wired in this registry (Phase 12 is out of
        # scope) - the task fails closed on THAT, not on a missing
        # customer_ref, proving the P1-2 gap is actually closed
        self.assertIn(deliver.status, ("FAILED_RETRYABLE", "FAILED_FINAL"))
        self.assertNotIn("no customer reference", deliver.error)
        self.assertIn("delivery BLOCKED", deliver.error)

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", "record_opportunity_payment(",
                          "process_payment_event(", ".record_deployment(",
                          "._by_id", ".add({", "ledger.add(", "FakePaymentAdapter"):
            self.assertNotIn(forbidden, code,
                             f"E2E test must not use {forbidden}")


if __name__ == "__main__":
    unittest.main()
