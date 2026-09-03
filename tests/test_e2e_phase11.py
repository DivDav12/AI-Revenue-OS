"""PHASE 11 - targeted E2E: LIVE -> CHECK_REVENUE -> FIRST_SALE.

Continues the Phase-18 proof one step: a confirmed incoming payment,
processed through the SAME execution architecture (TaskQueue -> Worker ->
adapter -> result -> EventLog -> state machine), books revenue exactly
once and moves the opportunity to FIRST_SALE.

Phase 11 ends at FIRST_SALE. No DELIVER task, no delivery, no ACTIVE, no
OPTIMIZE - those are later phases.

The ONLY fakes are external systems: FakeDeploymentAdapter (no GitHub) and
FakePaymentAdapter (no PayPal, no network, no real money). The test never
sets a status, never calls the state machine or the ledger directly, never
bypasses the worker / queue / approval gate.
"""

import os
import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import load_opportunities
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.revenue import RevenueLedger
from revenue_os.task_adapters import CheckRevenueAdapter, DeployTaskAdapter, default_registry
from revenue_os.worker import Worker


class Phase11E2ETests(unittest.TestCase):
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
            base_url="https://e2e.pages.test")))
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

    def test_live_to_first_sale_through_the_real_architecture(self):
        OID = self._discover_accept()
        # a confirmed incoming payment the provider has already settled,
        # attributed to this opportunity
        pay = [PaymentEvent(reference="FAKE-CAP-1", amount=29.0, currency="EUR",
                            opportunity_id=OID, customer_ref="buyer@example.test",
                            provider="fake")]
        reg = self._registry(FakePaymentAdapter(events=pay))

        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)   # -> VALIDATE
        self.assertNotEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")

        seq_before_deploy = load_events(self.d).last_seq()
        self._release_deploy(OID)
        # one drain: DEPLOY -> LIVE, then CHECK_REVENUE -> FIRST_SALE
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)

        # LIVE happened before CHECK_REVENUE's revenue events (real ordering)
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

        seq_at_live = seq_before_deploy

        # ---- A/B: the payment was processed and revenue booked --------
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(OID), 29.0)
        self.assertEqual(len(led.entries()), 1)
        entry = led.entries()[0]
        self.assertEqual(entry["ref"], "fake:FAKE-CAP-1")       # F: persistent ref
        self.assertEqual(entry["opportunity_id"], OID)
        self.assertEqual(entry["customer_ref"], "buyer@example.test")

        new_events = [e for e in load_events(self.d).all()
                      if e["seq"] > seq_at_live]
        types = [e["type"] for e in new_events]
        # ---- C/D: exactly one PAYMENT_DETECTED and one REVENUE_RECORDED
        self.assertEqual(types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(types.count("REVENUE_RECORDED"), 1)
        rr = next(e for e in new_events if e["type"] == "REVENUE_RECORDED")
        self.assertEqual(rr["opportunity_id"], OID)
        self.assertEqual(rr["data"]["reference"], "FAKE-CAP-1")
        self.assertEqual(rr["data"]["amount"], 29.0)

        # ---- E: FIRST_SALE, via the state machine, exactly once --------
        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_SALE")
        fs = [e for e in new_events
              if e["type"] == "OPPORTUNITY_TRANSITIONED"
              and e["data"].get("to") == "FIRST_SALE"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["data"]["from"], "LIVE")
        self.assertEqual(fs[0]["opportunity_id"], OID)
        self.assertEqual(s["transitions"][-1]["source"], "task")

        cr = next(t for t in load_tasks(self.d).all()
                  if t.task_type == "CHECK_REVENUE")
        self.assertEqual(cr.status, "SUCCEEDED")
        self.assertTrue(cr.output["first_sale"])

        # ---- G/H: idempotent across a fresh CHECK_REVENUE + restart ----
        q = load_tasks(self.d)
        cr2 = q.create(OID, "CHECK_REVENUE", priority=9,
                       depends_on=list(cr.depends_on))
        q.resolve_dependencies()
        q.save()
        # brand-new worker + registry instances (restart)
        reg2 = self._registry(FakePaymentAdapter(events=[
            PaymentEvent(reference="FAKE-CAP-1", amount=29.0, currency="EUR",
                         opportunity_id=OID, provider="fake")]))
        Worker(self.d, registry=reg2, name="e2e-restarted").run(max_ticks=50)

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total(), 29.0)                     # still 29, not 58
        self.assertEqual(len(led.entries()), 1)
        all_types = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(all_types.count("REVENUE_RECORDED"), 1)
        self.assertEqual(all_types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "FIRST_SALE"]), 1)
        self.assertEqual(load_tasks(self.d).get(cr2.task_id).status, "SUCCEEDED")
        self.assertTrue(load_tasks(self.d).get(cr2.task_id).output.get("payments"))
        self.assertFalse(load_tasks(self.d).get(cr2.task_id).output.get("first_sale"))

        # ---- the payment leg stops at FIRST_SALE ---------------------
        # (Phase 12 spawns a DELIVER task on the confirmed payment, but with
        #  no delivery provider configured here it cannot complete, so the
        #  opportunity correctly does NOT advance past FIRST_SALE.)
        state = load_opportunities(self.d).get(OID)["state"]
        self.assertEqual(state, "FIRST_SALE")
        seen = {t["next_state"] for t in load_opportunities(self.d).get(OID)["transitions"]}
        for later in ("DELIVERING", "ACTIVE", "PROFITABLE"):
            self.assertNotIn(later, seen)
        for dt in [t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]:
            self.assertNotEqual(dt.status, "SUCCEEDED")
        self.assertNotIn("DELIVERY_COMPLETE",
                         [e["type"] for e in load_events(self.d).all()])
        self.assertNotIn("OPTIMIZE", {t.task_type for t in load_tasks(self.d).all()})

        # ---- O: no external side effects, ids consistent -------------
        for artefact in ("llm_spend.json", "spend.json", "messages.json",
                         "deliveries.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        self.assertTrue(all(t.opportunity_id == OID
                            for t in load_tasks(self.d).all()))

    def _run_to_payment_check(self, payment_adapter):
        OID = self._discover_accept()
        reg = self._registry(payment_adapter)
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        self._release_deploy(OID)
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        return OID

    def test_no_payment_no_first_sale_no_revenue(self):
        OID = self._run_to_payment_check(FakePaymentAdapter(events=[]))
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertNotIn("REVENUE_RECORDED",
                         [e["type"] for e in load_events(self.d).all()])

    def test_failed_payment_provider_no_revenue_no_first_sale(self):
        OID = self._run_to_payment_check(FakePaymentAdapter(fail=True, error="pp 503"))
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")
        self.assertFalse((self.d / "revenue.json").exists())

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", "record_opportunity_payment(",
                          "process_payment_event(", ".record_deployment(",
                          "._by_id", ".add({", "ledger.add("):
            self.assertNotIn(forbidden, code,
                             f"E2E test must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
