import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from revenue_os.approval import record_decision
from revenue_os.sources import RawSignal, StaticSource
from revenue_os.store import Candidate, CandidateStore
from revenue_os.validation import plan_validation, record_validation_outcome
from revenue_os.workflow import investigate_approved, run_discovery_cycle

_FULL_BREAKDOWN = {
    "startup_affordability": 3.0,
    "automation_potential": 3.0,
    "demand": 3.0,
    "competition_headroom": 3.0,
    "legal_feasibility": 3.0,
    "speed_to_first_revenue": 3.0,
    "profit_potential": 3.0,
    "scalability": 3.0,
}


def _cand(name="c", *, description="", status="approved", **overrides) -> Candidate:
    breakdown = {**_FULL_BREAKDOWN, **overrides}
    return Candidate(
        name=name, description=description, status=status, breakdown=breakdown, total=3.0
    )


class PlanValidationTests(unittest.TestCase):
    def test_plan_is_deterministic_and_free(self):
        c = _cand()
        p1, p2 = plan_validation(c), plan_validation(c)
        self.assertEqual(p1.cheapest_test, p2.cheapest_test)
        self.assertEqual(p1.max_cost, 0.0)
        self.assertFalse(p1.needs_human_budget)

    def test_marketplace_template(self):
        p = plan_validation(_cand(description="a marketplace for widgets"))
        self.assertIn("Concierge test", p.cheapest_test)

    def test_low_scalability_template(self):
        p = plan_validation(_cand(scalability=1.0))
        self.assertIn("outreach", p.cheapest_test.lower())

    def test_high_automation_template(self):
        p = plan_validation(_cand(automation_potential=5.0))
        self.assertIn("landing", p.cheapest_test.lower())

    def test_fallback_template(self):
        p = plan_validation(_cand(automation_potential=2.5, scalability=3.0))
        self.assertIn("problem-interview", p.cheapest_test.lower())


class InvestigateApprovedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_only_approved_get_a_plan_and_advance(self):
        self.store.put(_cand("approved-one", status="approved"))
        self.store.put(_cand("shortlisted-one", status="shortlisted"))

        out = investigate_approved(self.store)

        self.assertEqual([c.name for c in out], ["approved-one"])
        self.assertEqual(self.store.get("approved-one").status, "investigating")
        self.assertTrue(self.store.get("approved-one").plan)
        self.assertEqual(self.store.get("shortlisted-one").status, "shortlisted")

    def test_idempotent(self):
        self.store.put(_cand("a", status="approved"))
        investigate_approved(self.store)
        first = self.store.get("a")
        investigate_approved(self.store)
        second = self.store.get("a")
        self.assertEqual(second.status, "investigating")
        self.assertEqual(second.history, first.history)  # no repeated transition
        self.assertEqual(second.plan, first.plan)


class RecordOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")
        self.store.put(_cand("a", status="approved"))
        investigate_approved(self.store)

    def tearDown(self):
        self._dir.cleanup()

    def test_validated_outcome(self):
        out = record_validation_outcome(
            self.store, "a", "validated", metric_value="30 signups", actor="owner"
        )
        self.assertEqual(out.status, "validated")
        self.assertEqual(out.outcome["metric_value"], "30 signups")
        self.assertEqual(out.outcome["actor"], "owner")

    def test_rejected_outcome(self):
        out = record_validation_outcome(
            self.store, "a", "rejected", metric_value="2 signups", actor="owner"
        )
        self.assertEqual(out.status, "rejected")

    def test_outcome_from_wrong_state_raises(self):
        self.store.put(_cand("b", status="approved"))
        with self.assertRaises(ValueError):
            record_validation_outcome(
                self.store, "b", "validated", metric_value="x", actor="owner"
            )

    def test_unknown_candidate_and_bad_outcome_raise(self):
        with self.assertRaises(ValueError):
            record_validation_outcome(
                self.store, "missing", "validated", metric_value="x", actor="owner"
            )
        with self.assertRaises(ValueError):
            record_validation_outcome(
                self.store, "a", "maybe", metric_value="x", actor="owner"
            )


class EndToEndTests(unittest.TestCase):
    def test_discover_to_validated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "candidates.json"
            store = CandidateStore.load(path)
            signals = [
                RawSignal(title="automation automate no-code api saas platform revenue"),
                RawSignal(title="plain note"),
            ]
            run_discovery_cycle(StaticSource(signals), store, shortlist_n=1)
            top = store.all()[0].name
            record_decision(store, top, "approve", approver="owner")
            investigate_approved(store)
            record_validation_outcome(
                store, top, "validated", metric_value="26 signups", actor="owner"
            )

            reloaded = CandidateStore.load(path)
            final = reloaded.get(top)
            self.assertEqual(final.status, "validated")
            self.assertTrue(final.plan)
            self.assertEqual(final.outcome["outcome"], "validated")


class RegressionTests(unittest.TestCase):
    def test_candidate_roundtrips_with_new_fields(self):
        c = replace(_cand("x"), plan={"a": 1}, outcome={"b": 2})
        restored = Candidate.from_dict(c.to_dict())
        self.assertEqual(restored.plan, {"a": 1})
        self.assertEqual(restored.outcome, {"b": 2})


if __name__ == "__main__":
    unittest.main()
