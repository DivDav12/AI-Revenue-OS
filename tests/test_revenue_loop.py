import json
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


class _Sleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class _Clock:
    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def __call__(self):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


class WatchTests(_Offline):
    def _now(self):
        n = {"i": 0}

        def _f():
            n["i"] += 1
            return f"2026-09-01T00:00:{n['i']:02d}+00:00"
        return _f

    def test_max_ticks_and_sleep_between_only(self):
        sleep = _Sleep()
        sess = rl.watch(self.d, interval=5, max_ticks=3, sleep_fn=sleep,
                        clock_fn=_Clock([0]), now_fn=self._now())
        self.assertEqual(sess["ticks"], 3)
        self.assertEqual(sess["end_reason"], "max-ticks")
        self.assertIsNotNone(sess["ended_at"])
        self.assertEqual(sleep.calls, [5, 5])   # between the 3 ticks, not after

    def test_max_runtime_stops(self):
        sess = rl.watch(self.d, interval=1, max_runtime_s=50,
                        sleep_fn=_Sleep(), clock_fn=_Clock([0, 0, 100]),
                        now_fn=self._now())
        self.assertEqual(sess["end_reason"], "max-runtime")
        self.assertLessEqual(sess["ticks"], 1)

    def test_max_spend_stops(self):
        from revenue_os.llm_spend import LlmSpendLog

        def seed(_steps, _fb):
            log = LlmSpendLog.load(self.d / "llm_spend.json")
            log.add({"activity": "evaluate", "cost_usd": 1.0, "api_calls": 1})
            log.save()
        sess = rl.watch(self.d, interval=1, max_spend_usd=0.5, on_tick=seed,
                        sleep_fn=_Sleep(), clock_fn=_Clock([0]), now_fn=self._now())
        self.assertEqual(sess["end_reason"], "max-spend")

    def test_keyboard_interrupt_is_clean(self):
        def boom(_steps, _fb):
            raise KeyboardInterrupt
        sess = rl.watch(self.d, interval=1, on_tick=boom, sleep_fn=_Sleep(),
                        clock_fn=_Clock([0]), now_fn=self._now())
        self.assertEqual(sess["end_reason"], "interrupted")
        self.assertIsNotNone(sess["ended_at"])
        s, resumed = rl.load_session(self.d)
        self.assertFalse(resumed)               # ended -> not resumable

    def test_unfinished_session_resumes(self):
        (self.d / "revenue_loop.json").write_text(json.dumps({
            "status": "running", "session": {
                "started_at": "ORIG", "last_tick_at": "", "ticks": 2,
                "ended_at": None, "end_reason": None, "spend_baseline_usd": 0.0},
        }), encoding="utf-8")
        sess = rl.watch(self.d, interval=1, max_ticks=4, sleep_fn=_Sleep(),
                        clock_fn=_Clock([0]), now_fn=self._now())
        self.assertEqual(sess["started_at"], "ORIG")   # resumed
        self.assertEqual(sess["ticks"], 4)

    def test_fresh_ignores_unfinished_session(self):
        (self.d / "revenue_loop.json").write_text(json.dumps({
            "session": {"started_at": "ORIG", "ended_at": None, "ticks": 9,
                        "spend_baseline_usd": 0.0}}), encoding="utf-8")
        sess = rl.watch(self.d, interval=1, max_ticks=1, fresh=True,
                        sleep_fn=_Sleep(), clock_fn=_Clock([0]), now_fn=self._now())
        self.assertNotEqual(sess["started_at"], "ORIG")
        self.assertEqual(sess["ticks"], 1)

    def test_gate_safety_and_zero_spend_no_anthropic_key(self):
        import os
        _cand(self.d)
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            rl.watch(self.d, interval=0, max_ticks=3, sleep_fn=_Sleep(),
                     clock_fn=_Clock([0]), now_fn=self._now())
        finally:
            if key is not None:
                os.environ["ANTHROPIC_API_KEY"] = key
        statuses = {c.status for c in
                    CandidateStore.load(self.d / "candidates.json").all()}
        self.assertTrue(statuses <= {"launched", "earning", "prepared",
                                     "discovered", "shortlisted"})
        spend = json.loads((self.d / "llm_spend.json").read_text()) \
            if (self.d / "llm_spend.json").exists() else []
        self.assertEqual(round(sum(e.get("cost_usd", 0) for e in spend), 4), 0.0)

    def test_feedback_runs_each_tick(self):
        # a brief -> open_from_briefs opens an experiment during the tick
        (self.d / "outreach.json").write_text(json.dumps([
            {"lead_id": "LX", "status": "draft",
             "brief": {"lead_id": "LX", "source": "hn-algolia",
                       "platform": "HN", "checkout_link": "x"}}]),
            encoding="utf-8")
        _cand(self.d)
        rl.watch(self.d, interval=0, max_ticks=1, sleep_fn=_Sleep(),
                 clock_fn=_Clock([0]), now_fn=self._now())
        from revenue_os.experiments import ExperimentStore
        self.assertEqual(
            ExperimentStore.load(self.d / "experiments.json").get("LX")["status"],
            "drafted")


class CliTests(_Offline):
    def test_revenue_step_and_status(self):
        from revenue_os.cli import main
        _cand(self.d, status="shortlisted")
        self.assertEqual(
            main(["revenue-step", "--no-discovery", "--data-dir", str(self.d)]), 0)
        self.assertEqual(main(["revenue-status", "--data-dir", str(self.d)]), 0)

    def test_revenue_loop_watch_bounded(self):
        from revenue_os.cli import main
        rc = main(["revenue-loop", "--watch", "--interval", "0", "--max-ticks",
                   "2", "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        state = json.loads((self.d / "revenue_loop.json").read_text())
        self.assertEqual(state["session"]["end_reason"], "max-ticks")
        self.assertEqual(state["session"]["ticks"], 2)

    def test_experiments_and_experiment_close_cli(self):
        from revenue_os.cli import main
        (self.d / "outreach.json").write_text(json.dumps([
            {"lead_id": "L9", "status": "draft",
             "brief": {"lead_id": "L9", "source": "lemmy", "platform": "Lemmy",
                       "checkout_link": "x"}}]), encoding="utf-8")
        _cand(self.d)
        from revenue_os import experiments as ex
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "L9", "posted")
        self.assertEqual(main(["experiments", "--data-dir", str(self.d)]), 0)
        self.assertEqual(main(["experiment-close", "L9", "no_sale",
                               "--data-dir", str(self.d)]), 0)
        from revenue_os.experiments import ExperimentStore
        self.assertEqual(
            ExperimentStore.load(self.d / "experiments.json").get("L9")["status"],
            "no_sale")


if __name__ == "__main__":
    unittest.main()
