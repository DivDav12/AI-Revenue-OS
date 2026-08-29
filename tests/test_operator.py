import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.agent_log import AgentLog
from revenue_os.lifecycle import STATUSES
from revenue_os.operator import Goal, OperatorAgent, decide, load_goal, save_goal
from revenue_os.revenue import RevenueLedger
from revenue_os.spend import SpendLedger
from revenue_os.store import Candidate, CandidateStore


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _obs(*, counts=None, candidates=None, queue=None, age=None, total=None):
    sc = {s: 0 for s in STATUSES}
    sc.update(counts or {})
    cands = candidates or []
    return {
        "report": {
            "status_counts": sc,
            "action_queue": queue or [],
            "candidates": cands,
            "totals": {"candidates": total if total is not None else len(cands)},
        },
        "last_discovery_age_days": age,
    }


class GoalTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_goal(d), Goal())  # default when absent
            g = Goal(sources=("static", "hn"), shortlist_n=5, target_validated=3)
            save_goal(d, g)
            self.assertEqual(load_goal(d), g)
            reloaded = Goal.from_dict(json.loads((Path(d) / "goal.json").read_text()))
            self.assertEqual(reloaded, g)


class DecideTests(unittest.TestCase):
    def test_cold_start_discovers(self):
        self.assertEqual(decide(_obs(), Goal()).action, "discover")

    def test_approved_triggers_investigate(self):
        d = decide(_obs(counts={"approved": 2}, total=2), Goal())
        self.assertEqual(d.action, "investigate")

    def test_validated_without_offer_triggers_prepare_launch(self):
        cands = [{"status": "validated", "offer": {}}]
        d = decide(_obs(counts={"validated": 1}, candidates=cands), Goal())
        self.assertEqual(d.action, "prepare_launch")

    def test_validated_with_offer_does_not_prepare_again(self):
        cands = [{"status": "validated", "offer": {"price": 9}}]
        obs = _obs(counts={"validated": 1, "shortlisted": 3}, candidates=cands, age=0,
                   queue=[{"next_action": "launch offer", "stale": False}])
        # funnel is full and discovery is fresh -> nothing left but the human
        self.assertEqual(decide(obs, Goal(shortlist_n=3)).action, "stop")

    def test_only_human_actions_left_stops(self):
        obs = _obs(counts={"shortlisted": 3}, total=3, age=0,
                   queue=[{"next_action": "approve or reject", "stale": False}])
        self.assertEqual(decide(obs, Goal(shortlist_n=3)).action, "stop")

    def test_stale_discovery_rediscovers(self):
        obs = _obs(counts={"shortlisted": 5}, total=5, age=30,
                   queue=[{"next_action": "approve or reject", "stale": True}])
        self.assertEqual(decide(obs, Goal(shortlist_n=3)).action, "discover")

    def test_target_validated_stops(self):
        obs = _obs(counts={"validated": 1, "approved": 5}, total=6)
        self.assertEqual(
            decide(obs, Goal(target_validated=1)).action, "stop"
        )

    def test_discovery_exhausted_prevents_rediscover(self):
        obs = _obs(counts={}, total=0, age=None)
        self.assertEqual(
            decide(obs, Goal(), discovery_exhausted=True).action, "stop"
        )

    def test_write_copy_needs_an_offer_and_the_opt_in(self):
        with_offer = [{"status": "validated", "offer": {"price": 9}}]
        no_offer = [{"status": "validated", "offer": {}}]
        g = Goal(copywriter="llm")
        self.assertEqual(
            decide(_obs(counts={"validated": 1}, candidates=with_offer, age=0), g).action,
            "write_copy",
        )
        self.assertEqual(
            decide(_obs(counts={"validated": 1}, candidates=no_offer, age=0), g).action,
            "prepare_launch",
        )
        self.assertNotEqual(
            decide(_obs(counts={"validated": 1}, candidates=with_offer, age=0),
                   Goal()).action,
            "write_copy",
        )
        drafted = [{"status": "validated", "offer": {"price": 9},
                    "launch_draft": {"headline": "h"}}]
        self.assertNotEqual(
            decide(_obs(counts={"validated": 1}, candidates=drafted, age=0), g).action,
            "write_copy",
        )


class OperatorAgentTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _statuses(self):
        return {c.name: c.status
                for c in CandidateStore.load(self.d / "candidates.json").all()}

    def test_step_discovers_and_logs(self):
        agent = OperatorAgent(self.d, Goal())
        step = agent.step()
        self.assertEqual(step.decision.action, "discover")
        self.assertGreater(step.result["new_candidates"], 0)
        self.assertEqual(len(AgentLog.load(self.d / "agent_log.json")), 1)

    def test_run_reaches_fixed_point(self):
        steps = OperatorAgent(self.d, Goal()).run(max_cycles=10)
        self.assertEqual(steps[-1].decision.action, "stop")
        self.assertLessEqual(len(steps), 10)
        self.assertEqual(steps[0].decision.action, "discover")

    def test_run_never_passes_a_human_gate(self):
        OperatorAgent(self.d, Goal()).run()
        statuses = set(self._statuses().values())
        self.assertTrue(statuses <= {"discovered", "shortlisted"})
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 0.0)
        self.assertEqual(SpendLedger.load(self.d / "spend.json").total_spent(), 0.0)

    def test_agent_investigates_after_human_approves(self):
        agent = OperatorAgent(self.d, Goal())
        agent.run()
        store = CandidateStore.load(self.d / "candidates.json")
        name = next(c.name for c in store.all() if c.status == "shortlisted")
        from revenue_os.approval import record_decision
        record_decision(store, name, "approve", approver="human")

        step = OperatorAgent(self.d, agent.goal).step()
        self.assertEqual(step.decision.action, "investigate")
        self.assertEqual(self._statuses()[name], "investigating")

    def test_empty_source_does_not_loop(self):
        empty = self.d / "empty.json"
        empty.write_text("[]", encoding="utf-8")
        agent = OperatorAgent(self.d, Goal(sources=(f"file:{empty}",)))
        steps = agent.run(max_cycles=15)
        actions = [s.decision.action for s in steps]
        self.assertEqual(actions.count("discover"), 1)
        self.assertEqual(actions[-1], "stop")

    def test_discovery_delegates_through_the_team_and_logs_lineage(self):
        from revenue_os.task_log import TaskLog
        OperatorAgent(self.d, Goal()).run()
        entries = TaskLog.load(self.d / "task_log.json").entries()
        caps = [e["capability"] for e in entries]
        self.assertIn("discover", caps)
        self.assertIn("evaluate", caps)
        self.assertIn("select", caps)
        root = next(e for e in entries if e["capability"] == "discover")
        self.assertIsNone(root["parent_id"])
        self.assertTrue(
            all(e["parent_id"] == root["task_id"]
                for e in entries if e["capability"] == "evaluate")
        )

    def test_trend_hunter_opt_in_writes_report_and_task(self):
        from revenue_os.task_log import TaskLog
        agent = OperatorAgent(self.d, Goal(trend_hunter=True))
        steps = agent.run()
        actions = [s.decision.action for s in steps]
        self.assertIn("analyze_trends", actions)
        report = json.loads((self.d / "trend_report.json").read_text())
        self.assertIn("keywords", report)
        self.assertGreaterEqual(report["count"], 1)
        agents = {e["agent"] for e in TaskLog.load(self.d / "task_log.json").entries()}
        self.assertIn("trend_hunter", agents)
        # runs once, then stops
        self.assertEqual(actions.count("analyze_trends"), 1)

    def test_trend_hunter_off_by_default(self):
        steps = OperatorAgent(self.d, Goal()).run()
        self.assertNotIn("analyze_trends",
                         [s.decision.action for s in steps])
        self.assertFalse((self.d / "trend_report.json").exists())

    def test_human_gated_capability_is_never_auto_executed(self):
        from revenue_os.operator import Decision
        agent = OperatorAgent(self.d, Goal())
        out = agent.act(Decision("run_ads", "hypothetical"))  # roster gate=human
        self.assertIn("human-gated", out["skipped"])

    def test_max_cycles_is_a_hard_cap(self):
        # a goal that would keep wanting discovery: stale threshold 0, shortlist huge
        agent = OperatorAgent(self.d, Goal(shortlist_n=999, discovery_stale_days=0))
        steps = agent.run(max_cycles=3)
        self.assertLessEqual(len(steps), 3)


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_agent_goal_persists(self):
        code, _ = _run(["agent-goal", "--shortlist", "5", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertEqual(load_goal(self.data).shortlist_n, 5)

    def test_agent_run_end_to_end(self):
        code, out = _run(["agent-run", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("discover", out)
        self.assertTrue((Path(self.data) / "candidates.json").exists())
        self.assertTrue((Path(self.data) / "agent_log.json").exists())

    def test_agent_log_empty(self):
        code, out = _run(["agent-log", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("no agent decisions", out)


if __name__ == "__main__":
    unittest.main()
