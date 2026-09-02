"""The synchronous Worker Executor + its connection to the state machine."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.events import load_events
from revenue_os.execution import TaskQueue, load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.worker import (
    AdapterContext,
    AdapterRegistry,
    AdapterResult,
    TaskAdapter,
    Worker,
    cancel_task,
    enqueue,
    run_worker,
)


def _iso(dt):
    return dt.isoformat()


# --- fake adapters --------------------------------------------------------

class _Ok(TaskAdapter):
    def __init__(self, types, output=None, cost=0.0):
        self.task_types = tuple(types)
        self._out = output or {"done": True}
        self._cost = cost
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return AdapterResult(ok=True, output=dict(self._out), actual_cost=self._cost)


class _Fail(TaskAdapter):
    def __init__(self, types, retryable=True):
        self.task_types = tuple(types)
        self.retryable = retryable
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return AdapterResult(ok=False, error="boom", retryable=self.retryable)


class _Crash(TaskAdapter):
    def __init__(self, types):
        self.task_types = tuple(types)

    def run(self, ctx):
        raise RuntimeError("kaboom")


class _Recorder(TaskAdapter):
    def __init__(self, types):
        self.task_types = tuple(types)
        self.seen_deps = None

    def run(self, ctx):
        self.seen_deps = dict(ctx.dep_outputs)
        return AdapterResult(ok=True, output={"validated": True})


def _reg(*adapters):
    r = AdapterRegistry()
    for a in adapters:
        r.register(a)
    return r


class WorkerBase(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _opp(self, state="SELECTED"):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Test offer", category="saas",
                                   est_revenue_eur=120))["id"]
        # walk a legal path to the desired starting state
        path = ["SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                "READY_TO_DEPLOY"]
        for st in path:
            if s.get(oid)["state"] == state:
                break
            s.transition(oid, st, reason="test setup", source="test")
        s.save()
        return oid


class ExecutionTests(WorkerBase):
    def test_ready_task_is_claimed_and_executed(self):
        oid = self._opp("PLANNING")
        t = enqueue(self.d, oid, "BUILD_PAGE")
        ok = _Ok(("BUILD_PAGE",), {"landing_html": "<h1>x</h1>"}, cost=0.0)
        out = Worker(self.d, registry=_reg(ok), name="w7").run()

        self.assertEqual(out["count"], 1)
        q = load_tasks(self.d)
        rec = q.get(t.task_id)
        self.assertEqual(rec.status, "SUCCEEDED")
        self.assertEqual(rec.worker, "w7")
        self.assertTrue(rec.started_at and rec.finished_at)
        self.assertEqual(rec.output["landing_html"], "<h1>x</h1>")
        self.assertEqual(ok.calls, 1)

    def test_success_emits_task_succeeded(self):
        oid = self._opp("PLANNING")
        enqueue(self.d, oid, "BUILD_PAGE")
        Worker(self.d, registry=_reg(_Ok(("BUILD_PAGE",)))).run()
        types = [e["type"] for e in load_events(self.d).all()]
        self.assertEqual(types[:1], ["TASK_CREATED"])
        for want in ("TASK_STARTED", "TASK_SUCCEEDED"):
            self.assertIn(want, types)

    def test_final_failure_from_non_retryable(self):
        oid = self._opp("BUILDING")
        t = enqueue(self.d, oid, "VALIDATE_PAGE")
        Worker(self.d, registry=_reg(_Fail(("VALIDATE_PAGE",), retryable=False))).run()
        q = load_tasks(self.d)
        self.assertEqual(q.get(t.task_id).status, "FAILED_FINAL")
        self.assertIn("TASK_FAILED", [e["type"] for e in load_events(self.d).all()])

    def test_retryable_failure_then_final(self):
        oid = self._opp("BUILDING")
        t = enqueue(self.d, oid, "VALIDATE_PAGE", max_attempts=2)
        fail = _Fail(("VALIDATE_PAGE",), retryable=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        Worker(self.d, registry=_reg(fail)).run(now=_iso(base))
        q = load_tasks(self.d)
        self.assertEqual(q.get(t.task_id).status, "FAILED_RETRYABLE")
        self.assertTrue(q.get(t.task_id).next_retry_at)
        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("TASK_RETRY_SCHEDULED", evs)

        # backoff not elapsed -> nothing runs
        r = Worker(self.d, registry=_reg(fail)).run(now=_iso(base + timedelta(seconds=5)))
        self.assertEqual(r["count"], 0)

        # backoff elapsed -> retried, runs out of attempts -> FAILED_FINAL
        Worker(self.d, registry=_reg(fail)).run(now=_iso(base + timedelta(hours=1)))
        q = load_tasks(self.d)
        self.assertEqual(q.get(t.task_id).status, "FAILED_FINAL")
        self.assertEqual(fail.calls, 2)

    def test_dependencies_gate_execution_and_pass_outputs(self):
        oid = self._opp("PLANNING")
        build = enqueue(self.d, oid, "BUILD_PAGE")
        val = enqueue(self.d, oid, "VALIDATE_PAGE", depends_on=[build.task_id])
        rec = _Recorder(("VALIDATE_PAGE",))
        registry = _reg(_Ok(("BUILD_PAGE",), {"landing_html": "<x>"}), rec)

        summary = Worker(self.d, registry=registry).run()
        order = [p["task_type"] for p in summary["processed"]]
        self.assertEqual(order, ["BUILD_PAGE", "VALIDATE_PAGE"])
        q = load_tasks(self.d)
        self.assertEqual(q.get(val.task_id).status, "SUCCEEDED")
        self.assertEqual(rec.seen_deps, {"BUILD_PAGE": {"landing_html": "<x>"}})

    def test_dependency_failure_fails_dependent_without_running_it(self):
        oid = self._opp("PLANNING")
        build = enqueue(self.d, oid, "BUILD_PAGE")
        val = enqueue(self.d, oid, "VALIDATE_PAGE", depends_on=[build.task_id])
        rec = _Recorder(("VALIDATE_PAGE",))
        Worker(self.d, registry=_reg(_Fail(("BUILD_PAGE",), retryable=False), rec)).run()
        q = load_tasks(self.d)
        self.assertEqual(q.get(val.task_id).status, "FAILED_FINAL")
        self.assertIsNone(rec.seen_deps)          # never executed

    def test_approval_block_is_never_executed(self):
        oid = self._opp("READY_TO_DEPLOY")
        t = enqueue(self.d, oid, "DEPLOY", requires_approval=True,
                    approval_type="money")
        ok = _Ok(("DEPLOY",))
        r = Worker(self.d, registry=_reg(ok)).run()
        self.assertEqual(r["count"], 0)
        q = load_tasks(self.d)
        self.assertEqual(q.get(t.task_id).status, "BLOCKED_APPROVAL")
        self.assertEqual(ok.calls, 0)
        self.assertIn("TASK_BLOCKED", [e["type"] for e in load_events(self.d).all()])

    def test_idempotent_task_runs_once(self):
        oid = self._opp("PLANNING")
        ok = _Ok(("BUILD_PAGE",))
        enqueue(self.d, oid, "BUILD_PAGE", idempotency_key="build:opp:v1")
        Worker(self.d, registry=_reg(ok)).run()
        # a second enqueue with the same key returns the finished task
        again = enqueue(self.d, oid, "BUILD_PAGE", idempotency_key="build:opp:v1")
        r = Worker(self.d, registry=_reg(ok)).run()
        self.assertEqual(r["count"], 0)
        self.assertEqual(ok.calls, 1)
        self.assertEqual(load_tasks(self.d).get(again.task_id).status, "SUCCEEDED")
        succ = [e for e in load_events(self.d).all() if e["type"] == "TASK_SUCCEEDED"]
        self.assertEqual(len(succ), 1)

    def test_adapter_exception_is_recovered_not_corrupted(self):
        oid = self._opp("PLANNING")
        t = enqueue(self.d, oid, "BUILD_PAGE")
        Worker(self.d, registry=_reg(_Crash(("BUILD_PAGE",)))).run()
        q = load_tasks(self.d)                    # reloads cleanly = not corrupt
        rec = q.get(t.task_id)
        self.assertEqual(rec.status, "FAILED_RETRYABLE")
        self.assertIn("kaboom", rec.error)
        self.assertIn("TASK_RETRY_SCHEDULED",
                      [e["type"] for e in load_events(self.d).all()])
        # the queue file is valid JSON
        json.loads((self.d / "tasks.json").read_text())

    def test_no_adapter_fails_cleanly(self):
        oid = self._opp("READY_TO_DEPLOY")
        # SPAWN_VARIANT classifies SAFE_AUTONOMOUS (Phase 6), so the classifier
        # gate passes it through; with no adapter registered it must still
        # fail cleanly and finally.
        t = enqueue(self.d, oid, "SPAWN_VARIANT")   # no SPAWN_VARIANT adapter in _reg()
        Worker(self.d, registry=_reg(_Ok(("BUILD_PAGE",)))).run()
        q = load_tasks(self.d)
        self.assertEqual(q.get(t.task_id).status, "FAILED_FINAL")
        self.assertIn("no adapter", q.get(t.task_id).error)


class TransitionTests(WorkerBase):
    def test_successful_task_drives_the_opportunity_transition(self):
        oid = self._opp("SCORED")
        enqueue(self.d, oid, "SCORE")
        Worker(self.d, registry=_reg(_Ok(("SCORE",)))).run()
        # SCORED -> SELECTED is NOT a task move; SCORE success targets SCORED
        # which is a no-op here. Use PLAN from SELECTED instead:
        oid2 = self._opp("SELECTED")
        enqueue(self.d, oid2, "PLAN")
        Worker(self.d, registry=_reg(_Ok(("PLAN",)))).run()
        s = load_opportunities(self.d)
        self.assertEqual(s.get(oid2)["state"], "PLANNING")
        tr = s.get(oid2)["transitions"][-1]
        self.assertEqual((tr["previous_state"], tr["next_state"]),
                         ("SELECTED", "PLANNING"))
        self.assertEqual(tr["source"], "task")
        evs = [e for e in load_events(self.d).all()
               if e["type"] == "OPPORTUNITY_TRANSITIONED" and e["opportunity_id"] == oid2]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["to"], "PLANNING")

    def test_validate_success_moves_to_ready_to_deploy(self):
        oid = self._opp("BUILDING")
        enqueue(self.d, oid, "VALIDATE_PAGE")
        Worker(self.d, registry=_reg(_Ok(("VALIDATE_PAGE",), {"qc_status": "pass"}))).run()
        self.assertEqual(load_opportunities(self.d).get(oid)["state"],
                         "READY_TO_DEPLOY")

    def test_failed_task_does_not_advance_the_opportunity(self):
        oid = self._opp("BUILDING")
        enqueue(self.d, oid, "VALIDATE_PAGE")
        Worker(self.d, registry=_reg(_Fail(("VALIDATE_PAGE",), retryable=False))).run()
        s = load_opportunities(self.d)
        # the start move to VALIDATING is honest (the task really ran);
        # the SUCCESS move to READY_TO_DEPLOY must NOT have happened.
        self.assertEqual(s.get(oid)["state"], "VALIDATING")
        tos = [e["data"].get("to") for e in load_events(self.d).all()
               if e["type"] == "OPPORTUNITY_TRANSITIONED"]
        self.assertNotIn("READY_TO_DEPLOY", tos)

    def test_transition_skipped_when_not_eligible_never_forced(self):
        oid = self._opp("SCORED")           # not SELECTED
        enqueue(self.d, oid, "PLAN")        # PLAN start targets PLANNING
        Worker(self.d, registry=_reg(_Ok(("PLAN",)))).run()
        s = load_opportunities(self.d)
        self.assertEqual(s.get(oid)["state"], "SCORED")   # unchanged, not forced
        self.assertFalse(any(t.get("forced") for t in s.get(oid)["transitions"]))


class DeterminismTests(WorkerBase):
    def test_multiple_tasks_processed_in_priority_order(self):
        oid = self._opp("PLANNING")
        enqueue(self.d, oid, "ANALYZE", priority=1)
        enqueue(self.d, oid, "BUILD_PAGE", priority=9)
        enqueue(self.d, oid, "OPTIMIZE", priority=5)
        reg = _reg(_Ok(("BUILD_PAGE",)), _Ok(("ANALYZE",)), _Ok(("OPTIMIZE",)))
        summary = Worker(self.d, registry=reg).run()
        self.assertEqual([p["task_type"] for p in summary["processed"]],
                         ["BUILD_PAGE", "OPTIMIZE", "ANALYZE"])

    def test_run_terminates_and_is_not_a_loop(self):
        oid = self._opp("PLANNING")
        for i in range(3):
            enqueue(self.d, oid, "ANALYZE", priority=i)
        summary = Worker(self.d, registry=_reg(_Ok(("ANALYZE",)))).run(max_ticks=50)
        self.assertEqual(summary["count"], 3)
        self.assertIsNone(summary["bounded_at"])      # stopped naturally
        started = [e for e in load_events(self.d).all() if e["type"] == "TASK_STARTED"]
        self.assertEqual(len(started), 3)             # each task started exactly once

    def test_worker_ignores_the_event_log_for_decisions(self):
        # a stray event in the log must not make the worker do anything when
        # the queue has no READY task (no event -> execution feedback loop)
        ev = load_events(self.d)
        ev.emit("TASK_SUCCEEDED", task_id="ghost", opportunity_id="ghost")
        ev.save()
        r = Worker(self.d, registry=_reg(_Ok(("ANALYZE",)))).run()
        self.assertEqual(r["count"], 0)

    def test_restart_resumes_pending_chain(self):
        oid = self._opp("PLANNING")
        build = enqueue(self.d, oid, "BUILD_PAGE")
        enqueue(self.d, oid, "VALIDATE_PAGE", depends_on=[build.task_id])
        # first worker instance does one tick only
        Worker(self.d, registry=_reg(_Ok(("BUILD_PAGE",)))).tick()
        self.assertEqual(load_tasks(self.d).get(build.task_id).status, "SUCCEEDED")
        # a fresh worker (restart) picks up the now-ready validate task
        out = run_worker(self.d, registry=_reg(_Ok(("BUILD_PAGE",)),
                                               _Ok(("VALIDATE_PAGE",))))
        self.assertEqual(out["count"], 1)

    def test_cancel_task_helper_emits_event(self):
        oid = self._opp("PLANNING")
        t = enqueue(self.d, oid, "BUILD_PAGE")
        cancel_task(self.d, t.task_id, reason="opportunity abandoned")
        self.assertEqual(load_tasks(self.d).get(t.task_id).status, "CANCELLED")
        self.assertIn("TASK_CANCELLED",
                      [e["type"] for e in load_events(self.d).all()])


class RealRegistryIntegrationTests(WorkerBase):
    def test_real_agents_run_a_build_validate_chain_without_corruption(self):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(
            title="Cold-email teardown pack for B2B founders", category="saas",
            target_customer="early B2B founders", est_revenue_eur=180,
            required_work="10 annotated teardowns + a checklist"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()

        build = enqueue(self.d, oid, "BUILD_PAGE")
        enqueue(self.d, oid, "VALIDATE_PAGE", depends_on=[build.task_id])

        out = run_worker(self.d)          # the real default registry
        # both tasks reach a terminal state, nothing hangs, queue is valid
        q = load_tasks(self.d)
        for t in q.all():
            self.assertIn(t.status,
                          ("SUCCEEDED", "FAILED_FINAL", "FAILED_RETRYABLE"))
        json.loads((self.d / "tasks.json").read_text())
        self.assertGreaterEqual(out["count"], 1)
        # BUILD_PAGE via the real Content Creator should succeed
        self.assertEqual(q.get(build.task_id).status, "SUCCEEDED")
        self.assertIn("landing_html", q.get(build.task_id).output)
        # opportunity advanced at least into BUILDING
        st = load_opportunities(self.d).get(oid)["state"]
        self.assertIn(st, ("BUILDING", "VALIDATING", "READY_TO_DEPLOY"))


if __name__ == "__main__":
    unittest.main()
