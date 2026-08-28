import unittest

from revenue_os.messages import Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.agent import WorkerAgent
from revenue_os.runner import run_once


class RunnerTests(unittest.TestCase):
    def test_task_flows_through_orchestrator_to_result(self):
        orch = Orchestrator(worker=WorkerAgent(name="worker-test"))
        task = Task(objective="do the thing", payload={"n": 1})
        orch.add_task(task)

        results = orch.run_cycle()

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.task_id, task.id)
        self.assertEqual(result.agent, "worker-test")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["handled_objective"], "do the thing")
        self.assertEqual(result.output["received_payload"], {"n": 1})
        self.assertEqual(orch.pending, 0)

    def test_dispatch_with_no_tasks_returns_none(self):
        orch = Orchestrator(worker=WorkerAgent())
        self.assertIsNone(orch.dispatch_next())

    def test_run_once_default_cycle(self):
        results = run_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok")


if __name__ == "__main__":
    unittest.main()
