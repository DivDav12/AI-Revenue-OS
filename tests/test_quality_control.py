import unittest

from revenue_os.messages import Task
from revenue_os.quality_control import QualityControlAgent, run_quality_checks

_OFFER = {"price": 29.9, "currency": "EUR", "what_is_sold": "Customer Launch Plan"}
_COPY = {"headline": "Find your first customers", "body": "Just 29.90 EUR to start."}
_PAGE = ("<div id='paypal-button-container'></div>"
         "<script src='https://www.paypal.com/sdk/js?client-id=x'></script>"
         "29.90 EUR  contact: divdav12support@gmail.com")


class RunQualityChecksTests(unittest.TestCase):
    def test_clean_inputs_pass(self):
        q = run_quality_checks(offer=_OFFER, copy=_COPY, landing_page=_PAGE,
                               expected_business_email="divdav12support@gmail.com")
        self.assertEqual(q["qc_status"], "pass")
        self.assertEqual(q["failed_checks"], [])

    def test_pricing_mismatch_blocks(self):
        q = run_quality_checks(offer=_OFFER, copy=_COPY,
                               landing_page="buy now for 49.00 EUR")
        self.assertEqual(q["qc_status"], "block")
        self.assertTrue(any("pricing mismatch" in f for f in q["failed_checks"]))

    def test_missing_required_offer_field_blocks(self):
        q = run_quality_checks(offer={"price": 10}, copy=_COPY, landing_page=_PAGE)
        self.assertEqual(q["qc_status"], "block")

    def test_prohibited_autonomous_action_blocks(self):
        q = run_quality_checks(offer=_OFFER, copy=_COPY, landing_page=_PAGE,
                               agent_results=[{"output": {"launched": True}}])
        self.assertEqual(q["qc_status"], "block")
        self.assertTrue(q["blocking_issues"])

    def test_spend_reported_blocks(self):
        q = run_quality_checks(offer=_OFFER, copy=_COPY, landing_page=_PAGE,
                               agent_results=[{"spent": 12.5}])
        self.assertEqual(q["qc_status"], "block")

    def test_never_passes_on_existence_alone(self):
        q = run_quality_checks(build_artifacts=["something exists"])
        self.assertEqual(q["qc_status"], "block")
        self.assertFalse(q["core_checks_ran"])

    def test_paypal_container_without_sdk_blocks(self):
        q = run_quality_checks(offer=_OFFER, copy=_COPY,
                               landing_page="<div id='paypal-button-container'></div> 29.90 EUR")
        self.assertTrue(any("SDK" in f for f in q["failed_checks"]))

    def test_can_block_downstream_flag(self):
        self.assertTrue(run_quality_checks(offer=_OFFER, copy=_COPY,
                                           landing_page=_PAGE)["can_block_downstream"])

    def test_deterministic(self):
        self.assertEqual(run_quality_checks(offer=_OFFER, copy=_COPY, landing_page=_PAGE),
                         run_quality_checks(offer=_OFFER, copy=_COPY, landing_page=_PAGE))


class QualityControlAgentTests(unittest.TestCase):
    def _run(self, payload):
        return QualityControlAgent(name="quality_control").run(
            Task(objective="x", capability="quality_check", payload=payload))

    def test_ok(self):
        r = self._run({"offer": _OFFER, "copy": _COPY, "landing_page": _PAGE})
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["qc_status"], "pass")

    def test_no_artefacts_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_malformed_offer_is_an_error(self):
        self.assertEqual(self._run({"offer": "nope"}).status, "error")

    def test_malformed_agent_results_is_an_error(self):
        self.assertEqual(
            self._run({"offer": _OFFER, "agent_results": {}}).status, "error")

    def test_blocks_a_bad_pipeline(self):
        out = self._run({"offer": _OFFER, "copy": _COPY,
                         "landing_page": "wrong 99.00 EUR",
                         "agent_results": [{"output": {"auto_sent": True}}]}).output
        self.assertEqual(out["qc_status"], "block")


if __name__ == "__main__":
    unittest.main()
