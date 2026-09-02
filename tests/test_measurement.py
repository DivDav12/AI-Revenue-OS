"""Measurement adapters, traction policy, and the recurring CHECK_* tasks
(Phase 10)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import (
    DEFAULT_TRACTION_POLICY,
    FakeMeasurementAdapter,
    NullMeasurementAdapter,
    TractionPolicy,
    evaluate_traction,
)
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def test_null_is_blocked(self):
        s = NullMeasurementAdapter().measure(kind="traffic", opportunity_id="o")
        self.assertFalse(s.ok)
        self.assertTrue(s.blocked)

    def test_fake_fixed_and_sequence(self):
        a = FakeMeasurementAdapter(traffic={"visitors": 5})
        self.assertEqual(a.measure(kind="traffic", opportunity_id="o").metrics,
                         {"visitors": 5})
        b = FakeMeasurementAdapter(traffic=[{"visitors": 0}, {"visitors": 9}])
        self.assertEqual(b.measure(kind="traffic", opportunity_id="o").metrics["visitors"], 0)
        self.assertEqual(b.measure(kind="traffic", opportunity_id="o").metrics["visitors"], 9)
        self.assertEqual(b.measure(kind="traffic", opportunity_id="o").metrics["visitors"], 9)

    def test_fake_fail_and_blocked(self):
        self.assertFalse(FakeMeasurementAdapter(fail=True).measure(
            kind="leads", opportunity_id="o").ok)
        self.assertTrue(FakeMeasurementAdapter(blocked=True).measure(
            kind="leads", opportunity_id="o").blocked)


# ---------------------------------------------------------------------------
# traction policy
# ---------------------------------------------------------------------------

class TractionTests(unittest.TestCase):
    def _opp(self, *, state="MEASURING", traffic=None, leads=None, revenue=0.0,
             ts_span_days=0):
        series = []
        t0 = BASE
        for i, v in enumerate(traffic or []):
            series.append({"ts": _iso(t0 + timedelta(days=i * ts_span_days)),
                           "kind": "traffic", "cycle": i, "metrics": {"visitors": v}})
        for i, v in enumerate(leads or []):
            series.append({"ts": _iso(t0 + timedelta(days=i * ts_span_days)),
                           "kind": "leads", "cycle": i, "metrics": {"leads": v}})
        return {"state": state,
                "execution": {"measurement_series": series,
                              "metrics": {"revenue": {"revenue_eur": revenue}}}}

    def test_not_enough_rounds(self):
        opp = self._opp(traffic=[0, 0])
        self.assertFalse(evaluate_traction(opp, now=_iso(BASE)).no_traction)

    def test_conversion_blocks_no_traction(self):
        opp = self._opp(traffic=[0] * 10, leads=[1])
        self.assertFalse(evaluate_traction(opp, now=_iso(BASE)).no_traction)
        opp2 = self._opp(traffic=[0] * 10, revenue=5.0)
        self.assertFalse(evaluate_traction(opp2, now=_iso(BASE)).no_traction)

    def test_needs_a_believable_basis(self):
        pol = TractionPolicy(min_cycles=8, min_wall_seconds=2 * 86400,
                             min_visitors=40)
        # 10 rounds but same-instant timestamps and few visitors -> no basis
        opp = self._opp(traffic=[1] * 10, ts_span_days=0)
        self.assertFalse(evaluate_traction(opp, now=_iso(BASE), policy=pol).no_traction)
        # spread over 3 days -> enough basis, zero conversion -> NO_TRACTION
        opp2 = self._opp(traffic=[0] * 10, ts_span_days=1)
        v = evaluate_traction(opp2, now=_iso(BASE), policy=pol)
        self.assertTrue(v.no_traction)
        self.assertIn("no conversion", v.reason)

    def test_only_from_early_states(self):
        opp = self._opp(state="ACTIVE", traffic=[0] * 12, ts_span_days=1)
        self.assertFalse(evaluate_traction(
            opp, now=_iso(BASE),
            policy=TractionPolicy(min_cycles=3, min_wall_seconds=0)).no_traction)


# ---------------------------------------------------------------------------
# CHECK_* tasks through the real worker
# ---------------------------------------------------------------------------

class MeasurementTaskTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="pack", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.record_deployment(self.oid, {"live_url": "https://x.pages.test/o/index.html",
                                       "provider": "fake"})
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _reg(self, *, traffic=None, leads=None, payments=(), meas_fail=False,
             meas_blocked=False):
        reg = default_registry()
        m = FakeMeasurementAdapter(traffic=traffic, leads=leads,
                                   fail=meas_fail, blocked=meas_blocked)
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=list(payments))))
        return reg, m

    def _seed_checks(self):
        q = load_tasks(self.d)
        for tt in ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            q.create(self.oid, tt, priority=5)
        q.resolve_dependencies()
        q.save()

    def _state(self):
        return load_opportunities(self.d).get(self.oid)["state"]

    def _series(self):
        return load_opportunities(self.d).get(self.oid)["execution"].get(
            "measurement_series", [])

    def test_traffic_measurement_moves_live_to_measuring_then_first_visitor(self):
        reg, m = self._reg(traffic=[{"visitors": 0}, {"visitors": 14}],
                           leads={"leads": 0})
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE), max_ticks=30)
        self.assertEqual(self._state(), "MEASURING")

        Worker(self.d, registry=reg, name="w").run(
            now=_iso(BASE + timedelta(hours=7)), max_ticks=30)
        self.assertEqual(self._state(), "FIRST_VISITOR")
        evs = [e for e in load_events(self.d).all()]
        self.assertEqual(
            len([e for e in evs if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "MEASURING"]), 1)
        self.assertEqual(
            len([e for e in evs if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "FIRST_VISITOR"]), 1)
        self.assertGreaterEqual(
            len([e for e in evs if e["type"] == "MEASUREMENT_RECORDED"]), 4)

    def test_lead_measurement_moves_to_first_lead(self):
        reg, m = self._reg(traffic={"visitors": 3},
                           leads=[{"leads": 0}, {"leads": 4}])
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE))
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE + timedelta(hours=7)))
        self.assertIn(self._state(), ("FIRST_LEAD",))

    def test_revenue_measurement_records_series_without_state_move(self):
        reg, m = self._reg(traffic={"visitors": 0}, leads={"leads": 0}, payments=[])
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE))
        rev = [s for s in self._series() if s["kind"] == "revenue"]
        self.assertTrue(rev)
        self.assertEqual(rev[0]["metrics"]["revenue_eur"], 0)
        # CHECK_REVENUE alone does not move LIVE->MEASURING (only traffic/leads do)
        # but the traffic check in this same drain does:
        self.assertEqual(self._state(), "MEASURING")

    def test_recurrence_one_live_task_per_type_no_explosion(self):
        reg, m = self._reg(traffic={"visitors": 0}, leads={"leads": 0})
        self._seed_checks()
        for h in range(0, 40, 7):
            Worker(self.d, registry=reg, name="w").run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=30)
        q = load_tasks(self.d)
        for tt in ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            live = [t for t in q.by_opportunity(self.oid)
                    if t.task_type == tt and not t.is_terminal]
            self.assertLessEqual(len(live), 1, tt)
        # it did run repeatedly
        self.assertGreaterEqual(m.calls.count(("traffic", self.oid)), 3)

    def test_scheduled_successor_is_not_ready_before_its_time(self):
        reg, m = self._reg(traffic={"visitors": 0}, leads={"leads": 0})
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE))
        n1 = m.calls.count(("traffic", self.oid))
        # only 1 hour later - successors are scheduled 6h out
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE + timedelta(hours=1)))
        self.assertEqual(m.calls.count(("traffic", self.oid)), n1)

    def test_retry_does_not_double_count(self):
        # provider blocked once (non-retryable) -> FAILED_FINAL, still recurs
        reg, m = self._reg(traffic={"visitors": 0}, leads={"leads": 0},
                           meas_blocked=True)
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE), max_ticks=30)
        traffic_series = [s for s in self._series() if s["kind"] == "traffic"]
        self.assertEqual(len(traffic_series), 0)     # blocked -> nothing recorded
        # a successor was still scheduled
        q = load_tasks(self.d)
        pend = [t for t in q.by_opportunity(self.oid)
                if t.task_type == "CHECK_TRAFFIC" and not t.is_terminal]
        self.assertEqual(len(pend), 1)

    def test_restart_keeps_series_and_does_not_recount(self):
        reg, m = self._reg(traffic=[{"visitors": 5}], leads={"leads": 0})
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE))
        series_len = len(self._series())
        # brand-new worker + adapters from disk
        reg2, _ = self._reg(traffic=[{"visitors": 5}], leads={"leads": 0})
        Worker(self.d, registry=reg2, name="w2").run(now=_iso(BASE + timedelta(minutes=1)))
        # the successors are not due yet -> series unchanged
        self.assertEqual(len(self._series()), series_len)

    def test_duplicate_payment_no_second_first_sale_or_deliver(self):
        pay = [PaymentEvent(reference="CAP-1", amount=29.0, currency="EUR",
                            opportunity_id=self.oid, customer_ref="b@example.test",
                            provider="fake")]
        reg, m = self._reg(traffic={"visitors": 1}, leads={"leads": 0}, payments=pay)
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE), max_ticks=40)
        st1 = self._state()
        self.assertEqual(st1, "FIRST_SALE")
        # recurring CHECK_REVENUE over the same settled payment
        for h in (7, 14, 21):
            Worker(self.d, registry=reg, name="w").run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=40)
        from revenue_os.revenue import RevenueLedger
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.0)
        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("REVENUE_RECORDED"), 1)
        self.assertEqual(evs.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "FIRST_SALE"]), 1)
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]), 1)

    def test_no_traction_after_a_real_basis_then_recurrence_stops(self):
        pol = TractionPolicy(min_cycles=3, min_wall_seconds=0, min_visitors=10 ** 9)
        reg, m = self._reg(traffic={"visitors": 0}, leads={"leads": 0})
        self._seed_checks()
        for h in range(0, 40, 7):
            Worker(self.d, registry=reg, name="w", traction_policy=pol).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=30)
        self.assertEqual(self._state(), "NO_TRACTION")
        nt = [e for e in load_events(self.d).all()
              if e["type"] == "OPPORTUNITY_TRANSITIONED"
              and e["data"].get("to") == "NO_TRACTION"]
        self.assertEqual(len(nt), 1)
        # recurrence stopped - no live measurement task remains
        q = load_tasks(self.d)
        self.assertFalse(any(not t.is_terminal and t.task_type in
                             ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE")
                             for t in q.by_opportunity(self.oid)))

    def test_advanced_state_is_not_regressed_by_measurement(self):
        # opportunity already at ACTIVE; a stray traffic measurement must not
        # pull it back to MEASURING
        s = load_opportunities(self.d)
        for st in ("MEASURING", "FIRST_SALE", "DELIVERING", "ACTIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.save()
        reg, m = self._reg(traffic={"visitors": 50}, leads={"leads": 5})
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE), max_ticks=30)
        self.assertEqual(self._state(), "ACTIVE")

    def test_invalid_adapter_metrics_are_coerced_not_crashing(self):
        reg, m = self._reg(traffic={"visitors": "not-a-number", "clicks": None},
                           leads={"leads": 0})
        self._seed_checks()
        Worker(self.d, registry=reg, name="w").run(now=_iso(BASE), max_ticks=30)
        traffic = [s for s in self._series() if s["kind"] == "traffic"][0]
        self.assertEqual(traffic["metrics"]["visitors"], 0.0)
        self.assertEqual(self._state(), "MEASURING")     # no FIRST_VISITOR on 0

    def test_no_phase_9_or_14_automation(self):
        reg, m = self._reg(traffic={"visitors": 30}, leads={"leads": 3})
        self._seed_checks()
        for h in range(0, 30, 7):
            Worker(self.d, registry=reg, name="w").run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=30)
        types = {t.task_type for t in load_tasks(self.d).all()}
        self.assertNotIn("OPTIMIZE", types)           # no Phase 14
        self.assertNotIn("SPAWN_VARIANT", types)
        # DISTRIBUTE exists only as the acceptance.CHAIN placeholder, never
        # auto-run to success (no Phase 9 automation)
        for t in load_tasks(self.d).all():
            if t.task_type == "DISTRIBUTE":
                self.assertNotEqual(t.status, "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
