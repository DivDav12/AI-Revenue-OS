import unittest

from revenue_os.messages import Task
from revenue_os.store_builder import StoreBuilderAgent, build_store_spec

_OPP = {"name": "ask-hn-how-do-you-find-your-first-paying-customers"}
_OFFER = {"what_is_sold": "Customer Launch Plan", "price": 29.9, "currency": "EUR",
          "call_to_action": "Get your plan",
          "includes": ["Ideal customer profile", "14-day action plan"]}


class BuildStoreSpecTests(unittest.TestCase):
    def test_normal_input(self):
        s = build_store_spec(_OPP, _OFFER)
        self.assertIn("hero", s["sections"])
        self.assertIn("feature_list", s["sections"])
        self.assertEqual(s["cta_specification"]["price"], 29.9)
        self.assertIn("build-checkout", s["checkout_intake_integration_spec"]["generator_command"])

    def test_preserves_paypal_business_email_and_formspree(self):
        s = build_store_spec(_OPP, _OFFER)
        spec = s["checkout_intake_integration_spec"]
        preserved = " ".join(spec["preserves"]).lower()
        self.assertIn("paypal", preserved)
        self.assertIn("business_email", preserved)
        self.assertIn("formspree", preserved)
        must_not = " ".join(spec["must_not"]).lower()
        self.assertIn("paypal client id", must_not)
        self.assertIn("formspree endpoint", must_not)

    def test_human_gate_and_no_publish(self):
        s = build_store_spec(_OPP, _OFFER)
        self.assertTrue(s["human_gate_required"])
        self.assertEqual(s["build_artifacts"], [])
        self.assertEqual(s["publish_blocked_until"], "human approval")

    def test_reads_no_environment_and_holds_no_credentials(self):
        # spec references $BUSINESS_EMAIL / the Formspree endpoint by name only,
        # never a resolved secret value
        s = build_store_spec(_OPP, _OFFER)
        self.assertIn("$BUSINESS_EMAIL", s["checkout_intake_integration_spec"]["generator_command"])
        self.assertEqual(s["checkout_intake_integration_spec"]["form_action"],
                         "<unchanged: existing Formspree endpoint>")

    def test_deterministic(self):
        self.assertEqual(build_store_spec(_OPP, _OFFER), build_store_spec(_OPP, _OFFER))


class StoreBuilderAgentTests(unittest.TestCase):
    def _run(self, payload):
        return StoreBuilderAgent(name="store_builder").run(
            Task(objective="x", capability="build_store", payload=payload))

    def test_ok_and_gated(self):
        r = self._run({"opportunity": _OPP, "offer": _OFFER})
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.output["human_gate_required"])

    def test_missing_offer_is_an_error(self):
        self.assertEqual(self._run({"opportunity": _OPP}).status, "error")

    def test_missing_opportunity_is_an_error(self):
        self.assertEqual(self._run({"offer": _OFFER}).status, "error")

    def test_malformed_design_is_ignored(self):
        r = self._run({"opportunity": _OPP, "offer": _OFFER, "design": 5})
        self.assertEqual(r.status, "ok")


if __name__ == "__main__":
    unittest.main()
