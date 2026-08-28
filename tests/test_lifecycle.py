import tempfile
import unittest
from pathlib import Path

from revenue_os import lifecycle
from revenue_os.approval import record_decision
from revenue_os.sources import RawSignal, StaticSource
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import run_discovery_cycle


def _cand(status: str = "discovered") -> Candidate:
    return Candidate(name="c", total=3.0, status=status)


class LifecycleTests(unittest.TestCase):
    def test_valid_transition_advances_and_records_history(self):
        out = lifecycle.advance(_cand(), "shortlisted", note="n", actor="system")
        self.assertEqual(out.status, "shortlisted")
        self.assertEqual(len(out.history), 1)
        self.assertEqual(out.history[0]["from"], "discovered")
        self.assertEqual(out.history[0]["to"], "shortlisted")
        self.assertEqual(out.history[0]["actor"], "system")

    def test_invalid_transition_raises(self):
        with self.assertRaises(ValueError):
            lifecycle.advance(_cand(), "approved")  # must be shortlisted first

    def test_reject_reachable_from_multiple_states(self):
        for status in ("discovered", "shortlisted", "approved", "investigating", "launched"):
            self.assertTrue(lifecycle.can_transition(status, "rejected"))
        self.assertFalse(lifecycle.can_transition("rejected", "rejected"))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")
        self.store.put(_cand("shortlisted"))

    def tearDown(self):
        self._dir.cleanup()

    def test_approve_moves_to_approved_with_approver(self):
        out = record_decision(self.store, "c", "approve", approver="owner", note="ok")
        self.assertEqual(out.status, "approved")
        self.assertEqual(out.history[-1]["actor"], "owner")
        self.assertEqual(self.store.get("c").status, "approved")

    def test_reject_moves_to_rejected(self):
        out = record_decision(self.store, "c", "reject", approver="owner")
        self.assertEqual(out.status, "rejected")

    def test_unknown_candidate_raises(self):
        with self.assertRaises(ValueError):
            record_decision(self.store, "missing", "approve", approver="owner")

    def test_invalid_decision_raises(self):
        with self.assertRaises(ValueError):
            record_decision(self.store, "c", "maybe", approver="owner")


class DiscoveryCycleTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "candidates.json"
        self.signals = [
            RawSignal(title="plain note one"),
            RawSignal(title="automation automate no-code api marketplace saas revenue"),
            RawSignal(title="a SaaS platform launch"),
            RawSignal(title="another plain note"),
        ]

    def tearDown(self):
        self._dir.cleanup()

    def test_cycle_persists_all_and_shortlists_top_n(self):
        store = CandidateStore.load(self.path)
        ranked = run_discovery_cycle(
            StaticSource(self.signals), store, limit=10, shortlist_n=2
        )
        self.assertEqual(len(ranked), 4)
        shortlisted = [c for c in ranked if c.status == "shortlisted"]
        self.assertEqual(len(shortlisted), 2)
        # highest scored is shortlisted
        self.assertEqual(ranked[0].status, "shortlisted")
        self.assertTrue(self.path.exists())

    def test_rerun_does_not_downgrade_human_decision(self):
        store = CandidateStore.load(self.path)
        run_discovery_cycle(StaticSource(self.signals), store, shortlist_n=2)
        top = store.all()[0].name
        record_decision(store, top, "approve", approver="owner")

        store2 = CandidateStore.load(self.path)
        run_discovery_cycle(StaticSource(self.signals), store2, shortlist_n=2)
        self.assertEqual(store2.get(top).status, "approved")

    def test_cycle_is_deterministic(self):
        s1 = CandidateStore(Path(self._dir.name) / "a.json")
        s2 = CandidateStore(Path(self._dir.name) / "b.json")
        r1 = run_discovery_cycle(StaticSource(self.signals), s1, shortlist_n=2)
        r2 = run_discovery_cycle(StaticSource(self.signals), s2, shortlist_n=2)
        self.assertEqual(
            [(c.name, c.total, c.status) for c in r1],
            [(c.name, c.total, c.status) for c in r2],
        )


if __name__ == "__main__":
    unittest.main()
