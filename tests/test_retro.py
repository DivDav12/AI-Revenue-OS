import tempfile
import unittest
from pathlib import Path

from revenue_os.opportunity import CRITERIA
from revenue_os.retro import outcome_retro
from revenue_os.store import Candidate, CandidateStore


def _cand(name, status, outcome=None, breakdown=None, total=0.0):
    return Candidate(
        name=name, status=status, total=total,
        breakdown=breakdown or {},
        outcome={} if outcome is None else {"outcome": outcome, "metric_value": "m"},
    )


def _bd(**over):
    b = {c: 2.5 for c in CRITERIA}
    b.update(over)
    return b


class OutcomeRetroTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_not_ready_below_min(self):
        self.store.put(_cand("a", "validated", "validated", _bd(), 3.0))
        self.store.put(_cand("b", "rejected", "rejected", _bd(), 2.0))
        retro = outcome_retro(self.store)
        self.assertFalse(retro["ready"])
        self.assertEqual(retro["counts"], {"validated": 1, "rejected": 1})

    def test_min_outcomes_override(self):
        self.store.put(_cand("a", "validated", "validated", _bd(), 3.0))
        self.assertTrue(outcome_retro(self.store, min_outcomes=1)["ready"])

    def test_shortlist_reject_excluded(self):
        # status rejected but no recorded validation outcome
        self.store.put(_cand("triage", "rejected", None, _bd(), 1.0))
        self.store.put(_cand("a", "validated", "validated", _bd(), 3.0))
        self.store.put(_cand("b", "validated", "validated", _bd(), 3.0))
        self.store.put(_cand("c", "rejected", "rejected", _bd(), 2.0))
        retro = outcome_retro(self.store)
        self.assertEqual(retro["counts"], {"validated": 2, "rejected": 1})

    def test_launched_counts_as_validated(self):
        self.store.put(_cand("live", "earning", "validated", _bd(), 3.5))
        self.store.put(_cand("a", "validated", "validated", _bd(), 3.0))
        self.store.put(_cand("c", "rejected", "rejected", _bd(), 2.0))
        retro = outcome_retro(self.store)
        self.assertEqual(retro["counts"]["validated"], 2)

    def test_by_criterion_averages_and_gap(self):
        self.store.put(_cand("v1", "validated", "validated", _bd(demand=4.0), 3.0))
        self.store.put(_cand("v2", "validated", "validated", _bd(demand=5.0), 3.0))
        self.store.put(_cand("r1", "rejected", "rejected", _bd(demand=1.0), 2.0))
        retro = outcome_retro(self.store)
        self.assertTrue(retro["ready"])
        d = retro["by_criterion"]["demand"]
        self.assertEqual(d["validated_avg"], 4.5)
        self.assertEqual(d["rejected_avg"], 1.0)
        self.assertEqual(d["gap"], 3.5)
        # a criterion equal on both sides has zero gap
        self.assertEqual(retro["by_criterion"]["scalability"]["gap"], 0.0)
        self.assertEqual(retro["total"]["validated_avg"], 3.0)

    def test_most_predictive_is_top_three_by_abs_gap(self):
        self.store.put(
            _cand("v", "validated", "validated",
                  _bd(demand=5.0, profit_potential=4.0, scalability=3.5), 4.0)
        )
        self.store.put(_cand("v2", "validated", "validated", _bd(demand=5.0), 3.0))
        self.store.put(_cand("r", "rejected", "rejected", _bd(), 2.0))
        retro = outcome_retro(self.store)
        self.assertEqual(len(retro["most_predictive"]), 3)
        self.assertEqual(retro["most_predictive"][0], "demand")

    def test_empty_store(self):
        retro = outcome_retro(self.store)
        self.assertFalse(retro["ready"])
        self.assertEqual(retro["outcomes"], [])
        self.assertEqual(retro["total"]["validated_avg"], 0.0)


if __name__ == "__main__":
    unittest.main()
