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

import os
import re
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


class _CapturingDeploy(FakeDeploymentAdapter):
    """Same fake external boundary as everywhere else in this file - just
    also remembers the exact files it was asked to publish, so a test can
    inspect the ACTUAL generated page rather than recomputing an expected
    value independently."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.last_files: dict = {}

    def deploy(self, artifact):
        self.last_files = dict(artifact.files)
        return super().deploy(artifact)


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
        # Phase 11-real P1-5: DEPLOY now builds a real checkout and requires
        # a real, live PayPal configuration - a fake-but-valid one here.
        self._old_env = {k: os.environ.get(k) for k in
                         ("PAYPAL_CLIENT_ID", "PAYPAL_ENV")}
        os.environ["PAYPAL_CLIENT_ID"] = "test-client-id"
        os.environ["PAYPAL_ENV"] = "live"

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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

    def test_deploy_produces_a_payable_page_attributable_without_any_manual_checkout_step(self):
        """Phase 11-real P1-5: DISCOVER -> ACCEPT -> PLAN -> BUILD -> VALIDATE
        -> DEPLOY, through the real architecture, with NO
        build-opportunity-checkout / deploy-opportunity-checkout CLI step
        anywhere. DEPLOY itself must publish a real, payable checkout page
        (Option A - it IS index.html), and a synthetic PayPal transaction
        matching EXACTLY what that real page contains must be attributable
        by the real, unmodified PayPalPaymentAdapter, reaching FIRST_SALE."""
        OID = self._discover_accept()
        capturing_deploy = _CapturingDeploy(base_url="https://e2e-real.pages.test")
        reg = self._registry(PayPalPaymentAdapter(self.d, client=_StubPayPalClient([])),
                             deploy=capturing_deploy)

        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=100)   # -> VALIDATE
        self.assertNotEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")

        self._release_deploy(OID)
        Worker(self.d, registry=reg, name="e2e-real").run(max_ticks=100)   # DEPLOY -> LIVE

        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "LIVE")
        live_url = s["execution"]["live_url"]
        self.assertTrue(live_url.startswith("https://e2e-real.pages.test/"))

        # the ACTUAL published page - not independently recomputed
        self.assertEqual(set(capturing_deploy.last_files), {"index.html"})
        published_html = capturing_deploy.last_files["index.html"]
        self.assertIn("paypal.com/sdk/js", published_html)   # a real checkout, not the old placeholder
        self.assertNotIn("disabled>", published_html)         # no inert waitlist button

        m = re.search(
            r"actions\.order\.create\(\{.*?amount:\s*\{\s*value:\s*\"([\d.]+)\","
            r"\s*currency_code:\s*\"([A-Z]{3})\"\s*\},\s*custom_id:\s*\"([^\"]+)\"",
            published_html, re.S)
        self.assertIsNotNone(m, "no PayPal order.create payload found on the deployed page")
        page_amount, page_currency, page_custom_id = m.groups()
        self.assertEqual(page_custom_id, OID)
        self.assertRegex(page_custom_id, r"^opp_[0-9a-f]{12}$")

        frozen_price, frozen_currency = self._frozen_offer(OID)
        self.assertEqual(round(float(page_amount), 2), round(float(frozen_price), 2))
        self.assertEqual(page_currency, frozen_currency)

        # now attribute a synthetic PayPal transaction matching EXACTLY the
        # deployed page's own payload - through the real, unmodified adapter
        stub = _StubPayPalClient([_txn(page_custom_id, float(page_amount), page_currency)])
        reg2 = self._registry(PayPalPaymentAdapter(self.d, client=stub),
                              deploy=capturing_deploy)
        q = load_tasks(self.d)
        q.create(OID, "CHECK_REVENUE", priority=9)
        q.resolve_dependencies()
        q.save()
        Worker(self.d, registry=reg2, name="e2e-real").run(max_ticks=50)

        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(OID), round(float(page_amount), 2))
        self.assertEqual(len(led.entries()), 1)

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
