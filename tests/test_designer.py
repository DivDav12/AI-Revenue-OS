import unittest

from revenue_os.designer import DesignerAgent, build_design_spec
from revenue_os.messages import Task

_OPP = {"name": "customer-launch-plan"}
_OFFER = {"what_is_sold": "Customer Launch Plan", "price": 29.9, "currency": "EUR",
          "positioning": "A premium personalized strategy",
          "includes": ["Ideal customer profile", "14-day action plan"]}
_COPY = {"headline": "Find your first 10 customers", "subheadline": "in 14 days",
         "body": "You built it. Now sell it.", "faq": [{"question": "q", "answer": "a"}]}


class BuildDesignSpecTests(unittest.TestCase):
    def test_normal_input(self):
        s = build_design_spec(_OPP, _OFFER, copy=_COPY)
        self.assertEqual(s["visual_direction"]["tone"], "refined, high-trust, restrained")
        self.assertIn("hero", s["page_layout"]["sections"])
        self.assertIn("feature_list", s["page_layout"]["sections"])
        self.assertIn("faq", s["page_layout"]["sections"])
        self.assertEqual(len(s["asset_specs"]), 4)

    def test_never_claims_assets_exist(self):
        s = build_design_spec(_OPP, _OFFER, copy=_COPY)
        self.assertFalse(s["assets_exist"])
        self.assertTrue(all(b["exists"] is False for b in s["image_briefs"]))
        self.assertEqual(s["publishing"], "none - specification only")

    def test_degrades_without_copy(self):
        s = build_design_spec(_OPP, _OFFER)
        self.assertNotIn("faq", s["page_layout"]["sections"])
        self.assertNotIn("narrative", s["page_layout"]["sections"])

    def test_deterministic(self):
        self.assertEqual(build_design_spec(_OPP, _OFFER, copy=_COPY),
                         build_design_spec(_OPP, _OFFER, copy=_COPY))


class DesignerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return DesignerAgent(name="designer").run(
            Task(objective="x", capability="design_assets", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"opportunity": _OPP, "offer": _OFFER}).status, "ok")

    def test_missing_opportunity_is_an_error(self):
        self.assertEqual(self._run({"offer": _OFFER}).status, "error")

    def test_missing_offer_is_an_error(self):
        self.assertEqual(self._run({"opportunity": _OPP}).status, "error")

    def test_empty_offer_is_an_error(self):
        self.assertEqual(self._run({"opportunity": _OPP, "offer": {}}).status, "error")

    def test_malformed_copy_is_ignored_not_fatal(self):
        r = self._run({"opportunity": _OPP, "offer": _OFFER, "copy": "not a dict"})
        self.assertEqual(r.status, "ok")


if __name__ == "__main__":
    unittest.main()
