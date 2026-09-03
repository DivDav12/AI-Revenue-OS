"""Delivery adapters + DELIVER task (Phase 12; autonomy guard: P1-3)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import action_class as ac
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


class SmtpAutonomyGuardTests(unittest.TestCase):
    """Phase 11-real P1-3: SmtpDeliveryAdapter.deliver() is one of the four
    documented autonomous leak paths and must refuse to run inside
    autonomous_context() - even when fully configured and otherwise ready
    to send - and must be a complete no-op outside it."""

    def setUp(self):
        from revenue_os.delivery import EmailConfig

        self._mailer_calls = []

        def _mailer(cfg, msg):
            self._mailer_calls.append((cfg, msg))
            return "<should-never-be-reached@test>"

        self._mailer = _mailer
        self._cfg = EmailConfig(host="h", user="u", password="p", sender="from@test")

    def tearDown(self):
        # never leak a stuck autonomy flag between tests
        ac._local.__dict__.pop("depth", None)

    def _adapter(self) -> SmtpDeliveryAdapter:
        return SmtpDeliveryAdapter(config=self._cfg, mailer=self._mailer)

    def test_blocked_inside_autonomous_context_before_touching_transport(self):
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                self._adapter().deliver(_art(), _rcpt())
        self.assertEqual(self._mailer_calls, [])   # never reached the transport

    def test_unaffected_outside_autonomous_context(self):
        r = self._adapter().deliver(_art(), _rcpt())
        self.assertTrue(r.success)
        self.assertEqual(len(self._mailer_calls), 1)

    def test_missing_config_still_blocks_first_inside_autonomy(self):
        # the guard fires before EmailConfig.from_env() is even attempted,
        # so a real ActionBlocked is raised, not a "blocked, no SMTP" result
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                SmtpDeliveryAdapter(environ={}).deliver(_art(), _rcpt())

    def test_guard_does_not_widen_or_alter_the_firewall(self):
        # the same firewall check other leak paths use, unmodified
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy("send customer e-mail")
        self.assertIsNone(ac.guard_no_money_in_autonomy("send customer e-mail"))


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

    def test_fully_configured_smtp_still_sends_nothing_through_the_real_worker(self):
        """Phase 11-real P1-3: even a fully-configured SmtpDeliveryAdapter
        that WOULD succeed if called directly (see
        SmtpAutonomyGuardTests.test_unaffected_outside_autonomous_context)
        sends nothing when driven by the real Worker, because the worker
        always executes adapters inside autonomous_context() (worker.py
        _execute()) and the guard fires before any SMTP transport is
        touched. This is the concrete proof that wiring this adapter in
        later cannot accidentally enable an autonomous real send."""
        from revenue_os.delivery import EmailConfig

        mailer_calls = []

        def _mailer(cfg, msg):
            mailer_calls.append((cfg, msg))
            return "<should-never-be-reached@test>"

        cfg = EmailConfig(host="h", user="u", password="p", sender="from@test")
        real_adapter = SmtpDeliveryAdapter(config=cfg, mailer=_mailer)

        self._pay_and_deliver(real_adapter)

        self.assertEqual(mailer_calls, [])   # the transport was never reached
        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "FIRST_SALE")          # never reaches ACTIVE
        deliver = self._deliver_task()
        self.assertEqual(deliver.status, "FAILED_FINAL")
        self.assertIn("BLOCKED", deliver.error)
        self.assertNotIn("DELIVERY_COMPLETE",
                         [e["type"] for e in load_events(self.d).all()])


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
