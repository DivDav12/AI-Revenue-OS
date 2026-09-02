"""PHASE 12 - full E2E: a confirmed incoming payment leads, through the
normal execution architecture, to exactly one confirmed delivery and then
to ACTIVE.

  DISCOVER -> ACCEPT -> PLAN -> BUILD -> VALIDATE -> DEPLOY -> LIVE
  -> CHECK_REVENUE -> PAYMENT -> REVENUE -> FIRST_SALE
  -> DELIVER -> DELIVERING -> ACTIVE

Real: opportunity store, TaskQueue, Worker, state machine, EventLog.
Fakes (external systems only): FakeDeploymentAdapter, FakePaymentAdapter,
FakeDeliveryAdapter. No GitHub, no PayPal, no SMTP, no network, no money.

The test never sets a status, never calls the state machine / ledger
directly, never bypasses the worker / queue / approval gate.
"""

import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, execution_view, release_task
from revenue_os.delivery_adapters import FakeDeliveryAdapter, NullDeliveryAdapter
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import load_opportunities
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.revenue import RevenueLedger
from revenue_os.task_adapters import (
    CheckRevenueAdapter,
    DeliverTaskAdapter,
    DeployTaskAdapter,
    default_registry,
)
from revenue_os.worker import Worker


class Phase12E2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _registry(self, payment_adapter, delivery):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        reg.register(CheckRevenueAdapter(payment_adapter))
        reg.register(DeliverTaskAdapter(delivery))
        return reg

    def _discover_accept(self):
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, OID, actor="founder")
        return OID

    def _go_live(self, reg, OID):
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        deploy_id = next(t.task_id for t in load_tasks(self.d).by_opportunity(OID)
                         if t.task_type == "DEPLOY")
        release_task(self.d, deploy_id, actor="founder")

    # -----------------------------------------------------------------
    def test_payment_to_confirmed_delivery_to_active(self):
        OID = self._discover_accept()
        deliver = FakeDeliveryAdapter()
        pay = [PaymentEvent(reference="FAKE-CAP-1", amount=29.0, currency="EUR",
                            opportunity_id=OID, customer_ref="buyer@example.test",
                            provider="fake")]
        reg = self._registry(FakePaymentAdapter(events=pay), deliver)

        self._go_live(reg, OID)
        seq_before = load_events(self.d).last_seq()
        # one drain: DEPLOY -> LIVE -> CHECK_REVENUE -> FIRST_SALE
        #            -> DELIVER -> DELIVERING -> ACTIVE
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)

        # ---- revenue booked once -----------------------------------
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(OID), 29.0)
        self.assertEqual(len(led.entries()), 1)

        after = [e for e in load_events(self.d).all() if e["seq"] > seq_before]
        types = [e["type"] for e in after]

        # ---- exactly one of each critical event, in order ----------
        self.assertEqual(types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(types.count("REVENUE_RECORDED"), 1)
        self.assertEqual(types.count("DELIVERY_COMPLETE"), 1)

        def seq_of(pred):
            return next(e["seq"] for e in after if pred(e))

        rr = seq_of(lambda e: e["type"] == "REVENUE_RECORDED")
        fs = seq_of(lambda e: e["type"] == "OPPORTUNITY_TRANSITIONED"
                    and e["data"].get("to") == "FIRST_SALE")
        dcreate = seq_of(lambda e: e["type"] == "TASK_CREATED"
                         and e["task_type"] == "DELIVER")
        dc = seq_of(lambda e: e["type"] == "DELIVERY_COMPLETE")
        dv = seq_of(lambda e: e["type"] == "OPPORTUNITY_TRANSITIONED"
                    and e["data"].get("to") == "DELIVERING")
        ac = seq_of(lambda e: e["type"] == "OPPORTUNITY_TRANSITIONED"
                    and e["data"].get("to") == "ACTIVE")
        self.assertLess(rr, fs)
        self.assertLess(fs, dc)          # FIRST_SALE before delivery completes
        self.assertLess(rr, dcreate)     # DELIVER task created after the payment
        self.assertLess(dc, dv)          # DELIVERING only after confirmed delivery
        self.assertLess(dv, ac)          # then ACTIVE

        # ---- exactly one DELIVER task, SUCCEEDED, sent once --------
        delivers = [t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]
        self.assertEqual(len(delivers), 1)
        self.assertEqual(delivers[0].status, "SUCCEEDED")
        self.assertEqual(delivers[0].opportunity_id, OID)
        self.assertEqual(delivers[0].idempotency_key, f"deliver:{OID}:fake:FAKE-CAP-1")
        self.assertEqual(deliver.calls, 1)

        # ---- state: FIRST_SALE -> DELIVERING -> ACTIVE via the machine
        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "ACTIVE")
        tail = [t["next_state"] for t in s["transitions"]][-3:]
        self.assertEqual(tail, ["FIRST_SALE", "DELIVERING", "ACTIVE"])
        for t in s["transitions"][-2:]:
            self.assertEqual(t["source"], "task")
        self.assertEqual(s["execution"]["deliveries"]["fake:FAKE-CAP-1"]["success"],
                         True)
        self.assertEqual(s["execution"]["deliveries"]["fake:FAKE-CAP-1"]["recipient"],
                         "buyer@example.test")

        row = execution_view(self.d, OID)[0]
        self.assertEqual(row["state"], "ACTIVE")

        # ---- idempotency: replay the payment + restart -------------
        q = load_tasks(self.d)
        cr = next(t for t in q.all() if t.task_type == "CHECK_REVENUE")
        q.create(OID, "CHECK_REVENUE", priority=9, depends_on=list(cr.depends_on))
        q.resolve_dependencies()
        q.save()
        # brand-new adapters + worker (restart)
        reg2 = self._registry(
            FakePaymentAdapter(events=[PaymentEvent(
                reference="FAKE-CAP-1", amount=29.0, currency="EUR",
                opportunity_id=OID, customer_ref="buyer@example.test",
                provider="fake")]),
            FakeDeliveryAdapter())
        Worker(self.d, registry=reg2, name="e2e-restart").run(max_ticks=100)

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total(), 29.0)                       # not 58
        self.assertEqual(len(led.entries()), 1)
        all_types = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(all_types.count("REVENUE_RECORDED"), 1)
        self.assertEqual(all_types.count("DELIVERY_COMPLETE"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "ACTIVE"]), 1)
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]),
            1)
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "ACTIVE")

        # ---- Phase 12 ENDS at ACTIVE ------------------------------
        seen = {t["next_state"] for t in load_opportunities(self.d).get(OID)["transitions"]}
        for later in ("PROFITABLE", "SCALING"):
            self.assertNotIn(later, seen)
        self.assertNotIn("OPTIMIZE", {t.task_type for t in load_tasks(self.d).all()})

        # ---- no external side effects, id consistent -------------
        for artefact in ("llm_spend.json", "spend.json", "messages.json",
                         "deliveries.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        self.assertTrue(all(t.opportunity_id == OID
                            for t in load_tasks(self.d).all()))
        seqs = [e["seq"] for e in load_events(self.d).all()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))     # monotonic

    # ---- negatives ------------------------------------------------
    def _run_full(self, payment_adapter, delivery):
        OID = self._discover_accept()
        reg = self._registry(payment_adapter, delivery)
        self._go_live(reg, OID)
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        return OID

    def test_no_payment_no_deliver_not_active(self):
        OID = self._run_full(FakePaymentAdapter(events=[]), FakeDeliveryAdapter())
        self.assertEqual(load_opportunities(self.d).get(OID)["state"], "LIVE")
        self.assertNotIn("DELIVER", {t.task_type for t in load_tasks(self.d).all()})
        self.assertFalse((self.d / "revenue.json").exists())

    def test_delivery_failure_stays_first_sale(self):
        OID = self._discover_accept()
        pay = [PaymentEvent(reference="C1", amount=29.0, currency="EUR",
                            opportunity_id=OID, customer_ref="b@example.test",
                            provider="fake")]
        reg = self._registry(FakePaymentAdapter(events=pay),
                             FakeDeliveryAdapter(fail=True, error="550"))
        self._go_live(reg, OID)
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)

        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_SALE")
        self.assertNotIn("ACTIVE", {t["next_state"] for t in s["transitions"]})
        self.assertNotIn("DELIVERY_COMPLETE",
                         [e["type"] for e in load_events(self.d).all()])
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total_for(OID),
                         29.0)

    def test_delivery_blocked_stays_first_sale(self):
        OID = self._discover_accept()
        pay = [PaymentEvent(reference="C1", amount=29.0, currency="EUR",
                            opportunity_id=OID, customer_ref="b@example.test",
                            provider="fake")]
        reg = self._registry(FakePaymentAdapter(events=pay), NullDeliveryAdapter())
        self._go_live(reg, OID)
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_SALE")
        dt = next(t for t in load_tasks(self.d).all() if t.task_type == "DELIVER")
        self.assertEqual(dt.status, "FAILED_FINAL")

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", "record_opportunity_payment(",
                          "process_payment_event(", ".record_deployment(",
                          ".record_delivery(", "._by_id", "ledger.add(",
                          ".deliver("):
            self.assertNotIn(forbidden, code,
                             f"E2E test must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
