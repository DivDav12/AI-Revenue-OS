import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from revenue_os import revenue_loop as rl
from revenue_os.store import Candidate, CandidateStore

_OFFER = {"what_is_sold": "Customer Launch Plan", "price": 29.9, "currency": "EUR",
          "delivery": "digital", "positioning": "find your first customers",
          "includes": ["analysis", "14-day plan"], "call_to_action": "Get it"}


def _cand(d, name="ask-hn", status="launched", offer=_OFFER):
    cs = CandidateStore.load(d / "candidates.json")
    cs.put(Candidate(name=name, description="how do I get first customers",
                     status=status, total=3.0, verdict="hold",
                     offer=dict(offer) if offer else {}, plan={"hypothesis": "h"}))
    cs.save()


class _Offline(unittest.TestCase):
    def setUp(self):
        self._t = TemporaryDirectory()
        self.d = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        import revenue_os.acquisition_sources as S

        class _Src:
            name = "hn-algolia"

            def search(self, q, limit, *, since_ts=None):
                return []

        self._orig = S.build_acquisition_source
        S.build_acquisition_source = lambda names, path=None, web_source=None: _Src()
        self.addCleanup(lambda: setattr(S, "build_acquisition_source", self._orig))


class DecideTests(_Offline):
    def test_empty_state_discovers_then_stops(self):
        st = rl.observe(self.d)
        self.assertEqual(rl.decide(st).action, "discover")
        self.assertEqual(rl.decide(st, done_once={"discover"}).action, "stop")

    def test_qualified_candidate_runs_pipeline(self):
        _cand(self.d)
        d = rl.decide(rl.observe(self.d))
        self.assertEqual(d.action, "run_pipeline")
        self.assertEqual(d.detail["candidate"], "ask-hn")

    def test_prepared_pipeline_is_not_rerun(self):
        _cand(self.d)
        (self.d / "pipeline.json").write_text(
            '{"candidate": "ask-hn", "status": "prepared", "human_gate": {}}',
            encoding="utf-8")
        d = rl.decide(rl.observe(self.d))
        self.assertIn(d.action, ("discover", "stop"))

    def test_approved_plan_is_staged_before_anything_else(self):
        _cand(self.d, status="earning")
        (self.d / "intake.json").write_text(
            '[{"order_id": "O1", "status": "reviewed", "candidate": "ask-hn",'
            ' "plan": {"status": "approved"}}]', encoding="utf-8")
        d = rl.decide(rl.observe(self.d))
        self.assertEqual(d.action, "stage_delivery")
        self.assertEqual(d.detail["order_id"], "O1")

    def test_human_queue_lists_concrete_commands(self):
        _cand(self.d, name="c1", status="shortlisted")
        cs = CandidateStore.load(self.d / "candidates.json")
        cs.put(Candidate(name="c2", status="investigating"))
        cs.save()
        q = rl._human_queue(rl.observe(self.d))
        self.assertTrue(any("approve" in x and "c1" in x for x in q))
        self.assertTrue(any("outcome" in x and "c2" in x for x in q))


class RunTests(_Offline):
    def _patch_deploy(self):
        from revenue_os import deploy as dep
        from tests.test_deploy import CFG, _FakeGitHub
        gh = _FakeGitHub(CFG)
        orig_from_env = dep.GitHubPagesConfig.from_env
        orig_deploy = dep.deploy_checkout
        dep.GitHubPagesConfig.from_env = staticmethod(lambda environ=None: CFG)
        dep.deploy_checkout = lambda dd, name, **kw: orig_deploy(
            dd, name, client=gh, config=CFG)
        self.addCleanup(lambda: setattr(dep.GitHubPagesConfig, "from_env",
                                        orig_from_env))
        self.addCleanup(lambda: setattr(dep, "deploy_checkout", orig_deploy))
        return gh

    def test_full_run_builds_deploys_then_stops_for_traffic(self):
        _cand(self.d)
        page = self.d / "deliverables" / "ask-hn"
        page.mkdir(parents=True)
        (page / "checkout.html").write_bytes(b"<html>pay</html>")
        self._patch_deploy()

        steps = rl.run(self.d, max_steps=10)
        actions = [s["action"] for s in steps]
        self.assertIn("run_pipeline", actions)
        self.assertEqual(steps[-1]["action"], "stop")

        cand = CandidateStore.load(self.d / "candidates.json").get("ask-hn")
        self.assertTrue(cand.public_url.endswith("checkout.html"))
        queue = steps[-1]["human_queue"]
        self.assertTrue(any("LIVE" in x or "traffic" in x for x in queue))

    def test_run_does_not_loop_forever_with_two_candidates(self):
        _cand(self.d, name="a")
        _cand(self.d, name="b")   # second qualified candidate
        cs = CandidateStore.load(self.d / "candidates.json")
        self.assertEqual(len({c.name for c in cs.all()}), 2)
        self._patch_deploy()
        steps = rl.run(self.d, max_steps=12)
        self.assertEqual(steps[-1]["action"], "stop")
        self.assertLessEqual(len(steps), 12)
        piped = [s["detail"].get("candidate") for s in steps
                 if s["action"] == "run_pipeline"]
        self.assertEqual(sorted(piped), ["a", "b"])   # each pipelined once

    def test_state_is_persisted(self):
        _cand(self.d)
        self._patch_deploy()
        rl.run(self.d, max_steps=6)
        state = rl.load_state(self.d)
        self.assertEqual(state["status"], "stopped")
        self.assertGreaterEqual(state["steps_taken"], 1)
        self.assertTrue(state["history"])


class CliTests(_Offline):
    def test_revenue_step_and_status(self):
        from revenue_os.cli import main
        _cand(self.d, status="shortlisted")
        self.assertEqual(
            main(["revenue-step", "--no-discovery", "--data-dir", str(self.d)]), 0)
        self.assertEqual(main(["revenue-status", "--data-dir", str(self.d)]), 0)


if __name__ == "__main__":
    unittest.main()
