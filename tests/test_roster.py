import unittest

from revenue_os import roster
from revenue_os.operator import Goal


class RosterTests(unittest.TestCase):
    def test_twenty_one_agents_grouped_in_five_clusters(self):
        self.assertEqual(len(roster.AGENTS), 21)
        self.assertEqual(set(roster.CLUSTERS),
                         {a.cluster for a in roster.AGENTS})
        counts = {c: len(v) for c, v in roster.by_cluster().items()}
        self.assertEqual(counts,
                         {"discovery": 6, "build": 6, "marketing": 3,
                          "revenue": 3, "support": 3})

    def test_ids_and_capabilities_are_unique(self):
        self.assertEqual(len({a.id for a in roster.AGENTS}), 21)
        self.assertEqual(len({a.capability for a in roster.AGENTS}), 21)

    def test_live_agents_are_the_implemented_workers(self):
        self.assertEqual(
            {a.id for a in roster.live()},
            {"market_scanner", "opportunity_finder", "product_researcher",
             "competitor_analyzer", "trend_hunter", "copywriter",
             "revenue_analyst", "content_creator"},
        )
        for a in roster.planned():
            self.assertEqual(a.status, "planned")

    def test_lookup_helpers(self):
        self.assertEqual(roster.get("ads_manager").name, "Ads Manager")
        self.assertEqual(roster.by_capability("select").id, "opportunity_finder")
        self.assertEqual(roster.by_capability("analyze_trends").id, "trend_hunter")
        self.assertEqual(roster.by_capability("analyze_competition").id,
                         "competitor_analyzer")
        self.assertEqual(roster.by_capability("write_copy").id, "copywriter")
        self.assertEqual(roster.by_capability("analyze_revenue").id, "revenue_analyst")
        self.assertEqual(roster.by_capability("package_deliverable").id, "content_creator")
        self.assertIsNone(roster.get("nope"))

    def test_money_touching_clusters_are_human_gated(self):
        for a in roster.AGENTS:
            if a.cluster == "marketing":
                self.assertEqual(a.gate, "human", a.id)

    def test_live_mode_fields_exist_on_goal(self):
        g = Goal()
        for a in roster.live():
            if a.mode_field is not None:
                self.assertTrue(hasattr(g, a.mode_field), a.mode_field)


if __name__ == "__main__":
    unittest.main()
