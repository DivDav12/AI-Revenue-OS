"""Promotion decision + SCALE task (Phase 15)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import FakeMeasurementAdapter, TractionPolicy
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.optimization import FakeOptimizationAdapter, OptimizationPolicy
from revenue_os.payments import FakePaymentAdapter
from revenue_os.scaling import (
    DEFAULT_PROMOTION_POLICY,
    FakeScalingAdapter,
    NullScalingAdapter,
    PromotionPolicy,
    ScalingRequest,
    evaluate_promotion,
)
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    OptimizeAdapter,
    ScaleTaskAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NNT = TractionPolicy(min_cycles=10 ** 9)
_OPT = OptimizationPolicy(min_measurement_rounds=3, min_visitors=10,
                          max_variants=3, cooldown_rounds=3)
_PROMO = PromotionPolicy(min_measurement_cycles=3, min_visitors=10,
                         min_leads=3, max_scalings=2)


def _iso(dt):
    return dt.isoformat()


def _opp(*, traffic_rounds=0, visitors_each=0, leads_each=0, revenue=0.0,
         optimizations=None, scalings=None):
    series = []
    for i in range(traffic_rounds):
        series.append({"ts": _iso(BASE + timedelta(days=i)), "kind": "traffic",
                       "cycle": i, "metrics": {"visitors": visitors_each}})
        series.append({"ts": _iso(BASE + timedelta(days=i)), "kind": "leads",
                       "cycle": i, "metrics": {"leads": leads_each}})
    return {"state": "FIRST_LEAD", "title": "Cold-email pack",
            "execution": {"measurement_series": series,
                          "metrics": {"revenue": {"revenue_eur": revenue}},
                          "optimizations": list(optimizations or []),
                          "scalings": list(scalings or [])}}


_V = [{"variant_id": "var-x-01", "focus": "offer_pricing"}]


# ---------------------------------------------------------------------------
# promotion decision
# ---------------------------------------------------------------------------

class PromotionDecisionTests(unittest.TestCase):
    def test_no_variant(self):
        d = evaluate_promotion(_opp(traffic_rounds=20, visitors_each=10,
                                    leads_each=5))
        self.assertFalse(d.promote)
        self.assertIn("no optimization variant", d.reason)

    def test_insufficient_cycles(self):
        d = evaluate_promotion(_opp(traffic_rounds=3, visitors_each=50,
                                    leads_each=5, optimizations=_V))
        self.assertFalse(d.promote)
        self.assertIn("insufficient measurement basis", d.reason)
        self.assertEqual(d.evidence["measurement_cycles"], 3)

    def test_insufficient_visitors(self):
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=2,
                                    leads_each=5, optimizations=_V))
        self.assertFalse(d.promote)
        self.assertIn("believable sample", d.reason)

    def test_no_traction_signal(self):
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=10,
                                    leads_each=0, revenue=0.0, optimizations=_V))
        self.assertFalse(d.promote)
        self.assertIn("no traction signal", d.reason)

    def test_promote_on_leads(self):
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=10,
                                    leads_each=1, optimizations=_V))
        # 12 leads cumulative >= default min 3
        self.assertTrue(d.promote)
        self.assertEqual(d.variant_id, "var-x-01")
        self.assertEqual(d.evidence["leads"], 12)
        self.assertIn("enough evidence to scale", d.reason)

    def test_promote_on_revenue(self):
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=10,
                                    leads_each=0, revenue=59.0, optimizations=_V))
        self.assertTrue(d.promote)
        self.assertEqual(d.evidence["revenue_eur"], 59.0)

    def test_scaling_cap(self):
        sc = [{"variant_id": f"v{i}", "status": "success"} for i in range(2)]
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=10,
                                    leads_each=5, optimizations=_V, scalings=sc))
        self.assertFalse(d.promote)
        self.assertIn("scaling cap", d.reason)

    def test_variant_already_scaled(self):
        sc = [{"variant_id": "var-x-01", "status": "success"}]
        d = evaluate_promotion(_opp(traffic_rounds=12, visitors_each=10,
                                    leads_each=5, optimizations=_V, scalings=sc))
        self.assertFalse(d.promote)
        self.assertIn("already scaled", d.reason)

    def test_default_policy_is_conservative(self):
        # a single visitor / single lead never promotes on the default policy
        d = evaluate_promotion(_opp(traffic_rounds=1, visitors_each=1,
                                    leads_each=1, optimizations=_V),
                               policy=DEFAULT_PROMOTION_POLICY)
        self.assertFalse(d.promote)


# ---------------------------------------------------------------------------
# scaling adapters
# ---------------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def _req(self, vid="var-x-01"):
        return ScalingRequest(opportunity_id="opp_x", variant_id=vid,
                              variant={"focus": "offer_pricing"},
                              evidence={"leads": 8})

    def test_null_is_blocked(self):
        r = NullScalingAdapter().scale(self._req())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertEqual(r.actions, [])

    def test_fake_success_is_safe_and_deterministic(self):
        a, b = FakeScalingAdapter(), FakeScalingAdapter()
        r1, r2 = a.scale(self._req()), b.scale(self._req())
        self.assertTrue(r1.success)
        self.assertEqual(r1.scale_id, r2.scale_id)
        self.assertEqual(r1.scale_id, "scale-opp_x-01")
        # every action is a SAFE internal 'draft'/'queue' step - no ads, no
        # spend, no posting, no accounts
        for act in r1.actions:
            self.assertTrue(act.startswith(("queued", "drafted")))
        blob = " ".join(r1.actions).lower()
        for bad in ("advertis", "buy ", "purchase", "pay ", "account", "post to"):
            self.assertNotIn(bad, blob)

    def test_fake_idempotent(self):
        a = FakeScalingAdapter()
        r1 = a.scale(self._req())
        r2 = a.scale(self._req())
        self.assertEqual(r1.scale_id, r2.scale_id)
        self.assertTrue(r2.details.get("duplicate_suppressed"))

    def test_fake_fail_and_blocked_and_requires_approval(self):
        self.assertFalse(FakeScalingAdapter(fail=True).scale(self._req()).success)
        self.assertTrue(FakeScalingAdapter(blocked=True).scale(self._req()).blocked)
        r = FakeScalingAdapter(requires_approval="money").scale(self._req())
        self.assertFalse(r.success)
        self.assertEqual(r.requires_approval, "money")

    def test_no_dangerous_functions_exist(self):
        import revenue_os.scaling as mod
        for name in ("buy_ads", "spend", "create_account", "post", "charge",
                     "purchase"):
            self.assertFalse(hasattr(mod, name))


# ---------------------------------------------------------------------------
# SCALE task through the real worker
# ---------------------------------------------------------------------------

class ScaleTaskTests(unittest.TestCase):
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

    def _reg(self, *, scale=None, leads=2):
        reg = default_registry()
        m = FakeMeasurementAdapter(traffic={"visitors": 12},
                                   leads={"leads": leads})
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[])))
        reg.register(OptimizeAdapter(FakeOptimizationAdapter()))
        reg.register(ScaleTaskAdapter(scale or FakeScalingAdapter()))
        return reg

    def _seed(self):
        q = load_tasks(self.d)
        for tt in ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            q.create(self.oid, tt, priority=5)
        q.resolve_dependencies()
        q.save()

    def _drive(self, reg, *, hours):
        for h in hours:
            Worker(self.d, registry=reg, name="w", traction_policy=_NNT,
                   optimization_policy=_OPT, promotion_policy=_PROMO).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=40)

    def _scalings(self):
        return load_opportunities(self.d).get(self.oid)["execution"].get(
            "scalings", [])

    def _opts(self):
        return load_opportunities(self.d).get(self.oid)["execution"].get(
            "optimizations", [])

    _CYCLES = range(0, 5 * 7, 7)      # 5 cycles: 1 optimization variant, 1 scale

    def test_promotion_creates_scale_task_and_records_it(self):
        fake = FakeScalingAdapter()
        reg = self._reg(scale=fake)
        self._seed()
        self._drive(reg, hours=self._CYCLES)

        self.assertEqual(len(self._opts()), 1)
        sc = self._scalings()
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["variant_id"], self._opts()[-1]["variant_id"])
        self.assertEqual(sc[0]["status"], "success")
        self.assertGreaterEqual(sc[0]["evidence"]["leads"], 3)
        self.assertTrue(sc[0]["scale_id"].startswith("scale-"))
        self.assertTrue(sc[0]["actions"])
        self.assertEqual(fake.calls, [(self.oid, self._opts()[-1]["variant_id"])])

        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(evs.count("PROMOTION_CREATED"), 1)
        self.assertEqual(evs.count("SCALE_COMPLETED"), 1)
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "SUCCEEDED")
        self.assertEqual(st.idempotency_key,
                         f"scale:{self.oid}:{self._opts()[-1]['variant_id']}")

    def test_scale_does_not_move_or_regress_state(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        s = load_opportunities(self.d).get(self.oid)
        self.assertIn(s["state"], ("MEASURING", "FIRST_VISITOR", "FIRST_LEAD"))
        self.assertNotIn("SCALING",
                         {t["next_state"] for t in s["transitions"]})

    def test_insufficient_evidence_no_scale(self):
        reg = self._reg(leads=0)                    # variant exists, but 0 leads
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        self.assertGreaterEqual(len(self._opts()), 1)
        self.assertEqual(self._scalings(), [])
        self.assertNotIn("SCALE",
                         {t.task_type for t in load_tasks(self.d).all()})

    def test_duplicate_measurement_no_second_scale(self):
        fake = FakeScalingAdapter()
        reg = self._reg(scale=fake)
        self._seed()
        # far more cycles than needed - scaling cap is 2, one variant scaled
        self._drive(reg, hours=range(0, 40 * 7, 7))
        sc = self._scalings()
        self.assertLessEqual(len(sc), 2)
        self.assertGreaterEqual(len(sc), 1)
        self.assertEqual(len({s["scale_id"] for s in sc}), len(sc))   # unique
        live = [t for t in load_tasks(self.d).all()
                if t.task_type == "SCALE" and not t.is_terminal]
        self.assertLessEqual(len(live), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "SCALE_COMPLETED"]), len(sc))

    def test_retry_does_not_double_scale(self):
        class _Flaky(FakeScalingAdapter):
            def __init__(self):
                super().__init__()
                self.n = 0

            def scale(self, req):
                self.n += 1
                if self.n == 1:
                    from revenue_os.scaling import ScalingResult
                    return ScalingResult(success=False, provider="fake",
                                         error="transient")
                return super().scale(req)

        flaky = _Flaky()
        reg = self._reg(scale=flaky)
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "SUCCEEDED")
        self.assertGreaterEqual(flaky.n, 2)
        self.assertEqual(len(self._scalings()), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "SCALE_COMPLETED"]), 1)

    def test_restart_does_not_double_scale(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        self.assertEqual(len(self._scalings()), 1)
        vid = self._opts()[-1]["variant_id"]
        # a fresh worker replays a SCALE for the same variant
        q = load_tasks(self.d)
        q.create(self.oid, "SCALE", priority=9,
                 input={"variant_id": vid, "evidence": {}})
        at = _iso(BASE + timedelta(hours=30))       # CHECK_* successors not due
        q.resolve_dependencies(now=at)
        q.save()
        Worker(self.d, registry=self._reg(), name="w2", traction_policy=_NNT,
               optimization_policy=_OPT, promotion_policy=_PROMO).run(
            now=at, max_ticks=20)
        self.assertEqual(len(self._scalings()), 1)          # not 2

    def test_already_scaled_variant_is_not_scaled_again(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=range(0, 30 * 7, 7))
        sc = self._scalings()
        scaled_vids = {s["variant_id"] for s in sc}
        # every scaled variant appears exactly once
        self.assertEqual(len(scaled_vids), len(sc))
        # a further drive does not re-scale an already-scaled variant
        n = len(sc)
        self._drive(reg, hours=range(30 * 7, 45 * 7, 7))
        self.assertEqual(len(self._scalings()), n)

    def test_null_scaling_adapter_fails_closed_no_record(self):
        reg = self._reg(scale=NullScalingAdapter())
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "FAILED_FINAL")
        self.assertIn("BLOCKED", st.error)
        self.assertEqual(self._scalings(), [])

    def test_scaling_that_would_cost_money_is_blocked_not_executed(self):
        reg = self._reg(scale=FakeScalingAdapter(requires_approval="money"))
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "BLOCKED_APPROVAL")
        self.assertEqual(st.approval_type, "money")
        self.assertEqual(self._scalings(), [])
        self.assertIn("TASK_BLOCKED",
                      [e["type"] for e in load_events(self.d).all()])

    def test_no_money_spend_smtp_or_new_variants(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=range(0, 30 * 7, 7))
        for artefact in ("revenue.json", "spend.json", "llm_spend.json",
                         "deliveries.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        # Phase-14 variant cap still holds - scaling did not spawn more OPTIMIZE
        self.assertLessEqual(len(self._opts()), _OPT.max_variants)
        for t in load_tasks(self.d).all():
            if t.task_type == "SPAWN_VARIANT":
                self.assertNotEqual(t.status, "SUCCEEDED")

    def test_event_sequence_stays_monotonic(self):
        reg = self._reg()
        self._seed()
        self._drive(reg, hours=self._CYCLES)
        seqs = [e["seq"] for e in load_events(self.d).all()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
