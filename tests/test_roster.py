import unittest

from revenue_os import roster
from revenue_os.operator import Goal
from revenue_os.team import build_team


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
        # every live agent except market_scanner (which needs a source) is
        # registered in the team by its roster id.
        registered = {a.name for a in build_team().registry.agents}
        for a in roster.live():
            if a.id == "market_scanner":
                continue
            self.assertIn(a.id, registered, a.id)
        for a in roster.planned():
            self.assertEqual(a.status, "planned")

    def test_phase_a_build_agents_are_live(self):
        for agent_id in ("supplier_finder", "designer", "store_builder",
                         "developer", "automation_engineer"):
            self.assertEqual(roster.get(agent_id).status, "live", agent_id)

    def test_phase_b_marketing_agents_are_live_and_human_gated(self):
        for agent_id in ("ads_manager", "campaign_optimizer", "budget_allocator"):
            spec = roster.get(agent_id)
            self.assertEqual(spec.status, "live", agent_id)
            self.assertEqual(spec.gate, "human", agent_id)

    def test_phase_c_revenue_agents_are_live(self):
        for agent_id in ("sales_tracker", "profit_master"):
            self.assertEqual(roster.get(agent_id).status, "live", agent_id)

    def test_phase_d_support_agents_are_live_and_all_21_live(self):
        for agent_id in ("customer_support", "review_manager", "quality_control"):
            self.assertEqual(roster.get(agent_id).status, "live", agent_id)
        self.assertEqual(len(roster.live()), 21)
        self.assertEqual(roster.planned(), ())
        self.assertEqual(roster.blocked(), ())

    def test_no_live_agent_has_unmet_dependencies(self):
        for a in roster.live():
            self.assertEqual(roster.unmet_dependencies(a), (), a.id)

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
