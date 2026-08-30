import unittest

from revenue_os.messages import Task
from revenue_os.review_manager import ReviewManagerAgent, build_review_report

_FEEDBACK = [
    {"text": "Really helpful and actionable plan, worth it", "rating": 5},
    {"text": "The plan was too generic, disappointed", "rating": 2},
    {"text": "Good but I wish it had more detail on pricing", "rating": 4},
    {"text": "generic advice, wanted a refund", "rating": 1},
]


class BuildReviewReportTests(unittest.TestCase):
    def test_normal_input(self):
        r = build_review_report(_FEEDBACK)
        self.assertEqual(r["reviewed_count"], 4)
        self.assertEqual(len(r["complaints"]), 2)
        self.assertGreaterEqual(len(r["positive_signals"]), 1)
        self.assertGreaterEqual(len(r["improvement_requests"]), 1)
        self.assertEqual(r["priority"], "high")
        self.assertIn("quality_control", r["feeds_into"])

    def test_no_feedback_fabricates_nothing(self):
        r = build_review_report([])
        self.assertEqual(r["reviewed_count"], 0)
        self.assertEqual(r["themes"], [])
        self.assertEqual(r["complaints"], [])
        self.assertEqual(r["fabricated"], 0)

    def test_items_without_text_are_ignored(self):
        r = build_review_report([{"rating": 5}, {"text": "  "}])
        self.assertEqual(r["reviewed_count"], 0)

    def test_deterministic(self):
        self.assertEqual(build_review_report(_FEEDBACK), build_review_report(_FEEDBACK))


class ReviewManagerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return ReviewManagerAgent(name="review_manager").run(
            Task(objective="x", capability="manage_reviews", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"feedback": _FEEDBACK}).status, "ok")

    def test_missing_feedback_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_malformed_feedback_is_an_error(self):
        self.assertEqual(self._run({"feedback": "nope"}).status, "error")

    def test_malformed_support_cases_is_an_error(self):
        self.assertEqual(
            self._run({"feedback": [], "support_cases": 3}).status, "error")


if __name__ == "__main__":
    unittest.main()
