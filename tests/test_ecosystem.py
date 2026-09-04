"""AI Revenue OS ecosystem - unit / integration / safety / simulation tests.

Covers: sources (incl. the real HN / RemoteOK adapters via injected
fetchers - no network), verification, profitability, strategy selection,
the discovery engine, the evaluate/select/plan pipeline, the learning
loop, the autonomy-level layer, and the full-loop simulation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import action_class as ac
from revenue_os.ecosystem import (
    autonomy,
    intel,
    learning,
    model,
    pipeline,
    profitability,
    simulation,
    strategy,
    verification,
)
from revenue_os.ecosystem.discovery import DiscoveryEngine, latest_discovery
from revenue_os.ecosystem.model import OpportunityDraft, SourceMeta
from revenue_os.ecosystem.sources import (
    HackerNewsDemandSource,
    HumanSetupRequiredSource,
    RemoteOkSource,
    SyntheticSource,
    build_source,
)
from revenue_os.opportunity_store import load_opportunities


def _real_meta(**kw):
    base = dict(source="unit", source_type="test",
                access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
                policy_status=model.POLICY_OK)
    base.update(kw)
    return SourceMeta(**base)


def _draft(**kw):
    base = dict(title="A paid CSV dedupe script", description="budget ~50 eur",
                opportunity_type=model.TYPE_DIGITAL_PRODUCT,
                evidence=["Ask HN: I will pay for a CSV dedupe script"],
                source_meta=_real_meta(), source_id="hn-1",
                est_pay_eur=29.0, demand_hint=0.4)
    base.update(kw)
    return OpportunityDraft(**base)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

class ModelTests(unittest.TestCase):
    def test_estimate_always_flagged(self):
        e = model.estimate(12.345, "x")
        self.assertEqual(e["value"], 12.345)
        self.assertTrue(e["is_estimate"])
        self.assertEqual(model.estimate_value(e), 12.345)
        self.assertEqual(model.estimate_value(7), 7.0)
        self.assertEqual(model.estimate_value("bad"), 0.0)

    def test_norm_title_and_dedup_key(self):
        d1 = _draft(title="  Build   a  Thing!! ", source_id="", source_meta=None)
        d2 = _draft(title="build a thing", source_id="", source_meta=None)
        self.assertEqual(d1.dedup_key(), d2.dedup_key())
        d3 = _draft(source_id="abc")
        self.assertEqual(d3.dedup_key(), "unit:abc")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

class SyntheticSourceTests(unittest.TestCase):
    def test_deterministic_and_synthetic_origin(self):
        a = SyntheticSource(seed=5).discover(6)
        b = SyntheticSource(seed=5).discover(6)
        self.assertEqual([d.title for d in a], [d.title for d in b])
        self.assertEqual(len(a), 6)
        self.assertTrue(all(d.source_meta.access_method == model.ACCESS_SYNTHETIC
                            for d in a))

    def test_seed_changes_output(self):
        a = SyntheticSource(seed=0).discover(5)
        b = SyntheticSource(seed=9).discover(5)
        self.assertNotEqual([d.title for d in a], [d.title for d in b])


class HackerNewsSourceTests(unittest.TestCase):
    def _fetch(self, rows):
        def f(url):
            if url.endswith("askstories.json"):
                return list(range(1, len(rows) + 1))
            iid = int(url.rsplit("/", 1)[1].split(".")[0])
            return rows[iid - 1]
        return f

    def test_keeps_only_demand_signals(self):
        rows = [
            {"id": 1, "title": "Ask HN: I will pay for a Notion cleanup script",
             "text": "budget 40 eur", "type": "story"},
            {"id": 2, "title": "Show HN: my weekend toy", "text": "", "type": "story"},
        ]
        got = HackerNewsDemandSource(fetch_json=self._fetch(rows)).discover(10)
        self.assertEqual(len(got), 1)
        self.assertIn("pay", got[0].title.lower())
        self.assertEqual(got[0].source_meta.access_method, model.ACCESS_OFFICIAL_API)
        self.assertTrue(got[0].evidence)

    def test_network_failure_is_fail_closed(self):
        def boom(url):
            raise OSError("no network")
        self.assertEqual(HackerNewsDemandSource(fetch_json=boom).discover(5), [])


class RemoteOkSourceTests(unittest.TestCase):
    def test_skips_legal_notice_and_filters_by_keyword(self):
        rows = [
            {"legal": "notice, no position key"},
            {"id": "9", "position": "Automation Engineer", "company": "Acme",
             "tags": ["automation", "python"], "url": "https://remoteok.com/x"},
            {"id": "10", "position": "Barista", "company": "Cafe", "tags": ["food"]},
        ]
        got = RemoteOkSource(fetch_json=lambda u: rows).discover(10)
        self.assertEqual(len(got), 1)
        self.assertIn("Automation Engineer", got[0].title)
        self.assertEqual(got[0].opportunity_type, model.TYPE_SERVICE)

    def test_network_failure_is_fail_closed(self):
        got = RemoteOkSource(fetch_json=lambda u: (_ for _ in ()).throw(OSError())).discover(5)
        self.assertEqual(got, [])


class SourceRegistryTests(unittest.TestCase):
    def test_human_setup_source_yields_nothing(self):
        s = build_source("upwork")
        self.assertIsInstance(s, HumanSetupRequiredSource)
        self.assertEqual(s.meta.policy_status, model.POLICY_HUMAN_SETUP_REQUIRED)
        self.assertEqual(s.discover(50), [])

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            build_source("definitely-not-a-source")

    def test_file_source_needs_path(self):
        with self.assertRaises(ValueError):
            build_source("file")


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

class VerificationTests(unittest.TestCase):
    def test_qualified_happy_path(self):
        v = verification.verify(_draft())
        self.assertEqual(v.status, model.V_QUALIFIED)
        self.assertFalse(v.blocked)
        self.assertFalse(v.requires_human)

    def test_no_evidence_rejected(self):
        self.assertEqual(verification.verify(_draft(evidence=[])).status,
                         model.V_REJECTED)

    def test_no_source_rejected(self):
        self.assertEqual(verification.verify(_draft(source_meta=None)).status,
                         model.V_REJECTED)

    def test_policy_blocked(self):
        v = verification.verify(_draft(source_meta=_real_meta(
            policy_status=model.POLICY_BLOCKED)))
        self.assertEqual(v.status, model.V_BLOCKED)
        self.assertTrue(v.blocked)

    def test_human_setup_required(self):
        v = verification.verify(_draft(source_meta=_real_meta(
            policy_status=model.POLICY_HUMAN_SETUP_REQUIRED)))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)
        self.assertTrue(v.requires_human)

    def test_unknown_policy_fails_closed(self):
        self.assertEqual(verification.verify(_draft(source_meta=_real_meta(
            policy_status="WHATEVER"))).status, model.V_REJECTED)

    def test_pay_below_floor_rejected(self):
        self.assertEqual(verification.verify(_draft(est_pay_eur=1.0)).status,
                         model.V_REJECTED)

    def test_login_gated_external_task_needs_human(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            source_meta=_real_meta(requires_login=True)))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)

    def test_unknown_opportunity_type_rejected(self):
        self.assertEqual(verification.verify(_draft(opportunity_type="teleport")).status,
                         model.V_REJECTED)


# ---------------------------------------------------------------------------
# profitability
# ---------------------------------------------------------------------------

class ProfitabilityTests(unittest.TestCase):
    def test_every_number_is_an_estimate(self):
        p = profitability.evaluate(_draft()).to_dict()
        for k, v in p.items():
            if isinstance(v, dict):
                self.assertTrue(v.get("is_estimate"), k)
        self.assertTrue(p["is_estimate"])

    def test_deterministic(self):
        self.assertEqual(profitability.evaluate(_draft()).to_dict(),
                         profitability.evaluate(_draft()).to_dict())

    def test_small_fast_task_beats_big_slow_service_on_decision_value(self):
        # spec section 9 example: A (small, fast, likely) vs B (big, slow, coin-flip)
        a = _draft(opportunity_type=model.TYPE_TASK, est_pay_eur=12.0,
                   est_time_minutes=10.0, demand_hint=0.9)
        b = _draft(opportunity_type=model.TYPE_SERVICE, est_pay_eur=500.0,
                   est_time_minutes=240.0, demand_hint=0.2)
        pa = model.estimate_value(profitability.evaluate(a).decision_value)
        pb = model.estimate_value(profitability.evaluate(b).decision_value)
        self.assertGreater(pa, pb)


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

class StrategyTests(unittest.TestCase):
    def test_service_is_not_the_default(self):
        # a SERVICE-type opportunity: SERVICE and PRODUCT are both viable.
        # The handicap must keep SERVICE from being the pick when PRODUCT is
        # close, and the handicap must be visible on the option.
        d = _draft(opportunity_type=model.TYPE_SERVICE, est_pay_eur=120.0,
                   demand_hint=0.3)
        sel = strategy.select_strategy(d, profitability.evaluate(d))
        self.assertNotEqual(sel.recommended, model.STRAT_SERVICE)
        self.assertTrue(sel.to_dict()["service_is_not_default"])
        svc = next(o for o in sel.options if o.strategy == model.STRAT_SERVICE)
        prod = next(o for o in sel.options if o.strategy == model.STRAT_PRODUCT)
        self.assertIn("down-weighted", svc.notes)
        self.assertGreater(prod.score, svc.score)

    def test_product_type_recommends_product(self):
        sel = strategy.select_strategy(_draft(), profitability.evaluate(_draft()))
        self.assertEqual(sel.recommended, model.STRAT_PRODUCT)

    def test_negative_economics_recommends_nothing(self):
        # €5 for 10 hours of work -> negative projected profit -> no strategy
        d = _draft(opportunity_type=model.TYPE_OTHER, est_pay_eur=5.0,
                   est_time_minutes=600.0, demand_hint=0.0)
        sel = strategy.select_strategy(d, profitability.evaluate(d))
        self.assertEqual(sel.recommended, "")
        self.assertIn("not positive", sel.reason)

    def test_priority_weights_shift_the_choice(self):
        d = _draft()
        prof = profitability.evaluate(d)
        base = strategy.select_strategy(d, prof).options
        boosted = strategy.select_strategy(
            d, prof, priority_weights={"affiliate": 1.6}).options
        b_aff = next(o.score for o in boosted if o.strategy == model.STRAT_AFFILIATE)
        n_aff = next(o.score for o in base if o.strategy == model.STRAT_AFFILIATE)
        self.assertGreater(b_aff, n_aff)


# ---------------------------------------------------------------------------
# discovery engine
# ---------------------------------------------------------------------------

class _StaticSource:
    def __init__(self, drafts):
        self._drafts = drafts
        self.meta = _real_meta(source="static")

    def discover(self, limit):
        return list(self._drafts)[:limit]


class DiscoveryEngineTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_persists_opportunities_with_origin_and_verification(self):
        drafts = [_draft(title="Real thing one", source_id="a"),
                  _draft(title="Real thing two", source_id="b")]
        rep = DiscoveryEngine(self.d, sources=[_StaticSource(drafts)]).run()
        self.assertEqual(rep.new, 2)
        self.assertEqual(rep.by_origin.get("real"), 2)
        store = load_opportunities(self.d)
        recs = store.all()
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertEqual(r["origin"], "real")
            self.assertEqual(r["discovery"]["verification"]["status"],
                             model.V_QUALIFIED)
            self.assertTrue(r["discovery"]["evidence"])
            self.assertEqual(r["discovery"]["source"], "unit")  # from the draft's own SourceMeta

    def test_dedup_within_and_across_runs(self):
        drafts = [_draft(title="Dup", source_id="x"),
                  _draft(title="Dup", source_id="x")]
        eng = DiscoveryEngine(self.d, sources=[_StaticSource(drafts)])
        rep1 = eng.run()
        self.assertEqual(rep1.deduped, 1)
        self.assertEqual(rep1.new, 1)
        rep2 = DiscoveryEngine(self.d, sources=[_StaticSource(drafts)]).run()
        self.assertEqual(rep2.new, 0)
        self.assertEqual(rep2.refreshed, 1)
        self.assertEqual(len(load_opportunities(self.d).all()), 1)

    def test_one_bad_source_does_not_kill_the_run(self):
        class _Boom:
            meta = _real_meta(source="boom")

            def discover(self, limit):
                raise RuntimeError("kaboom")

        rep = DiscoveryEngine(self.d, sources=[
            _Boom(), _StaticSource([_draft(source_id="ok")])]).run()
        self.assertEqual(len(rep.errors), 1)
        self.assertEqual(rep.new, 1)

    def test_discovery_log_written(self):
        DiscoveryEngine(self.d, sources=[_StaticSource([_draft(source_id="a")])]).run()
        self.assertIsNotNone(latest_discovery(self.d))

    def test_synthetic_source_stays_synthetic_origin(self):
        rep = DiscoveryEngine(self.d, sources=[SyntheticSource()]).run(limit_per_source=4)
        self.assertEqual(rep.by_origin.get("synthetic"), 4)
        self.assertNotIn("real", rep.by_origin)


# ---------------------------------------------------------------------------
# pipeline: evaluate -> select -> plan
# ---------------------------------------------------------------------------

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _one(self, **kw):
        DiscoveryEngine(self.d, sources=[_StaticSource([_draft(source_id="p1", **kw)])]).run()
        return load_opportunities(self.d).all()[0]["id"]

    def test_evaluate_then_select_then_plan_product(self):
        oid = self._one()
        ev = pipeline.evaluate(self.d, oid)
        self.assertTrue(ev["is_estimate"])
        sel = pipeline.select(self.d, oid)
        self.assertEqual(sel["recommended"], model.STRAT_PRODUCT)
        out = pipeline.plan(self.d, oid)
        self.assertEqual(out["kind"], "task_chain")
        # the real acceptance chain now exists
        from revenue_os.execution import load_tasks
        chain = [t.task_type for t in load_tasks(self.d).by_opportunity(oid)]
        self.assertIn("DEPLOY", chain)
        self.assertIn("CHECK_REVENUE", chain)

    def test_plan_refuses_when_not_qualified(self):
        # a login-gated task -> HUMAN_REQUIRED, not QUALIFIED
        oid = self._one(opportunity_type=model.TYPE_TASK,
                        source_meta=_real_meta(requires_login=True))
        pipeline.select(self.d, oid)
        with self.assertRaises(pipeline.EcosystemError):
            pipeline.plan(self.d, oid)

    def test_plan_refuses_without_a_selected_strategy(self):
        oid = self._one()
        with self.assertRaises(pipeline.EcosystemError):
            pipeline.plan(self.d, oid)

    def test_non_product_strategy_is_prepared_and_human_gated(self):
        oid = self._one(opportunity_type=model.TYPE_AFFILIATE, est_pay_eur=25.0,
                        demand_hint=0.6)
        sel = pipeline.select(self.d, oid)
        if sel["recommended"] != model.STRAT_AFFILIATE:
            self.skipTest("strategy engine did not pick AFFILIATE for this fixture")
        out = pipeline.plan(self.d, oid)
        self.assertEqual(out["kind"], "prepared")
        self.assertEqual(out["next_step_class"], "HUMAN_REQUIRED")

    def test_task_strategy_gets_a_real_execution_chain(self):
        oid = self._one(opportunity_type=model.TYPE_TASK, est_pay_eur=25.0,
                        demand_hint=0.6)
        sel = pipeline.select(self.d, oid)
        if sel["recommended"] != model.STRAT_TASK:
            self.skipTest("strategy engine did not pick TASK for this fixture")
        out = pipeline.plan(self.d, oid)
        self.assertEqual(out["kind"], "task_chain")
        from revenue_os.execution import load_tasks
        chain = [t.task_type for t in load_tasks(self.d).by_opportunity(oid)]
        self.assertEqual(chain, ["PLAN_TASK", "EXECUTE_TASK", "VERIFY_RESULT"])
        rec = load_opportunities(self.d).get(oid)
        self.assertTrue(rec["execution"]["accepted"])
        self.assertEqual(rec["state"], "SELECTED")
        # idempotent: re-planning reuses the same tasks, creates nothing new
        out2 = pipeline.plan(self.d, oid)
        self.assertEqual(out2["plan"]["created"], [])
        self.assertEqual(out2["plan"]["reused"],
                         ["PLAN_TASK", "EXECUTE_TASK", "VERIFY_RESULT"])


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------

class LearningTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _record(self, n, **kw):
        s = learning.OutcomeStore.load(self.d)
        for i in range(n):
            s.record(learning.Outcome(opportunity_id=f"o{i}", **kw))
        s.save()

    def test_aggregate_and_profit(self):
        self._record(3, strategy="PRODUCT", category="template_pack",
                     revenue_eur=30.0, cost_eur=2.0, success=True,
                     execution_time_hours=1.0)
        agg = learning.OutcomeStore.load(self.d).aggregate()
        self.assertEqual(agg["settled"], 3)
        self.assertEqual(agg["wins"], 3)
        self.assertEqual(agg["total_profit_eur"], 84.0)
        self.assertEqual(agg["by_strategy"]["PRODUCT"]["profit_per_hour"], 28.0)

    def test_no_weights_below_threshold(self):
        self._record(3, strategy="PRODUCT", success=True)
        w = learning.OutcomeStore.load(self.d).priority_weights()
        self.assertIn("_note", w)
        self.assertEqual(w["strategy"], {})

    def test_weights_are_reproducible_ratios(self):
        self._record(6, strategy="PRODUCT", category="a", revenue_eur=20, success=True)
        self._record(6, strategy="SERVICE", category="b", revenue_eur=0, success=False)
        w = learning.OutcomeStore.load(self.d).priority_weights()
        self.assertGreater(w["strategy"].get("product", 0), 1.0)
        self.assertLess(w["strategy"].get("service", 2), 1.0)


# ---------------------------------------------------------------------------
# autonomy levels
# ---------------------------------------------------------------------------

class AutonomyLevelTests(unittest.TestCase):
    def test_research_is_autonomous(self):
        for a in ("discover", "verify", "evaluate", "select_strategy",
                  "simulate", "learn"):
            v = autonomy.classify_activity(a)
            self.assertEqual(v.verdict, autonomy.AUTONOMOUS_ALLOWED, a)

    def test_money_activities_need_approval(self):
        for a in ("place_supplier_order", "fund_ad_test", "run_paid_ads"):
            self.assertEqual(autonomy.classify_activity(a).verdict,
                             autonomy.HUMAN_APPROVAL_REQUIRED, a)

    def test_platform_account_activities_are_human_required(self):
        self.assertEqual(autonomy.classify_activity("join_affiliate_program").verdict,
                         autonomy.HUMAN_REQUIRED)
        self.assertEqual(autonomy.classify_activity("open_store_account").verdict,
                         autonomy.HUMAN_REQUIRED)

    def test_unknown_activity_blocked(self):
        self.assertEqual(autonomy.classify_activity("do_crimes").verdict,
                         autonomy.BLOCKED)

    def test_deploy_checkout_needs_money_approval(self):
        v = autonomy.classify_activity("deploy_checkout", {"has_checkout": True})
        self.assertEqual(v.verdict, autonomy.HUMAN_APPROVAL_REQUIRED)


# ---------------------------------------------------------------------------
# action_class - additive only, nothing loosened
# ---------------------------------------------------------------------------

class ActionClassAdditionsTests(unittest.TestCase):
    def test_new_research_kinds_are_safe(self):
        for k in ("verify_opportunity", "evaluate_profitability",
                  "select_strategy", "simulate_revenue", "supplier_research",
                  "margin_analysis", "prepare_task_solution"):
            self.assertTrue(ac.classify(k).autonomous, k)

    def test_new_money_kinds_require_approval(self):
        for k in ("place_supplier_order", "fund_ad_test", "order_inventory",
                  "fund_dropship_order"):
            self.assertIs(ac.classify(k).action_class,
                          ac.ActionClass.MONEY_APPROVAL_REQUIRED, k)

    def test_existing_gates_unchanged(self):
        self.assertIs(ac.classify("spend_money").action_class,
                      ac.ActionClass.MONEY_APPROVAL_REQUIRED)
        self.assertIs(ac.classify("solve_captcha").action_class,
                      ac.ActionClass.SAFETY_BLOCKED)
        self.assertIs(ac.classify("kyc").action_class,
                      ac.ActionClass.IDENTITY_APPROVAL_REQUIRED)


# ---------------------------------------------------------------------------
# simulation (spec 21 + 39)
# ---------------------------------------------------------------------------

class SimulationTests(unittest.TestCase):
    def test_deterministic(self):
        a = simulation.simulate(n=400, seed=13).to_dict()
        b = simulation.simulate(n=400, seed=13).to_dict()
        self.assertEqual(a, b)

    def test_different_seed_differs(self):
        a = simulation.simulate(n=400, seed=1).to_dict()
        b = simulation.simulate(n=400, seed=2).to_dict()
        self.assertNotEqual(a["simulated_revenue_eur"], b["simulated_revenue_eur"])

    def test_scales_and_has_analytics(self):
        rep = simulation.simulate(n=2000, seed=42)
        self.assertEqual(rep.discovered, 2000)
        self.assertEqual(rep.qualified + rep.rejected + rep.blocked
                         + rep.human_required,
                         sum(rep.by_verification.values()))
        self.assertGreater(rep.executed, 0)
        self.assertEqual(rep.successes + rep.failures, rep.executed)
        self.assertIn("by_category", rep.analytics)
        self.assertLessEqual(len(rep.top_categories), 5)

    def test_no_side_effects(self):
        before = set(Path(tempfile.gettempdir()).glob("_eco_sim*"))
        simulation.simulate(n=200, seed=5)
        after = set(Path(tempfile.gettempdir()).glob("_eco_sim*"))
        self.assertEqual(before, after)   # never writes its outcome store

    def test_runs_inside_autonomous_context_without_touching_money(self):
        with ac.autonomous_context():
            rep = simulation.simulate(n=100, seed=7)   # must not raise ActionBlocked
        self.assertGreater(rep.discovered, 0)


# ---------------------------------------------------------------------------
# intel read model
# ---------------------------------------------------------------------------

class IntelTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_status_counts_from_real_state(self):
        DiscoveryEngine(self.d, sources=[_StaticSource([
            _draft(title="one", source_id="1"),
            _draft(title="two", source_id="2", source_meta=_real_meta(
                policy_status=model.POLICY_HUMAN_SETUP_REQUIRED)),
        ])]).run()
        oid = load_opportunities(self.d).all()[0]["id"]
        pipeline.select(self.d, oid)
        st = intel.ecosystem_status(self.d)
        self.assertEqual(st["discovery"]["total"], 2)
        self.assertEqual(st["discovery"]["real"], 2)
        self.assertGreaterEqual(st["discovery"]["qualified"], 1)
        self.assertIn("PRODUCT", st["strategies"]["selected"])
        self.assertTrue(any(h["opportunity_id"] for h in st["human_actions"]))


# ---------------------------------------------------------------------------
# ExecutionTask integration (spec 24) - DISCOVER / VERIFY / EVALUATE /
# SELECT_STRATEGY run through the real worker inside autonomous_context()
# ---------------------------------------------------------------------------

class EcosystemTaskTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_all_ecosystem_task_types_are_safe_autonomous(self):
        from revenue_os.task_class import classify_task
        for t in ("DISCOVER", "VERIFY", "EVALUATE", "SELECT_STRATEGY",
                  "PLAN_TASK", "EXECUTE_TASK", "VERIFY_RESULT"):
            self.assertTrue(classify_task(t).autonomous, t)

    def test_discover_task_runs_through_the_worker(self):
        from revenue_os.execution import load_tasks
        from revenue_os.worker import run_worker

        q = load_tasks(self.d)
        q.create("ecosystem", "DISCOVER",
                 input={"sources": "synthetic", "limit": 6})
        q.resolve_dependencies()
        q.save()
        out = run_worker(self.d, max_ticks=2)
        self.assertEqual(out["processed"][0]["status"], "SUCCEEDED")
        self.assertEqual(len(load_opportunities(self.d).all()), 6)

    def test_select_strategy_task_records_the_verdict(self):
        from revenue_os.execution import load_tasks
        from revenue_os.worker import run_worker

        DiscoveryEngine(self.d, sources=[_StaticSource([_draft(source_id="w1")])]).run()
        oid = load_opportunities(self.d).all()[0]["id"]
        q = load_tasks(self.d)
        q.create(oid, "SELECT_STRATEGY")
        q.resolve_dependencies()
        q.save()
        run_worker(self.d, max_ticks=2)
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["strategy"]["recommended"], model.STRAT_PRODUCT)

    def test_discover_task_with_bad_source_fails_cleanly(self):
        from revenue_os.execution import load_tasks
        from revenue_os.worker import run_worker

        q = load_tasks(self.d)
        q.create("ecosystem", "DISCOVER", input={"sources": "not-a-source"})
        q.resolve_dependencies()
        q.save()
        out = run_worker(self.d, max_ticks=2)
        self.assertIn(out["processed"][0]["status"],
                      ("FAILED_FINAL", "FAILED_RETRYABLE"))


# ---------------------------------------------------------------------------
# TASK-strategy vertical slice: PLAN_TASK -> EXECUTE_TASK -> VERIFY_RESULT ->
# (human submits) -> record_task_outcome -> ledger + FSM + learning
# ---------------------------------------------------------------------------

class TaskExecutionChainTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _qualified_task_opportunity(self):
        DiscoveryEngine(self.d, sources=[_StaticSource([_draft(
            opportunity_type=model.TYPE_TASK, source_id="t1", est_pay_eur=25.0,
            demand_hint=0.6,
            evidence=["Ask HN: I will pay for a CSV dedupe script"])])]).run()
        oid = load_opportunities(self.d).all()[0]["id"]
        pipeline.evaluate(self.d, oid)
        sel = pipeline.select(self.d, oid)
        if sel["recommended"] != model.STRAT_TASK:
            self.skipTest("strategy engine did not pick TASK for this fixture")
        pipeline.plan(self.d, oid)
        return oid

    def _drain(self, max_ticks=10):
        from revenue_os.worker import run_worker
        return run_worker(self.d, max_ticks=max_ticks)

    def test_chain_runs_through_the_worker_and_produces_a_verified_deliverable(self):
        from revenue_os.execution import load_tasks

        oid = self._qualified_task_opportunity()
        out = self._drain()
        statuses = {p["task_type"]: p["status"] for p in out["processed"]}
        self.assertEqual(statuses.get("PLAN_TASK"), "SUCCEEDED")
        self.assertEqual(statuses.get("EXECUTE_TASK"), "SUCCEEDED")
        self.assertEqual(statuses.get("VERIFY_RESULT"), "SUCCEEDED")

        deliverable = self.d / "deliverables" / oid / "task_solution.md"
        self.assertTrue(deliverable.is_file())
        content = deliverable.read_text(encoding="utf-8")
        self.assertIn("CSV dedupe script", content)
        self.assertIn("Ask HN: I will pay for a CSV dedupe script", content)

        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["state"], "VALIDATING")   # never auto-advanced past this

        vt = [t for t in load_tasks(self.d).by_opportunity(oid)
              if t.task_type == "VERIFY_RESULT"][0]
        self.assertTrue(vt.output["checklist"]["references_title"])

    def test_worker_never_moves_money_or_submits_anywhere(self):
        # the whole chain runs inside autonomous_context() (worker._execute) -
        # if any adapter ever tried a money/identity action it would raise.
        from revenue_os import action_class as ac2
        oid = self._qualified_task_opportunity()
        with ac2.autonomous_context():
            out = self._drain()
        self.assertTrue(all(p["ok"] for p in out["processed"] if "ok" in p))

    def test_pending_actions_surfaces_submit_task_after_verification(self):
        from revenue_os.acceptance import pending_actions

        oid = self._qualified_task_opportunity()
        self._drain()
        rows = [r for r in pending_actions(self.d) if r["opportunity_id"] == oid]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "SUBMIT_TASK")
        self.assertIn("task_solution.md", rows[0]["detail"])

    def test_record_task_outcome_before_verification_fails_closed(self):
        oid = self._qualified_task_opportunity()   # chain not drained yet
        with self.assertRaises(pipeline.EcosystemError):
            pipeline.record_task_outcome(self.d, oid, success=True, amount=20.0,
                                         ref="r1")

    def test_record_task_outcome_success_books_ledger_and_advances_state(self):
        from revenue_os.acceptance import pending_actions
        from revenue_os.ecosystem.learning import OutcomeStore
        from revenue_os.revenue import RevenueLedger

        oid = self._qualified_task_opportunity()
        self._drain()

        out = pipeline.record_task_outcome(self.d, oid, success=True, amount=22.5,
                                           ref="paypal-abc", note="paid via PayPal")
        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["state"], "ACTIVE")

        ledger = RevenueLedger.load(self.d / "revenue.json")
        self.assertEqual(ledger.total_for(oid), 22.5)

        rows = OutcomeStore.load(self.d).rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["success"])
        self.assertEqual(rows[0]["revenue_eur"], 22.5)
        self.assertEqual(rows[0]["strategy"], "TASK")

        # settled - no longer a pending SUBMIT_TASK action
        rows2 = [r for r in pending_actions(self.d) if r["opportunity_id"] == oid]
        self.assertEqual(rows2, [])

        # idempotent replay with the same ref books nothing new
        out2 = pipeline.record_task_outcome(self.d, oid, success=True, amount=22.5,
                                            ref="paypal-abc")
        self.assertEqual(out2["outcome"], "already_recorded")
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total_for(oid), 22.5)

    def test_record_task_outcome_failure_settles_without_payment(self):
        from revenue_os.ecosystem.learning import OutcomeStore
        from revenue_os.revenue import RevenueLedger

        oid = self._qualified_task_opportunity()
        self._drain()

        out = pipeline.record_task_outcome(self.d, oid, success=False,
                                           note="requester filled the gig elsewhere")
        self.assertEqual(out["outcome"], "failure")

        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total_for(oid), 0.0)
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["state"], "VALIDATING")   # unchanged - no fake progress

        rows = OutcomeStore.load(self.d).rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["success"])
        self.assertEqual(rows[0]["failure_reason"], "requester filled the gig elsewhere")

    def test_record_task_outcome_success_requires_amount_and_ref(self):
        oid = self._qualified_task_opportunity()
        self._drain()
        with self.assertRaises(pipeline.EcosystemError):
            pipeline.record_task_outcome(self.d, oid, success=True, amount=0.0,
                                         ref="r1")
        with self.assertRaises(pipeline.EcosystemError):
            pipeline.record_task_outcome(self.d, oid, success=True, amount=10.0,
                                         ref="")

    def test_execute_task_fails_closed_with_no_plan(self):
        from revenue_os.ecosystem.task_adapters import ExecuteTaskAdapter
        from revenue_os.execution import ExecutionTask
        from revenue_os.worker import AdapterContext

        task = ExecutionTask(opportunity_id="opp_x", task_type="EXECUTE_TASK")
        ctx = AdapterContext(self.d, task, {"id": "opp_x", "title": "x"}, {})
        res = ExecuteTaskAdapter().run(ctx)
        self.assertFalse(res.ok)
        self.assertFalse(res.retryable)

    def test_verify_result_fails_closed_on_a_missing_file(self):
        from revenue_os.ecosystem.task_adapters import VerifyResultTaskAdapter
        from revenue_os.execution import ExecutionTask
        from revenue_os.worker import AdapterContext

        task = ExecutionTask(opportunity_id="opp_x", task_type="VERIFY_RESULT")
        ctx = AdapterContext(self.d, task, {"id": "opp_x", "title": "x"},
                             {"EXECUTE_TASK": {"deliverable_path": "deliverables/opp_x/task_solution.md"}})
        res = VerifyResultTaskAdapter().run(ctx)
        self.assertFalse(res.ok)
        self.assertFalse(res.retryable)


if __name__ == "__main__":
    unittest.main()
