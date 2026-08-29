import unittest

from revenue_os.messages import Task
from revenue_os.trend import TrendHunterAgent, build_trend_report


class TrendReportTests(unittest.TestCase):
    def test_counts_repeated_keywords_and_sources(self):
        cands = [
            {"name": "notion automation tool", "description": "automation for teams",
             "source": "hn", "total": 3.0},
            {"name": "zapier automation clone", "description": "workflow automation",
             "source": "hn", "total": 2.0},
            {"name": "budget tracker", "description": "personal finance",
             "source": "file", "total": 1.0},
        ]
        rep = build_trend_report(cands, runs=2)
        self.assertEqual(rep["count"], 3)
        self.assertEqual(rep["runs"], 2)
        kw = dict(rep["keywords"])
        self.assertEqual(kw.get("automation"), 4)   # appears 4x, > 1
        self.assertNotIn("finance", kw)             # appears once -> dropped
        self.assertEqual(rep["sources"], {"hn": 2, "file": 1})
        self.assertEqual(rep["score_max"], 3.0)

    def test_empty_corpus(self):
        rep = build_trend_report([], runs=0)
        self.assertEqual(rep["keywords"], [])
        self.assertEqual(rep["score_avg"], 0.0)

    def test_agent_wraps_report(self):
        agent = TrendHunterAgent(name="trend_hunter")
        r = agent.run(Task(objective="t", capability="analyze_trends",
                           payload={"candidates": [], "runs": 1}))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.output["runs"], 1)

    def test_agent_bad_payload(self):
        agent = TrendHunterAgent(name="trend_hunter")
        r = agent.run(Task(objective="t", capability="analyze_trends", payload={}))
        self.assertEqual(r.status, "error")


if __name__ == "__main__":
    unittest.main()
