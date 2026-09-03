"""PHASE 11-REAL P1-7 - targeted E2E: FIRST_SALE -> real product delivery.

Continues test_e2e_phase11_real.py's proof one step further: DISCOVER ->
ACCEPT -> PLAN -> BUILD_PRODUCT (a real product.md, P1-6) -> ... ->
DEPLOY (a real, live payable checkout, P1-5) -> a real, PayPal-attributed
FIRST_SALE (P0-1/P1-1/P1-2) -> `acceptance.deliver_now()` (P1-7) sends
the EXACT product.md BUILD_PRODUCT wrote to the buyer's real email via
the real, unmodified `SmtpDeliveryAdapter` - outside the worker, outside
autonomous_context() - reaching ACTIVE.

The ONLY fakes are external transports: a stub standing in for the
authenticated PayPalClient (no PayPal network), and an injected `mailer`
function standing in for the real SMTP socket (no real email is ever
sent). Attribution, delivery, and the state machine are all real,
unmodified production code.
"""

import os
import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import (
    AcceptanceError,
    accept_opportunity,
    deliver_now,
    release_task,
)
from revenue_os.delivery import EmailConfig
from revenue_os.delivery_adapters import SmtpDeliveryAdapter
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


class Phase12RealE2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
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

    def _registry(self, payment_adapter):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e-p17.pages.test")))
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

    def test_first_sale_to_real_smtp_delivery_reaches_active(self):
        OID = self._discover_accept()
        Worker(self.d, registry=self._registry(
            PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        ), name="e2e-p17").run(max_ticks=100)   # PLAN -> BUILD_PRODUCT -> ... -> VALIDATE

        product_path = self.d / "deliverables" / OID / "product.md"
        self.assertTrue(product_path.is_file())
        product_bytes = product_path.read_bytes()

        price, currency = self._frozen_offer(OID)
        stub = _StubPayPalClient([_txn(OID, price, currency,
                                       payer_email="buyer@example.test")])
        reg = self._registry(PayPalPaymentAdapter(self.d, client=stub))
        self._release_deploy(OID)
        # DEPLOY -> LIVE, then CHECK_REVENUE -> FIRST_SALE (the auto-spawned
        # DELIVER task fails closed here: no default delivery provider)
        Worker(self.d, registry=reg, name="e2e-p17").run(max_ticks=100)

        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        deliver_task = next(t for t in load_tasks(self.d).all()
                            if t.task_type == "DELIVER")
        self.assertEqual(deliver_task.status, "FAILED_FINAL")
        self.assertEqual(deliver_task.input["customer_ref"], "buyer@example.test")

        # --- P1-7: the human-triggered real delivery, outside the worker ---
        sent: dict = {}

        def _mailer(cfg, msg):
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]
            sent["attachments"] = [
                part.get_filename() for part in msg.iter_attachments()]
            sent["attachment_bytes"] = [
                part.get_payload(decode=True) for part in msg.iter_attachments()]
            return "<real-delivery-e2e@test>"

        cfg = EmailConfig(host="h", user="u", password="p", sender="shop@example.test")
        real_adapter = SmtpDeliveryAdapter(config=cfg, mailer=_mailer)

        result = deliver_now(self.d, OID, adapter=real_adapter, actor="founder")

        # ---- the buyer, the product, and the state are all exactly right
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(sent["to"], "buyer@example.test")
        self.assertIn("product.md", sent["attachments"])
        idx = sent["attachments"].index("product.md")
        self.assertEqual(sent["attachment_bytes"][idx], product_bytes)

        rec = load_opportunities(self.d).get(OID)
        self.assertEqual(rec["state"], "ACTIVE")
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(OID), price)

        types = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("DELIVERY_COMPLETE", types)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "ACTIVE"]), 1)

        # idempotent: calling again does not re-send
        result2 = deliver_now(self.d, OID, adapter=real_adapter, actor="founder")
        self.assertEqual(result2["outcome"], "already_delivered")
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DELIVERY_COMPLETE"]), 1)

    def test_no_real_smtp_call_without_explicit_human_trigger(self):
        """The autonomous worker itself never sends, even with a real,
        fully-configured SmtpDeliveryAdapter registered - because it always
        executes inside autonomous_context() (worker.py _execute()) and the
        adapter's own guard (Phase 11-real P1-3) refuses there unconditionally."""
        from revenue_os import action_class as ac
        from revenue_os.task_adapters import DeliverTaskAdapter

        OID = self._discover_accept()
        Worker(self.d, registry=self._registry(
            PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        ), name="e2e-p17").run(max_ticks=100)

        price, currency = self._frozen_offer(OID)
        mailer_calls = []

        def _mailer(cfg, msg):
            mailer_calls.append(msg)
            return "<should-never-be-reached@test>"

        cfg = EmailConfig(host="h", user="u", password="p", sender="shop@example.test")
        reg = self._registry(PayPalPaymentAdapter(
            self.d, client=_StubPayPalClient([_txn(OID, price, currency,
                                                    payer_email="buyer@example.test")])))
        reg.register(DeliverTaskAdapter(SmtpDeliveryAdapter(config=cfg, mailer=_mailer)))
        self._release_deploy(OID)
        Worker(self.d, registry=reg, name="e2e-p17").run(max_ticks=100)

        self.assertEqual(mailer_calls, [])   # never reached the transport
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        deliver_task = next(t for t in load_tasks(self.d).all()
                            if t.task_type == "DELIVER")
        self.assertEqual(deliver_task.status, "FAILED_FINAL")
        self.assertIn("BLOCKED", deliver_task.error)
        ac._local.__dict__.pop("depth", None)   # never leak a stuck context

    def _first_sale_without_buyer_email(self):
        """DISCOVER -> ... -> FIRST_SALE where the PayPal transaction carried
        NO payer email, so the auto-spawned DELIVER task has customer_ref=''."""
        OID = self._discover_accept()
        Worker(self.d, registry=self._registry(
            PayPalPaymentAdapter(self.d, client=_StubPayPalClient([]))
        ), name="e2e-p112").run(max_ticks=100)

        price, currency = self._frozen_offer(OID)
        stub = _StubPayPalClient([_txn(OID, price, currency)])   # no payer_email
        reg = self._registry(PayPalPaymentAdapter(self.d, client=stub))
        self._release_deploy(OID)
        Worker(self.d, registry=reg, name="e2e-p112").run(max_ticks=100)

        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        deliver_task = next(t for t in load_tasks(self.d).all()
                            if t.task_type == "DELIVER")
        self.assertEqual(deliver_task.input.get("customer_ref", ""), "")
        return OID, price

    def _mailer_capture(self, sent: dict):
        def _mailer(cfg, msg):
            sent["to"] = msg["To"]
            sent["attachments"] = [p.get_filename() for p in msg.iter_attachments()]
            return "<p112-e2e@test>"
        return _mailer

    def test_D_no_paypal_email_delivers_with_explicit_override_reaches_active(self):
        OID, price = self._first_sale_without_buyer_email()
        sent: dict = {}
        cfg = EmailConfig(host="h", user="u", password="p", sender="shop@example.test")
        real_adapter = SmtpDeliveryAdapter(config=cfg, mailer=self._mailer_capture(sent))

        result = deliver_now(self.d, OID, adapter=real_adapter,
                             customer_ref="manual-buyer@example.test", actor="founder")

        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["customer_ref_source"], "override")
        self.assertEqual(sent["to"], "manual-buyer@example.test")
        self.assertIn("product.md", sent["attachments"])
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "ACTIVE")

        # idempotent: a second call does not re-send
        result2 = deliver_now(self.d, OID, adapter=real_adapter,
                              customer_ref="manual-buyer@example.test", actor="founder")
        self.assertEqual(result2["outcome"], "already_delivered")
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DELIVERY_COMPLETE"]), 1)

    def test_E_no_paypal_email_and_no_override_fails_closed_stays_first_sale(self):
        OID, _ = self._first_sale_without_buyer_email()
        calls = []

        def _mailer(cfg, msg):
            calls.append(msg)
            return "<never@test>"

        cfg = EmailConfig(host="h", user="u", password="p", sender="shop@example.test")
        real_adapter = SmtpDeliveryAdapter(config=cfg, mailer=_mailer)

        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, OID, adapter=real_adapter, actor="founder")
        self.assertEqual(calls, [])
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "FIRST_SALE")
        rec = load_opportunities(self.d).get(OID)
        self.assertFalse((rec.get("execution") or {}).get("deliveries"))

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", "record_opportunity_payment(",
                          "process_payment_event(", ".record_deployment(",
                          ".record_delivery(", "._by_id", ".add({", "ledger.add(",
                          "FakePaymentAdapter", "NullDeliveryAdapter", "mark_succeeded("):
            self.assertNotIn(forbidden, code,
                             f"E2E test must not use {forbidden}")


if __name__ == "__main__":
    unittest.main()
