import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from revenue_os import agent_control
from revenue_os.jarvis_server import (
    apply_control,
    build_server,
    jarvis_snapshot,
    render_console,
    run_one_agent,
)
from revenue_os.store import Candidate, CandidateStore

_OFFER = {
    "what_is_sold": "Template Pack", "price": 29.0, "currency": "EUR",
    "delivery": "digital", "positioning": "save hours",
    "includes": ["50 templates"], "call_to_action": "Get it",
}


def _form(**kw):
    return {k: [str(v)] for k, v in kw.items()}


def _seed(d: Path, *, status="validated", name="dt"):
    st = CandidateStore(d / "candidates.json")
    st.put(Candidate(name=name, description="reusable document templates",
                     status=status, total=3.4, verdict="hold",
                     offer=dict(_OFFER), plan={"hypothesis": "h"}))
    st.save()


class ControlActionTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def _ctrl(self):
        return agent_control.load_agent_control(self.d)

    # --- enable / disable --------------------------------------------
    def test_disable_and_enable_persist(self):
        msg = apply_control(self.d, "tester", _form(action="disable", agent="designer"))
        self.assertTrue(msg.startswith("ok"))
        self.assertFalse(self._ctrl().is_enabled("designer"))

        msg = apply_control(self.d, "tester", _form(action="enable", agent="designer"))
        self.assertTrue(msg.startswith("ok"))
        self.assertTrue(self._ctrl().is_enabled("designer"))

    def test_disable_unknown_agent_is_a_clean_error(self):
        msg = apply_control(self.d, "t", _form(action="disable", agent="nope"))
        self.assertTrue(msg.startswith("error"))
        self.assertFalse((self.d / "agent_control.json").exists())

    # --- pause / resume --------------------------------------------
    def test_pause_then_resume(self):
        self.assertIn("PAUSED", apply_control(self.d, "t", _form(action="pause",
                                                                reason="audit")))
        self.assertTrue(self._ctrl().is_paused())
        self.assertIn("RESUMED", apply_control(self.d, "t", _form(action="resume")))
        self.assertFalse(self._ctrl().is_paused())

    # --- run one agent --------------------------------------------
    def test_run_deterministic_agent_persists_output(self):
        msg = apply_control(self.d, "t", _form(action="run", agent="designer"))
        self.assertTrue(msg.startswith("ok"), msg)
        from revenue_os.agent_runner import last_output
        self.assertIsNotNone(last_output(self.d, "design_assets"))

    def test_run_is_refused_when_agent_disabled(self):
        apply_control(self.d, "t", _form(action="disable", agent="designer"))
        msg = apply_control(self.d, "t", _form(action="run", agent="designer"))
        self.assertTrue(msg.startswith("error"))
        self.assertIn("disabled", msg)

    def test_run_is_refused_when_globally_paused(self):
        apply_control(self.d, "t", _form(action="pause"))
        msg = apply_control(self.d, "t", _form(action="run", agent="quality_control"))
        self.assertTrue(msg.startswith("error"))
        self.assertIn("paused", msg)

    def test_run_human_gated_agent_produces_draft_only(self):
        # store_builder is human-gated but part of the runnable chain
        msg = apply_control(self.d, "t", _form(action="run", agent="store_builder"))
        self.assertTrue(msg.startswith("ok"), msg)
        self.assertIn("human gate", msg)
        from revenue_os.agent_runner import last_output
        out = last_output(self.d, "build_store")
        self.assertTrue(out["human_gate_required"])
        self.assertEqual(out["build_artifacts"], [])

    def test_run_agent_not_runnable_here_explains_why(self):
        msg = apply_control(self.d, "t", _form(action="run", agent="sales_tracker"))
        self.assertTrue(msg.startswith("error"))
        self.assertIn("funnel", msg)

    def test_run_chain_agent_without_qualified_candidate(self):
        d2 = Path(tempfile.mkdtemp())
        try:
            msg = run_one_agent(d2, "designer")
            self.assertTrue(msg.startswith("error"))
            self.assertIn("qualified candidate", msg)
        finally:
            import shutil
            shutil.rmtree(d2)

    # --- run pipeline --------------------------------------------
    def test_run_pipeline_reaches_prepared(self):
        msg = apply_control(self.d, "t", _form(action="run-pipeline"))
        self.assertIn("-> prepared", msg)

    def test_run_pipeline_blocked_while_paused(self):
        apply_control(self.d, "t", _form(action="pause"))
        msg = apply_control(self.d, "t", _form(action="run-pipeline"))
        self.assertTrue(msg.startswith("error"))
        self.assertIn("paused", msg)

    def test_run_pipeline_stops_at_disabled_agent(self):
        apply_control(self.d, "t", _form(action="disable", agent="quality_control"))
        msg = apply_control(self.d, "t", _form(action="run-pipeline",
                                               candidate="dt", restart="1"))
        self.assertIn("-> blocked", msg)
        import json
        st = json.loads((self.d / "pipeline.json").read_text())
        self.assertEqual(st["status"], "blocked")
        self.assertEqual(st["steps"]["quality_check"]["status"], "blocked")

    def test_jarvis_pipeline_never_deploys(self):
        # a checkout page + fake creds present -> the CLI pipeline would
        # publish; JARVIS must always record deploy as skipped.
        apply_control(self.d, "t", _form(action="run-pipeline", candidate="dt"))
        import json
        st = json.loads((self.d / "pipeline.json").read_text())
        dep = st["steps"]["deploy"]
        self.assertEqual(dep["status"], "skipped")
        self.assertIn("JARVIS", dep["reason"])

    # --- async fleet jobs --------------------------------------------
    def test_async_sweep_runs_in_background_and_completes(self):
        import time
        msg = apply_control(self.d, "t",
                            _form(action="run-sweep", mode="async", restart="1"))
        self.assertIn("started", msg)
        for _ in range(80):
            if not jarvis_snapshot(self.d)["job"]["running"]:
                break
            time.sleep(0.25)
        snap = jarvis_snapshot(self.d)
        self.assertFalse(snap["job"]["running"])
        self.assertEqual(snap["pipeline"]["status"], "prepared")
        self.assertEqual(snap["pipeline"]["pct"], 100)
        from revenue_os.agent_runner import last_output
        self.assertIsNotNone(last_output(self.d, "analyze_revenue"))
        self.assertIsNotNone(last_output(self.d, "analyze_trends"))

    def test_only_one_background_job_at_a_time(self):
        first = apply_control(self.d, "t",
                              _form(action="run-pipeline", mode="async", restart="1"))
        self.assertIn("started", first)
        second = apply_control(self.d, "t",
                               _form(action="run-sweep", mode="async"))
        # either it's still running (rejected) or it already finished (ok)
        if "already running" not in second:
            import time
            time.sleep(0.3)
        else:
            self.assertIn("error", second)
        # let the job drain so tearDown is clean
        import time
        for _ in range(80):
            if not jarvis_snapshot(self.d)["job"]["running"]:
                break
            time.sleep(0.25)

    # --- human gate delegation ----------------------------------
    def test_gate_action_is_delegated_to_dashboard_server(self):
        _seed(self.d, status="shortlisted", name="sl")
        msg = apply_control(self.d, "human", _form(action="approve", name="sl"))
        self.assertIn("-> approved", msg)

    # --- fixing human-gated agents from JARVIS ---------------------
    def test_human_gated_agents_run_draft_only(self):
        apply_control(self.d, "t", _form(action="run-pipeline", candidate="dt"))
        for agent, cap in (("developer", "develop"),
                           ("automation_engineer", "automate"),
                           ("ads_manager", "run_ads")):
            msg = apply_control(self.d, "t", _form(action="run", agent=agent))
            self.assertTrue(msg.startswith("ok"), msg)
            self.assertIn("human gate", msg)
            from revenue_os.agent_runner import last_output
            out = last_output(self.d, cap)
            self.assertIsNotNone(out)
            self.assertTrue(out.get("human_gate_required"))

    def test_ack_gate_clears_waiting_and_reopen_restores_it(self):
        apply_control(self.d, "t", _form(action="run", agent="store_builder"))
        before = {a["id"]: a for a in jarvis_snapshot(self.d)["agents"]}["store_builder"]
        self.assertTrue(before["why_waiting"])
        self.assertFalse(before["gate_acknowledged"])

        msg = apply_control(self.d, "boss",
                            _form(action="ack-gate", agent="store_builder",
                                  note="built + deployed by hand"))
        self.assertIn("marked handled", msg)
        after = {a["id"]: a for a in jarvis_snapshot(self.d)["agents"]}["store_builder"]
        self.assertEqual(after["state"], "handled")
        self.assertEqual(after["why_waiting"], "")
        self.assertEqual(after["gate_ack_by"], "boss")
        self.assertEqual(after["gate_ack_note"], "built + deployed by hand")

        apply_control(self.d, "boss", _form(action="reopen-gate", agent="store_builder"))
        again = {a["id"]: a for a in jarvis_snapshot(self.d)["agents"]}["store_builder"]
        self.assertTrue(again["why_waiting"])
        self.assertFalse(again["gate_acknowledged"])

    def test_ack_gate_reopens_when_agent_re_runs(self):
        apply_control(self.d, "t", _form(action="run", agent="store_builder"))
        apply_control(self.d, "t", _form(action="ack-gate", agent="store_builder"))
        self.assertTrue({a["id"]: a for a in jarvis_snapshot(self.d)["agents"]}
                        ["store_builder"]["gate_acknowledged"])
        # a fresh run produces a new output ts -> the old ack no longer matches
        import time
        time.sleep(0.01)
        apply_control(self.d, "t", _form(action="run", agent="store_builder"))
        self.assertFalse({a["id"]: a for a in jarvis_snapshot(self.d)["agents"]}
                         ["store_builder"]["gate_acknowledged"])

    def test_ack_gate_rejects_non_gated_agent(self):
        msg = apply_control(self.d, "t", _form(action="ack-gate", agent="designer"))
        self.assertTrue(msg.startswith("error"))

    def test_resolve_blocker(self):
        from revenue_os.blockers import load_blockers
        bs = load_blockers(self.d)
        bs.add("pp", "PayPal restricted", area="payment", severity="critical")
        bs.save()
        self.assertEqual(len(jarvis_snapshot(self.d)["blockers"]), 1)
        msg = apply_control(self.d, "me", _form(action="resolve-blocker", id="pp"))
        self.assertIn("resolved", msg)
        self.assertEqual(len(jarvis_snapshot(self.d)["blockers"]), 0)
        self.assertTrue(apply_control(self.d, "me",
                        _form(action="resolve-blocker", id="nope")).startswith("error"))

    def test_outreach_status_logs_what_the_human_did(self):
        from revenue_os.outreach import OutreachStore
        os_ = OutreachStore.load(self.d / "outreach.json")
        os_.put({"lead_id": "abc123", "url": "https://x.test", "platform": "HN"},
                status="draft")
        os_.save()
        self.assertEqual(len(jarvis_snapshot(self.d)["outreach"]), 1)
        msg = apply_control(self.d, "me", _form(action="outreach-status",
                                               lead_id="abc123", status="posted"))
        self.assertIn("posted", msg)
        self.assertEqual(
            OutreachStore.load(self.d / "outreach.json").get("abc123")["status"],
            "posted")
        self.assertEqual(len(jarvis_snapshot(self.d)["outreach"]), 0)

    def test_outreach_status_rejects_bad_status(self):
        msg = apply_control(self.d, "me", _form(action="outreach-status",
                                               lead_id="x", status="deleted"))
        self.assertTrue(msg.startswith("error"))

    def test_unknown_action(self):
        self.assertIn("unknown action",
                      apply_control(self.d, "t", _form(action="frobnicate")))

    # --- read model ------------------------------------------------
    def test_snapshot_lists_all_agents_with_real_state(self):
        snap = jarvis_snapshot(self.d)
        self.assertEqual(len(snap["agents"]), 25)
        self.assertFalse(snap["paused"])
        apply_control(self.d, "t", _form(action="disable", agent="designer"))
        snap = jarvis_snapshot(self.d)
        d = next(a for a in snap["agents"] if a["id"] == "designer")
        self.assertEqual(d["state"], "disabled")
        self.assertFalse(d["enabled"])

    def test_every_agent_has_a_progress_value(self):
        snap = jarvis_snapshot(self.d)
        for a in snap["agents"]:
            self.assertIn("progress", a)
            self.assertGreaterEqual(a["progress"], 0)
            self.assertLessEqual(a["progress"], 100)
            self.assertIn(a["progress_kind"], ("ok", "run", "bad", "idle"))
            self.assertTrue(a["progress_label"])

    def test_progress_reflects_pipeline_and_readiness(self):
        apply_control(self.d, "t", _form(action="run-pipeline", candidate="dt"))
        snap = jarvis_snapshot(self.d)
        by = {a["id"]: a for a in snap["agents"]}
        # a step that ran -> 100 / ok
        self.assertEqual(by["designer"]["progress"], 100)
        self.assertEqual(by["designer"]["progress_kind"], "ok")
        # a downstream agent whose sole input now exists -> fully armed
        self.assertEqual(by["ads_manager"]["progress"], 100)
        # a no-dependency agent that never ran -> standby at 0
        self.assertEqual(by["customer_support"]["progress"], 0)
        pipe = snap["pipeline"]
        self.assertEqual(pipe["pct"],
                         round(100 * pipe["done"] / pipe["total"]))

    def test_snapshot_explains_human_gate_waiting(self):
        snap = jarvis_snapshot(self.d)
        ob = next(a for a in snap["agents"] if a["id"] == "outreach_drafter")
        self.assertTrue(ob["why_waiting"])
        self.assertIn("post", ob["why_waiting"].lower())

    def test_render_console_is_self_contained_html(self):
        html = render_console(self.d, flash="ok: hi", csrf="tok")
        self.assertIn("J A R V I S", html)
        self.assertIn("<style>", html)
        self.assertNotIn("http://", html.split("<style>")[1].split("</style>")[0])
        # partial view is just the main region
        part = render_console(self.d, csrf="tok", partial=True)
        self.assertIn("id=j-main", part)


class ServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)
        self.httpd, self.csrf = build_server(self.d, port=0)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.t.join(timeout=2)
        self._d.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def test_get_root_renders(self):
        c = self._conn()
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"J A R V I S", r.read())

    def test_unknown_path_404(self):
        c = self._conn()
        c.request("GET", "/secrets")
        self.assertEqual(c.getresponse().status, 404)

    def test_post_without_csrf_is_403_and_changes_nothing(self):
        c = self._conn()
        body = "action=disable&agent=designer"
        c.request("POST", "/control", body,
                  {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(c.getresponse().status, 403)
        self.assertTrue(agent_control.load_agent_control(self.d).is_enabled("designer"))

    def test_post_cross_origin_is_403(self):
        c = self._conn()
        body = f"action=pause&csrf={self.csrf}"
        c.request("POST", "/control", body, {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://evil.example",
        })
        self.assertEqual(c.getresponse().status, 403)
        self.assertFalse(agent_control.load_agent_control(self.d).is_paused())

    def test_valid_post_applies_and_redirects(self):
        c = self._conn()
        body = f"action=pause&csrf={self.csrf}"
        c.request("POST", "/control", body, {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://127.0.0.1:{self.port}",
        })
        r = c.getresponse()
        self.assertEqual(r.status, 303)
        r.read()
        self.assertTrue(agent_control.load_agent_control(self.d).is_paused())

    def test_non_loopback_host_is_refused(self):
        with self.assertRaises(ValueError):
            build_server(self.d, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
