"""Opportunity acceptance -> execution chain (Phase 3)."""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import acceptance, autonomy
from revenue_os.acceptance import (
    AcceptanceError,
    abandon_opportunity,
    accept_opportunity,
    execution_view,
)
from revenue_os.approvals import load_approvals
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.worker import run_worker


def _form(**kw):
    return {k: [str(v)] for k, v in kw.items()}


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _opp(self, state="SCORED", **extra):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Cold-email teardown pack",
                                   category="saas", est_revenue_eur=150,
                                   target_customer="B2B founders", **extra))["id"]
        if state != "DISCOVERED":
            for st in ("SCORED", "SELECTED"):
                if s.get(oid)["state"] == state:
                    break
                s.transition(oid, st, reason="setup", source="test")
        s.save()
        return oid


class AcceptTests(AcceptanceBase):
    def test_accept_moves_to_selected_and_builds_the_chain(self):
        oid = self._opp("DISCOVERED")
        r = accept_opportunity(self.d, oid, actor="owner")
        self.assertEqual(r["state"], "SELECTED")
        self.assertEqual([c["task_type"] for c in r["chain"]],
                         [t for t, _, _ in acceptance.CHAIN])
        q = load_tasks(self.d)
        self.assertEqual(len(q), len(acceptance.CHAIN))
        # PLAN has no deps -> READY; the rest wait
        plan = next(t for t in q.all() if t.task_type == "PLAN")
        self.assertEqual(plan.status, "READY")
        deploy = next(t for t in q.all() if t.task_type == "DEPLOY")
        self.assertEqual(deploy.status, "BLOCKED_APPROVAL")
        self.assertEqual(deploy.approval_type, "money")
        # opportunity carries the breadcrumb; autonomy will skip it
        self.assertTrue(load_opportunities(self.d).is_accepted(oid))
        evs = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("TASK_CREATED", evs)
        self.assertIn("TASK_BLOCKED", evs)
        self.assertIn("OPPORTUNITY_TRANSITIONED", evs)

    def test_accept_is_idempotent(self):
        oid = self._opp("SELECTED")
        a = accept_opportunity(self.d, oid)
        b = accept_opportunity(self.d, oid)
        self.assertEqual(len(a["created"]), len(acceptance.CHAIN))
        self.assertEqual(b["created"], [])
        self.assertEqual(len(b["reused"]), len(acceptance.CHAIN))
        self.assertEqual(len(load_tasks(self.d)), len(acceptance.CHAIN))

    def test_accept_does_not_create_an_approval_request(self):
        # acceptance is a BUSINESS decision - separate from the firewall
        oid = self._opp()
        accept_opportunity(self.d, oid)
        appr = load_approvals(self.d)
        self.assertEqual(appr.counts()["money"]["pending"], 0)

    def test_accept_rejects_abandoned(self):
        oid = self._opp("SELECTED")
        abandon_opportunity(self.d, oid)
        with self.assertRaises(AcceptanceError):
            accept_opportunity(self.d, oid)

    def test_accept_unknown_opportunity(self):
        with self.assertRaises(AcceptanceError):
            accept_opportunity(self.d, "opp_missing")


class ChainExecutionTests(AcceptanceBase):
    def test_worker_runs_the_accepted_chain_to_the_deploy_gate(self):
        oid = self._opp("SELECTED")
        accept_opportunity(self.d, oid, actor="owner")
        run_worker(self.d, max_ticks=50)          # real default registry

        q = load_tasks(self.d)
        by_type = {t.task_type: t for t in q.all()}
        self.assertEqual(by_type["PLAN"].status, "SUCCEEDED")
        self.assertEqual(by_type["BUILD_PAGE"].status, "SUCCEEDED")
        self.assertIn(by_type["VALIDATE_PAGE"].status,
                      ("SUCCEEDED", "FAILED_FINAL"))   # QC verdict is real
        # DEPLOY never runs: still gated on the money approval
        self.assertEqual(by_type["DEPLOY"].status, "BLOCKED_APPROVAL")
        # distribution / measurement wait on a real live URL that does not exist
        for tt in ("DISTRIBUTE", "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE"):
            self.assertIn(by_type[tt].status, ("PENDING", "FAILED_FINAL"))
        self.assertNotEqual(by_type["DISTRIBUTE"].status, "SUCCEEDED")

        state = load_opportunities(self.d).get(oid)["state"]
        self.assertIn(state, ("BUILDING", "VALIDATING", "READY_TO_DEPLOY"))
        # nothing external happened
        self.assertFalse((self.d / "deliveries.json").exists())
        self.assertFalse((self.d / "revenue.json").exists())

    def test_opportunity_never_reaches_live_without_deploy(self):
        oid = self._opp("SELECTED")
        accept_opportunity(self.d, oid)
        run_worker(self.d, max_ticks=50)
        s = load_opportunities(self.d).get(oid)
        self.assertNotEqual(s["state"], "LIVE")
        self.assertNotIn("LIVE", [t["next_state"] for t in s["transitions"]])


class AbandonTests(AcceptanceBase):
    def test_abandon_cancels_tasks_and_moves_state(self):
        oid = self._opp("SELECTED")
        accept_opportunity(self.d, oid)
        r = abandon_opportunity(self.d, oid, actor="owner", reason="no demand")
        self.assertEqual(r["state"], "ABANDONED")
        self.assertTrue(r["cancelled"])
        q = load_tasks(self.d)
        self.assertTrue(all(t.status in ("CANCELLED",) for t in q.all()))
        self.assertIn("TASK_CANCELLED", [e["type"] for e in load_events(self.d).all()])

    def test_abandon_after_partial_execution(self):
        oid = self._opp("SELECTED")
        accept_opportunity(self.d, oid)
        run_worker(self.d, max_ticks=3)
        abandon_opportunity(self.d, oid)
        q = load_tasks(self.d)
        # succeeded tasks stay succeeded; the rest are cancelled
        for t in q.all():
            self.assertIn(t.status, ("SUCCEEDED", "FAILED_FINAL", "CANCELLED"))


class AutonomyIsolationTests(AcceptanceBase):
    def test_autonomy_loop_skips_accepted_opportunities(self):
        (self.d / "candidates.json").write_text(json.dumps([
            {"name": "c", "description": "founders first paying customers "
             "onboarding SaaS API docs cold email research changelog"}]))
        autonomy.run_cycle(self.d)
        s = load_opportunities(self.d)
        target = s.by_status("evaluating")[0]["id"]
        accept_opportunity(self.d, target, actor="owner")

        before = dict(load_opportunities(self.d).get(target))
        autonomy.run_cycle(self.d)
        autonomy.run_cycle(self.d)
        after = load_opportunities(self.d).get(target)
        # the loop did not touch its legacy status or its state
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["state"], before["state"])


class ViewTests(AcceptanceBase):
    def test_execution_view_shape(self):
        oid = self._opp("SELECTED")
        accept_opportunity(self.d, oid, actor="owner")
        rows = execution_view(self.d)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["opportunity_id"], oid)
        self.assertEqual(row["state"], "SELECTED")
        self.assertTrue(row["accepted"])
        self.assertEqual(row["accepted_by"], "owner")
        self.assertEqual(row["current_task"], "PLAN")     # the one READY task
        self.assertIn("DEPLOY needs a money approval", row["blocker"])
        self.assertEqual(len(row["tasks"]), len(acceptance.CHAIN))

    def test_execution_view_ignores_untouched_opportunities(self):
        self._opp("SCORED")
        self.assertEqual(execution_view(self.d), [])


class JarvisWiringTests(AcceptanceBase):
    def test_accept_abandon_run_worker_through_apply_control(self):
        from revenue_os.jarvis_server import (
            apply_control,
            jarvis_snapshot,
            render_console,
        )
        oid = self._opp("SCORED")

        msg = apply_control(self.d, "me", _form(action="accept-opportunity", opp=oid))
        self.assertIn("accepted", msg)
        snap = jarvis_snapshot(self.d)
        self.assertEqual(len(snap["execution"]), 1)
        self.assertIn("EXECUTION", render_console(self.d, csrf="t"))

        msg = apply_control(self.d, "me", _form(action="run-worker", max_ticks=10))
        self.assertIn("started", msg)
        # wait for the background job
        import time
        for _ in range(50):
            if not jarvis_snapshot(self.d)["job"]["running"]:
                break
            time.sleep(0.1)
        q = load_tasks(self.d)
        self.assertEqual(next(t for t in q.all() if t.task_type == "PLAN").status,
                         "SUCCEEDED")

        msg = apply_control(self.d, "me", _form(action="abandon-opportunity", opp=oid))
        self.assertIn("abandoned", msg)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "ABANDONED")

    def test_accept_unknown_opportunity_is_a_flash_error(self):
        from revenue_os.jarvis_server import apply_control
        msg = apply_control(self.d, "me", _form(action="accept-opportunity", opp="nope"))
        self.assertIn("error", msg)


if __name__ == "__main__":
    unittest.main()
