import unittest

from revenue_os.ads_manager import AdsManagerAgent, build_campaign_plan
from revenue_os.messages import Task

_OFFER = {"what_is_sold": "Customer Launch Plan", "price": 29.9, "currency": "EUR",
          "call_to_action": "Get your plan",
          "positioning": "find your first paying customers"}
_AUD = {"channels": ["search", "r/startups"], "suggested_daily_budget": 10.0,
        "test_days": 7, "hypotheses": ["searchers", "community members"]}


class BuildCampaignPlanTests(unittest.TestCase):
    def test_normal_input(self):
        p = build_campaign_plan(_OFFER, audience=_AUD, landing_page="checkout.html")
        self.assertEqual(len(p["ad_variants"]), 3)
        self.assertEqual(p["campaign_plan"]["channels"], ["search", "r/startups"])
        self.assertEqual(p["estimated_budget"]["total"], 70.0)
        self.assertEqual(p["estimated_budget"]["currency"], "EUR")

    def test_never_launches_or_spends(self):
        p = build_campaign_plan(_OFFER, audience=_AUD)
        self.assertFalse(p["launched"])
        self.assertEqual(p["spent"], 0)
        self.assertIn("NOT authorized", p["estimated_budget"]["basis"])
        self.assertTrue(p["human_gate_required"])

    def test_degrades_without_audience(self):
        p = build_campaign_plan(_OFFER)
        self.assertEqual(p["estimated_budget"]["total"], 0.0)
        self.assertTrue(p["targeting_hypotheses"])

    def test_deterministic(self):
        self.assertEqual(build_campaign_plan(_OFFER, audience=_AUD),
                         build_campaign_plan(_OFFER, audience=_AUD))


class AdsManagerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return AdsManagerAgent(name="ads_manager").run(
            Task(objective="x", capability="run_ads", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"offer": _OFFER}).status, "ok")

    def test_missing_offer_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_empty_offer_is_an_error(self):
        self.assertEqual(self._run({"offer": {}}).status, "error")

    def test_malformed_audience_is_an_error(self):
        self.assertEqual(self._run({"offer": _OFFER, "audience": "nope"}).status, "error")

    def test_human_gate(self):
        self.assertTrue(self._run({"offer": _OFFER}).output["human_gate_required"])


if __name__ == "__main__":
    unittest.main()
