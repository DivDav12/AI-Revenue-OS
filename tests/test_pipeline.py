import dataclasses
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import roster

from revenue_os import cli
from revenue_os.agent_outputs import AgentOutputStore
from revenue_os.pipeline import PipelineState, pipeline_status, run_pipeline
from revenue_os.store import Candidate, CandidateStore

_OFFER = {
    "what_is_sold": "Template Pack", "price": 29.0, "currency": "EUR",
    "delivery": "digital", "positioning": "save hours on paperwork",
    "includes": ["50 templates", "monthly updates"],
    "call_to_action": "Get the pack",
}
_LLM_STEPS = {"research", "analyze_competition", "write_copy"}
_DET_STEPS = {"select", "find_suppliers", "research_distribution",
              "package_deliverable", "design_assets", "build_store",
              "quality_check"}


def _seed(d: Path, *, status="validated", offer=_OFFER, name="dt") -> None:
    st = CandidateStore(d / "candidates.json")
    st.put(Candidate(name=name, description="reusable document templates marketplace",
                     status=status, total=3.4, verdict="hold",
                     offer=dict(offer) if offer else {}, plan={"hypothesis": "h"}))
    st.save()


class PipelineRunTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_full_run_reaches_prepared_and_a_human_gate(self):
        _seed(self.d)
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "prepared")
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertTrue(all(by[c] == "ok" for c in _DET_STEPS), by)
        self.assertTrue(all(by[c] == "skipped" for c in _LLM_STEPS), by)
        hg = rep["human_gate"]
        self.assertIn("QC passed", hg["reason"])
        # no GitHub Pages credentials in the test env -> deploy is skipped,
        # the page is not published, and payment is not yet possible
        self.assertEqual(by["deploy"], "skipped")
        self.assertFalse(hg["payment_ready"])
        self.assertTrue(any("not published" in x for x in hg["not_done"]))
        self.assertTrue(any("no money spent" in x for x in hg["not_done"]))
        self.assertTrue(any("store_builder" in x for x in hg["human_gated_next"]))

    def test_disabled_chain_agent_blocks_the_pipeline(self):
        from revenue_os.agent_control import AgentControl

        _seed(self.d)
        ctrl = AgentControl.load(self.d / "agent_control.json")
        ctrl.set_agent("designer", False, note="held")
        ctrl.save()

        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "blocked")
        by = {s["step"]: s for s in rep["steps"]}
        self.assertEqual(by["design_assets"]["status"], "blocked")
        # a step before the disabled one still ran; a step after never started
        self.assertEqual(by["find_suppliers"]["status"], "ok")
        self.assertEqual(by["quality_check"]["status"], "pending")
        self.assertIn("disabled", rep["human_gate"]["reason"])

        # re-enable -> a re-run completes
        ctrl.set_agent("designer", True)
        ctrl.save()
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "prepared")

    def test_global_pause_blocks_the_pipeline(self):
        from revenue_os.agent_control import AgentControl

        _seed(self.d)
        ctrl = AgentControl.load(self.d / "agent_control.json")
        ctrl.set_paused(True, reason="maintenance window")
        ctrl.save()
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "blocked")
        self.assertIn("paused", rep["human_gate"]["reason"])

    def test_skip_deploy_records_skipped_without_touching_the_host(self):
        _seed(self.d, status="launched")
        page_dir = self.d / "deliverables" / "dt"
        page_dir.mkdir(parents=True)
        (page_dir / "checkout.html").write_bytes(b"<html>pay</html>")
        rep = run_pipeline(self.d, "dt", skip_deploy=True)
        by = {s["step"]: s for s in rep["steps"]}
        self.assertEqual(by["deploy"]["status"], "skipped")
        self.assertIn("JARVIS", by["deploy"]["reason"])
        self.assertEqual(rep["status"], "prepared")
        self.assertFalse(rep["human_gate"]["payment_ready"])

    def test_step_delay_only_paces_and_marks_running(self):
        import time
        _seed(self.d)
        t0 = time.monotonic()
        rep = run_pipeline(self.d, "dt", step_delay=0.05)
        self.assertGreater(time.monotonic() - t0, 0.2)
        self.assertEqual(rep["status"], "prepared")

    def test_deploy_step_publishes_and_marks_payment_ready(self):
        from revenue_os import deploy as dep
        from tests.test_deploy import CFG, _FakeGitHub

        _seed(self.d, status="launched")
        page_dir = self.d / "deliverables" / "dt"
        page_dir.mkdir(parents=True)
        (page_dir / "checkout.html").write_bytes(b"<html>pay</html>")
        (page_dir / "intake.html").write_bytes(b"<html>intake</html>")

        gh = _FakeGitHub(CFG)
        orig_from_env = dep.GitHubPagesConfig.from_env
        orig_deploy = dep.deploy_checkout
        dep.GitHubPagesConfig.from_env = staticmethod(lambda environ=None: CFG)
        dep.deploy_checkout = lambda dd, name, **kw: orig_deploy(
            dd, name, client=gh, config=CFG)
        try:
            rep = run_pipeline(self.d, "dt")
        finally:
            dep.GitHubPagesConfig.from_env = orig_from_env
            dep.deploy_checkout = orig_deploy

        by = {s["step"]: s for s in rep["steps"]}
        self.assertEqual(by["deploy"]["status"], "ok")
        self.assertEqual(rep["status"], "prepared")
        hg = rep["human_gate"]
        self.assertTrue(hg["payment_ready"])
        self.assertEqual(
            hg["public_url"],
            "https://divdav12.github.io/customer-launch-plan/checkout.html")
        cand = CandidateStore.load(self.d / "candidates.json").get("dt")
        self.assertEqual(cand.public_url, hg["public_url"])
        # re-run: deploy is not repeated (idempotent)
        n_puts = len(gh.puts)
        dep.deploy_checkout = lambda dd, name, **kw: orig_deploy(
            dd, name, client=gh, config=CFG)
        try:
            run_pipeline(self.d, "dt")
        finally:
            dep.deploy_checkout = orig_deploy
        self.assertEqual(len(gh.puts), n_puts)

    def test_hand_off_outputs_are_real_agent_outputs(self):
        _seed(self.d)
        run_pipeline(self.d, "dt")
        outs = json.loads((self.d / "agent_outputs.json").read_text(encoding="utf-8"))
        self.assertEqual(set(outs), _DET_STEPS)                 # only deterministic ran
        self.assertEqual(outs["find_suppliers"]["output"]["opportunity"], "dt")
        self.assertIs(outs["build_store"]["output"]["human_gate_required"], True)
        # no LLM copy -> QC warns (missing headline) but does not block
        self.assertIn(outs["quality_check"]["output"]["qc_status"], ("pass", "warn"))
        # content creator's real landing page flowed into QC
        self.assertIn("Template Pack", outs["package_deliverable"]["output"]["landing_html"])

    def test_idempotent_rerun_does_not_redispatch(self):
        _seed(self.d)
        run_pipeline(self.d, "dt")
        ts1 = json.loads((self.d / "agent_outputs.json").read_text())["design_assets"]["ts"]
        rep = run_pipeline(self.d, "dt")
        ts2 = json.loads((self.d / "agent_outputs.json").read_text())["design_assets"]["ts"]
        self.assertEqual(rep["status"], "prepared")
        self.assertEqual(ts1, ts2)                              # not re-run

    def test_restart_safe_completed_step_not_repeated(self):
        _seed(self.d)
        run_pipeline(self.d, "dt")
        # simulate a restart: brand-new state object off the same file
        st = PipelineState.load(self.d / "pipeline.json")
        self.assertEqual(st.data["candidate"], "dt")
        self.assertEqual(st.data["steps"]["select"]["status"], "ok")
        ts1 = json.loads((self.d / "agent_outputs.json").read_text())["select"]["ts"]
        run_pipeline(self.d, "dt")                              # resume
        ts2 = json.loads((self.d / "agent_outputs.json").read_text())["select"]["ts"]
        self.assertEqual(ts1, ts2)

    def test_failed_step_stops_the_pipeline_and_persists_the_error(self):
        _seed(self.d)
        # total < min_score -> Opportunity Finder drops it
        st = CandidateStore.load(self.d / "candidates.json")
        st.put(dataclasses.replace(st.get("dt"), total=-1.0))
        st.save()
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "failed")
        self.assertIn("Opportunity Finder", rep["error"])
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["select"], "failed")
        self.assertEqual(by["build_store"], "pending")
        self.assertEqual(by["quality_check"], "pending")
        raw = json.loads((self.d / "pipeline.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["status"], "failed")

    def test_dependency_liveness_checked_before_each_step(self):
        _seed(self.d)

        def _set(agents):
            roster.AGENTS = agents
            roster._BY_ID = {a.id: a for a in agents}
            roster._BY_CAP = {a.capability: a for a in agents}

        orig = roster.AGENTS
        _set(tuple(dataclasses.replace(a, status="planned")
                   if a.id == "store_builder" else a for a in orig))
        try:
            rep = run_pipeline(self.d, "dt")
        finally:
            _set(orig)
        self.assertEqual(rep["status"], "failed")
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["package_deliverable"], "ok")   # earlier steps still ran
        self.assertEqual(by["build_store"], "failed")
        self.assertEqual(by["quality_check"], "pending")

    def test_qc_block_stops_the_pipeline(self):
        _seed(self.d)
        # pre-seed a real-shaped write_copy output whose body has the wrong price
        ao = AgentOutputStore.load(self.d / "agent_outputs.json")
        ao.put("write_copy", {"launch_draft": {"headline": "h",
               "body": "grab it for 99.00 EUR", "primary_cta": "Buy"}},
               objective="seeded elsewhere")
        ao.save()
        rep = run_pipeline(self.d, "dt", restart=True)
        self.assertEqual(rep["status"], "blocked")
        self.assertIn("qc_status=block", rep["human_gate"]["reason"])
        self.assertTrue(any("pricing mismatch" in f
                            for f in rep["human_gate"]["failed_checks"]))
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["write_copy"], "ok")               # consumed the seeded output

    def test_llm_step_consumes_existing_real_output(self):
        _seed(self.d)
        ao = AgentOutputStore.load(self.d / "agent_outputs.json")
        ao.put("research", {"candidate_name": "dt",
                            "research": {"verdict": "go", "basis": "model knowledge"}},
               objective="ran via agent-run")
        ao.save()
        rep = run_pipeline(self.d, "dt")
        research = [s for s in rep["steps"] if s["step"] == "research"][0]
        self.assertEqual(research["status"], "ok")
        self.assertIn("existing real output", research["note"])

    def test_entry_gate_not_qualified(self):
        _seed(self.d, status="shortlisted")
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "failed")
        self.assertIn("not qualified", rep["error"])

    def test_entry_gate_no_offer(self):
        _seed(self.d, offer=None)
        rep = run_pipeline(self.d, "dt")
        self.assertEqual(rep["status"], "failed")
        self.assertIn("no offer", rep["error"])

    def test_unknown_candidate_raises(self):
        _seed(self.d)
        with self.assertRaises(ValueError):
            run_pipeline(self.d, "ghost")

    def test_error_state_is_persisted(self):
        _seed(self.d, status="shortlisted")
        run_pipeline(self.d, "dt")
        raw = json.loads((self.d / "pipeline.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["status"], "failed")
        self.assertTrue(raw["error"])
        self.assertEqual(raw["steps"]["entry"]["status"], "failed")

    def test_no_money_no_network_no_paypal_touched(self):
        _seed(self.d)
        run_pipeline(self.d, "dt")
        for f in ("llm_spend.json", "revenue.json", "spend.json"):
            self.assertFalse((self.d / f).exists(), f)

    def test_status_helper_reports_last_run(self):
        _seed(self.d)
        run_pipeline(self.d, "dt")
        self.assertEqual(pipeline_status(self.d, "dt")["status"], "prepared")
        other = pipeline_status(self.d, "someone-else")
        self.assertEqual(other["status"], "no run")


class PipelineCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        _seed(self.d)

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["pipeline", *argv, "--data-dir", str(self.d)])
        return code, buf.getvalue()

    def test_run_then_status(self):
        code, out = self._run("run", "dt")
        self.assertEqual(code, 0)
        self.assertIn("status=prepared", out)
        self.assertIn("HUMAN GATE", out)
        self.assertIn("publishes nothing, sends nothing, spends nothing", out)
        code, out = self._run("status", "dt")
        self.assertEqual(code, 0)
        self.assertIn("quality_check", out)

    def test_run_without_name_is_a_usage_error(self):
        code, out = self._run("run")
        self.assertEqual(code, 2)

    def test_json_flag(self):
        code, out = self._run("run", "dt", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "prepared")


if __name__ == "__main__":
    unittest.main()
