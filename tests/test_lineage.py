import unittest

from revenue_os.agent import Agent
from revenue_os.messages import Result, Task
from revenue_os.orchestrator import Orchestrator
from revenue_os.registry import AgentRegistry


class _Recurse(Agent):
    """Emits one child 'recurse' task on every run - bounded by max_depth."""

    role = "recurse"
    capabilities = ("recurse",)

    def run(self, task):
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={"seen": task.objective},
            follow_ups=(Task(capability="recurse", objective="child"),),
        )


class _FanOut(Agent):
    role = "fan"
    capabilities = ("fan",)

    def __init__(self, n, name=None):
        super().__init__(name=name)
        self.n = n

    def run(self, task):
        if task.depth > 0:
            return Result(task_id=task.id, agent=self.name, status="ok")
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            follow_ups=tuple(
                Task(capability="fan", objective=f"leaf-{i}") for i in range(self.n)
            ),
        )


class _MixedFollowUps(Agent):
    role = "mixed"
    capabilities = ("mixed",)

    def run(self, task):
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            follow_ups=(
                Task(capability="recurse", objective="handled"),
                Task(capability="nobody", objective="orphan"),
            ),
        )


def _orch(*agents, **kw) -> Orchestrator:
    reg = AgentRegistry()
    for a in agents:
        reg.register(a)
    return Orchestrator(registry=reg, **kw)


class MessageDefaultsTests(unittest.TestCase):
    def test_task_and_result_defaults(self):
        t = Task(objective="x")
        self.assertIsNone(t.parent_id)
        self.assertEqual(t.depth, 0)
        self.assertEqual(Result(task_id=t.id, agent="a", status="ok").follow_ups, ())


class LineageTests(unittest.TestCase):
    def test_child_gets_parent_and_depth(self):
        orch = _orch(_Recurse(name="r"), max_depth=1)
        root = Task(capability="recurse", objective="root")
        orch.add_task(root)
        orch.run_cycle()
        child = orch.children_of(root.id)
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0].parent_id, root.id)
        self.assertEqual(child[0].depth, 1)

    def test_three_level_chain(self):
        orch = _orch(_Recurse(name="r"), max_depth=2)
        root = Task(capability="recurse", objective="root")
        orch.add_task(root)
        orch.run_cycle()
        # root -> d1 -> d2 ; d3 dropped by max_depth
        depths = sorted(t.depth for t in orch.tasks_seen)
        self.assertEqual(depths, [0, 1, 2])
        d2 = next(t for t in orch.tasks_seen if t.depth == 2)
        d1 = next(t for t in orch.tasks_seen if t.depth == 1)
        self.assertEqual(d2.parent_id, d1.id)

    def test_max_depth_records_error_and_completes(self):
        orch = _orch(_Recurse(name="r"), max_depth=1)
        orch.add_task(Task(capability="recurse", objective="root"))
        orch.run_cycle()
        self.assertTrue(
            any("max task depth exceeded" in (r.error or "") for r in orch.results)
        )
        self.assertEqual(orch.pending, 0)

    def test_max_tasks_cap(self):
        orch = _orch(_Recurse(name="r"), max_depth=99)
        orch.add_task(Task(capability="recurse", objective="root"))
        results = orch.run_cycle(max_tasks=5)
        self.assertTrue(
            any("max tasks per cycle exceeded" in (r.error or "") for r in results)
        )
        self.assertEqual(orch.pending, 0)

    def test_unknown_capability_followup_does_not_block_siblings(self):
        orch = _orch(_MixedFollowUps(name="m"), _Recurse(name="r"), max_depth=2)
        orch.add_task(Task(capability="mixed", objective="root"))
        results = orch.run_cycle()
        statuses = [r.status for r in results]
        self.assertIn("error", statuses)   # the "nobody" orphan
        self.assertIn("ok", statuses)      # the recurse child ran
        self.assertTrue(any(r.error and "no capable agent" in r.error for r in results))

    def test_fan_out_lineage(self):
        orch = _orch(_FanOut(3, name="f"))
        root = Task(capability="fan", objective="root")
        orch.add_task(root)
        orch.run_cycle()
        kids = orch.children_of(root.id)
        self.assertEqual(len(kids), 3)
        self.assertEqual({k.parent_id for k in kids}, {root.id})
        self.assertEqual(len(orch.descendants_of(root.id)), 3)

    def test_sink_receives_every_dispatched_task(self):
        seen = []
        orch = _orch(_FanOut(2, name="f"),
                     sink=lambda t, r: seen.append((t.id, r.agent)))
        orch.add_task(Task(capability="fan", objective="root"))
        orch.run_cycle()
        self.assertEqual(len(seen), len(orch.tasks_seen))
        self.assertEqual([s[0] for s in seen], [t.id for t in orch.tasks_seen])

    def test_failing_sink_does_not_break_dispatch(self):
        def _boom(task, result):
            raise RuntimeError("sink down")
        orch = _orch(_FanOut(1, name="f"), sink=_boom)
        orch.add_task(Task(capability="fan", objective="root"))
        with self.assertLogs("revenue_os.orchestrator", level="WARNING"):
            self.assertEqual(orch.run_cycle()[0].status, "ok")


if __name__ == "__main__":
    unittest.main()
