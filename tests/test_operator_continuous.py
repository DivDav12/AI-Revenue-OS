import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import cli
from revenue_os.agent_log import AgentLog
from revenue_os.approval import record_decision
from revenue_os.llm_spend import LlmSpendLog
from revenue_os.operator import Goal, OperatorAgent, load_session
from revenue_os.store import CandidateStore


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


class _Now:
    def __init__(self):
        self.t = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class RunContinuousTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _agent(self, goal=None):
        return OperatorAgent(self.d, goal or Goal())

    def _kw(self, **over):
        base = dict(
            sleep_fn=_Sleep(), clock_fn=_Clock([0]), now_fn=_Now(),
        )
        base.update(over)
        return base

    def test_max_ticks_and_sleep_between_only(self):
        sleep = _Sleep()
        session = self._agent().run_continuous(
            5, max_ticks=3, **self._kw(sleep_fn=sleep)
        )
        self.assertEqual(session.ticks, 3)
        self.assertEqual(session.end_reason, "max-ticks")
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(sleep.calls, [5, 5])  # between the 3 ticks, not after

    def test_max_runtime_stops(self):
        clock = _Clock([0, 0, 100])  # start, first check, after tick 1
        session = self._agent().run_continuous(
            1, max_runtime_s=50, **self._kw(clock_fn=clock)
        )
        self.assertEqual(session.end_reason, "max-runtime")
        self.assertLessEqual(session.ticks, 1)

    def test_max_total_cycles_stops(self):
        session = self._agent().run_continuous(
            1, max_total_cycles=2, **self._kw()
        )
        self.assertEqual(session.end_reason, "max-total-cycles")
        self.assertEqual(session.ticks, 1)  # first tick = discover + stop = 2 cycles

    def test_max_spend_stops(self):
        def seed(_steps):
            log = LlmSpendLog.load(self.d / "llm_spend.json")
            log.add({"activity": "evaluate", "cost_usd": 1.0, "api_calls": 1})
            log.save()

        session = self._agent().run_continuous(
            1, max_spend_usd=0.5, on_tick=seed, **self._kw()
        )
        self.assertEqual(session.end_reason, "max-spend")

    def test_previous_ended_session_starts_fresh(self):
        agent = self._agent()
        now = _Now()
        s1 = agent.run_continuous(
            1, max_ticks=1, sleep_fn=_Sleep(), clock_fn=_Clock([0]), now_fn=now
        )
        s2 = agent.run_continuous(
            1, max_ticks=1, sleep_fn=_Sleep(), clock_fn=_Clock([0]), now_fn=now
        )
        self.assertNotEqual(s1.started_at, s2.started_at)  # a new session, not resumed
        self.assertEqual(s2.ticks, 1)

    def test_unfinished_session_resumes(self):
        (self.d).mkdir(parents=True, exist_ok=True)
        (self.d / "agent_session.json").write_text(json.dumps({
            "started_at": "ORIG", "last_tick_at": "", "ticks": 2, "cycles": 4,
            "spend_baseline_usd": 0.0, "ended_at": None, "end_reason": None,
        }), encoding="utf-8")
        session = self._agent().run_continuous(1, max_ticks=4, **self._kw())
        self.assertEqual(session.started_at, "ORIG")  # resumed, not restarted
        self.assertEqual(session.ticks, 4)

    def test_log_hygiene_no_repeated_stop(self):
        self._agent().run_continuous(1, max_ticks=4, **self._kw())
        actions = [e["action"] for e in AgentLog.load(self.d / "agent_log.json").entries()]
        self.assertEqual(actions.count("stop"), 1)
        self.assertIn("session_start", actions)
        self.assertIn("session_end", actions)
        self.assertEqual(actions.count("discover"), 1)

    def test_human_action_between_ticks_is_picked_up(self):
        approved = {}

        def approve_one(_steps):
            if approved:
                return
            store = CandidateStore.load(self.d / "candidates.json")
            name = next((c.name for c in store.all() if c.status == "shortlisted"), None)
            if name:
                record_decision(store, name, "approve", approver="test")
                approved["name"] = name

        self._agent().run_continuous(1, max_ticks=3, on_tick=approve_one, **self._kw())
        status = CandidateStore.load(self.d / "candidates.json").get(approved["name"]).status
        self.assertEqual(status, "investigating")

    def test_keyboard_interrupt_is_clean(self):
        def boom(_steps):
            raise KeyboardInterrupt

        session = self._agent().run_continuous(1, on_tick=boom, **self._kw())
        self.assertEqual(session.end_reason, "interrupted")
        self.assertIsNotNone(session.ended_at)
        AgentLog.load(self.d / "agent_log.json")  # must not raise
        s, resumed = load_session(self.d)
        self.assertFalse(resumed)  # ended -> not resumable

    def test_gate_safety_across_continuous_run(self):
        self._agent().run_continuous(1, max_ticks=5, **self._kw())
        statuses = {
            c.status for c in CandidateStore.load(self.d / "candidates.json").all()
        }
        self.assertTrue(statuses <= {"discovered", "shortlisted"})


class AgentLoopCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_agent_loop_bounded(self):
        code, out = _run([
            "agent-loop", "--interval", "0", "--max-ticks", "2", "--data-dir", self.data,
        ])
        self.assertEqual(code, 0)
        self.assertIn("stopped: max-ticks", out)
        session = json.loads((Path(self.data) / "agent_session.json").read_text())
        self.assertIsNotNone(session["ended_at"])

    def test_agent_run_still_works(self):
        code, out = _run(["agent-run", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("discover", out)


if __name__ == "__main__":
    unittest.main()
