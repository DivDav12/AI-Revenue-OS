import unittest

from revenue_os.agent import ReverseAgent, WorkerAgent
from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry


def _orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(WorkerAgent(name="echo-worker"))
    registry.register(ReverseAgent(name="reverse-worker"))
    return Orchestrator(registry=registry)


class MultiAgentTests(unittest.TestCase):
    def test_task_routed_to_agent_by_capability(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="abc", capability="reverse"))

        result = orch.dispatch_next()

        self.assertEqual(result.agent, "reverse-worker")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["reversed"], "cba")

    def test_task_without_capability_falls_back_to_first_agent(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="no capability given"))

        result = orch.dispatch_next()

        self.assertEqual(result.agent, "echo-worker")
        self.assertEqual(result.status, "ok")

    def test_unknown_capability_produces_error_result(self):
        orch = _orchestrator()
        orch.add_task(Task(objective="x", capability="does-not-exist"))

        result = orch.dispatch_next()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.agent, "orchestrator")

    def test_registry_rejects_duplicate_agent_name(self):
        registry = AgentRegistry()
        registry.register(WorkerAgent(name="dup"))
        with self.assertRaises(ValueError):
            registry.register(WorkerAgent(name="dup"))


if __name__ == "__main__":
    unittest.main()
