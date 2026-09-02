"""Optimization decision + OPTIMIZE task (Phase 14)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import FakeMeasurementAdapter
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.measurement import TractionPolicy
from revenue_os.optimization import (
    DEFAULT_OPTIMIZATION_POLICY,
    FakeOptimizationAdapter,
    NullOptimizationAdapter,
    OptimizationPolicy,
    OptimizationRequest,
    evaluate_optimization,
)

# a traction policy that never declares NO_TRACTION, so the measurement loop
# keeps producing rounds for the optimization decision to act on
_NO_NO_TRACTION = TractionPolicy(min_cycles=10 ** 9)
# a test optimization policy: small basis so tests stay fast + deterministic
_OPT = OptimizationPolicy(min_measurement_rounds=3, min_visitors=10,
                          max_variants=3, cooldown_rounds=3)
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    OptimizeAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _opp(*, state="MEASURING", traffic_rounds=0, visitors_each=0, leads_each=0,
         revenue=0.0, optimizations=None):
    series = []
    for i in range(traffic_rounds):
        series.append({"ts": _iso(BASE + timedelta(days=i)), "kind": "traffic",
                       "cycle": i, "metrics": {"visitors": visitors_each,
                                               "clicks": 0}})
        series.append({"ts": _iso(BASE + timedelta(days=i)), "kind": "leads",
                       "cycle": i, "metrics": {"leads": leads_each}})
    return {"state": state, "title": "Cold-email pack",
            "execution": {"measurement_series": series,
                          "metrics": {"revenue": {"revenue_eur": revenue}},
                          "optimizations": list(optimizations or [])}}


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

class DecisionTests(unittest.TestCase):
    def test_unsuitable_state(self):
        for st in ("LIVE", "BUILDING", "DEPLOYING", "FIRST_SALE", "DELIVERING",
                   "ABANDONED", "SCALING"):
            d = evaluate_optimization(_opp(state=st, traffic_rounds=12,
                                           visitors_each=10))
            self.assertFalse(d.optimize, st)

    def test_insufficient_measurement_basis(self):
        d = evaluate_optimization(_opp(traffic_rounds=3, visitors_each=50))
        self.assertFalse(d.optimize)
        self.assertIn("insufficient measurement basis", d.reason)

    def test_not_enough_traffic_is_a_distribution_problem(self):
        d = evaluate_optimization(_opp(traffic_rounds=10, visitors_each=1))
        self.assertFalse(d.optimize)
        self.assertIn("not enough traffic", d.reason)

    def test_traffic_no_conversion_triggers_landing_copy(self):
        d = evaluate_optimization(_opp(traffic_rounds=10, visitors_each=6,
                                       leads_each=0))
        self.assertTrue(d.optimize)
        self.assertEqual(d.focus, "landing_copy")
        self.assertEqual(d.signal["visitors"], 60)

    def test_leads_no_sales_triggers_pricing(self):
        d = evaluate_optimization(_opp(traffic_rounds=10, visitors_each=6,
                                       leads_each=1, revenue=0.0))
        self.assertTrue(d.optimize)
        self.assertEqual(d.focus, "offer_pricing")

    def test_active_with_revenue_triggers_scale_variant(self):
        d = evaluate_optimization(_opp(state="ACTIVE", traffic_rounds=10,
                                       visitors_each=6, leads_each=1,
                                       revenue=29.0))
        self.assertTrue(d.optimize)
        self.assertEqual(d.focus, "scale_variant")

    def test_no_traction_triggers_offer_landing_copy(self):
        d = evaluate_optimization(_opp(state="NO_TRACTION", traffic_rounds=10,
                                       visitors_each=0))
        self.assertTrue(d.optimize)
        self.assertEqual(d.focus, "offer_landing_copy")

    def test_variant_cap(self):
        opts = [{"variant_id": f"v{i}", "rounds_at_creation": 0} for i in range(3)]
        d = evaluate_optimization(_opp(traffic_rounds=20, visitors_each=6,
                                       optimizations=opts))
        self.assertFalse(d.optimize)
        self.assertIn("variant cap", d.reason)

    def test_cooldown(self):
        opts = [{"variant_id": "v1", "rounds_at_creation": 9}]
        d = evaluate_optimization(_opp(traffic_rounds=10, visitors_each=6,
                                       optimizations=opts))
        self.assertFalse(d.optimize)
        self.assertIn("cooldown", d.reason)


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def _req(self, focus="landing_copy", n=1):
        return OptimizationRequest(opportunity_id="opp_abc", focus=focus,
                                   opportunity={"title": "Pack"},
                                   variant_number=n)

    def test_null_is_blocked(self):
        r = NullOptimizationAdapter().optimize(self._req())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)

    def test_fake_deterministic_variant(self):
        a = FakeOptimizationAdapter()
        r1 = a.optimize(self._req(focus="landing_copy", n=1))
        r2 = FakeOptimizationAdapter().optimize(self._req(focus="landing_copy", n=1))
        self.assertTrue(r1.success)
        self.assertEqual(r1.variant_id, r2.variant_id)
        self.assertEqual(r1.variant_id, "var-opp_abc-01")
        self.assertIn("build_page", r1.requires_before_live)
        self.assertTrue(r1.hypothesis)

    def test_fake_fail_and_blocked(self):
        self.assertFalse(FakeOptimizationAdapter(fail=True).optimize(self._req()).success)
        self.assertTrue(FakeOptimizationAdapter(blocked=True).optimize(self._req()).blocked)


# ---------------------------------------------------------------------------
# OPTIMIZE task through the real worker
# ---------------------------------------------------------------------------

class OptimizeTaskTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="Cold-email pack",
                                        category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.record_deployment(self.oid, {"live_url": "https://x.pages.test/o/index.html",
                                       "provider": "fake"})
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _reg(self, *, opt=None, traffic=None, leads=None, payments=()):
        reg = default_registry()
        m = FakeMeasurementAdapter(traffic=traffic or {"visitors": 8},
                                   leads=leads or {"leads": 0})
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=list(payments))))
        reg.register(OptimizeAdapter(opt or FakeOptimizationAdapter()))
        return reg

    def _seed(self):
        q = load_tasks(self.d)
        for tt in ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            q.create(self.oid, tt, priority=5)
        q.resolve_dependencies()
        q.save()

    def _drive(self, reg, *, hours, opt_policy=_OPT, traction_policy=_NO_NO_TRACTION):
        for h in hours:
            Worker(self.d, registry=reg, name="w",
                   optimization_policy=opt_policy,
                   traction_policy=traction_policy).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=40)

    def _opps(self):
        return load_opportunities(self.d).get(self.oid)["execution"].get(
            "optimizations", [])

    def _state(self):
        return load_opportunities(self.d).get(self.oid)["state"]

    _CYCLES = range(0, 5 * 7, 7)          # 5 cycles: >= _OPT.min (3), < cooldown -> 1 variant

    def test_optimize_task_created_and_variant_recorded(self):
        fake = FakeOptimizationAdapter()
        reg = self._reg(opt=fake, traffic={"visitors": 8}, leads={"leads": 0})
        self._seed()
        self._drive(reg, hours=self._CYCLES)

        opts = self._opps()
        self.assertEqual(len(opts), 1)
        v = opts[0]
        self.assertEqual(v["variant_id"], f"var-{self.oid[:12]}-01")
        self.assertEqual(v["focus"], "landing_copy")
        self.assertIn("deploy_approval", v["requires_before_live"])
        self.assertTrue(v["hypothesis"])
        self.assertEqual(v["task_id"],
                         next(t.task_id for t in load_tasks(self.d).all()
                              if t.task_type == "OPTIMIZE"))

        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("OPTIMIZATION_CREATED"), 1)
        self.assertEqual(evs.count("OPTIMIZATION_COMPLETED"), 1)
        self.assertEqual(fake.calls[0][1], "landing_copy")

    def test_optimize_does_not_move_or_regress_the_state(self):
        reg = self._reg(traffic={"visitors": 20})
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        self.assertEqual(self._state(), "FIRST_VISITOR")   # visitors > 0
        self.assertNotIn(
            "OPTIMIZING",
            {t["next_state"]
             for t in load_opportunities(self.d).get(self.oid)["transitions"]})

    def test_insufficient_data_no_optimize(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=range(0, 2 * 7, 7))         # only 2 cycles < 3
        self.assertEqual(self._opps(), [])
        self.assertNotIn("OPTIMIZE",
                         {t.task_type for t in load_tasks(self.d).all()})

    def test_idempotent_variant_cap_and_cooldown(self):
        fake = FakeOptimizationAdapter()
        reg = self._reg(opt=fake, traffic={"visitors": 8})
        self._seed()
        self._drive(reg, hours=range(0, 60 * 7, 7))        # far more than needed
        opts = self._opps()
        self.assertEqual(len(opts), 3)                     # exactly the cap
        vids = [o["variant_id"] for o in opts]
        self.assertEqual(len(vids), len(set(vids)))        # unique
        rounds = [o["rounds_at_creation"] for o in opts]
        for a, b in zip(rounds, rounds[1:]):
            self.assertGreaterEqual(b - a, _OPT.cooldown_rounds)  # cooldown honoured
        live = [t for t in load_tasks(self.d).all()
                if t.task_type == "OPTIMIZE" and not t.is_terminal]
        self.assertLessEqual(len(live), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPTIMIZATION_COMPLETED"]), 3)

    def test_retry_does_not_double_record(self):
        class _Flaky(FakeOptimizationAdapter):
            def __init__(self):
                super().__init__()
                self.n = 0

            def optimize(self, req):
                self.n += 1
                if self.n == 1:
                    from revenue_os.optimization import OptimizationResult
                    return OptimizationResult(success=False, provider="fake",
                                              focus=req.focus, error="transient")
                return super().optimize(req)

        flaky = _Flaky()
        reg = self._reg(opt=flaky)
        self._seed()
        # the retry (backoff elapses as `now` advances) happens within the run
        self._drive(reg, hours=self._CYCLES)
        ot = next(t for t in load_tasks(self.d).all() if t.task_type == "OPTIMIZE")
        self.assertEqual(ot.status, "SUCCEEDED")
        self.assertGreaterEqual(flaky.n, 2)               # failed once, retried
        self.assertEqual(len(self._opps()), 1)            # recorded exactly once
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPTIMIZATION_COMPLETED"]), 1)

    def test_restart_or_rerun_does_not_double_record(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        self.assertEqual(len(self._opps()), 1)
        v1 = self._opps()[0]["variant_id"]

        # a fresh worker re-runs an OPTIMIZE for the SAME opp/focus/number
        # (crash-recovery replay): the adapter regenerates the same
        # variant_id -> the worker recognises it and records nothing new.
        at = _iso(BASE + timedelta(hours=30))
        q = load_tasks(self.d)
        q.create(self.oid, "OPTIMIZE", priority=9,
                 input={"focus": "landing_copy", "signal": {"traffic_rounds": 3},
                        "variant_number": 1})
        q.resolve_dependencies(now=at)
        q.save()
        # run at hour 30: the recurring CHECK_* successors (scheduled for
        # hour 34) are not due, so only the replayed OPTIMIZE task runs
        Worker(self.d, registry=self._reg(), name="w2",
               optimization_policy=_OPT, traction_policy=_NO_NO_TRACTION).run(
            now=at, max_ticks=20)
        self.assertEqual(len(self._opps()), 1)            # not 2
        self.assertEqual(self._opps()[0]["variant_id"], v1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPTIMIZATION_COMPLETED"]), 1)

    def test_null_optimization_adapter_fails_closed_no_variant(self):
        reg = self._reg(opt=NullOptimizationAdapter())
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        ot = next(t for t in load_tasks(self.d).all() if t.task_type == "OPTIMIZE")
        self.assertEqual(ot.status, "FAILED_FINAL")
        self.assertIn("BLOCKED", ot.error)
        self.assertEqual(self._opps(), [])

    def test_no_automatic_distribution_deploy_payment_or_spend(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=range(0, 40 * 7, 7))
        for t in load_tasks(self.d).all():
            if t.task_type in ("DISTRIBUTE", "SPAWN_VARIANT", "SCALE"):
                self.assertNotEqual(t.status, "SUCCEEDED")
        for artefact in ("revenue.json", "deliveries.json", "spend.json",
                         "llm_spend.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        # OPTIMIZE never creates a DEPLOY follow-up
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DEPLOY"]), 0)


if __name__ == "__main__":
    unittest.main()
