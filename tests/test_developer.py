import unittest

from revenue_os.developer import DeveloperAgent, build_implementation_plan
from revenue_os.messages import Task

_SPEC = {"component": "sales-tracker", "requirements": [
    "add a funnel_state aggregator", "expose conversion_metrics"]}


class BuildImplementationPlanTests(unittest.TestCase):
    def test_normal_input_plans_a_test_per_file(self):
        p = build_implementation_plan(_SPEC)
        self.assertEqual(p["implementation_status"], "planned")
        self.assertEqual(len(p["proposed_files"]), 2)
        self.assertEqual(len(p["required_tests"]), 2)
        self.assertTrue(p["tests_cover_every_file"])
        self.assertEqual(p["files_changed"], [])
        self.assertEqual(p["tests_added"], [])

    def test_empty_requirements(self):
        p = build_implementation_plan({"component": "x", "requirements": []})
        self.assertEqual(p["implementation_status"], "empty")
        self.assertFalse(p["tests_cover_every_file"])

    def test_forbidden_paypal_credential_change_is_blocked(self):
        p = build_implementation_plan(
            {"component": "x", "requirements": ["rotate the paypal client secret"]})
        self.assertEqual(p["implementation_status"], "blocked")
        self.assertTrue(any("PayPal" in b for b in p["blocking_issues"]))

    def test_destructive_op_is_blocked(self):
        p = build_implementation_plan(
            {"requirements": ["run rm -rf on the data dir"]})
        self.assertEqual(p["implementation_status"], "blocked")

    def test_secret_in_spec_is_blocked_not_echoed(self):
        p = build_implementation_plan(
            {"requirements": ["set api_key=sk-abcdefgh12345678 in config"]})
        self.assertEqual(p["implementation_status"], "blocked")
        self.assertNotIn("sk-abcdefgh12345678", str(p))

    def test_human_gate_and_no_execution(self):
        p = build_implementation_plan(_SPEC)
        self.assertTrue(p["human_gate_required"])
        self.assertIn("plan only", p["note"])

    def test_deterministic(self):
        self.assertEqual(build_implementation_plan(_SPEC),
                         build_implementation_plan(_SPEC))


class DeveloperAgentTests(unittest.TestCase):
    def _run(self, payload):
        return DeveloperAgent(name="developer").run(
            Task(objective="x", capability="develop", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"build_specification": _SPEC}).status, "ok")

    def test_missing_spec_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_empty_spec_is_an_error(self):
        self.assertEqual(self._run({"build_specification": {}}).status, "error")

    def test_malformed_requirements_is_an_error(self):
        self.assertEqual(
            self._run({"build_specification": _SPEC,
                       "technical_requirements": "nope"}).status, "error")


if __name__ == "__main__":
    unittest.main()
