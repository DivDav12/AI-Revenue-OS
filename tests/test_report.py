import tempfile
import unittest
from pathlib import Path

from revenue_os import lifecycle
from revenue_os.analytics import roi_summary
from revenue_os.report import next_action, pipeline_report, render_text
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


if __name__ == "__main__":
    unittest.main()
