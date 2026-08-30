import unittest

from revenue_os.messages import Task
from revenue_os.sales_tracker import SalesTrackerAgent, build_funnel_state


class BuildFunnelStateTests(unittest.TestCase):
    def test_normal_input(self):
        s = build_funnel_state(
            leads=[{}, {}, {}, {}],
            reviewed_opportunities=[{"human_review_status": "reviewed"},
                                    {"human_review_status": "reviewed"}],
            offers=[{"price": 1}],
            payment_events=[{"capture_id": "abc"}],
        )
        self.assertEqual(s["lead_count"], 4)
        self.assertEqual(s["qualified_count"], 2)
        self.assertEqual(s["offer_count"], 1)
        self.assertEqual(s["paid_count"], 1)
        self.assertEqual(s["conversion_metrics"]["offer_to_paid"], 1.0)
        self.assertEqual(s["funnel_state"]["paid"], 1)

    def test_no_payment_feed_means_zero_not_inferred(self):
        s = build_funnel_state(leads=[{}], offers=[{"x": 1}])
        self.assertEqual(s["paid_count"], 0)
        self.assertIn("not inferred", s["payment_source_note"])
        self.assertEqual(s["fabricated_sales"], 0)

    def test_empty_input(self):
        s = build_funnel_state()
        self.assertEqual(s["funnel_state"],
                         {"leads": 0, "qualified": 0, "offers": 0, "paid": 0})

    def test_deterministic(self):
        self.assertEqual(build_funnel_state(leads=[{}], payment_events=[]),
                         build_funnel_state(leads=[{}], payment_events=[]))


class SalesTrackerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return SalesTrackerAgent(name="sales_tracker").run(
            Task(objective="x", capability="track_sales", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"leads": [{}], "payment_events": []}).status, "ok")

    def test_no_inputs_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_malformed_leads_is_an_error(self):
        self.assertEqual(self._run({"leads": "nope"}).status, "error")

    def test_malformed_payment_events_is_an_error(self):
        self.assertEqual(self._run({"payment_events": 5}).status, "error")

    def test_payment_truth_comes_from_the_feed(self):
        out = self._run({"offers": [{"a": 1}], "payment_events": [{"id": 1}, {"id": 2}]}).output
        self.assertEqual(out["paid_count"], 2)
        self.assertIn("PayPal", out["payment_source_note"])


if __name__ == "__main__":
    unittest.main()
