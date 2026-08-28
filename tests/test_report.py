import tempfile
import unittest
from pathlib import Path

from revenue_os import lifecycle
from revenue_os.analytics import roi_summary
from revenue_os.opportunity import CRITERIA
from revenue_os.report import (
    next_action,
    pipeline_report,
    render_candidate,
    render_text,
)
from revenue_os.revenue import RevenueLedger, mark_launched, record_payment
from revenue_os.spend import SpendLedger, SpendRequest, authorize_spend, record_spend, set_budget
from revenue_os.store import Candidate, CandidateStore


def _ledgers(d: str):
    return (
        RevenueLedger(Path(d) / "revenue.json"),
        SpendLedger(Path(d) / "spend.json"),
    )


class NextActionTests(unittest.TestCase):
    def test_mapping_covers_every_status(self):
        expected = {
            "discovered": None,
            "shortlisted": "approve or reject",
            "approved": "run investigation",
            "investigating": "record validation outcome",
            "validated": "launch offer",
            "launched": "record first payment",
            "earning": "record further payments / spend",
            "rejected": None,
        }
        for status in lifecycle.STATUSES:
            self.assertIn(status, expected)
            self.assertEqual(
                next_action(Candidate(name="c", status=status)), expected[status]
            )

    def test_budget_decision_state_for_costed_plan(self):
        with tempfile.TemporaryDirectory() as d:
            _, spend = _ledgers(d)
            cand = Candidate(
                name="c", status="investigating",
                plan={"needs_human_budget": True, "max_cost": 62.0},
            )
            self.assertEqual(
                next_action(cand, spend),
                "set a validation budget, then record outcome",
            )
            from revenue_os.spend import set_budget

            set_budget(spend, "c", 70.0, approver="o")
            self.assertEqual(next_action(cand, spend), "record validation outcome")

    def test_free_plan_keeps_default_action(self):
        with tempfile.TemporaryDirectory() as d:
            _, spend = _ledgers(d)
            cand = Candidate(
                name="c", status="investigating",
                plan={"needs_human_budget": False},
            )
            self.assertEqual(next_action(cand, spend), "record validation outcome")


class PipelineReportTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")
        self.rev, self.spend = _ledgers(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_status_counts_and_action_queue(self):
        self.store.put(Candidate(name="a", status="discovered"))
        self.store.put(Candidate(name="b", status="shortlisted"))
        self.store.put(Candidate(name="c", status="investigating"))
        self.store.put(Candidate(name="d", status="rejected"))

        report = pipeline_report(self.store, self.rev, self.spend)

        self.assertEqual(report["status_counts"]["shortlisted"], 1)
        self.assertEqual(report["status_counts"]["discovered"], 1)
        self.assertEqual(report["status_counts"]["validated"], 0)
        queued = {i["name"]: i["next_action"] for i in report["action_queue"]}
        self.assertEqual(
            queued, {"b": "approve or reject", "c": "record validation outcome"}
        )

    def test_roi_matches_analytics(self):
        self.store.put(Candidate(name="x", status="launched"))
        record_payment(self.store, self.rev, "x", 50.0, actor="o")
        set_budget(self.spend, "x", 20.0, approver="o")
        authorize_spend(
            self.spend,
            SpendRequest(candidate_name="x", purpose="p", amount=10.0, requested_by="s"),
            approver="o",
            ceiling=100.0,
        )
        record_spend(self.spend, "x", 10.0, actor="o")

        report = pipeline_report(self.store, self.rev, self.spend)
        self.assertEqual(report["roi"], roi_summary(self.store, self.rev, self.spend))
        self.assertEqual(report["totals"]["grand_net"], 40.0)

    def test_render_text_is_deterministic_with_sections(self):
        self.store.put(Candidate(name="b", status="shortlisted"))
        report = pipeline_report(self.store, self.rev, self.spend)
        text1 = render_text(report)
        text2 = render_text(pipeline_report(self.store, self.rev, self.spend))
        self.assertEqual(text1, text2)
        self.assertIn("PIPELINE STATUS", text1)
        self.assertIn("ACTION QUEUE", text1)
        self.assertIn("ROI", text1)
        self.assertIn("b [shortlisted] -> approve or reject", text1)

    def test_empty_store(self):
        report = pipeline_report(self.store, self.rev, self.spend)
        self.assertEqual(report["action_queue"], [])
        self.assertEqual(report["totals"], {
            "candidates": 0,
            "grand_revenue": 0.0,
            "grand_spent": 0.0,
            "grand_net": 0.0,
        })
        self.assertIn("(nothing awaiting a human)", render_text(report))


class RenderCandidateTests(unittest.TestCase):
    def _scored(self, **over):
        breakdown = {name: 3.0 for name in CRITERIA}
        breakdown["demand"] = 1.0
        breakdown.update(over)
        return Candidate(
            name="c", status="shortlisted", total=2.75, verdict="hold",
            breakdown=breakdown,
        )

    def test_shows_all_eight_criteria_in_order(self):
        text = render_candidate(self._scored())
        self.assertIn("score breakdown", text)
        for name in CRITERIA:
            self.assertIn(name, text)
        positions = [text.index(name) for name in CRITERIA]
        self.assertEqual(positions, sorted(positions))

    def test_marks_only_sub_neutral_criteria(self):
        lines = render_candidate(self._scored()).splitlines()
        demand = next(l for l in lines if "demand" in l)
        automation = next(l for l in lines if "automation_potential" in l)
        self.assertTrue(demand.rstrip().endswith("<"))
        self.assertFalse(automation.rstrip().endswith("<"))

    def test_empty_breakdown_renders(self):
        text = render_candidate(Candidate(name="c", status="discovered"))
        self.assertIn("score breakdown", text)
        self.assertIn("(none)", text)

    def test_deterministic(self):
        cand = self._scored()
        self.assertEqual(render_candidate(cand), render_candidate(cand))

    def test_shows_estimate_source_and_rationale(self):
        cand = Candidate(
            name="c", status="shortlisted", total=3.0, verdict="hold",
            estimate_source="llm", rationale="clear demand, thin margins",
        )
        text = render_candidate(cand)
        self.assertIn("[llm]", text)
        self.assertIn("clear demand, thin margins", text)

    def test_empty_rationale_renders_none(self):
        text = render_candidate(Candidate(name="c", status="discovered"))
        self.assertIn("rationale", text)
        self.assertIn("[keyword]", text)


if __name__ == "__main__":
    unittest.main()
