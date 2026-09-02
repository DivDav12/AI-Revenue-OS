"""PHASE 9 - targeted E2E:
DISCOVER -> ACCEPT -> PLAN -> BUILD -> VALIDATE -> DEPLOY -> LIVE
-> DISTRIBUTE -> ACQUIRING_TRAFFIC -> CHECK_TRAFFIC -> (Phase 10 measurement)

Real: opportunity store, TaskQueue, Worker, state machine, EventLog,
Phase-10 measurement architecture.
Fakes (external systems only): FakeDeploymentAdapter, FakeDistributionAdapter,
FakeMeasurementAdapter, FakePaymentAdapter. No network, no accounts, no money.

Proves that DISTRIBUTE success does NOT create FIRST_VISITOR - only
CHECK_TRAFFIC does. The test never sets a status, never calls the state
machine / ledger / record_* directly.
"""

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
from revenue_os.payments import FakePaymentAdapter
from revenue_os.task_adapters import (
    CheckLeadsAdapter,
    CheckRevenueAdapter,
    CheckTrafficAdapter,
    DeployTaskAdapter,
    DistributeTaskAdapter,
    default_registry,
)
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NNT = TractionPolicy(min_cycles=10 ** 9)


def _iso(dt):
    return dt.isoformat()


class Phase9E2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _registry(self, *, distribution, traffic):
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        reg.register(DistributeTaskAdapter(distribution))
        m = FakeMeasurementAdapter(traffic=traffic, leads={"leads": 0})
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[])))
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

    def _state(self, oid):
        return load_opportunities(self.d).get(oid)["state"]

    # -----------------------------------------------------------------
    def test_live_distribute_acquiring_traffic_then_measurement(self):
        dist = FakeDistributionAdapter()
        reg = self._registry(distribution=dist,
                             traffic=[{"visitors": 0}, {"visitors": 11}])
        OID = self._accept_deploy(reg, now=_iso(BASE))

        # one drain: DEPLOY -> LIVE, DISTRIBUTE (owned_web) -> ACQUIRING_TRAFFIC,
        # then CHECK_TRAFFIC (cycle 0, 0 visitors) -> MEASURING
        Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT).run(
            now=_iso(BASE), max_ticks=100)

        evs = load_events(self.d).all()
        seq = {e["type"] + ":" + str(e["data"].get("to", "")): e["seq"]
               for e in evs if e["type"] in
               ("DISTRIBUTION_COMPLETED", "OPPORTUNITY_TRANSITIONED",
                "MEASUREMENT_RECORDED")}

        # order: LIVE  <  DISTRIBUTION_COMPLETED(owned_web) < ACQUIRING_TRAFFIC
        #        < first MEASUREMENT_RECORDED < MEASURING
        live = next(e["seq"] for e in evs
                    if e["type"] == "OPPORTUNITY_TRANSITIONED"
                    and e["data"].get("to") == "LIVE")
        dc = next(e["seq"] for e in evs
                  if e["type"] == "DISTRIBUTION_COMPLETED"
                  and e["data"].get("channel") == "owned_web")
        acq = next(e["seq"] for e in evs
                   if e["type"] == "OPPORTUNITY_TRANSITIONED"
                   and e["data"].get("to") == "ACQUIRING_TRAFFIC")
        m0 = next(e["seq"] for e in evs if e["type"] == "MEASUREMENT_RECORDED")
        meas = next(e["seq"] for e in evs
                    if e["type"] == "OPPORTUNITY_TRANSITIONED"
                    and e["data"].get("to") == "MEASURING")
        self.assertLess(live, dc)
        self.assertLess(dc, acq)
        self.assertLess(acq, m0)
        self.assertLess(m0, meas)

        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "MEASURING")
        # ACQUIRING_TRAFFIC exactly once, from LIVE, driven by the DISTRIBUTE task
        acqs = [e for e in evs if e["type"] == "OPPORTUNITY_TRANSITIONED"
                and e["data"].get("to") == "ACQUIRING_TRAFFIC"]
        self.assertEqual(len(acqs), 1)
        self.assertEqual(acqs[0]["data"]["from"], "LIVE")
        self.assertEqual(acqs[0]["task_type"], "DISTRIBUTE")

        # DISTRIBUTE success did NOT create FIRST_VISITOR
        self.assertNotIn("FIRST_VISITOR",
                         {t["next_state"] for t in s["transitions"]})
        d = s["execution"]["distributions"]
        self.assertEqual(sorted(x["channel"] for x in d),
                         ["community_draft", "owned_content", "owned_web",
                          "social_draft"])
        for x in d:
            if x["channel"] in ("community_draft", "social_draft"):
                self.assertTrue(x["draft_only"])
                self.assertEqual(x["resulting_url"], "")
                self.assertFalse(x["draft"].get("auto_post"))

        # ---- now real traffic arrives -> CHECK_TRAFFIC creates FIRST_VISITOR
        Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT).run(
            now=_iso(BASE + timedelta(hours=7)), max_ticks=100)
        s = load_opportunities(self.d).get(OID)
        self.assertEqual(s["state"], "FIRST_VISITOR")
        fv = next(e for e in load_events(self.d).all()
                  if e["type"] == "OPPORTUNITY_TRANSITIONED"
                  and e["data"].get("to") == "FIRST_VISITOR")
        self.assertEqual(fv["task_type"], "CHECK_TRAFFIC")   # measurement, not distribution

        # monotonic event log
        seqs = [e["seq"] for e in load_events(self.d).all()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertTrue(all(t.opportunity_id == OID
                            for t in load_tasks(self.d).all()))

    # -----------------------------------------------------------------
    def test_no_distribution_channel_measurement_still_works(self):
        # NullDistributionAdapter (the default) -> DISTRIBUTE is a no-op
        # (SUCCEEDED, nothing published); the CHECK_* tasks that depend on it
        # still run and the measurement loop proceeds from LIVE.
        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))
        m = FakeMeasurementAdapter(traffic={"visitors": 7}, leads={"leads": 0})
        reg.register(CheckTrafficAdapter(m))
        reg.register(CheckLeadsAdapter(m))
        reg.register(CheckRevenueAdapter(FakePaymentAdapter(events=[])))
        OID = self._accept_deploy(reg, now=_iso(BASE))
        Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT).run(
            now=_iso(BASE), max_ticks=100)

        s = load_opportunities(self.d).get(OID)
        self.assertNotIn("ACQUIRING_TRAFFIC",
                         {t["next_state"] for t in s["transitions"]})
        self.assertIn(s["state"], ("MEASURING", "FIRST_VISITOR"))  # measurement ran
        dt = next(t for t in load_tasks(self.d).all()
                  if t.task_type == "DISTRIBUTE"
                  and t.input.get("channel") == "owned_web")
        self.assertEqual(dt.status, "SUCCEEDED")           # no-op, not an error
        self.assertFalse(dt.output.get("success"))
        self.assertEqual(s["execution"].get("distributions", []), [])

    # -----------------------------------------------------------------
    def test_failed_distribution_no_acquiring_traffic(self):
        reg = self._registry(distribution=FakeDistributionAdapter(fail=True),
                             traffic={"visitors": 0})
        OID = self._accept_deploy(reg, now=_iso(BASE))
        Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT).run(
            now=_iso(BASE), max_ticks=100)
        s = load_opportunities(self.d).get(OID)
        self.assertNotIn("ACQUIRING_TRAFFIC",
                         {t["next_state"] for t in s["transitions"]})
        self.assertEqual(s["execution"].get("distributions", []), [])

    # -----------------------------------------------------------------
    def test_no_money_spend_smtp_social_autopost_or_scale(self):
        reg = self._registry(distribution=FakeDistributionAdapter(),
                             traffic={"visitors": 0})
        OID = self._accept_deploy(reg, now=_iso(BASE))
        for h in range(0, 20 * 7, 7):
            Worker(self.d, registry=reg, name="e2e", traction_policy=_NNT).run(
                now=_iso(BASE + timedelta(hours=h)), max_ticks=100)
        for artefact in ("revenue.json", "spend.json", "llm_spend.json",
                         "deliveries.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        types = {t.task_type for t in load_tasks(self.d).all()}
        self.assertNotIn("SPAWN_VARIANT", types)
        self.assertNotIn("SCALE", types)
        for t in load_tasks(self.d).all():
            if t.task_type == "OPTIMIZE":
                self.assertNotEqual(t.status, "SUCCEEDED")   # opt policy default: 8 rounds
        # the community/social distributions are draft-only, never "posted"
        drafts = [x for x in load_opportunities(self.d).get(OID)["execution"]
                  ["distributions"] if x["draft_only"]]
        self.assertEqual(len(drafts), 2)
        for x in drafts:
            self.assertFalse(x["draft"].get("auto_post"))
            self.assertEqual(x["resulting_url"], "")

    def test_this_file_takes_no_shortcuts(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.split("\nfrom revenue_os", 1)[-1].splitlines()
            if not ln.lstrip().startswith("#"))
        code = code.split("def test_this_file_takes_no_shortcuts")[0]
        for forbidden in (".set_status(", ".transition(", ".record_distribution(",
                          ".record_measurement(", "._by_id", "ledger.add("):
            self.assertNotIn(forbidden, code, f"E2E must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
