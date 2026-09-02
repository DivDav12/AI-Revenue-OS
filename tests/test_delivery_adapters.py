"""Delivery adapters + DELIVER task (Phase 12)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.delivery_adapters import (
    DeliveryArtifact,
    DeliveryRecipient,
    FakeDeliveryAdapter,
    NullDeliveryAdapter,
    SmtpDeliveryAdapter,
)
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.task_adapters import (
    CheckRevenueAdapter,
    DeliverTaskAdapter,
    default_registry,
)
from revenue_os.worker import Worker


def _art(oid="opp_1", name="Cold-email pack"):
    return DeliveryArtifact(opportunity_id=oid, product_name=name,
                            live_url="https://x.pages.test/opp/index.html")


def _rcpt(ref="buyer@example.test", oid="opp_1"):
    return DeliveryRecipient(reference=ref, opportunity_id=oid)


class AdapterUnitTests(unittest.TestCase):
    def test_fake_success(self):
        r = FakeDeliveryAdapter().deliver(_art(), _rcpt())
        self.assertTrue(r.success)
        self.assertEqual(r.provider, "fake")
        self.assertTrue(r.delivery_id and r.reference)
        self.assertEqual(r.recipient, "buyer@example.test")

    def test_fake_failure(self):
        r = FakeDeliveryAdapter(fail=True, error="bounced").deliver(_art(), _rcpt())
        self.assertFalse(r.success)
        self.assertFalse(r.blocked)
        self.assertIn("bounced", r.error)

    def test_fake_blocked(self):
        r = FakeDeliveryAdapter(blocked=True).deliver(_art(), _rcpt())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)

    def test_fake_needs_recipient(self):
        r = FakeDeliveryAdapter().deliver(_art(), _rcpt(ref=""))
        self.assertFalse(r.success)

    def test_fake_duplicate_is_suppressed(self):
        a = FakeDeliveryAdapter()
        r1 = a.deliver(_art(), _rcpt())
        r2 = a.deliver(_art(), _rcpt())
        self.assertEqual(a.calls, 2)
        self.assertEqual(r1.delivery_id, r2.delivery_id)
        self.assertTrue(r2.details.get("duplicate_suppressed"))

    def test_null_adapter_is_blocked(self):
        r = NullDeliveryAdapter().deliver(_art(), _rcpt())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)

    def test_smtp_adapter_fail_closed_without_config(self):
        r = SmtpDeliveryAdapter(environ={}).deliver(_art(), _rcpt())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertIn("SMTP", r.error)

    def test_smtp_adapter_uses_injected_mailer_never_real_smtp(self):
        from revenue_os.delivery import EmailConfig
        sent = {}

        def _fake_mailer(cfg, msg):
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]
            return "<fake-message-id@test>"

        cfg = EmailConfig(host="h", user="u", password="p", sender="from@test")
        r = SmtpDeliveryAdapter(config=cfg, mailer=_fake_mailer).deliver(
            _art(), _rcpt())
        self.assertTrue(r.success)
        self.assertEqual(r.reference, "<fake-message-id@test>")
        self.assertEqual(sent["to"], "buyer@example.test")


class DeliverTaskThroughWorkerTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="Cold-email pack", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        # a confirmed deployment so the DELIVER artifact has a live_url
        s.record_deployment(self.oid, {"live_url": "https://x.pages.test/o/index.html",
                                       "provider": "fake"})
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _pay_and_deliver(self, delivery_adapter, *, customer="buyer@example.test",
                         amount=29.0, ref="CAP-1"):
        q = load_tasks(self.d)
        q.create(self.oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[PaymentEvent(
            reference=ref, amount=amount, currency="EUR",
            opportunity_id=self.oid, customer_ref=customer, provider="fake")])))
        reg.register(DeliverTaskAdapter(delivery_adapter))
        Worker(self.d, registry=reg, name="w").run(max_ticks=30)
        return reg

    def _deliver_task(self):
        return next((t for t in load_tasks(self.d).all()
                     if t.task_type == "DELIVER"), None)

    def test_payment_spawns_exactly_one_deliver_task_then_active(self):
        fake = FakeDeliveryAdapter()
        self._pay_and_deliver(fake)

        delivers = [t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]
        self.assertEqual(len(delivers), 1)
        self.assertEqual(delivers[0].status, "SUCCEEDED")
        self.assertEqual(delivers[0].idempotency_key,
                         f"deliver:{self.oid}:fake:CAP-1")
        self.assertEqual(fake.calls, 1)

        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "ACTIVE")
        seq = [t["next_state"] for t in s["transitions"]]
        # FIRST_SALE -> DELIVERING -> ACTIVE, in order, all from the DELIVER task
        self.assertEqual(seq[-3:], ["FIRST_SALE", "DELIVERING", "ACTIVE"])
        self.assertEqual(s["transitions"][-1]["source"], "task")
        self.assertEqual(s["execution"]["deliveries"]["fake:CAP-1"]["success"], True)

        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("DELIVERY_COMPLETE"), 1)
        dc = next(e for e in load_events(self.d).all()
                  if e["type"] == "DELIVERY_COMPLETE")
        self.assertEqual(dc["opportunity_id"], self.oid)
        self.assertEqual(dc["data"]["payment_ref"], "fake:CAP-1")

    def test_delivery_failure_does_not_reach_active(self):
        self._pay_and_deliver(FakeDeliveryAdapter(fail=True, error="smtp 550"))
        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "FIRST_SALE")
        self.assertNotIn("ACTIVE", {t["next_state"] for t in s["transitions"]})
        self.assertIn(self._deliver_task().status,
                      ("FAILED_RETRYABLE", "FAILED_FINAL"))
        self.assertNotIn("DELIVERY_COMPLETE",
                         [e["type"] for e in load_events(self.d).all()])

    def test_delivery_blocked_does_not_reach_active(self):
        self._pay_and_deliver(NullDeliveryAdapter())
        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "FIRST_SALE")
        self.assertEqual(self._deliver_task().status, "FAILED_FINAL")

    def test_missing_customer_reference_is_non_retryable(self):
        self._pay_and_deliver(FakeDeliveryAdapter(), customer="")
        t = self._deliver_task()
        self.assertEqual(t.status, "FAILED_FINAL")
        self.assertIn("customer reference", t.error)
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"],
                         "FIRST_SALE")

    def test_duplicate_payment_does_not_spawn_a_second_deliver(self):
        fake = FakeDeliveryAdapter()
        reg = self._pay_and_deliver(fake)
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "ACTIVE")

        # a second CHECK_REVENUE task over the SAME payment
        q = load_tasks(self.d)
        cr = next(t for t in q.all() if t.task_type == "CHECK_REVENUE")
        q.create(self.oid, "CHECK_REVENUE", priority=9,
                 depends_on=list(cr.depends_on))
        q.resolve_dependencies()
        q.save()
        Worker(self.d, registry=reg, name="w2").run(max_ticks=30)

        delivers = [t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]
        self.assertEqual(len(delivers), 1)                 # still one
        self.assertEqual(fake.calls, 1)                    # not re-sent
        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("DELIVERY_COMPLETE"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "ACTIVE"]), 1)

    def test_deliver_retry_does_not_double_send(self):
        # provider fails once, then succeeds; the DELIVER task retries
        class _FlakyDelivery(FakeDeliveryAdapter):
            def __init__(self):
                super().__init__()
                self.n = 0

            def deliver(self, artifact, recipient):
                self.n += 1
                if self.n == 1:
                    from revenue_os.delivery_adapters import DeliveryResult
                    return DeliveryResult(success=False, provider="fake",
                                          opportunity_id=artifact.opportunity_id,
                                          error="transient")
                return super().deliver(artifact, recipient)

        flaky = _FlakyDelivery()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        q = load_tasks(self.d)
        q.create(self.oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[PaymentEvent(
            reference="CAP-1", amount=29.0, currency="EUR",
            opportunity_id=self.oid, customer_ref="buyer@example.test",
            provider="fake")])))
        reg.register(DeliverTaskAdapter(flaky))

        Worker(self.d, registry=reg, name="w").run(now=base.isoformat())
        dt = self._deliver_task()
        self.assertEqual(dt.status, "FAILED_RETRYABLE")
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"],
                         "FIRST_SALE")

        Worker(self.d, registry=reg, name="w").run(
            now=(base + timedelta(hours=2)).isoformat())
        self.assertEqual(self._deliver_task().status, "SUCCEEDED")
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "ACTIVE")
        # delivered exactly once despite the retry
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DELIVERY_COMPLETE"]), 1)

    def test_restart_between_deliver_attempts_does_not_double_send(self):
        fake = FakeDeliveryAdapter()
        self._pay_and_deliver(fake)
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "ACTIVE")

        # "restart": brand-new stores + worker + a fresh delivery adapter,
        # replay the same DELIVER task
        q = load_tasks(self.d)
        dt = self._deliver_task()
        q.create(self.oid, "DELIVER", priority=9,
                 idempotency_key=dt.idempotency_key)   # same key -> same task
        q.save()
        fake2 = FakeDeliveryAdapter()
        reg = default_registry()
        reg.register(DeliverTaskAdapter(fake2))
        Worker(self.d, registry=reg, name="restart").run(max_ticks=20)

        self.assertEqual(fake2.calls, 0)               # recorded delivery -> no send
        delivers = [t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]
        self.assertEqual(len(delivers), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DELIVERY_COMPLETE"]), 1)

    def test_no_smtp_no_real_send(self):
        # the real SMTP adapter, no config -> blocked, nothing sent
        self._pay_and_deliver(SmtpDeliveryAdapter(environ={}))
        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "FIRST_SALE")
        self.assertEqual(self._deliver_task().status, "FAILED_FINAL")
        self.assertIn("BLOCKED", self._deliver_task().error)


class NoPaymentNoDeliverTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="pack", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _run(self, payment_adapter):
        q = load_tasks(self.d)
        q.create(self.oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(payment_adapter))
        reg.register(DeliverTaskAdapter(FakeDeliveryAdapter()))
        Worker(self.d, registry=reg, name="w").run(max_ticks=30)

    def test_no_payment_no_deliver_task(self):
        self._run(FakePaymentAdapter(events=[]))
        self.assertNotIn("DELIVER",
                         {t.task_type for t in load_tasks(self.d).all()})
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "LIVE")

    def test_payment_provider_error_no_deliver_task(self):
        self._run(FakePaymentAdapter(fail=True, error="503"))
        self.assertNotIn("DELIVER",
                         {t.task_type for t in load_tasks(self.d).all()})


if __name__ == "__main__":
    unittest.main()
