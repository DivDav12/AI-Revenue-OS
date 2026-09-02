"""Payment provider abstraction + CHECK_REVENUE -> revenue ledger (Phase 11)."""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.payments import (
    FakePaymentAdapter,
    NullPaymentAdapter,
    PaymentEvent,
    process_payment_event,
)
from revenue_os.revenue import RevenueLedger, record_opportunity_payment
from revenue_os.task_adapters import CheckRevenueAdapter, default_registry
from revenue_os.worker import Worker


def _ev(ref="FAKE-CAP-1", amount=29.0, oid="opp_e2e", **kw):
    return PaymentEvent(reference=ref, amount=amount, currency="EUR",
                        opportunity_id=oid, provider="fake", **kw)


# ---------------------------------------------------------------------------
# ledger path
# ---------------------------------------------------------------------------

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_record_opportunity_payment_is_idempotent_by_ref(self):
        led = RevenueLedger.load(self.d / "revenue.json")
        a = record_opportunity_payment(led, opportunity_id="opp_1", amount=29.0,
                                       ref="fake:CAP1")
        self.assertEqual(a["outcome"], "booked")
        led2 = RevenueLedger.load(self.d / "revenue.json")
        b = record_opportunity_payment(led2, opportunity_id="opp_1", amount=29.0,
                                       ref="fake:CAP1")
        self.assertEqual(b["outcome"], "already_booked")
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.0)
        self.assertEqual(len(RevenueLedger.load(self.d / "revenue.json").entries()), 1)

    def test_rejects_bad_input(self):
        led = RevenueLedger.load(self.d / "revenue.json")
        for bad in (dict(amount=0.0, ref="x"), dict(amount=-5, ref="x"),
                    dict(amount=10, ref=""), dict(amount=10, ref="x",
                                                  opportunity_id="")):
            with self.assertRaises(ValueError):
                record_opportunity_payment(
                    led, opportunity_id=bad.pop("opportunity_id", "opp_1"), **bad)

    def test_it_never_touches_the_candidate_path(self):
        # no candidates.json, no candidate lifecycle - still books fine
        led = RevenueLedger.load(self.d / "revenue.json")
        record_opportunity_payment(led, opportunity_id="opp_x", amount=12.0,
                                   ref="fake:Y")
        self.assertFalse((self.d / "candidates.json").exists())
        self.assertEqual(led.total_for("opp_x"), 12.0)


# ---------------------------------------------------------------------------
# process_payment_event
# ---------------------------------------------------------------------------

class ProcessEventTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self.led_path = Path(self.d) / "revenue.json"

    def tearDown(self):
        self._d.cleanup()

    def test_success(self):
        r = process_payment_event(RevenueLedger.load(self.led_path), _ev())
        self.assertTrue(r.success)
        self.assertFalse(r.already_booked)
        self.assertEqual(r.payment_id, "fake:FAKE-CAP-1")
        self.assertEqual(r.amount, 29.0)

    def test_duplicate_is_already_booked_not_a_new_row(self):
        process_payment_event(RevenueLedger.load(self.led_path), _ev())
        r = process_payment_event(RevenueLedger.load(self.led_path), _ev())
        self.assertTrue(r.success)
        self.assertTrue(r.already_booked)
        self.assertEqual(RevenueLedger.load(self.led_path).total(), 29.0)

    def test_invalid_events_are_rejected(self):
        for ev in (_ev(amount=0.0), _ev(amount=-1.0), _ev(ref=""),
                   _ev(oid="")):
            r = process_payment_event(RevenueLedger.load(self.led_path), ev)
            self.assertFalse(r.success)
        self.assertFalse(self.led_path.exists())


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def test_null_adapter_is_blocked(self):
        r = NullPaymentAdapter().poll(opportunity_id="opp_1")
        self.assertFalse(r.ok)
        self.assertTrue(r.blocked)

    def test_fake_adapter_modes(self):
        self.assertTrue(FakePaymentAdapter(events=[_ev()]).poll(
            opportunity_id="opp_e2e").ok)
        self.assertFalse(FakePaymentAdapter(fail=True).poll(
            opportunity_id="opp_e2e").ok)
        blk = FakePaymentAdapter(blocked=True).poll(opportunity_id="opp_e2e")
        self.assertTrue(blk.blocked)
        # only returns events for the matching opportunity
        got = FakePaymentAdapter(events=[_ev(oid="opp_a"), _ev(ref="B", oid="opp_b")]
                                 ).poll(opportunity_id="opp_a")
        self.assertEqual([e.reference for e in got.events], ["FAKE-CAP-1"])


# ---------------------------------------------------------------------------
# CHECK_REVENUE task through the real worker
# ---------------------------------------------------------------------------

class CheckRevenueTaskTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="pack", category="saas"))["id"]
        # walk to LIVE by legal transitions (test setup only - not the flow
        # under test; the flow under test is CHECK_REVENUE -> FIRST_SALE)
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _run(self, payment_adapter, extra_task=False):
        from revenue_os.execution import load_tasks as _lt
        q = _lt(self.d)
        t = q.create(self.oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(payment_adapter))
        Worker(self.d, registry=reg, name="pay").run(max_ticks=20)
        return t.task_id

    def test_successful_payment_books_revenue_and_first_sale(self):
        fake = FakePaymentAdapter(events=[_ev(oid=self.oid)])
        tid = self._run(fake)

        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(self.oid), 29.0)
        self.assertEqual(len(led.entries()), 1)
        self.assertEqual(led.entries()[0]["ref"], "fake:FAKE-CAP-1")

        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(evs.count("REVENUE_RECORDED"), 1)

        s = load_opportunities(self.d).get(self.oid)
        self.assertEqual(s["state"], "FIRST_SALE")
        first_sale_ev = [e for e in load_events(self.d).all()
                         if e["type"] == "OPPORTUNITY_TRANSITIONED"
                         and e["data"].get("to") == "FIRST_SALE"]
        self.assertEqual(len(first_sale_ev), 1)
        self.assertEqual(load_tasks(self.d).get(tid).status, "SUCCEEDED")

    def test_no_payment_no_first_sale(self):
        self._run(FakePaymentAdapter(events=[]))
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "LIVE")
        self.assertNotIn("REVENUE_RECORDED",
                         [e["type"] for e in load_events(self.d).all()])

    def test_failed_provider_no_revenue(self):
        self._run(FakePaymentAdapter(fail=True, error="503"))
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "LIVE")

    def test_invalid_payment_no_revenue(self):
        self._run(FakePaymentAdapter(events=[_ev(oid=self.oid, amount=0.0)]))
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "LIVE")

    def test_blocked_provider_no_revenue(self):
        self._run(FakePaymentAdapter(blocked=True))
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"], "LIVE")

    def test_duplicate_event_processed_twice_books_once(self):
        fake = FakePaymentAdapter(events=[_ev(oid=self.oid)])
        self._run(fake)                       # first CHECK_REVENUE task
        s1 = load_opportunities(self.d).get(self.oid)["state"]
        self.assertEqual(s1, "FIRST_SALE")

        self._run(fake)                       # a second CHECK_REVENUE task, same event
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total_for(self.oid), 29.0)        # not 58
        self.assertEqual(len(led.entries()), 1)
        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("REVENUE_RECORDED"), 1)     # not re-emitted
        self.assertEqual(evs.count("PAYMENT_DETECTED"), 1)
        fs = [e for e in load_events(self.d).all()
              if e["type"] == "OPPORTUNITY_TRANSITIONED"
              and e["data"].get("to") == "FIRST_SALE"]
        self.assertEqual(len(fs), 1)                           # not re-transitioned
        self.assertEqual(load_opportunities(self.d).get(self.oid)["state"],
                         "FIRST_SALE")

    def test_retry_of_the_same_task_books_once(self):
        # a flaky provider: fails, then on retry returns the payment
        class _Flaky(FakePaymentAdapter):
            def __init__(self, oid):
                super().__init__(events=[_ev(oid=oid)])
                self.n = 0

            def poll(self, *, opportunity_id):
                self.n += 1
                if self.n == 1:
                    from revenue_os.payments import PaymentPollResult
                    return PaymentPollResult(ok=False, provider="fake",
                                             error="transient")
                return super().poll(opportunity_id=opportunity_id)

        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        q = load_tasks(self.d)
        t = q.create(self.oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(_Flaky(self.oid)))
        Worker(self.d, registry=reg, name="pay").run(now=base.isoformat())
        self.assertEqual(load_tasks(self.d).get(t.task_id).status,
                         "FAILED_RETRYABLE")
        Worker(self.d, registry=reg, name="pay").run(
            now=(base + timedelta(hours=2)).isoformat())
        self.assertEqual(load_tasks(self.d).get(t.task_id).status, "SUCCEEDED")
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.0)
        self.assertEqual(len(RevenueLedger.load(self.d / "revenue.json").entries()), 1)

    def test_restart_between_processings_books_once(self):
        fake = FakePaymentAdapter(events=[_ev(oid=self.oid)])
        self._run(fake)
        # "restart": brand new stores/queue/log from disk + a fresh adapter
        fake2 = FakePaymentAdapter(events=[_ev(oid=self.oid)])
        self._run(fake2)
        led = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(led.total(), 29.0)
        self.assertEqual(len(led.entries()), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "REVENUE_RECORDED"]), 1)


class MoneyFirewallTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_incoming_payment_does_not_enable_any_outgoing_spend(self):
        from revenue_os import action_class as ac
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="p", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        q = load_tasks(self.d)
        q.create(oid, "CHECK_REVENUE", priority=5)
        q.resolve_dependencies()
        q.save()
        reg = default_registry()
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[_ev(oid=oid)])))
        Worker(self.d, registry=reg, name="pay").run(max_ticks=20)

        # revenue booked, but the money firewall is unchanged and no spend
        # side-effect files exist
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.0)
        for spent in ("spend.json", "llm_spend.json", "approvals.json"):
            self.assertFalse((self.d / spent).exists(), spent)
        # the classifier still gates outgoing money exactly as before
        self.assertFalse(ac.classify("spend_money").autonomous)
        self.assertFalse(ac.classify("buy_ads").autonomous)
        self.assertTrue(ac.classify("record_real_payment").action_class
                        is ac.ActionClass.MONEY_APPROVAL_REQUIRED)


if __name__ == "__main__":
    unittest.main()
