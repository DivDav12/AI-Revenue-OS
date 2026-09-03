"""PHASE 15 - targeted E2E:
Measurement -> Phase-14 optimization variant -> enough evidence ->
Promotion Decision -> SCALE task -> FakeScalingAdapter -> SCALE_COMPLETED
-> execution.scalings.

Real: opportunity store, TaskQueue, Worker, state machine, EventLog,
Phase-10 measurement + Phase-14 optimization architecture.
Fakes (external systems only): FakeDeploymentAdapter, FakeDistributionAdapter,
FakeMeasurementAdapter, FakePaymentAdapter, FakeOptimizationAdapter,
FakeScalingAdapter. No network, no LLM, no money, no ads, no accounts.

Phase 15 ends at "scaling decision recorded" (safe internal actions only).
The test never sets a status, never calls the state machine / ledger /
record_* directly.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.distribution_adapters import FakeDistributionAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import FakeMeasurementAdapter, TractionPolicy
from revenue_os.opportunity_store import load_opportunities
from revenue_os.optimization import FakeOptimizationAdapter, OptimizationPolicy
from revenue_os.payments import FakePaymentAdapter
from revenue_os.scaling import FakeScalingAdapter, NullScalingAdapter, PromotionPolicy
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    DeployTaskAdapter,
    DistributeTaskAdapter,
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


class Phase15E2ETests(unittest.TestCase):
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

    def _registry(self, *, leads, scaling):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        reg.register(DistributeTaskAdapter(FakeDistributionAdapter()))
        m = FakeMeasurementAdapter(traffic={"visitors": 14}, leads={"leads": leads})
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[])))
        reg.register(OptimizeAdapter(FakeOptimizationAdapter()))
        reg.register(ScaleTaskAdapter(scaling))
        return reg

    def _accept_deploy(self, reg):
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, OID, actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(now=_iso(BASE), max_ticks=100)
        release_task(self.d, next(
            t.task_id for t in load_tasks(self.d).by_opportunity(OID)
            if t.task_type == "DEPLOY"), actor="founder")
        return OID

    def _run(self, reg, OID, *, hours):
        for h in hours:
            Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT,
                   optimization_policy=_OPT, promotion_policy=_PROMO).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=100)

    def _ex(self, OID):
        return load_opportunities(self.d).get(OID)["execution"]

    # -----------------------------------------------------------------
    def test_evidence_promotion_scale(self):
        scaling = FakeScalingAdapter()
        reg = self._registry(leads=3, scaling=scaling)
        OID = self._accept_deploy(reg)
        self._run(reg, OID, hours=range(0, 5 * 7, 7))

        ex = self._ex(OID)
        self.assertEqual(len(ex["optimizations"]), 1)
        vid = ex["optimizations"][0]["variant_id"]

        sc = ex["scalings"]
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["variant_id"], vid)
        self.assertEqual(sc[0]["status"], "success")
        self.assertTrue(sc[0]["scale_id"])
        self.assertTrue(sc[0]["actions"])
        # evidence is explicit + persisted
        ev = sc[0]["evidence"]
        self.assertEqual(ev["variant_id"], vid)
        self.assertGreaterEqual(ev["measurement_cycles"], 3)
        self.assertGreaterEqual(ev["visitors"], 10)
        self.assertGreaterEqual(ev["leads"], 3)
        self.assertIn("reason", ev)

        evs = load_events(self.d).all()
        types = [e["type"] for e in evs]
        self.assertEqual(types.count("PROMOTION_CREATED"), 1)
        self.assertEqual(types.count("SCALE_COMPLETED"), 1)
        # PROMOTION_CREATED after a real MEASUREMENT_RECORDED, before SCALE_COMPLETED
        m0 = next(e["seq"] for e in evs if e["type"] == "MEASUREMENT_RECORDED")
        pc = next(e["seq"] for e in evs if e["type"] == "PROMOTION_CREATED")
        sd = next(e["seq"] for e in evs if e["type"] == "SCALE_COMPLETED")
        self.assertLess(m0, pc)
        self.assertLess(pc, sd)
        self.assertEqual([e["seq"] for e in evs], list(range(1, len(evs) + 1)))

        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "SUCCEEDED")
        self.assertEqual(st.opportunity_id, OID)
        self.assertEqual(st.idempotency_key, f"scale:{OID}:{vid}")

        # SCALE did not move / regress the opportunity state
        s = load_opportunities(self.d).get(OID)
        self.assertNotIn("SCALING",
                         {t["next_state"] for t in s["transitions"]})

        # no external side effects
        for artefact in ("revenue.json", "spend.json", "llm_spend.json",
                         "deliveries.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        self.assertTrue(all(t.opportunity_id == OID
                            for t in load_tasks(self.d).all()))
        # Phase-14 variant cap untouched by scaling
        self.assertLessEqual(len(ex["optimizations"]), _OPT.max_variants)

    # -----------------------------------------------------------------
    def test_insufficient_evidence_no_scale(self):
        reg = self._registry(leads=0, scaling=FakeScalingAdapter())   # 0 leads
        OID = self._accept_deploy(reg)
        self._run(reg, OID, hours=range(0, 5 * 7, 7))
        ex = self._ex(OID)
        self.assertGreaterEqual(len(ex["optimizations"]), 1)   # variant exists
        self.assertEqual(ex.get("scalings", []), [])           # but no scale
        self.assertNotIn("SCALE",
                         {t.task_type for t in load_tasks(self.d).all()})
        self.assertNotIn("PROMOTION_CREATED",
                         [e["type"] for e in load_events(self.d).all()])

    # -----------------------------------------------------------------
    def test_duplicate_measurement_no_infinite_scale(self):
        reg = self._registry(leads=3, scaling=FakeScalingAdapter())
        OID = self._accept_deploy(reg)
        self._run(reg, OID, hours=range(0, 40 * 7, 7))
        ex = self._ex(OID)
        self.assertLessEqual(len(ex["scalings"]), _PROMO.max_scalings)
        self.assertGreaterEqual(len(ex["scalings"]), 1)
        self.assertEqual(len({s["scale_id"] for s in ex["scalings"]}),
                         len(ex["scalings"]))
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "SCALE_COMPLETED"]), len(ex["scalings"]))

    # -----------------------------------------------------------------
    def test_null_scaling_fails_closed(self):
        reg = self._registry(leads=3, scaling=NullScalingAdapter())
        OID = self._accept_deploy(reg)
        self._run(reg, OID, hours=range(0, 5 * 7, 7))
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "FAILED_FINAL")
        self.assertEqual(self._ex(OID).get("scalings", []), [])

    # -----------------------------------------------------------------
    def test_money_costing_scaling_is_blocked_not_executed(self):
        reg = self._registry(leads=3,
                             scaling=FakeScalingAdapter(requires_approval="money"))
        OID = self._accept_deploy(reg)
        self._run(reg, OID, hours=range(0, 5 * 7, 7))
        st = next(t for t in load_tasks(self.d).all() if t.task_type == "SCALE")
        self.assertEqual(st.status, "BLOCKED_APPROVAL")
        self.assertEqual(st.approval_type, "money")
        self.assertEqual(self._ex(OID).get("scalings", []), [])
        for artefact in ("spend.json", "revenue.json"):
            self.assertFalse((self.d / artefact).exists())

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", ".record_scaling(",
                          ".record_optimization(", ".record_measurement(",
                          "._by_id", "ledger.add("):
            self.assertNotIn(forbidden, code, f"E2E must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
