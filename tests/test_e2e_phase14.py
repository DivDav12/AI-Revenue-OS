"""PHASE 14 - targeted E2E: LIVE -> Measurement -> Optimization Decision
-> OPTIMIZE task -> Optimization Result (a recorded variant DRAFT).

Real: opportunity store, TaskQueue, Worker, state machine, EventLog.
Fakes (external systems only): FakeDeploymentAdapter, FakeMeasurementAdapter,
FakePaymentAdapter, FakeOptimizationAdapter. No network, no LLM, no money.

The variant is NOT built, deployed, or promoted. Phase 14 ends at
"hypothesis recorded".
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, execution_view, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import FakeMeasurementAdapter, TractionPolicy
from revenue_os.opportunity_store import load_opportunities
from revenue_os.optimization import FakeOptimizationAdapter, OptimizationPolicy
from revenue_os.payments import FakePaymentAdapter
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    DeployTaskAdapter,
    OptimizeAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NNT = TractionPolicy(min_cycles=10 ** 9)          # never NO_TRACTION here
_OPT = OptimizationPolicy(min_measurement_rounds=3, min_visitors=10,
                          max_variants=3, cooldown_rounds=3)


def _iso(dt):
    return dt.isoformat()


class Phase14E2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _registry(self, measurement, optimization):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        reg.register(CheckTrafficAdapter(measurement))
        reg.register(CheckLeadsAdapter(measurement))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[])))
        reg.register(OptimizeAdapter(optimization))
        return reg

    def _accept_deploy(self, reg, *, now):
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, OID, actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(now=now, max_ticks=100)
        release_task(self.d, next(
            t.task_id for t in load_tasks(self.d).by_opportunity(OID)
            if t.task_type == "DEPLOY"), actor="founder")
        return OID

    def _run(self, reg, *, hours):
        for h in hours:
            Worker(self.d, registry=reg, name="e2e",
                   optimization_policy=_OPT, traction_policy=_NNT).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=80)

    # -----------------------------------------------------------------
    def test_measurement_to_optimization_decision_to_variant(self):
        opt = FakeOptimizationAdapter()
        m = FakeMeasurementAdapter(traffic={"visitors": 9}, leads={"leads": 0})
        reg = self._registry(m, opt)
        OID = self._accept_deploy(reg, now=_iso(BASE))

        # DEPLOY -> LIVE, then recurring measurement (traffic, no conversion)
        self._run(reg, hours=range(0, 5 * 7, 7))

        s = load_opportunities(self.d).get(OID)
        # measurement drove the state; optimization did NOT move it
        self.assertIn(s["state"], ("MEASURING", "FIRST_VISITOR"))
        self.assertNotIn(
            "OPTIMIZING",
            {t["next_state"] for t in s["transitions"]})

        opts = s["execution"]["optimizations"]
        self.assertEqual(len(opts), 1)
        v = opts[0]
        self.assertEqual(v["variant_id"], f"var-{OID[:12]}-01")
        self.assertEqual(v["focus"], "landing_copy")
        self.assertTrue(v["hypothesis"])
        self.assertEqual(sorted(v["requires_before_live"]),
                         ["build_page", "deploy_approval", "validate_page"])
        self.assertEqual(v["task_id"],
                         next(t.task_id for t in load_tasks(self.d).all()
                              if t.task_type == "OPTIMIZE"))

        evs = load_events(self.d).all()
        types = [e["type"] for e in evs]
        self.assertEqual(types.count("OPTIMIZATION_CREATED"), 1)
        self.assertEqual(types.count("OPTIMIZATION_COMPLETED"), 1)
        # created after a real MEASUREMENT_RECORDED, completed after the task
        oc = next(e for e in evs if e["type"] == "OPTIMIZATION_CREATED")
        first_measure = next(e for e in evs if e["type"] == "MEASUREMENT_RECORDED")
        self.assertLess(first_measure["seq"], oc["seq"])
        self.assertLess(oc["seq"],
                        next(e["seq"] for e in evs
                             if e["type"] == "OPTIMIZATION_COMPLETED"))
        # monotonic event log
        self.assertEqual([e["seq"] for e in evs], list(range(1, len(evs) + 1)))

        ot = next(t for t in load_tasks(self.d).all() if t.task_type == "OPTIMIZE")
        self.assertEqual(ot.status, "SUCCEEDED")
        self.assertEqual(ot.opportunity_id, OID)

        # execution_view still renders, opportunity id stable
        self.assertEqual(execution_view(self.d, OID)[0]["opportunity_id"], OID)
        self.assertTrue(all(t.opportunity_id == OID
                            for t in load_tasks(self.d).all()))

    # -----------------------------------------------------------------
    def test_optimization_creates_no_external_or_money_action(self):
        opt = FakeOptimizationAdapter()
        m = FakeMeasurementAdapter(traffic={"visitors": 9}, leads={"leads": 0})
        reg = self._registry(m, opt)
        OID = self._accept_deploy(reg, now=_iso(BASE))
        self._run(reg, hours=range(0, 30 * 7, 7))

        self.assertGreaterEqual(
            len(load_opportunities(self.d).get(OID)["execution"]["optimizations"]),
            1)
        # variant cap holds even over a long run
        self.assertLessEqual(
            len(load_opportunities(self.d).get(OID)["execution"]["optimizations"]),
            3)
        # no deploy re-run, no payment, no delivery, no spend; no Phase-15 tasks
        for t in load_tasks(self.d).all():
            if t.task_type in ("SPAWN_VARIANT", "SCALE"):
                self.assertNotEqual(t.status, "SUCCEEDED")
            if t.task_type == "DISTRIBUTE":
                self.assertFalse(t.output.get("success"))   # no owned channel here
        for artefact in ("revenue.json", "deliveries.json", "spend.json",
                         "llm_spend.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        # the DEPLOY task from acceptance is SUCCEEDED and never re-run
        deploys = [t for t in load_tasks(self.d).all() if t.task_type == "DEPLOY"]
        self.assertEqual(len(deploys), 1)
        self.assertEqual(deploys[0].status, "SUCCEEDED")

    # -----------------------------------------------------------------
    def test_no_optimization_without_a_real_measurement_basis(self):
        opt = FakeOptimizationAdapter()
        m = FakeMeasurementAdapter(traffic={"visitors": 9}, leads={"leads": 0})
        reg = self._registry(m, opt)
        OID = self._accept_deploy(reg, now=_iso(BASE))
        self._run(reg, hours=range(0, 2 * 7, 7))          # only 2 rounds
        self.assertEqual(
            load_opportunities(self.d).get(OID)["execution"].get("optimizations", []),
            [])
        self.assertNotIn("OPTIMIZE",
                         {t.task_type for t in load_tasks(self.d).all()})

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", ".record_optimization(",
                          ".record_measurement(", "._by_id", "ledger.add("):
            self.assertNotIn(forbidden, code, f"E2E must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
