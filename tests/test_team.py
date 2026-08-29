import tempfile
import unittest
from pathlib import Path

from revenue_os.messages import Task
from revenue_os.sources import build_source
from revenue_os.task_log import TaskLog
from revenue_os.team import build_team


class TeamTests(unittest.TestCase):
    def test_discovery_fan_out_lineage_is_real(self):
        with tempfile.TemporaryDirectory() as d:
            tlog = TaskLog(Path(d) / "task_log.json")
            team = build_team(source=build_source("static"), sink=tlog.record)
            root = Task(objective="discover", capability="discover",
                        payload={"limit": 10, "then": "evaluate"})
            team.add_task(root)
            results = team.run_cycle()

            agents = [r.agent for r in results]
            self.assertEqual(agents[0], "market_scanner")
            self.assertTrue(all(a == "evaluator" for a in agents[1:]))
            self.assertGreater(len(agents), 1)

            # every evaluate task is a real child of the discover task
            children = team.children_of(root.id)
            self.assertEqual(len(children), len(agents) - 1)
            self.assertTrue(all(c.depth == 1 for c in children))

            entries = tlog.entries()
            self.assertEqual(entries[0]["capability"], "discover")
            self.assertTrue(
                all(e["parent_id"] == root.id for e in entries[1:])
            )

    def test_select_agent_ranks_the_scores(self):
        with tempfile.TemporaryDirectory() as d:
            tlog = TaskLog(Path(d) / "task_log.json")
            team = build_team(source=build_source("static"), sink=tlog.record)
            team.add_task(Task(objective="d", capability="discover",
                               payload={"limit": 10, "then": "evaluate"}))
            scored = [
                {"name": r.output["opportunity_name"], "total": r.output["total"]}
                for r in team.run_cycle() if r.output.get("opportunity_name")
            ]
            team.add_task(Task(objective="s", capability="select",
                               payload={"scored": scored, "min_score": 0.0,
                                        "shortlist_n": 2}))
            out = team.run_cycle()[0]
            self.assertEqual(out.agent, "opportunity_finder")
            self.assertEqual(len(out.output["shortlist"]), 2)

    def test_team_registers_the_live_agents(self):
        from revenue_os.messages import Task as _T
        team = build_team()
        names = {a.name for a in team.registry.agents}
        self.assertEqual(
            names,
            {"evaluator", "opportunity_finder", "product_researcher",
             "competitor_analyzer", "copywriter", "trend_hunter"},
        )
        for cap, name in (("analyze_competition", "competitor_analyzer"),
                          ("write_copy", "copywriter")):
            self.assertEqual(
                team.registry.find_for(_T(objective="x", capability=cap)).name, name
            )

    def test_no_then_keeps_discovery_inert(self):
        team = build_team(source=build_source("static"))
        team.add_task(Task(objective="d", capability="discover",
                           payload={"limit": 5}))
        results = team.run_cycle()
        self.assertEqual(len(results), 1)          # no evaluate follow-ups
        self.assertEqual(results[0].agent, "market_scanner")


if __name__ == "__main__":
    unittest.main()
