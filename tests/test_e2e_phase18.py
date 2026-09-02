"""PHASE 18 - End-to-End proof.

Drives ONE opportunity from a real discovery result all the way through
the SAME execution architecture production uses:

  opportunity_engine.generate        (DISCOVER - real, deterministic)
    -> acceptance.accept_opportunity  (ACCEPT - the real Phase-3 entry point)
      -> execution.TaskQueue          (the real persistent queue)
        -> worker.Worker              (the real synchronous executor)
          -> task_adapters / real roster agents + injected fakes
            -> AdapterResult
              -> events.EventLog      (the real append-only log)
                -> the real opportunity state machine

The ONLY things faked are EXTERNAL systems:
  * FakeDeploymentAdapter  - no GitHub, no GITHUB_TOKEN, no network
  * a measurement/traffic/payment fake for the post-LIVE task slots

The test drives state ONLY through accept_opportunity / release_task /
Worker. It never sets a status, never moves the state machine directly,
never writes the ledger, never hand-creates a task that an event/adapter
would normally produce, and never bypasses the worker, the queue, or the
approval gate. test_this_file_takes_no_shortcuts enforces this.
"""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import acceptance, opportunity_engine
from revenue_os.acceptance import CHAIN, accept_opportunity, execution_view, release_task
from revenue_os.deployment import FakeDeploymentAdapter, valid_live_url
from revenue_os.events import EVENT_TYPES, load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import load_opportunities
from revenue_os.task_adapters import DeployTaskAdapter, default_registry
from revenue_os.worker import AdapterResult, TaskAdapter, Worker


# --- fakes for EXTERNAL systems only -------------------------------------

class _CountingDeploy(FakeDeploymentAdapter):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def deploy(self, artifact):
        self.calls += 1
        return super().deploy(artifact)


class _FakeMeasurement(TaskAdapter):
    """Stands in for the (unbuilt) Phase 9/10/11 external reads: a CDN /
    analytics / payment-provider poll. Deterministic, offline. It returns a
    realistic successful payload but drives NO opportunity transition -
    that wiring is exactly the documented gap."""

    task_types = ("DISTRIBUTE", "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE")
    name = "fake-measurement"

    def __init__(self):
        self.seen: list[str] = []

    def run(self, ctx):
        self.seen.append(ctx.task.task_type)
        payloads = {
            "DISTRIBUTE": {"channel": "owned_site", "published": True,
                           "url": "https://e2e.pages.test/opp/index.html"},
            "CHECK_TRAFFIC": {"visitors": 42, "source": "fake-analytics"},
            "CHECK_LEADS": {"leads": 3},
            "CHECK_REVENUE": {"payment_detected": True, "amount_eur": 29.0,
                              "currency": "EUR", "capture_id": "FAKE-CAP-1",
                              "note": "fake payment provider poll"},
        }
        return AdapterResult(ok=True, output=payloads[ctx.task.task_type])


def _registry(deploy_adapter, measurement):
    reg = default_registry()
    reg.register(DeployTaskAdapter(deploy_adapter))
    reg.register(measurement)
    return reg


def _started_order(events):
    return [e["task_type"] for e in events if e["type"] == "TASK_STARTED"]


class E2EProofTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_discover_to_live_through_the_real_architecture(self):
        d = self.d

        # ============================================================
        # 1. DISCOVER - real, deterministic engine
        # ============================================================
        fresh = opportunity_engine.generate(d, n=8)
        self.assertGreaterEqual(len(fresh), 4)
        store = load_opportunities(d)
        self.assertTrue(all(r["state"] == "DISCOVERED" for r in store.all()))
        opp = store.by_status("discovered")[0]          # top-scored real result
        OID = opp["id"]
        self.assertEqual(store.get(OID)["state"], "DISCOVERED")

        # ============================================================
        # 2. ACCEPT - the real Phase-3 business-decision entry point
        # ============================================================
        acc = accept_opportunity(d, OID, actor="founder")
        self.assertEqual(acc["opportunity_id"], OID)
        self.assertEqual(acc["state"], "SELECTED")       # via legal transitions only

        # ---- 2a. task queue + dependency wiring (assertion B) --------
        q = load_tasks(d)
        chain = {t.task_type: t for t in q.by_opportunity(OID)}
        self.assertEqual(sorted(chain), sorted(t for t, _, _ in CHAIN))
        type_to_id = {t: chain[t].task_id for t in chain}
        for ttype, deps, approval in CHAIN:
            self.assertEqual(sorted(chain[ttype].depends_on),
                             sorted(type_to_id[d_] for d_ in deps),
                             f"{ttype} dependency wiring")
        # DEPLOY is born behind the approval gate; downstream tasks wait
        self.assertEqual(chain["DEPLOY"].status, "BLOCKED_APPROVAL")
        self.assertEqual(chain["DEPLOY"].approval_type, "money")
        for tt in ("DISTRIBUTE", "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            self.assertEqual(chain[tt].status, "PENDING")

        deploy_fake = _CountingDeploy(base_url="https://e2e.pages.test")
        measurement = _FakeMeasurement()
        reg = _registry(deploy_fake, measurement)

        # ============================================================
        # 3-5. WORKER pass 1: PLAN -> BUILD_* -> VALIDATE_* (real agents)
        # ============================================================
        Worker(d, registry=reg, name="e2e").run(max_ticks=100)
        ev1 = load_events(d).all()
        order1 = _started_order(ev1)

        # assertion C: nothing ran before its dependencies
        self.assertEqual(order1[0], "PLAN")
        self.assertLess(order1.index("PLAN"), order1.index("BUILD_PAGE"))
        self.assertLess(order1.index("BUILD_PAGE"), order1.index("VALIDATE_PAGE"))
        self.assertLess(order1.index("BUILD_PRODUCT"), order1.index("VALIDATE_PRODUCT"))
        # assertion: no BLOCKED_APPROVAL task ran, no downstream task ran
        self.assertNotIn("DEPLOY", order1)
        self.assertNotIn("CHECK_TRAFFIC", order1)
        self.assertEqual(deploy_fake.calls, 0)

        q = load_tasks(d)
        self.assertEqual(q.get(type_to_id["VALIDATE_PAGE"]).status, "SUCCEEDED")
        s = load_opportunities(d).get(OID)
        self.assertIn(s["state"], ("BUILDING", "VALIDATING", "READY_TO_DEPLOY"))
        self.assertNotEqual(s["state"], "LIVE")

        # ============================================================
        # 6. RELEASE the DEPLOY approval gate - explicit human action
        #    (Phase 7). This does NOT run the task.
        # ============================================================
        release_task(d, type_to_id["DEPLOY"], actor="founder")
        self.assertIn("TASK_UNBLOCKED",
                      [e["type"] for e in load_events(d).all()])
        self.assertEqual(deploy_fake.calls, 0)

        # ============================================================
        # 7. WORKER pass 2: DEPLOY -> DEPLOYMENT_COMPLETE -> LIVE,
        #    then the downstream slots run (deps now satisfied)
        # ============================================================
        Worker(d, registry=reg, name="e2e").run(max_ticks=100)
        ev2 = load_events(d).all()

        # assertion D: exactly one DEPLOYMENT_COMPLETE, from the real path
        dc = [e for e in ev2 if e["type"] == "DEPLOYMENT_COMPLETE"]
        self.assertEqual(len(dc), 1)
        self.assertEqual(deploy_fake.calls, 1)
        self.assertTrue(valid_live_url(dc[0]["data"]["live_url"]))

        # assertion E: LIVE only via confirmed deploy + valid url, exactly once
        live_trans = [e for e in ev2
                      if e["type"] == "OPPORTUNITY_TRANSITIONED"
                      and e["data"].get("to") == "LIVE"
                      and e["opportunity_id"] == OID]
        self.assertEqual(len(live_trans), 1)
        s = load_opportunities(d).get(OID)
        self.assertEqual(s["state"], "LIVE")
        self.assertTrue(valid_live_url(s["execution"]["live_url"]))
        self.assertEqual(q_deploy := load_tasks(d).get(type_to_id["DEPLOY"]).status,
                         "SUCCEEDED")

        # assertion C (downstream): DISTRIBUTE / CHECK_* only started AFTER
        # DEPLOY succeeded
        order2 = _started_order(ev2)
        deploy_succeeded_seq = next(e["seq"] for e in ev2
                                    if e["type"] == "TASK_SUCCEEDED"
                                    and e["task_type"] == "DEPLOY")
        for tt in ("DISTRIBUTE", "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            started = next(e for e in ev2 if e["type"] == "TASK_STARTED"
                           and e["task_type"] == tt)
            self.assertGreater(started["seq"], deploy_succeeded_seq, tt)
        self.assertEqual(sorted(measurement.seen),
                         ["CHECK_LEADS", "CHECK_REVENUE", "CHECK_TRAFFIC",
                          "DISTRIBUTE"])

        # ============================================================
        # 8. IDEMPOTENCY: a further drain re-publishes nothing, re-transitions
        #    nothing
        # ============================================================
        Worker(d, registry=reg, name="e2e").run(max_ticks=50)
        self.assertEqual(deploy_fake.calls, 1)
        ev3 = load_events(d).all()
        self.assertEqual(len([e for e in ev3 if e["type"] == "DEPLOYMENT_COMPLETE"]), 1)
        self.assertEqual(len([e for e in ev3
                              if e["type"] == "OPPORTUNITY_TRANSITIONED"
                              and e["data"].get("to") == "LIVE"]), 1)
        self.assertEqual(load_opportunities(d).get(OID)["state"], "LIVE")

        # ============================================================
        # 9. RESTART SAFETY (assertion K): fresh objects from disk, fresh
        #    worker -> state unchanged, nothing re-runs
        # ============================================================
        q_r = load_tasks(d)
        ev_r = load_events(d)
        counts_before = q_r.counts()
        seq_before = ev_r.last_seq()
        Worker(d, registry=_registry(_CountingDeploy(), _FakeMeasurement()),
               name="e2e-restarted").run(max_ticks=50)
        self.assertEqual(load_tasks(d).counts(), counts_before)
        self.assertEqual(load_events(d).last_seq(), seq_before)
        self.assertEqual(load_opportunities(d).get(OID)["state"], "LIVE")

        # ============================================================
        # 10. THE STOP POINT - payment/revenue/delivery/first-sale are NOT
        #     reached. Proven honestly, no shortcut.
        # ============================================================
        # F: no revenue was booked - there is no payment-event -> ledger bridge
        self.assertFalse((d / "revenue.json").exists())
        # the fake payment payload IS in the task output, but nothing consumes it
        check_rev = load_tasks(d).get(type_to_id["CHECK_REVENUE"])
        self.assertEqual(check_rev.status, "SUCCEEDED")
        self.assertTrue(check_rev.output.get("payment_detected"))
        # G: no delivery - the chain never even creates a DELIVER task
        self.assertFalse((d / "deliveries.json").exists())
        self.assertNotIn("DELIVER", {t.task_type for t in load_tasks(d).all()})
        # H: the opportunity never advanced past LIVE
        seen_states = {t["next_state"]
                       for t in load_opportunities(d).get(OID)["transitions"]}
        for beyond in ("FIRST_VISITOR", "FIRST_LEAD", "FIRST_SALE",
                       "DELIVERING", "ACTIVE", "PROFITABLE"):
            self.assertNotIn(beyond, seen_states)
        # I: no OPTIMIZE task was auto-created after the (fake) sale signal
        self.assertNotIn("OPTIMIZE", {t.task_type for t in load_tasks(d).all()})

        # ============================================================
        # 11-14. cross-cutting proofs
        # ============================================================
        # A: one opportunity id throughout
        self.assertTrue(all(t.opportunity_id == OID for t in load_tasks(d).all()))
        oid_events = [e for e in load_events(d).all() if e["opportunity_id"]]
        self.assertTrue(all(e["opportunity_id"] == OID for e in oid_events))

        # M: every event persisted with a strictly monotonic seq
        seqs = [e["seq"] for e in load_events(d).all()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        got_types = {e["type"] for e in load_events(d).all()}
        for want in ("TASK_CREATED", "TASK_READY", "TASK_STARTED",
                     "TASK_SUCCEEDED", "TASK_BLOCKED", "TASK_UNBLOCKED",
                     "DEPLOYMENT_COMPLETE", "OPPORTUNITY_TRANSITIONED"):
            self.assertIn(want, got_types)

        # L: no real external side effects
        for artefact in ("llm_spend.json", "outreach.json", "messages.json"):
            self.assertFalse((d / artefact).exists(), artefact)

        # final supported state
        self.assertEqual(load_opportunities(d).get(OID)["state"], "LIVE")
        row = execution_view(d, OID)[0]
        self.assertEqual(row["state"], "LIVE")
        self.assertTrue(valid_live_url(row["live_url"]))

    # -----------------------------------------------------------------
    def test_failed_deploy_never_produces_live(self):
        """Same real path, but the external deploy fails -> the opportunity
        must NOT reach LIVE and no DEPLOYMENT_COMPLETE is emitted."""
        d = self.d
        opportunity_engine.generate(d, n=6)
        OID = load_opportunities(d).by_status("discovered")[0]["id"]
        accept_opportunity(d, OID, actor="founder")
        q = load_tasks(d)
        deploy_id = next(t.task_id for t in q.by_opportunity(OID)
                         if t.task_type == "DEPLOY")

        reg = _registry(FakeDeploymentAdapter(fail=True, error="pages 503"),
                        _FakeMeasurement())
        Worker(d, registry=reg, name="e2e").run(max_ticks=100)
        release_task(d, deploy_id, actor="founder")
        Worker(d, registry=reg, name="e2e").run(max_ticks=100)

        s = load_opportunities(d).get(OID)
        self.assertNotEqual(s["state"], "LIVE")
        self.assertNotIn("LIVE", {t["next_state"] for t in s["transitions"]})
        self.assertNotIn("DEPLOYMENT_COMPLETE",
                         [e["type"] for e in load_events(d).all()])
        self.assertIn(load_tasks(d).get(deploy_id).status,
                      ("FAILED_RETRYABLE", "FAILED_FINAL"))
        self.assertFalse((d / "revenue.json").exists())

    # -----------------------------------------------------------------
    def test_this_file_takes_no_shortcuts(self):
        """Anti-cheating guard: this E2E file must not touch state directly."""
        src = Path(__file__).read_text(encoding="utf-8")
        # keep only real code: drop the module docstring, drop this method,
        # drop comment lines
        after_imports = src.split("\nimport ", 1)[-1]
        body = after_imports.split("def test_this_file_takes_no_shortcuts")[0]
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        for forbidden in (".set_status(", ".transition(", ".record_deployment(",
                          ".record_payment(", "._by_id", ".mark_accepted(",
                          ".mark_launched(", ".record_result("):
            self.assertNotIn(forbidden, code,
                             f"E2E test must not call {forbidden}")


if __name__ == "__main__":
    unittest.main()
