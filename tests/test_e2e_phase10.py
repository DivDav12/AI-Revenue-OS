"""PHASE 10 - targeted E2E: LIVE -> recurring measurement -> MEASURING ->
FIRST_VISITOR -> FIRST_LEAD, through the normal execution architecture.

Real: opportunity store, TaskQueue, Worker, state machine, EventLog.
Fakes (external systems only): FakeDeploymentAdapter, FakeMeasurementAdapter,
FakePaymentAdapter. No network, no analytics account, no money.

The test never sets a status, never calls the state machine / ledger
directly, never bypasses the worker / queue.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.measurement import FakeMeasurementAdapter, TractionPolicy
from revenue_os.opportunity_store import load_opportunities
from revenue_os.payments import FakePaymentAdapter, PaymentEvent
from revenue_os.revenue import RevenueLedger
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    DeployTaskAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


class Phase10E2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _registry(self, measurement, payment_events=()):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        reg.register(CheckTrafficAdapter(measurement))
        reg.register(CheckLeadsAdapter(measurement))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(
            events=list(payment_events))))
        return reg

    def _accept_and_deploy(self, reg, *, now):
        """discover -> accept -> build -> validate -> release DEPLOY.
        Stops BEFORE the DEPLOY task runs so the caller controls the clock
        for the whole measurement leg."""
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, OID, actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(now=now, max_ticks=100)
        self.assertIn(load_opportunities(self.d).get(OID)["state"],
                      ("BUILDING", "VALIDATING", "READY_TO_DEPLOY"))
        release_task(self.d, next(
            t.task_id for t in load_tasks(self.d).by_opportunity(OID)
            if t.task_type == "DEPLOY"), actor="founder")
        return OID

    def _state(self, oid):
        return load_opportunities(self.d).get(oid)["state"]

    # -----------------------------------------------------------------
    def test_live_to_first_lead_through_recurring_measurement(self):
        m = FakeMeasurementAdapter(
            traffic=[{"visitors": 0}, {"visitors": 18}, {"visitors": 6}],
            leads=[{"leads": 0}, {"leads": 3}])
        reg = self._registry(m)
        OID = self._accept_and_deploy(reg, now=_iso(BASE))

        # round 0: DEPLOY -> LIVE, then measurement -> MEASURING (0/0)
        Worker(self.d, registry=reg, name="e2e").run(now=_iso(BASE), max_ticks=60)
        self.assertEqual(self._state(OID), "MEASURING")

        # round 1 (7h later): 18 visitors -> FIRST_VISITOR ; 3 leads -> FIRST_LEAD
        Worker(self.d, registry=reg, name="e2e").run(
            now=_iso(BASE + timedelta(hours=7)), max_ticks=60)
        self.assertEqual(self._state(OID), "FIRST_LEAD")

        evs = load_events(self.d).all()
        types = [e["type"] for e in evs]
        for want, n in (("MEASUREMENT_RECORDED", 6),):     # 3 kinds x 2 rounds
            self.assertGreaterEqual(types.count(want), n)
        for to in ("MEASURING", "FIRST_VISITOR", "FIRST_LEAD"):
            self.assertEqual(
                len([e for e in evs if e["type"] == "OPPORTUNITY_TRANSITIONED"
                     and e["data"].get("to") == to and e["opportunity_id"] == OID]),
                1, to)
            fr = next(e for e in evs if e["type"] == "OPPORTUNITY_TRANSITIONED"
                      and e["data"].get("to") == to)
            self.assertEqual(fr["data"]["source"] if "source" in fr["data"] else "task",
                             "task")

        # time series persisted, monotonic event seq
        series = load_opportunities(self.d).get(OID)["execution"]["measurement_series"]
        self.assertGreaterEqual(len(series), 6)
        self.assertEqual([e["seq"] for e in evs], list(range(1, len(evs) + 1)))

        # exactly one live occurrence of each CHECK_* type - no explosion
        q = load_tasks(self.d)
        for tt in ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            self.assertLessEqual(
                len([t for t in q.by_opportunity(OID)
                     if t.task_type == tt and not t.is_terminal]), 1, tt)

        # round 2: a later measurement must NOT regress FIRST_LEAD
        Worker(self.d, registry=reg, name="e2e").run(
            now=_iso(BASE + timedelta(hours=14)), max_ticks=60)
        self.assertEqual(self._state(OID), "FIRST_LEAD")

        # ---- Phase 10 does NOT auto-start Phase 14 / Phase 9 -------
        all_types = {t.task_type for t in load_tasks(self.d).all()}
        self.assertNotIn("OPTIMIZE", all_types)
        self.assertNotIn("SPAWN_VARIANT", all_types)
        for t in load_tasks(self.d).all():
            if t.task_type == "DISTRIBUTE":
                self.assertNotEqual(t.status, "SUCCEEDED")

        # no external side effects
        for artefact in ("llm_spend.json", "spend.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)

    # -----------------------------------------------------------------
    def test_measurement_stays_phase_11_compatible(self):
        pay = [PaymentEvent(reference="FAKE-CAP-1", amount=29.0, currency="EUR",
                            opportunity_id=None, customer_ref="b@example.test",
                            provider="fake")]
        m = FakeMeasurementAdapter(traffic={"visitors": 4}, leads={"leads": 0})
        reg = self._registry(m, payment_events=pay)
        opportunity_engine.generate(self.d, n=8)
        OID = load_opportunities(self.d).by_status("discovered")[0]["id"]
        pay[0].opportunity_id = OID
        accept_opportunity(self.d, OID, actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        release_task(self.d, next(
            t.task_id for t in load_tasks(self.d).by_opportunity(OID)
            if t.task_type == "DEPLOY"), actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(now=_iso(BASE), max_ticks=100)

        # a sale still routes straight to FIRST_SALE and spawns one DELIVER
        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_SALE")
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total_for(OID),
                         29.0)
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]), 1)

        # recurring CHECK_REVENUE over the same settled payment: no double
        # revenue / FIRST_SALE / DELIVER
        for h in (7, 14, 21, 28):
            Worker(self.d, registry=reg, name="e2e").run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=100)
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.0)
        ev_types = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(ev_types.count("REVENUE_RECORDED"), 1)
        self.assertEqual(ev_types.count("PAYMENT_DETECTED"), 1)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "FIRST_SALE"]), 1)
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DELIVER"]), 1)

    # -----------------------------------------------------------------
    def test_no_traction_after_a_real_basis(self):
        pol = TractionPolicy(min_cycles=3, min_wall_seconds=0, min_visitors=10 ** 9)
        m = FakeMeasurementAdapter(traffic={"visitors": 0}, leads={"leads": 0})
        reg = self._registry(m)
        OID = self._accept_and_deploy(reg, now=_iso(BASE))
        for h in range(0, 40, 7):
            Worker(self.d, registry=reg, name="e2e", traction_policy=pol).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=60)
        self.assertEqual(self._state(OID), "NO_TRACTION")
        # recurrence stopped
        self.assertFalse(any(
            not t.is_terminal and t.task_type in
            ("CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE")
            for t in load_tasks(self.d).by_opportunity(OID)))
        # still no OPTIMIZE (Phase 14)
        self.assertNotIn("OPTIMIZE",
                         {t.task_type for t in load_tasks(self.d).all()})

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", ".record_measurement(",
                          ".record_opportunity_payment(", "process_payment_event(",
                          "._by_id", "ledger.add("):
            self.assertNotIn(forbidden, code, f"E2E must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
