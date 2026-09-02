"""PHASE 6 - the classifier is the binding security instance for the
execution layer, and no execution path skips it.

Real: opportunity store, TaskQueue, Worker, EventLog, state machine,
acceptance chain. Fakes: offline task adapters only. No network, no money,
no LLM, no identity/legal action.
"""

import tempfile
import unittest
from pathlib import Path

from revenue_os import action_class as ac
from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.events import load_events
from revenue_os.execution import ExecutionTask, load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.task_adapters import DeployTaskAdapter, default_registry
from revenue_os.worker import (
    AdapterRegistry,
    AdapterResult,
    TaskAdapter,
    Worker,
    enqueue,
    run_worker,
)


class _Ok(TaskAdapter):
    def __init__(self, types, output=None, *, authorized=None):
        self.task_types = tuple(types)
        self._out = output or {"done": True}
        self.calls = 0
        if authorized is not None:
            self.authorized = authorized

    def run(self, ctx):
        self.calls += 1
        return AdapterResult(ok=True, output=dict(self._out))


class _LeakAdapter(TaskAdapter):
    """Simulates an adapter that reaches a money/PayPal/e-mail/LLM leak
    path - the guard must fire because the Worker runs inside
    autonomous_context()."""

    task_types = ("ANALYZE",)

    def __init__(self):
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        ac.guard_no_money_in_autonomy("spend money")   # raises inside autonomy
        return AdapterResult(ok=True, output={"done": True})


def _reg(*adapters):
    r = AdapterRegistry()
    for a in adapters:
        r.register(a)
    return r


class Phase6Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _opp(self, state="READY_TO_DEPLOY"):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Test offer", category="saas",
                                   est_revenue_eur=120))["id"]
        for st in ["SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY"]:
            if s.get(oid)["state"] == state:
                break
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        return oid

    def _task(self, oid, ttype):
        return next(t for t in load_tasks(self.d).by_opportunity(oid)
                    if t.task_type == ttype)


class SafeAndAuthorizedRun(Phase6Base):
    def test_safe_autonomous_task_executes(self):
        oid = self._opp("PLANNING")
        ok = _Ok(("BUILD_PAGE",))
        enqueue(self.d, oid, "BUILD_PAGE")
        Worker(self.d, registry=_reg(ok)).run()
        self.assertEqual(ok.calls, 1)
        self.assertEqual(self._task(oid, "BUILD_PAGE").status, "SUCCEEDED")

    def test_external_authorized_with_authorized_adapter_executes(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DEPLOY",), {"live_url": "https://x.pages.test/i/index.html"},
                 authorized=True)
        enqueue(self.d, oid, "DEPLOY")            # no requires_approval
        Worker(self.d, registry=_reg(ok)).run()
        self.assertEqual(ok.calls, 1)
        self.assertEqual(self._task(oid, "DEPLOY").status, "SUCCEEDED")

    def test_external_authorized_via_human_release_executes_even_if_unauthorized(self):
        oid = self._opp("READY_TO_DEPLOY")
        t = enqueue(self.d, oid, "DEPLOY", requires_approval=True,
                    approval_type="money")
        # unauthorized adapter (no `authorized` attr)
        ok = _Ok(("DEPLOY",), {"live_url": "https://x.pages.test/i/index.html"})
        run_worker(self.d, registry=_reg(ok))
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")
        self.assertEqual(ok.calls, 0)

        release_task(self.d, t.task_id, actor="founder")
        run_worker(self.d, registry=_reg(ok))
        self.assertEqual(ok.calls, 1)
        self.assertEqual(self._task(oid, "DEPLOY").status, "SUCCEEDED")


class BlockedClasses(Phase6Base):
    def test_external_authorized_without_authorization_is_blocked(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DEPLOY",))                     # not authorized, not released
        enqueue(self.d, oid, "DEPLOY")
        run_worker(self.d, registry=_reg(ok))
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")
        self.assertEqual(self._task(oid, "DEPLOY").approval_type, "money")
        self.assertEqual(ok.calls, 0)
        self.assertIn("TASK_BLOCKED",
                      [e["type"] for e in load_events(self.d).all()])

    def test_money_task_is_blocked_not_executed(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DEPLOY",), {"live_url": "https://x.pages.test/i/index.html"},
                 authorized=True)
        enqueue(self.d, oid, "DEPLOY", input={"has_checkout": True})
        # even a working, authorized adapter must not run a MONEY task
        for _ in range(4):
            run_worker(self.d, registry=_reg(ok))
        self.assertEqual(ok.calls, 0)
        t = self._task(oid, "DEPLOY")
        self.assertEqual(t.status, "BLOCKED_APPROVAL")
        self.assertEqual(t.approval_type, "money")

    def test_tos_blocked_task_is_permanently_failed(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DISTRIBUTE",))
        enqueue(self.d, oid, "DISTRIBUTE", input={"channel": "reddit"})
        run_worker(self.d, registry=_reg(ok))
        t = self._task(oid, "DISTRIBUTE")
        self.assertEqual(t.status, "FAILED_FINAL")
        self.assertEqual(ok.calls, 0)
        self.assertIn("TOS_BLOCKED", t.error)
        # retry / re-resolve cannot revive it
        for _ in range(3):
            run_worker(self.d, registry=_reg(ok))
        self.assertEqual(self._task(oid, "DISTRIBUTE").status, "FAILED_FINAL")
        self.assertEqual(ok.calls, 0)

    def test_unknown_task_type_cannot_even_be_enqueued(self):
        q = load_tasks(self.d)
        with self.assertRaises(Exception):
            q.create("opp", "SEND_MONEY")


class NoBypass(Phase6Base):
    def test_retry_and_reresolve_cannot_bypass_classification(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DEPLOY",), {"live_url": "https://x.pages.test/i/index.html"},
                 authorized=True)
        enqueue(self.d, oid, "DEPLOY", input={"has_checkout": True})
        # drain repeatedly: reclaim_stale + requeue_due + resolve every tick
        for _ in range(6):
            run_worker(self.d, registry=_reg(ok))
        self.assertEqual(ok.calls, 0)
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")

    def test_restart_keeps_a_blocked_task_blocked(self):
        oid = self._opp("READY_TO_DEPLOY")
        ok = _Ok(("DEPLOY",), authorized=True)
        enqueue(self.d, oid, "DEPLOY", input={"has_checkout": True})
        run_worker(self.d, registry=_reg(ok))
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")
        # "restart": brand-new worker + queue read from disk
        run_worker(self.d, registry=_reg(_Ok(("DEPLOY",), authorized=True)))
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")

    def test_restart_keeps_an_approved_task_runnable_exactly_once(self):
        oid = self._opp("READY_TO_DEPLOY")
        t = enqueue(self.d, oid, "DEPLOY", requires_approval=True,
                    approval_type="money")
        run_worker(self.d, registry=_reg(_Ok(("DEPLOY",))))
        release_task(self.d, t.task_id, actor="founder")

        reloaded = load_tasks(self.d).get(t.task_id)
        self.assertTrue(reloaded.approval_granted)          # persisted
        self.assertEqual(reloaded.approval_granted_by, "founder")

        ok = _Ok(("DEPLOY",), {"live_url": "https://x.pages.test/i/index.html"})
        run_worker(self.d, registry=_reg(ok))              # fresh worker
        self.assertEqual(ok.calls, 1)
        self.assertEqual(self._task(oid, "DEPLOY").status, "SUCCEEDED")

    def test_approval_state_round_trips_through_disk(self):
        q = load_tasks(self.d)
        t = q.create("opp-x", "DEPLOY", requires_approval=True,
                     approval_type="money")
        q.resolve_dependencies()
        q.unblock(t.task_id, by="alice")
        q.save()
        again = load_tasks(self.d).get(t.task_id)
        self.assertTrue(again.approval_granted)
        self.assertEqual(again.approval_granted_by, "alice")
        self.assertFalse(again.requires_approval)

    def test_autonomous_context_leak_guard_fires_during_task_execution(self):
        oid = self._opp("READY_TO_DEPLOY")
        leak = _LeakAdapter()
        enqueue(self.d, oid, "ANALYZE")
        run_worker(self.d, registry=_reg(leak))
        t = self._task(oid, "ANALYZE")
        self.assertEqual(t.status, "FAILED_FINAL")
        self.assertIn("firewall blocked the adapter", t.error)
        self.assertEqual(leak.calls, 1)                    # ran, then refused


class EndToEnd(Phase6Base):
    def _accepted(self):
        opportunity_engine.generate(self.d, n=8)
        oid = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, oid, actor="founder")
        return oid

    def test_full_chain_classifier_gates_every_task(self):
        oid = self._accepted()

        # DEPLOY approval bucket is DERIVED from the classifier, not hard-coded
        from revenue_os.acceptance import _DEPLOY_APPROVAL
        self.assertEqual(_DEPLOY_APPROVAL, "money")
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")
        self.assertEqual(self._task(oid, "DEPLOY").approval_type, "money")

        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://e2e.pages.test")))

        # a forbidden third-party post sneaked into the queue -> permanently
        # failed by the classifier, never executed
        enqueue(self.d, oid, "DISTRIBUTE", input={"channel": "linkedin"},
                idempotency_key=f"tos:{oid}")

        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)

        types = [e["type"] for e in load_events(self.d).all()]
        tos = next(t for t in load_tasks(self.d).by_opportunity(oid)
                   if (t.input or {}).get("channel") == "linkedin")
        self.assertEqual(tos.status, "FAILED_FINAL")
        self.assertNotIn(tos.task_id,
                         [e.get("task_id") for e in load_events(self.d).all()
                          if e["type"] == "TASK_STARTED"])

        # SAFE tasks ran; DEPLOY still gated
        self.assertEqual(self._task(oid, "PLAN").status, "SUCCEEDED")
        self.assertEqual(self._task(oid, "DEPLOY").status, "BLOCKED_APPROVAL")
        s = load_opportunities(self.d).get(oid)
        self.assertIn(s["state"], ("BUILDING", "VALIDATING", "READY_TO_DEPLOY"))

        # release the gate -> DEPLOY runs through the real architecture
        release_task(self.d, self._task(oid, "DEPLOY").task_id, actor="founder")
        Worker(self.d, registry=reg, name="e2e").run(max_ticks=100)
        self.assertEqual(self._task(oid, "DEPLOY").status, "SUCCEEDED")
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")


if __name__ == "__main__":
    unittest.main()
