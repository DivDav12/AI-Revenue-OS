import unittest

from revenue_os.customer_support import CustomerSupportAgent, build_support_case
from revenue_os.messages import Task

_INTAKE = {"name": "Dana Lee", "email": "dana@example.com", "order_id": "ord-1"}


class BuildSupportCaseTests(unittest.TestCase):
    def test_delivery_question(self):
        c = build_support_case(_INTAKE, ["How long until I get my plan?"])
        self.assertEqual(c["support_case"]["category"], "delivery")
        self.assertIn("ord-1", c["response_draft"])
        self.assertFalse(c["auto_sent"])

    def test_refund_escalates(self):
        c = build_support_case(_INTAKE, ["I want a refund please"])
        self.assertEqual(c["support_case"]["category"], "refund")
        self.assertEqual(c["support_case"]["priority"], "high")
        self.assertIn("refund", c["escalation_reason"])
        self.assertIn("human", c["required_action"])

    def test_email_is_masked(self):
        c = build_support_case(_INTAKE, ["hi"])
        self.assertEqual(c["support_case"]["customer_ref"]["email_masked"], "d***@e***.com")
        self.assertNotIn("dana@example.com", str(c))

    def test_deterministic(self):
        self.assertEqual(build_support_case(_INTAKE, ["eta?"]),
                         build_support_case(_INTAKE, ["eta?"]))


class CustomerSupportAgentTests(unittest.TestCase):
    def _run(self, payload):
        return CustomerSupportAgent(name="customer_support").run(
            Task(objective="x", capability="support_customers", payload=payload))

    def test_ok(self):
        self.assertEqual(self._run({"intake": _INTAKE, "questions": ["eta?"]}).status, "ok")

    def test_missing_intake_is_an_error(self):
        self.assertEqual(self._run({"questions": ["x"]}).status, "error")

    def test_no_question_is_an_error(self):
        self.assertEqual(self._run({"intake": _INTAKE}).status, "error")

    def test_malformed_questions_is_an_error(self):
        self.assertEqual(self._run({"intake": _INTAKE, "questions": "x"}).status, "error")

    def test_never_auto_sends(self):
        out = self._run({"intake": _INTAKE, "questions": ["eta?"]}).output
        self.assertFalse(out["auto_sent"])
        self.assertIn("a human sends", out["delivery"])


if __name__ == "__main__":
    unittest.main()
