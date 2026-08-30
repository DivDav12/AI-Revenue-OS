import unittest

from revenue_os.automation_engineer import (
    AutomationEngineerAgent,
    build_workflow_graph,
)
from revenue_os.messages import Task

_STEPS = [
    {"id": "discover", "capability": "discover"},
    {"id": "select", "capability": "select"},
    {"id": "store", "capability": "build_store"},   # roster gate=human
    {"id": "ads", "capability": "run_ads"},         # roster gate=human
]


class BuildWorkflowGraphTests(unittest.TestCase):
    def test_normal_input_builds_a_linear_graph(self):
        g = build_workflow_graph(_STEPS)
        self.assertEqual(len(g["workflow_graph"]["nodes"]), 4)
        self.assertEqual(len(g["dependencies"]), 3)
        self.assertEqual(len(g["failure_paths"]), 4)

    def test_no_daemon_only_cycle_triggers(self):
        g = build_workflow_graph(_STEPS)
        self.assertFalse(g["daemon"])
        joined = " ".join(g["triggers"]).lower()
        self.assertIn("manual", joined)
        self.assertNotIn("cron", joined)
        self.assertNotIn("daemon", joined)

    def test_human_gated_steps_are_flagged(self):
        g = build_workflow_graph(_STEPS)
        self.assertIn("store", g["human_gates"])
        self.assertIn("ads", g["human_gates"])
        for always in ("publishing", "spending", "payment", "external contact"):
            self.assertIn(always, g["human_gates"])

    def test_daemon_request_is_refused(self):
        g = build_workflow_graph(_STEPS, workflow_specification={"schedule": "every 5 min"})
        self.assertTrue(g["blocking_issues"])

    def test_restart_safe(self):
        g = build_workflow_graph(_STEPS)
        self.assertTrue(g["restart_safe"])

    def test_deterministic(self):
        self.assertEqual(build_workflow_graph(_STEPS), build_workflow_graph(_STEPS))


class AutomationEngineerAgentTests(unittest.TestCase):
    def _run(self, payload):
        return AutomationEngineerAgent(name="automation_engineer").run(
            Task(objective="x", capability="automate", payload=payload))

    def test_ok_from_steps(self):
        self.assertEqual(self._run({"steps": _STEPS}).status, "ok")

    def test_ok_from_agent_outputs_key(self):
        self.assertEqual(self._run({"agent_outputs": _STEPS}).status, "ok")

    def test_missing_steps_is_an_error(self):
        self.assertEqual(self._run({}).status, "error")

    def test_empty_steps_is_an_error(self):
        self.assertEqual(self._run({"steps": []}).status, "error")

    def test_malformed_workflow_spec_is_an_error(self):
        self.assertEqual(
            self._run({"steps": _STEPS, "workflow_specification": "nope"}).status,
            "error")

    def test_human_gate_required(self):
        self.assertTrue(self._run({"steps": _STEPS}).output["human_gate_required"])


if __name__ == "__main__":
    unittest.main()
