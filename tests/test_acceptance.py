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
    deliver_now,
    execution_view,
    pending_actions,
)
from revenue_os.approvals import load_approvals
from revenue_os.cli import main as _cli_main
from revenue_os.delivery_adapters import FakeDeliveryAdapter
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


# ---------------------------------------------------------------------------
# Phase 11-real P1-7: deliver_now() - human-triggered real delivery
# ---------------------------------------------------------------------------

class DeliverNowTests(AcceptanceBase):
    def _live_opp_with_deliver_task(self, *, customer_ref="buyer@example.test",
                                    payment_ref="paypal:CAP-1", with_product=True,
                                    include_customer_ref=True, include_payment_ref=True):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Cold-email pack", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE", "MEASURING", "FIRST_SALE"):
            s.transition(oid, st, reason="setup", source="test")
        s.record_deployment(oid, {"live_url": "https://x.pages.test/o/index.html",
                                  "provider": "fake"})
        s.save()

        if with_product:
            product_dir = self.d / "deliverables" / oid
            product_dir.mkdir(parents=True, exist_ok=True)
            (product_dir / "product.md").write_text(
                "# Cold-email pack\n\nreal product content", encoding="utf-8")

        inp = {}
        if include_payment_ref:
            inp["payment_ref"] = payment_ref
        if include_customer_ref:
            inp["customer_ref"] = customer_ref
        q = load_tasks(self.d)
        t = q.create(oid, "DELIVER", priority=7,
                     idempotency_key=f"deliver:{oid}:{payment_ref}", input=inp)
        q.mark_failed(t.task_id, "no delivery provider is configured", retryable=False)
        q.save()
        return oid, t.task_id, payment_ref

    def test_A_successful_delivery_sends_and_reaches_active(self):
        oid, task_id, pref = self._live_opp_with_deliver_task()
        fake = FakeDeliveryAdapter()
        result = deliver_now(self.d, oid, adapter=fake, actor="founder")

        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["state"], "ACTIVE")
        self.assertEqual(fake.calls, 1)

        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["state"], "ACTIVE")
        self.assertTrue(rec["execution"]["deliveries"][pref]["success"])
        # the original DELIVER task's own status is deliberately untouched
        # (terminal statuses cannot be rewritten - execution.py's _LEGAL
        # table) - it remains an accurate record that the AUTONOMOUS
        # attempt failed; record_delivery + the state transition are the
        # authoritative facts that the product really was delivered.
        self.assertEqual(load_tasks(self.d).get(task_id).status, "FAILED_FINAL")
        types = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("DELIVERY_COMPLETE", types)
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "ACTIVE"]), 1)

    def test_C_idempotent_second_call_is_a_noop(self):
        oid, task_id, pref = self._live_opp_with_deliver_task()
        fake = FakeDeliveryAdapter()
        deliver_now(self.d, oid, adapter=fake, actor="founder")
        result2 = deliver_now(self.d, oid, adapter=fake, actor="founder")

        self.assertEqual(result2["outcome"], "already_delivered")
        self.assertEqual(fake.calls, 1)          # never sent twice
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DELIVERY_COMPLETE"]), 1)

    def test_unknown_opportunity_fails_closed(self):
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, "opp_" + "a" * 12, adapter=FakeDeliveryAdapter())

    def test_no_deliver_task_fails_closed(self):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="no-deliver", category="saas"))["id"]
        s.save()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=FakeDeliveryAdapter())

    def test_ambiguous_deliver_tasks_require_payment_ref(self):
        oid, _, _ = self._live_opp_with_deliver_task(payment_ref="paypal:CAP-1")
        q = load_tasks(self.d)
        q.create(oid, "DELIVER", priority=7,
                 idempotency_key=f"deliver:{oid}:paypal:CAP-2",
                 input={"payment_ref": "paypal:CAP-2", "customer_ref": "b@example.test"})
        q.save()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=FakeDeliveryAdapter())
        # disambiguated by payment_ref -> works
        result = deliver_now(self.d, oid, payment_ref="paypal:CAP-1",
                             adapter=FakeDeliveryAdapter())
        self.assertEqual(result["outcome"], "delivered")

    def test_missing_payment_ref_on_the_task_fails_closed(self):
        oid, _, _ = self._live_opp_with_deliver_task(include_payment_ref=False)
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=FakeDeliveryAdapter())

    def test_F_missing_customer_ref_fails_closed_no_send(self):
        oid, task_id, _ = self._live_opp_with_deliver_task(include_customer_ref=False)
        fake = FakeDeliveryAdapter()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")
        self.assertEqual(load_tasks(self.d).get(task_id).status, "FAILED_FINAL")

    def test_F_missing_product_file_fails_closed_no_send(self):
        oid, task_id, _ = self._live_opp_with_deliver_task(with_product=False)
        fake = FakeDeliveryAdapter()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")

    def test_blocked_adapter_fails_closed_no_state_change(self):
        oid, task_id, _ = self._live_opp_with_deliver_task()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=FakeDeliveryAdapter(blocked=True))
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")
        self.assertEqual(load_tasks(self.d).get(task_id).status, "FAILED_FINAL")
        self.assertFalse((self.d / "deliverables" / oid / "product.md").read_text(
            encoding="utf-8") == "")   # the file itself is untouched

    def test_failed_adapter_fails_closed_no_state_change(self):
        oid, task_id, _ = self._live_opp_with_deliver_task()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=FakeDeliveryAdapter(fail=True, error="smtp 550"))
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")

    def test_exact_product_bytes_are_what_gets_delivered(self):
        oid, task_id, _ = self._live_opp_with_deliver_task()
        captured = {}

        class _Capturing(FakeDeliveryAdapter):
            def deliver(self, artifact, recipient):
                captured["files"] = dict(artifact.files)
                captured["recipient"] = recipient.reference
                return super().deliver(artifact, recipient)

        deliver_now(self.d, oid, adapter=_Capturing())
        self.assertEqual(captured["recipient"], "buyer@example.test")
        self.assertIn("product.md", captured["files"])
        self.assertIn(b"real product content", captured["files"]["product.md"])

    # -- P1-12: explicit --email / customer_ref override -------------------
    def test_P1_12_override_used_when_task_has_no_customer_ref(self):
        oid, task_id, _ = self._live_opp_with_deliver_task(include_customer_ref=False)
        captured = {}

        class _Capturing(FakeDeliveryAdapter):
            def deliver(self, artifact, recipient):
                captured["recipient"] = recipient.reference
                return super().deliver(artifact, recipient)

        result = deliver_now(self.d, oid, adapter=_Capturing(),
                             customer_ref="manual@example.test", actor="founder")
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["customer_ref_source"], "override")
        self.assertEqual(result["customer_ref"], "manual@example.test")
        self.assertEqual(captured["recipient"], "manual@example.test")
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "ACTIVE")

    def test_P1_12_captured_payment_ref_wins_over_override(self):
        oid, _, _ = self._live_opp_with_deliver_task(customer_ref="paid@example.test")
        captured = {}

        class _Capturing(FakeDeliveryAdapter):
            def deliver(self, artifact, recipient):
                captured["recipient"] = recipient.reference
                return super().deliver(artifact, recipient)

        result = deliver_now(self.d, oid, adapter=_Capturing(),
                             customer_ref="other@example.test", actor="founder")
        self.assertEqual(captured["recipient"], "paid@example.test")
        self.assertEqual(result["customer_ref_source"], "payment")

    def test_P1_12_invalid_override_fails_closed_no_send(self):
        oid, _, _ = self._live_opp_with_deliver_task(include_customer_ref=False)
        fake = FakeDeliveryAdapter()
        with self.assertRaises(AcceptanceError):
            deliver_now(self.d, oid, adapter=fake, customer_ref="not-an-email")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "FIRST_SALE")

    def test_P1_12_override_idempotent_second_call_is_a_noop(self):
        oid, _, _ = self._live_opp_with_deliver_task(include_customer_ref=False)
        fake = FakeDeliveryAdapter()
        deliver_now(self.d, oid, adapter=fake, customer_ref="manual@example.test")
        result2 = deliver_now(self.d, oid, adapter=fake, customer_ref="manual@example.test")
        self.assertEqual(result2["outcome"], "already_delivered")
        self.assertEqual(fake.calls, 1)

    def test_P1_12_cli_rejects_invalid_email_and_delivers_nothing(self):
        oid, _, _ = self._live_opp_with_deliver_task(include_customer_ref=False)
        rc = _cli_main(["deliver-product", oid, "--email", "not-an-email",
                        "--data-dir", str(self.d)])
        self.assertEqual(rc, 1)
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["state"], "FIRST_SALE")
        self.assertFalse((rec.get("execution") or {}).get("deliveries"))


# ---------------------------------------------------------------------------
# Phase 11-real P1-9: pending_actions() - read-only visibility
# ---------------------------------------------------------------------------

class PendingActionsTests(AcceptanceBase):
    def _bare_opp(self, *, title="pack"):
        s = OpportunityStore.load(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title=title, category="saas"))["id"]
        s.save()
        return oid

    def test_no_pending_actions_on_an_untouched_store(self):
        self._bare_opp()
        self.assertEqual(pending_actions(self.d), [])

    def test_blocked_approval_is_reported_with_the_blocker_and_no_cli_command(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DEPLOY", requires_approval=True, approval_type="money")
        q.resolve_dependencies()
        q.save()
        self.assertEqual(load_tasks(self.d).get(t.task_id).status, "BLOCKED_APPROVAL")

        rows = pending_actions(self.d)
        release = [r for r in rows if r["action"] == "RELEASE_TASK"]
        self.assertEqual(len(release), 1)
        self.assertEqual(release[0]["opportunity_id"], oid)
        self.assertIn("money", release[0]["detail"])
        self.assertIn(t.task_id, release[0]["command"])
        self.assertIn("JARVIS", release[0]["command"])

    def test_check_payments_reported_when_latest_check_revenue_was_blocked(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "CHECK_REVENUE")
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_failed(
            t.task_id,
            "payment check BLOCKED: no opportunity payment provider is "
            "configured - the payment path is ready, a real provider "
            "adapter must be wired", retryable=False)
        q.save()
        self.assertEqual(load_tasks(self.d).get(t.task_id).status, "FAILED_FINAL")

        rows = pending_actions(self.d)
        cp = [r for r in rows if r["action"] == "CHECK_PAYMENTS"]
        self.assertEqual(len(cp), 1)
        self.assertEqual(cp[0]["opportunity_id"], oid)
        self.assertEqual(cp[0]["command"], "revenue_os check-payments")
        self.assertIn(t.task_id, cp[0]["detail"])

    def test_check_payments_not_reported_once_the_latest_run_succeeded(self):
        # a later, real check-payments run that found nothing (SUCCEEDED,
        # first_sale False) must stop the recommendation - not time-based,
        # purely the latest recorded task outcome
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t1 = q.create(oid, "CHECK_REVENUE", idempotency_key="cr1")
        q.resolve_dependencies()
        q.claim(t1.task_id, "test")
        q.mark_failed(t1.task_id, "payment check BLOCKED: no provider", retryable=False)
        t2 = q.create(oid, "CHECK_REVENUE", idempotency_key="cr2")
        q.resolve_dependencies()
        q.claim(t2.task_id, "test")
        q.mark_succeeded(t2.task_id, {"first_sale": False, "payments": []})
        q.save()

        rows = pending_actions(self.d)
        self.assertEqual([r for r in rows if r["action"] == "CHECK_PAYMENTS"], [])

    def test_deliver_product_reported_when_no_successful_delivery_is_recorded(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DELIVER", input={"payment_ref": "paypal:CAP-1",
                                            "customer_ref": "buyer@example.test"})
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_failed(t.task_id, "delivery BLOCKED: no delivery provider is "
                                 "configured", retryable=False)
        q.save()

        rows = pending_actions(self.d)
        dp = [r for r in rows if r["action"] == "DELIVER_PRODUCT"]
        self.assertEqual(len(dp), 1)
        self.assertEqual(dp[0]["opportunity_id"], oid)
        self.assertEqual(dp[0]["command"],
                         f"revenue_os deliver-product {oid} --payment-ref paypal:CAP-1")

    def test_deliver_product_not_reported_once_delivery_is_recorded_successful(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DELIVER", input={"payment_ref": "paypal:CAP-1",
                                            "customer_ref": "buyer@example.test"})
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_succeeded(t.task_id, {"success": True})
        q.save()

        s = OpportunityStore.load(self.d / "opportunities.json")
        s.record_delivery(oid, "paypal:CAP-1", {"success": True})
        s.save()

        rows = pending_actions(self.d)
        self.assertEqual([r for r in rows if r["action"] == "DELIVER_PRODUCT"], [])

    def test_deliver_product_without_a_payment_ref_is_never_reported(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        q.create(oid, "DELIVER", input={"customer_ref": "buyer@example.test"})
        q.resolve_dependencies()
        q.save()
        rows = pending_actions(self.d)
        self.assertEqual([r for r in rows if r["action"] == "DELIVER_PRODUCT"], [])

    def test_multiple_actions_across_multiple_opportunities_all_reported(self):
        oid_a = self._bare_opp(title="pack-a")
        oid_b = self._bare_opp(title="pack-b")
        q = load_tasks(self.d)
        q.create(oid_a, "DEPLOY", requires_approval=True, approval_type="money")
        q.create(oid_b, "DELIVER", input={"payment_ref": "paypal:CAP-9",
                                          "customer_ref": "x@example.test"})
        q.resolve_dependencies()
        t2 = next(t for t in q.by_opportunity(oid_b) if t.task_type == "DELIVER")
        q.claim(t2.task_id, "test")
        q.mark_failed(t2.task_id, "delivery BLOCKED: no delivery provider is "
                                  "configured", retryable=False)
        q.save()

        rows = pending_actions(self.d)
        actions_by_opp = {(r["opportunity_id"], r["action"]) for r in rows}
        self.assertIn((oid_a, "RELEASE_TASK"), actions_by_opp)
        self.assertIn((oid_b, "DELIVER_PRODUCT"), actions_by_opp)
        self.assertNotIn((oid_a, "DELIVER_PRODUCT"), actions_by_opp)
        self.assertNotIn((oid_b, "RELEASE_TASK"), actions_by_opp)

    def test_cli_pending_actions_prints_no_pending_actions_when_empty(self):
        self._bare_opp()
        rc = _cli_main(["pending-actions", "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)

    def test_cli_pending_actions_prints_the_command_line(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        q.create(oid, "DEPLOY", requires_approval=True, approval_type="money")
        q.resolve_dependencies()
        q.save()
        rc = _cli_main(["pending-actions", "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Phase 11-real P1-10: CLI parity for accept/release/abandon
# ---------------------------------------------------------------------------

class CliLifecycleCommandsTests(AcceptanceBase):
    def _bare_opp(self, *, title="pack"):
        s = OpportunityStore.load(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title=title, category="saas"))["id"]
        s.save()
        return oid

    def _run(self, *args):
        return _cli_main(["--data-dir", str(self.d), *args])

    # --- accept-opportunity ---------------------------------------------
    def test_accept_opportunity_builds_the_real_chain(self):
        oid = self._bare_opp()
        rc = self._run("accept-opportunity", oid, "--actor", "founder")
        self.assertEqual(rc, 0)

        row = execution_view(self.d, oid)[0]
        self.assertTrue(row["accepted"])
        self.assertEqual(row["accepted_by"], "founder")
        self.assertEqual(len(row["tasks"]), len(acceptance.CHAIN))
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "SELECTED")

    def test_accept_opportunity_is_idempotent_via_cli(self):
        oid = self._bare_opp()
        self._run("accept-opportunity", oid)
        rc = self._run("accept-opportunity", oid)
        self.assertEqual(rc, 0)
        self.assertEqual(len(load_tasks(self.d).by_opportunity(oid)),
                         len(acceptance.CHAIN))   # not duplicated

    def test_accept_opportunity_unknown_id_fails_closed(self):
        rc = self._run("accept-opportunity", "opp_" + "a" * 12)
        self.assertEqual(rc, 1)

    # --- release-task -----------------------------------------------------
    def test_release_task_satisfies_the_approval_gate(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DEPLOY", requires_approval=True, approval_type="money")
        q.resolve_dependencies()
        q.save()
        self.assertEqual(load_tasks(self.d).get(t.task_id).status, "BLOCKED_APPROVAL")

        rc = self._run("release-task", t.task_id, "--actor", "founder")
        self.assertEqual(rc, 0)

        released = load_tasks(self.d).get(t.task_id)
        self.assertNotEqual(released.status, "BLOCKED_APPROVAL")
        self.assertTrue(released.approval_granted)
        self.assertEqual(released.approval_granted_by, "founder")
        types = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("TASK_UNBLOCKED", types)

    def test_release_task_never_executes_the_task_itself(self):
        # release only clears the approval gate - it does not run the
        # adapter or touch execution/live_url at all
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DEPLOY", requires_approval=True, approval_type="money")
        q.resolve_dependencies()
        q.save()
        self._run("release-task", t.task_id)
        self.assertNotIn("live_url", load_opportunities(self.d).get(oid).get("execution", {}))
        self.assertNotEqual(load_tasks(self.d).get(t.task_id).status, "SUCCEEDED")

    def test_release_task_unknown_id_fails_closed(self):
        rc = self._run("release-task", "task_" + "a" * 16)
        self.assertEqual(rc, 1)

    def test_release_task_not_blocked_fails_closed(self):
        oid = self._bare_opp()
        q = load_tasks(self.d)
        t = q.create(oid, "DEPLOY")   # PENDING, not BLOCKED_APPROVAL
        q.save()
        rc = self._run("release-task", t.task_id)
        self.assertEqual(rc, 1)

    # --- abandon-opportunity ----------------------------------------------
    def test_abandon_opportunity_cancels_tasks_and_moves_to_abandoned(self):
        oid = self._bare_opp()
        self._run("accept-opportunity", oid)
        rc = self._run("abandon-opportunity", oid, "--reason", "not worth it",
                       "--actor", "founder")
        self.assertEqual(rc, 0)

        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "ABANDONED")
        statuses = {t.status for t in load_tasks(self.d).by_opportunity(oid)}
        self.assertIn("CANCELLED", statuses)
        self.assertNotIn("PENDING", statuses)
        self.assertNotIn("READY", statuses)

    def test_abandon_opportunity_unknown_id_fails_closed(self):
        rc = self._run("abandon-opportunity", "opp_" + "a" * 12)
        self.assertEqual(rc, 1)


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
