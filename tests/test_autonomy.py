"""Autonomous loop: opportunity store, engine, strategist, approval
firewall, and the full DISCOVER->...->REINVEST cycle. Fake data, EUR 0.
"""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import autonomy, strategist
from revenue_os.approvals import ApprovalStore, load_approvals, request_id
from revenue_os.opportunity_engine import generate
from revenue_os.opportunity_store import (
    Opportunity,
    OpportunityStore,
    load_opportunities,
    score_opportunity,
)


def _seed(d: Path):
    (d / "candidates.json").write_text(json.dumps([
        {"name": "a", "description": "how founders get first paying customers "
         "SaaS onboarding changelog release notes API docs cold email"}]))


class OpportunityStoreTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_score_rewards_ev_punishes_effort_and_risk(self):
        a = Opportunity(title="A", est_revenue_eur=300, probability=0.3,
                        effort_points=2, difficulty=2, legal_platform_risk="low")
        b = Opportunity(title="B", est_revenue_eur=300, probability=0.3,
                        effort_points=5, difficulty=5, legal_platform_risk="high")
        self.assertGreater(score_opportunity(a), score_opportunity(b))

    def test_lifecycle_and_persistence(self):
        s = OpportunityStore(self.d / "opportunities.json")
        r = s.upsert(Opportunity(title="X", category="micro_saas",
                                 est_revenue_eur=200, probability=0.2))
        oid = r["id"]
        s.set_status(oid, "building", note="picked")
        s.add_experiment(oid, "build", "made a page", result="ok")
        s.record_result(oid, revenue_eur=29.9, cycles=1)
        s.save()
        s2 = load_opportunities(self.d)
        rec = s2.get(oid)
        self.assertEqual(rec["status"], "building")
        self.assertEqual(rec["results"]["revenue_eur"], 29.9)
        self.assertEqual(len(rec["experiments"]), 2)   # status + build

    def test_prune_abandoned_is_a_batch_sweep(self):
        s = OpportunityStore(self.d / "opportunities.json")
        for i in range(149):
            o = Opportunity(title=f"x{i}", category="saas")
            s.upsert(o)
            s._by_id[o.id]["status"] = "abandoned"
            s._by_id[o.id]["updated_at"] = f"2026-01-01T00:{i:02d}:00"
        self.assertEqual(s.prune_abandoned(trigger=150, keep=10), 0)  # below trigger
        o = Opportunity(title="x149", category="saas")
        s.upsert(o); s._by_id[o.id]["status"] = "abandoned"
        s._by_id[o.id]["updated_at"] = "2026-01-01T01:00:00"
        self.assertEqual(len([r for r in s.all() if r["status"] == "abandoned"]), 150)
        dropped = s.prune_abandoned(trigger=150, keep=10)
        self.assertEqual(dropped, 140)
        left = [r for r in s.all() if r["status"] == "abandoned"]
        self.assertEqual(len(left), 10)
        self.assertIn(o.id, {r["id"] for r in left})   # newest kept

    def test_prune_never_touches_active_states(self):
        s = OpportunityStore(self.d / "opportunities.json")
        keep = s.upsert(Opportunity(title="live", category="saas"))
        s.set_status(keep["id"], "testing")
        for i in range(200):
            o = Opportunity(title=f"a{i}", category="other")
            s.upsert(o); s._by_id[o.id]["status"] = "abandoned"
        s.prune_abandoned(trigger=150, keep=10)
        self.assertEqual(s.get(keep["id"])["status"], "testing")

    def test_upsert_preserves_lifecycle(self):
        s = OpportunityStore(self.d / "opportunities.json")
        opp = Opportunity(title="X", category="saas")
        s.upsert(opp)
        s.set_status(opp.id, "testing")
        s.upsert(Opportunity(title="X", category="saas", est_revenue_eur=999))
        self.assertEqual(s.get(opp.id)["status"], "testing")   # not reset to discovered


class OpportunityEngineTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_generates_varied_categories(self):
        fresh = generate(self.d, n=10)
        self.assertGreaterEqual(len(fresh), 5)
        cats = {f["category"] for f in fresh}
        self.assertGreaterEqual(len(cats), 3)   # not a single business model

    def test_dedups_across_runs(self):
        first = {f["id"] for f in generate(self.d, n=8)}
        second = {f["id"] for f in generate(self.d, n=8)}
        self.assertEqual(first & second, set())   # only NEW ones each run

    def test_llm_discovery_is_money_gated(self):
        from revenue_os.action_class import ActionBlocked
        with self.assertRaises(ActionBlocked):
            generate(self.d, llm=True)


class StrategistTests(unittest.TestCase):
    def _board(self, **kw):
        b = {s: [] for s in ("discovered", "evaluating", "building", "testing",
                             "active", "successful", "abandoned")}
        b.update(kw)
        return b

    def test_select_fills_capacity_across_categories(self):
        ev = [{"id": f"o{i}", "title": f"o{i}", "score": 100 - i,
               "category": f"cat{i}"} for i in range(6)]
        picks = strategist.select_experiments(self._board(evaluating=ev), capacity=3)
        self.assertEqual(len(picks), 3)
        self.assertEqual(len({p["category"] for p in picks}), 3)  # never same cat

    def test_select_prefers_a_different_category_over_a_higher_score(self):
        ev = [{"id": f"a{i}", "title": "a", "score": 100 - i, "category": "same"}
              for i in range(5)]
        ev += [{"id": "b", "title": "b", "score": 1, "category": "other"}]
        picks = strategist.select_experiments(self._board(evaluating=ev), capacity=3)
        cats = [p["category"] for p in picks]
        self.assertIn("other", cats)            # low score, different category, gets a slot
        self.assertGreaterEqual(len(set(cats)), 2)   # not all one model
        # with 6 categories available it would be all distinct:
        ev6 = [{"id": f"c{i}", "score": 100 - i, "category": f"c{i}"} for i in range(6)]
        p6 = strategist.select_experiments(self._board(evaluating=ev6), capacity=3)
        self.assertEqual(len({x["category"] for x in p6}), 3)

    def test_select_reserves_an_exploration_slot(self):
        board = self._board(
            testing=[{"id": "t", "category": "b2b_service", "results": {"cycles": 2}}],
            abandoned=[{"id": "x", "category": "b2b_service", "results": {"cycles": 4}}],
            evaluating=[{"id": "hi", "score": 99, "category": "b2b_service"},
                        {"id": "explore", "score": 10, "category": "template_pack"}])
        picks = strategist.select_experiments(board, capacity=2)
        self.assertIn("explore", [p["id"] for p in picks])   # untested category wins a slot

    def test_select_skips_money_blocked(self):
        ev = [{"id": "o0", "title": "o0", "score": 100, "category": "c0"},
              {"id": "o1", "title": "o1", "score": 90, "category": "c1"}]
        picks = strategist.select_experiments(self._board(evaluating=ev), capacity=3,
                                              money_blocked={"o0"})
        self.assertEqual([p["id"] for p in picks], ["o1"])

    def test_review_promotes_earner_abandons_stale_continues_new(self):
        b = self._board(testing=[
            {"id": "earn", "results": {"revenue_eur": 29.9, "cycles": 2}},
            {"id": "stale", "results": {"cycles": 5}, "experiments": []},
            {"id": "new", "results": {"cycles": 0}},
        ])
        v = strategist.review_experiments(b)
        self.assertEqual(v["earn"][0], "promote")
        self.assertEqual(v["stale"][0], "abandon")
        self.assertEqual(v["new"][0], "continue")

    def test_adjacent_opportunities(self):
        adj = strategist.adjacent_opportunities(
            {"id": "x", "title": "T", "category": "template_pack",
             "target_customer": "devs"})
        self.assertEqual(len(adj), 2)
        self.assertTrue(all(a["parent_id"] == "x" for a in adj))


class ApprovalFirewallTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_money_request_has_all_fields(self):
        s = ApprovalStore(self.d / "approvals.json")
        r = s.request_money(key="k1", what="Buy X", why="need X", amount=25.0,
                            opportunity="opp_1", expected_benefit="growth",
                            downside="capped", recommended_max_budget=25.0,
                            expected_roi="positive", necessity="optional")
        for f in ("what", "why", "amount", "expected_benefit", "downside",
                  "recommended_max_budget", "expected_roi", "necessity",
                  "what_happens_after"):
            self.assertIn(f, r)
        self.assertEqual(r["status"], "pending")

    def test_decide_and_persist(self):
        s = ApprovalStore(self.d / "approvals.json")
        r = s.request_identity(key="kyc1", what="PayPal KYC", why="payout",
                               boundary="paypal")
        s.decide(r["id"], "approved", by="owner")
        s.save()
        s2 = load_approvals(self.d)
        self.assertEqual(s2.get(r["id"])["status"], "approved")
        self.assertIn(r["id"], s2.granted_ids("identity"))

    def test_refiling_keeps_a_human_decision(self):
        s = ApprovalStore(self.d / "approvals.json")
        r = s.request_money(key="k", what="w", why="y", amount=1.0)
        s.decide(r["id"], "denied", by="o")
        again = s.request_money(key="k", what="w2", why="y2", amount=2.0)
        self.assertEqual(again["status"], "denied")   # not reopened

    def test_withdraw_on_abandon(self):
        s = ApprovalStore(self.d / "approvals.json")
        s.request_money(key="checkout:opp_1", what="checkout", why="fees apply",
                        fees=True, creates_payment_obligation=True,
                        opportunity="opp_1")
        n = s.withdraw_for_opportunity("opp_1")
        self.assertEqual(n, 1)
        self.assertEqual(s.pending("money"), [])

    def test_nominal_zero_amount_files_nothing(self):
        s = ApprovalStore(self.d / "approvals.json")
        r = s.request_money(key="k", what="free thing", why="y", amount=0.0)
        self.assertEqual(r["status"], "not_required")
        self.assertEqual(s.pending("money"), [])

    def test_request_id_is_stable(self):
        self.assertEqual(request_id("money", "k"), request_id("money", "k"))


class AutonomyCycleTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        _seed(self.d)

    def tearDown(self):
        self._d.cleanup()

    def test_one_cycle_runs_every_phase(self):
        rep = autonomy.run_cycle(self.d)
        self.assertEqual([p["phase"] for p in rep["phases"]], list(autonomy.PHASES))
        self.assertIsNone(rep.get("stopped"))

    def test_cycle_discovers_builds_and_stages_pages_only(self):
        rep = autonomy.run_cycle(self.d)
        self.assertGreater(rep["phases"][0]["new_opportunities"], 0)
        self.assertTrue(rep["published"])
        for oid in rep["published"]:
            page = self.d / "published" / oid / "index.html"
            self.assertTrue(page.is_file())
            meta = json.loads((self.d / "published" / oid / "meta.json").read_text())
            self.assertEqual(meta["classification"], "SAFE_AUTONOMOUS")
        # every staged opp filed a MONEY approval for the checkout - never acted
        self.assertGreater(rep["pending_approvals"]["money"]["pending"], 0)
        self.assertEqual(rep["pending_approvals"]["money"]["approved"], 0)

    def test_cycle_stops_when_paused(self):
        from revenue_os.agent_control import AgentControl
        c = AgentControl.load(self.d / "agent_control.json")
        c.set_paused(True, reason="test")
        c.save()
        rep = autonomy.run_cycle(self.d)
        self.assertEqual(rep["stopped"], "fleet is paused")

    def test_loop_abandons_stale_and_keeps_going(self):
        for _ in range(4):
            rep = autonomy.run_cycle(self.d)
        counts = rep["opportunity_counts"]
        self.assertGreater(counts["abandoned"], 0)     # abandoned bad ones
        self.assertGreater(counts["evaluating"] + counts["testing"], 0)  # still working
        self.assertIsNone(rep.get("stopped"))

    def test_revenue_promotes_and_spawns_adjacent(self):
        autonomy.run_cycle(self.d)
        s = load_opportunities(self.d)
        testing = [r for r in s.all() if r["status"] == "testing"]
        self.assertTrue(testing)
        s.record_result(testing[0]["id"], revenue_eur=29.9)
        s.save()
        rep = autonomy.run_cycle(self.d)
        v = rep["decisions"][-1]["verdicts"]
        self.assertIn(testing[0]["id"], v.get("promote", []))
        after = load_opportunities(self.d)
        self.assertEqual(after.get(testing[0]["id"])["status"], "successful")
        self.assertTrue(after.by_status("discovered"))   # adjacent ideas queued
        # a "fund scaling" money request now exists
        appr = load_approvals(self.d)
        self.assertTrue(any("caling" in r["what"] for r in appr.pending("money")))

    def test_state_has_objective_next_action_reasoning(self):
        autonomy.run_cycle(self.d)
        st = autonomy.load_state(self.d)
        self.assertTrue(st["objective"])
        self.assertTrue(st["next_action"])
        self.assertTrue(st["reasoning"])
        self.assertGreaterEqual(st["cycles"], 1)

    def test_no_money_no_llm_no_send_artifacts(self):
        for _ in range(3):
            autonomy.run_cycle(self.d)
        self.assertFalse((self.d / "revenue.json").exists())
        self.assertFalse((self.d / "deliveries.json").exists())
        self.assertFalse((self.d / "llm_spend.json").exists())


if __name__ == "__main__":
    unittest.main()
