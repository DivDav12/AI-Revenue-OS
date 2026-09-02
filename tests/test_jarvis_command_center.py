"""Command-center behaviours: recommendation engine, human-actions,
financial safety, revenue pipeline, acquisition view, activity feed,
fleet modes, safe stop, draft-first automation, agent detail, and a
dashboard-untouched regression check. Fake/static data only - no LLM,
no network, no money.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from revenue_os import agent_control, jarvis_intel
from revenue_os.jarvis_events import JarvisEvents, load_events, record_event
from revenue_os.jarvis_server import (
    apply_control,
    jarvis_snapshot,
    render_console,
)
from revenue_os.store import Candidate, CandidateStore

_OFFER = {
    "what_is_sold": "Plan", "price": 29.9, "currency": "EUR", "delivery": "digital",
    "positioning": "find first customers", "includes": ["a"], "call_to_action": "Get it",
}


def _form(**kw):
    return {k: [str(v)] for k, v in kw.items()}


def _seed(d: Path, *, status="launched", name="cand"):
    st = CandidateStore(d / "candidates.json")
    st.put(Candidate(name=name, description="d", status=status, total=3.0,
                     verdict="hold", offer=dict(_OFFER), plan={"hypothesis": "h"}))
    st.save()


# ---------------------------------------------------------------------------
# jarvis_intel - pure deterministic functions
# ---------------------------------------------------------------------------

class RecommendationEngineTests(unittest.TestCase):
    def test_paypal_restriction_is_the_top_recommendation(self):
        snap = {
            "blockers": [{"id": "paypal-payee-restricted", "area": "payment",
                          "title": "PayPal checkout blocked",
                          "detail": "PAYEE_ACCOUNT_RESTRICTED"}],
            "financial": {}, "acquisition": {"total": 3, "fresh": 1},
            "counts": {"running": 0}, "pipeline": {}, "job": {}, "agents": [],
        }
        recs = jarvis_intel.recommendations(snap)
        self.assertEqual(recs[0]["severity"], "critical")
        self.assertIn("PayPal", recs[0]["title"])
        self.assertIn("resolve", recs[0]["detail"].lower())

    def test_no_leads_recommends_discovery(self):
        snap = {"blockers": [], "financial": {}, "acquisition": {"total": 0, "fresh": 0},
                "counts": {"running": 0}, "pipeline": {"status": "idle"},
                "job": {}, "agents": []}
        recs = jarvis_intel.recommendations(snap)
        self.assertTrue(any("discovery" in r["detail"].lower() for r in recs))

    def test_running_job_short_circuits(self):
        snap = {"blockers": [{"area": "payment", "detail": "PAYEE"}], "financial": {},
                "acquisition": {}, "counts": {"running": 1},
                "pipeline": {"current_step": "build_store", "done": 4, "total": 10},
                "job": {"running": True, "what": "pipeline"}, "agents": []}
        recs = jarvis_intel.recommendations(snap)
        self.assertEqual(len(recs), 1)
        self.assertIn("build_store", recs[0]["title"])

    def test_idle_fleet_recommends_run_fleet(self):
        snap = {"blockers": [], "financial": {}, "acquisition": {"total": 5, "fresh": 3},
                "counts": {"running": 0}, "pipeline": {"status": "idle"},
                "job": {}, "agents": []}
        recs = jarvis_intel.recommendations(snap)
        self.assertTrue(any(r.get("cta_action") == "run-sweep" for r in recs))

    def test_deterministic(self):
        snap = {"blockers": [], "financial": {}, "acquisition": {"total": 1, "fresh": 0},
                "counts": {"running": 0}, "pipeline": {"status": "prepared",
                "human_gate": {"reason": "QC passed", "human_gated_next": ["x"]}},
                "job": {}, "agents": []}
        self.assertEqual(jarvis_intel.recommendations(snap),
                         jarvis_intel.recommendations(snap))


class HumanActionsTests(unittest.TestCase):
    def test_every_gate_is_spelled_out(self):
        snap = {
            "blockers": [{"id": "pp", "area": "payment", "title": "PayPal",
                          "detail": "PAYEE_ACCOUNT_RESTRICTED"}],
            "agents": [{"id": "store_builder", "name": "Store Builder",
                        "human_gated": True, "gate_acknowledged": False,
                        "why_waiting": "publishes a real page",
                        "next_step_hint": "run build-checkout then deploy",
                        "has_draft": True, "runnable_here": True}],
            "action_queue": [{"name": "cand", "next_action": "record payment",
                              "status": "launched"}],
            "outreach": [{"lead_id": "abc", "title": "HN post"}],
        }
        acts = jarvis_intel.human_actions(snap)
        for a in acts:
            self.assertTrue(a["human_action"])
            self.assertTrue(a["what"])
        money = [a for a in acts if a["affects_money"]]
        self.assertTrue(money)          # PayPal + the launched candidate
        self.assertTrue(any("post it" in a["human_action"].lower()
                            for a in acts if a["area"] == "OUTREACH"))

    def test_acknowledged_gate_drops_out(self):
        snap = {"blockers": [], "action_queue": [], "outreach": [],
                "agents": [{"id": "developer", "name": "Developer AI",
                            "human_gated": True, "gate_acknowledged": True,
                            "why_waiting": "", "has_draft": True}]}
        self.assertEqual(jarvis_intel.human_actions(snap), [])


class FinancialSafetyTests(unittest.TestCase):
    def test_reports_no_spend_capability(self):
        fin = jarvis_intel.financial_safety(
            budget={"presale_cap_eur": 3.0, "presale_cap_usd": 3.2,
                    "presale_remaining_usd": 3.2, "presale_active": True},
            blockers=[{"area": "payment", "detail": "PAYEE_ACCOUNT_RESTRICTED"}],
            llm_spend_summary={"total_cost_usd": 0.0, "api_calls": 0},
            revenue_eur=0.0)
        self.assertEqual(fin["anthropic"]["state"], "DISABLED")
        self.assertEqual(fin["anthropic"]["spent_usd"], 0.0)
        self.assertEqual(fin["paypal"]["state"], "BLOCKED")
        self.assertEqual(fin["money_actions"], "HUMAN ONLY")
        self.assertFalse(fin["can_spend_now"])

    def test_paypal_ready_when_no_blocker(self):
        fin = jarvis_intel.financial_safety(
            budget={}, blockers=[], llm_spend_summary={}, revenue_eur=0.0)
        self.assertEqual(fin["paypal"]["state"], "READY")


class RevenuePipelineTests(unittest.TestCase):
    def test_stage_order_and_paypal_red(self):
        stages = jarvis_intel.revenue_pipeline(
            candidate={"name": "c", "status": "launched"}, checkout_built=True,
            checkout_deployed=False, paypal_blocked=True, intake_count=0,
            plan_count=0, delivered_count=0, revenue_eur=0.0, leads=3,
            outreach_ready=1)
        names = [s["stage"] for s in stages]
        self.assertEqual(names, ["LEAD", "OUTREACH", "CHECKOUT", "PAYPAL", "INTAKE",
                                 "PLAN", "PDF", "DELIVERY", "REVENUE"])
        paypal = next(s for s in stages if s["stage"] == "PAYPAL")
        self.assertEqual(paypal["state"], "red")
        self.assertIn("RESTRICTED", paypal["note"])

    def test_revenue_green_when_booked(self):
        stages = jarvis_intel.revenue_pipeline(
            candidate=None, checkout_built=True, checkout_deployed=True,
            paypal_blocked=False, intake_count=1, plan_count=1, delivered_count=1,
            revenue_eur=29.9, leads=1, outreach_ready=0)
        self.assertEqual(next(s for s in stages if s["stage"] == "REVENUE")["state"],
                         "green")


class AcquisitionViewTests(unittest.TestCase):
    def test_counts_and_recommended_actions(self):
        leads = [
            {"lead_id": "fresh_hi", "title": "help", "age_days": 2, "final_score": 80},
            {"lead_id": "old_hi", "title": "help", "age_days": 60, "final_score": 75},
            {"lead_id": "fresh_lo", "title": "news", "age_days": 1, "final_score": 10},
        ]
        briefs = [{"lead_id": "fresh_hi", "status": "draft"}]
        v = jarvis_intel.acquisition_view(leads=leads, briefs=briefs,
                                          last_discovery={"ts": "2026-08-31T10:00:00"})
        self.assertEqual(v["total"], 3)
        self.assertEqual(v["fresh"], 2)
        self.assertEqual(v["stale"], 1)
        self.assertEqual(v["awaiting_outreach"], 1)
        by = {r["lead_id"]: r for r in v["leads"]}
        self.assertIn("Draft ready", by["fresh_hi"]["recommended_action"])
        self.assertIn("too old", by["old_hi"]["recommended_action"])
        self.assertIn("skip", by["fresh_lo"]["recommended_action"].lower())

    def test_never_has_a_send_action(self):
        v = jarvis_intel.acquisition_view(
            leads=[{"lead_id": "x", "age_days": 1, "final_score": 90, "title": "t"}],
            briefs=[], last_discovery=None)
        for r in v["leads"]:
            self.assertNotIn("send", r["recommended_action"].lower())


# ---------------------------------------------------------------------------
# activity feed
# ---------------------------------------------------------------------------

class ActivityFeedTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_record_and_recent_newest_first(self):
        record_event(self.d, "job", "fleet started")
        record_event(self.d, "action", "pause: ok")
        ev = load_events(self.d).recent(10)
        self.assertEqual(ev[0]["text"], "pause: ok")
        self.assertEqual(ev[1]["text"], "fleet started")

    def test_cap_and_atomic(self):
        e = JarvisEvents(self.d / "jarvis_events.json")
        for i in range(500):
            e.record("x", f"e{i}")
        e.save()
        self.assertEqual(len(load_events(self.d)), 400)
        json.loads((self.d / "jarvis_events.json").read_text())  # valid JSON

    def test_actions_are_logged(self):
        _seed(self.d)
        apply_control(self.d, "me", _form(action="pause"))
        texts = [e["text"] for e in load_events(self.d).recent(5)]
        self.assertTrue(any("pause" in t for t in texts))


# ---------------------------------------------------------------------------
# fleet modes
# ---------------------------------------------------------------------------

class FleetModeTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_default_mode_is_manual(self):
        self.assertEqual(jarvis_snapshot(self.d)["mode"], "manual")

    def test_set_auto_then_manual(self):
        self.assertIn("AUTO", apply_control(self.d, "me", _form(action="set-mode", mode="auto")))
        self.assertEqual(agent_control.load_agent_control(self.d).mode, "auto")
        apply_control(self.d, "me", _form(action="set-mode", mode="manual"))
        self.assertEqual(agent_control.load_agent_control(self.d).mode, "manual")

    def test_paused_mode_pauses_the_fleet(self):
        apply_control(self.d, "me", _form(action="set-mode", mode="paused"))
        ctrl = agent_control.load_agent_control(self.d)
        self.assertTrue(ctrl.is_paused())
        self.assertEqual(ctrl.mode, "paused")
        # leaving paused unpauses
        apply_control(self.d, "me", _form(action="set-mode", mode="manual"))
        self.assertFalse(agent_control.load_agent_control(self.d).is_paused())

    def test_bad_mode_is_rejected(self):
        self.assertTrue(apply_control(self.d, "me",
                        _form(action="set-mode", mode="turbo")).startswith("error"))

    def test_mode_never_bypasses_a_human_gate(self):
        apply_control(self.d, "me", _form(action="set-mode", mode="auto"))
        # a human-gated agent still only produces a draft
        apply_control(self.d, "me", _form(action="run", agent="store_builder"))
        from revenue_os.agent_runner import last_output
        self.assertTrue(last_output(self.d, "build_store")["human_gate_required"])


# ---------------------------------------------------------------------------
# safe stop
# ---------------------------------------------------------------------------

class SafeStopTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_stop_with_no_job_is_an_error(self):
        self.assertTrue(apply_control(self.d, "me",
                        _form(action="stop-job")).startswith("error"))

    def test_stop_request_halts_pipeline_cleanly(self):
        from revenue_os import pipeline

        calls = {"n": 0}

        def stopper():
            calls["n"] += 1
            return calls["n"] >= 3          # let 2 steps run, then stop

        rep = pipeline.run_pipeline(self.d, "cand", restart=True, skip_deploy=True,
                                    should_stop=stopper)
        self.assertEqual(rep["status"], "stopped")
        # state is valid JSON and a later run resumes
        st = json.loads((self.d / "pipeline.json").read_text())
        self.assertEqual(st["status"], "stopped")
        rep2 = pipeline.run_pipeline(self.d, "cand", skip_deploy=True)
        self.assertEqual(rep2["status"], "prepared")

    def test_async_job_honours_stop(self):
        first = apply_control(self.d, "me",
                              _form(action="run-sweep", mode="async", restart="1"))
        self.assertIn("started", first)
        msg = apply_control(self.d, "me", _form(action="stop-job"))
        self.assertIn("STOP REQUESTED", msg)
        for _ in range(80):
            if not jarvis_snapshot(self.d)["job"]["running"]:
                break
            time.sleep(0.25)
        self.assertFalse(jarvis_snapshot(self.d)["job"]["running"])


# ---------------------------------------------------------------------------
# draft-first automation for the marketing agents
# ---------------------------------------------------------------------------

class DraftFirstTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)
        apply_control(self.d, "me", _form(action="run-pipeline", candidate="cand"))

    def tearDown(self):
        self._d.cleanup()

    def test_optimize_and_allocate_produce_drafts_without_touching_money(self):
        from revenue_os.agent_runner import last_output

        for agent, cap in (("campaign_optimizer", "optimize_campaigns"),
                           ("budget_allocator", "allocate_budget"),
                           ("ads_manager", "run_ads")):
            msg = apply_control(self.d, "me", _form(action="run", agent=agent))
            self.assertTrue(msg.startswith("ok"), f"{agent}: {msg}")
            out = last_output(self.d, cap)
            self.assertIsNotNone(out)
            self.assertTrue(out.get("human_gate_required"))

        ba = last_output(self.d, "allocate_budget")
        # a truthy authorizes_spend / launched / spent would mean money moved
        self.assertNotEqual(ba.get("authorizes_spend"), True)
        self.assertNotEqual(ba.get("launched"), True)
        self.assertFalse(ba.get("spent"))


# ---------------------------------------------------------------------------
# agent detail view
# ---------------------------------------------------------------------------

class AgentDetailTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_detail_shows_identity_deps_and_why_run_is_unavailable(self):
        html = render_console(self.d, csrf="t", agent="customer_support")
        self.assertIn("Customer Support", html)
        self.assertIn("Dependencies", html)
        self.assertIn("Execution history", html)
        # customer_support cannot run from JARVIS - the reason is explained,
        # not merely hidden behind a disabled button
        self.assertIn("Run now is unavailable", html)
        self.assertIn("intake", html.lower())

    def test_unknown_agent_is_handled(self):
        html = render_console(self.d, csrf="t", agent="does_not_exist")
        self.assertIn("unknown agent", html)


# ---------------------------------------------------------------------------
# pipeline visualization data
# ---------------------------------------------------------------------------

class PipelineVizTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_every_step_carries_agent_status_and_current_step(self):
        apply_control(self.d, "me", _form(action="run-pipeline", candidate="cand"))
        pipe = jarvis_snapshot(self.d)["pipeline"]
        self.assertEqual(len(pipe["steps"]), pipe["total"])
        for s in pipe["steps"]:
            self.assertIn("agent", s)
            self.assertIn("status", s)
        self.assertIn("current_step", pipe)
        html = render_console(self.d, csrf="t")
        self.assertIn("SKIPPED", html)     # research/analyze_competition
        self.assertIn("COMPLETE", html)


class EcosystemViewTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        (self.d / "candidates.json").write_text(
            '[{"name":"a","description":"founders customers onboarding changelog API docs"}]')

    def tearDown(self):
        self._d.cleanup()

    def test_all_25_agents_are_creatures_with_positions(self):
        from revenue_os.jarvis_server import _ecosystem_data
        eco = _ecosystem_data(jarvis_snapshot(self.d))
        self.assertEqual(len(eco["nodes"]), 25)
        for n in eco["nodes"]:
            self.assertIn(n["cluster"], ("discovery", "build", "marketing",
                                         "acquisition", "revenue", "support"))
            self.assertIsInstance(n["x"], (int, float))
            self.assertIsInstance(n["y"], (int, float))
            self.assertTrue(n["accent"].startswith("#"))
            self.assertIn(n["mood"], ("work", "sleep", "wait", "happy"))

    def test_dependency_wires_are_present(self):
        from revenue_os.jarvis_server import _ecosystem_data
        eco = _ecosystem_data(jarvis_snapshot(self.d))
        wires = [e for e in eco["edges"] if e["kind"] == "wire"]
        self.assertGreater(len(wires), 10)   # the roster depends_on graph

    def test_real_build_flows_appear_after_a_cycle(self):
        from revenue_os import autonomy
        from revenue_os.jarvis_server import _ecosystem_data
        autonomy.run_cycle(self.d)
        eco = _ecosystem_data(jarvis_snapshot(self.d))
        flows = {(e["from"], e["to"]) for e in eco["edges"]
                 if e["kind"] in ("flow", "live")}
        # Content Creator -> Designer is a real edge the build chain produced
        self.assertIn(("content_creator", "designer"), flows)
        self.assertIn(("opportunity_finder", "content_creator"), flows)

    def test_panel_renders_svg_creatures(self):
        html = render_console(self.d, csrf="t")
        self.assertIn("THE ECOSYSTEM", html)
        self.assertEqual(html.count("class='eco "), 25)   # 25 little creatures
        self.assertIn("eco-svg", html)
        self.assertIn("eco-smile", html)

    def test_creatures_carry_avatar_glyph_and_home_coords(self):
        html = render_console(self.d, csrf="t")
        # every creature has a home position for the wander loop + its avatar
        self.assertEqual(html.count("data-hx="), 25)
        self.assertEqual(html.count("class=eco-icon"), 25)
        self.assertIn("data-id='developer'", html)
        # edges name their endpoints so JS can keep them attached while moving
        self.assertIn("data-from=", html)

    def test_ambient_wander_loop_is_shipped(self):
        html = render_console(self.d, csrf="t")
        self.assertIn("function ecoFrame", html)
        self.assertIn("startEco()", html)
        self.assertIn("requestAnimationFrame(ecoFrame)", html)


# ---------------------------------------------------------------------------
# dashboard regression - JARVIS must not change it
# ---------------------------------------------------------------------------

class DashboardUntouchedTests(unittest.TestCase):
    def test_revenue_dashboard_render_is_independent_of_jarvis_state(self):
        from revenue_os.cli import build_dashboard_html

        d = Path(tempfile.mkdtemp())
        try:
            _seed(d)
            before = build_dashboard_html(d)
            # mutate every JARVIS-owned surface
            apply_control(d, "me", _form(action="pause"))
            apply_control(d, "me", _form(action="set-mode", mode="auto"))
            apply_control(d, "me", _form(action="disable", agent="designer"))
            record_event(d, "x", "noise")
            after = build_dashboard_html(d)
            # the dashboard body is byte-identical apart from its timestamp
            import re
            strip = lambda h: re.sub(r"\d{4}-\d\d-\d\dT[\d:.+-]+", "T", h)
            self.assertEqual(strip(before), strip(after))
        finally:
            import shutil
            shutil.rmtree(d)

    def test_dashboard_server_actions_still_the_five_gates(self):
        from revenue_os.dashboard_server import _ACTIONS
        self.assertEqual(set(_ACTIONS),
                         {"approve", "reject", "outcome", "launch", "payment"})


if __name__ == "__main__":
    unittest.main()
