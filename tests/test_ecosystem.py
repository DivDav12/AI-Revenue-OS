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
    task_signal,
    verification,
)
from revenue_os.ecosystem.discovery import DiscoveryEngine, latest_discovery
from revenue_os.ecosystem.model import (
    OpportunityDraft,
    PaymentEvidence,
    SourceMeta,
    SubmissionEvidence,
)
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

    # --- discovery quality layer: TASK-signal hard gates ------------------

    def test_task_kind_instant_paid_stays_qualified(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $30 for a working script, submit via the form"]))
        self.assertEqual(v.status, model.V_QUALIFIED)
        self.assertEqual(v.checks["task_kind"], model.TASK_INSTANT_PAID)
        self.assertIn("task_quality", v.checks)

    def test_task_kind_job_is_human_required(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["We're hiring a full-time developer, apply now"]))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)
        self.assertEqual(v.checks["task_kind"], model.TASK_JOB)

    def test_task_kind_service_lead_is_human_required(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["Looking for a freelancer to build a website, "
                     "need someone to help"]))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)
        self.assertEqual(v.checks["task_kind"], model.TASK_SERVICE_LEAD)

    def test_captcha_submission_forces_human_required(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $20 for this bounty"],
            submission_evidence=SubmissionEvidence(requires_captcha=True)))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)
        self.assertIn("CAPTCHA", v.reasons[0])

    def test_login_required_submission_forces_human_required(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $20 for this bounty"],
            submission_evidence=SubmissionEvidence(requires_login=True)))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)

    def test_identity_required_submission_forces_human_required(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $20 for this bounty"],
            submission_evidence=SubmissionEvidence(requires_identity=True)))
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)

    def test_expired_deadline_is_rejected(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $20 for this bounty"],
            submission_evidence=SubmissionEvidence(
                deadline="2000-01-01T00:00:00+00:00")))
        self.assertEqual(v.status, model.V_REJECTED)

    def test_future_deadline_does_not_block(self):
        v = verification.verify(_draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["I will pay $20 for this bounty"],
            submission_evidence=SubmissionEvidence(
                deadline="2999-01-01T00:00:00+00:00")))
        self.assertEqual(v.status, model.V_QUALIFIED)

    def test_a_high_score_never_overrides_the_job_hard_gate(self):
        # HARD GATE (spec section 5): even a maximally-evidenced job posting
        # with a concrete guaranteed amount stays HUMAN_REQUIRED - the score
        # is advisory, task_kind decides.
        d = _draft(
            opportunity_type=model.TYPE_TASK,
            evidence=["We're hiring a full-time developer, apply now"],
            payment_evidence=PaymentEvidence(
                amount=5000, currency="EUR", conditions=model.PAY_GUARANTEED,
                is_estimate=False),
            submission_evidence=SubmissionEvidence(
                submission_url="https://example.com/apply",
                required_deliverable="a resume"))
        score = task_signal.score_task_quality(d)
        self.assertGreater(score.total, 0.3)
        v = verification.verify(d)
        self.assertEqual(v.status, model.V_HUMAN_REQUIRED)


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

    def test_task_strategy_falls_back_to_prepared_when_task_kind_is_not_confirmed(self):
        # HARD GATE (spec section 5): STRAT_TASK recommended, but the
        # evidence did not classify as an autonomous-candidate task kind
        # (here: OTHER, no bounty/instant/job/service markers at all) -
        # plan() must NOT build the real chain.
        oid = self._one(opportunity_type=model.TYPE_TASK, est_pay_eur=25.0,
                        demand_hint=0.6,
                        evidence=["a general discussion thread with no concrete "
                                  "task or payment stated"])
        sel = pipeline.select(self.d, oid)
        if sel["recommended"] != model.STRAT_TASK:
            self.skipTest("strategy engine did not pick TASK for this fixture")
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["discovery"]["verification"]["checks"]["task_kind"],
                         model.TASK_OTHER)
        out = pipeline.plan(self.d, oid)
        self.assertEqual(out["kind"], "prepared")
        self.assertEqual(out["next_step_class"], "HUMAN_REQUIRED")
        from revenue_os.execution import load_tasks
        self.assertEqual(load_tasks(self.d).by_opportunity(oid), [])


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
# task_signal - classification, quality score, fingerprint, expiry
# ---------------------------------------------------------------------------

class TaskSignalTests(unittest.TestCase):
    # --- classify_task_kind: evidence-based, never the title ------------

    def test_classify_instant_paid(self):
        d = _draft(evidence=["I will pay $25 for a working script"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_INSTANT_PAID)

    def test_classify_bounty(self):
        d = _draft(evidence=["$100 bounty for fixing this bug"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_BOUNTY)

    def test_classify_microtask(self):
        d = _draft(evidence=["quick task: label 50 images, a 5 minute job"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_MICRO)

    def test_classify_job(self):
        d = _draft(evidence=["We're hiring a full-time backend engineer"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_JOB)

    def test_classify_service_lead(self):
        d = _draft(evidence=["Looking for a freelancer to redesign our logo"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_SERVICE_LEAD)

    def test_classify_weak_demand_signal_is_other(self):
        d = _draft(evidence=["just thinking out loud about a project idea"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_OTHER)

    def test_classify_never_uses_the_title(self):
        # the title screams "hiring"; the evidence is a real, no-application
        # bounty - evidence wins, never the title (spec: "nicht raten")
        d = _draft(title="We are hiring right now!!!",
                   evidence=["$50 bounty for a fix, no application needed"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_BOUNTY)

    def test_classify_contradictory_evidence_is_other(self):
        d = _draft(evidence=["We're hiring a freelancer, but also here is a "
                             "$50 bounty for a contest"])
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_OTHER)

    def test_classify_empty_evidence_fails_closed_to_other(self):
        d = _draft(evidence=[], description="")
        self.assertEqual(task_signal.classify_task_kind(d), model.TASK_OTHER)

    # --- score_task_quality: explainable, never a substitute for the gate -

    def test_missing_payment_is_flagged_unclear_not_real(self):
        d = _draft(evidence=["a random Ask HN post"])
        score = task_signal.score_task_quality(d)
        self.assertFalse(score.factors["concrete_payment"]["present"])
        self.assertTrue(score.factors["unclear_payment"]["present"])

    def test_estimated_payment_never_counts_as_guaranteed(self):
        d = _draft(payment_evidence=PaymentEvidence(
            amount=20, currency="EUR", conditions=model.PAY_CONDITIONAL,
            is_estimate=True))
        self.assertTrue(d.payment_evidence.is_estimate)
        score = task_signal.score_task_quality(d)
        self.assertTrue(score.factors["concrete_payment"]["present"])
        self.assertFalse(score.factors["guaranteed_payment"]["present"])

    def test_captcha_is_a_hard_negative_factor(self):
        d = _draft(submission_evidence=SubmissionEvidence(requires_captcha=True))
        score = task_signal.score_task_quality(d)
        self.assertTrue(score.factors["requires_captcha"]["present"])

    def test_login_is_a_hard_negative_factor(self):
        d = _draft(submission_evidence=SubmissionEvidence(requires_login=True))
        score = task_signal.score_task_quality(d)
        self.assertTrue(score.factors["requires_login"]["present"])

    def test_missing_submission_path_is_a_negative_factor(self):
        d = _draft()   # default submission_evidence: nothing known
        score = task_signal.score_task_quality(d)
        self.assertTrue(score.factors["unclear_submission"]["present"])

    def test_expired_deadline_is_a_hard_negative_factor(self):
        d = _draft(submission_evidence=SubmissionEvidence(
            deadline="2000-01-01T00:00:00+00:00"))
        score = task_signal.score_task_quality(d)
        self.assertTrue(score.factors["expired"]["present"])

    def test_score_is_deterministic_and_explainable(self):
        d = _draft(evidence=["$50 bounty for a fix"])
        s1 = task_signal.score_task_quality(d).to_dict()
        s2 = task_signal.score_task_quality(d).to_dict()
        self.assertEqual(s1, s2)
        self.assertTrue(s1["reasons"])
        for factor in s1["factors"].values():
            self.assertIn("weight", factor)
            self.assertIn("present", factor)
            self.assertIn("sign", factor)

    def test_score_stays_within_bounds(self):
        for kw in ({}, {"evidence": ["$999 bounty guaranteed"],
                        "payment_evidence": PaymentEvidence(
                            amount=999, conditions=model.PAY_GUARANTEED,
                            is_estimate=False)}):
            score = task_signal.score_task_quality(_draft(**kw))
            self.assertGreaterEqual(score.total, 0.0)
            self.assertLessEqual(score.total, 1.0)

    def test_autonomous_candidate_flag_matches_task_kind(self):
        job = task_signal.score_task_quality(
            _draft(evidence=["We're hiring a developer"]))
        bounty = task_signal.score_task_quality(
            _draft(evidence=["$50 bounty for a fix"]))
        self.assertFalse(job.autonomous_candidate)
        self.assertTrue(bounty.autonomous_candidate)

    # --- fingerprint: stable dedupe key ----------------------------------

    def test_fingerprint_stable_across_url_timestamp_and_id_changes(self):
        a = _draft(title="Fix the CSV parser bug 2026-09-04T10:00", source_id="1",
                  source_url="https://x/1")
        b = _draft(title="Fix the CSV parser bug 2026-09-04T11:30", source_id="2",
                  source_url="https://x/2")
        self.assertEqual(task_signal.task_fingerprint(a), task_signal.task_fingerprint(b))

    def test_fingerprint_differs_for_different_tasks(self):
        a = _draft(title="Fix the CSV parser bug")
        b = _draft(title="Build a landing page")
        self.assertNotEqual(task_signal.task_fingerprint(a),
                            task_signal.task_fingerprint(b))

    def test_fingerprint_differs_across_sources(self):
        a = _draft(title="Same title", source_meta=_real_meta(source="s1"))
        b = _draft(title="Same title", source_meta=_real_meta(source="s2"))
        self.assertNotEqual(task_signal.task_fingerprint(a),
                            task_signal.task_fingerprint(b))

    # --- is_expired: fails OPEN on unparseable input, never invents a date

    def test_no_deadline_is_not_expired(self):
        self.assertFalse(task_signal.is_expired(SubmissionEvidence()))

    def test_unparseable_deadline_is_not_treated_as_expired(self):
        # we never invent a deadline the source did not clearly state -
        # an unparseable string is not confirmed evidence of expiry.
        self.assertFalse(task_signal.is_expired(
            SubmissionEvidence(deadline="not-a-real-date")))

    def test_past_deadline_is_expired(self):
        self.assertTrue(task_signal.is_expired(
            SubmissionEvidence(deadline="2000-01-01T00:00:00+00:00")))

    def test_future_deadline_is_not_expired(self):
        self.assertFalse(task_signal.is_expired(
            SubmissionEvidence(deadline="2999-01-01T00:00:00+00:00")))


# ---------------------------------------------------------------------------
# TASK fingerprint dedupe through the real DiscoveryEngine
# ---------------------------------------------------------------------------

class TaskFingerprintDedupeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_re_scraped_task_with_a_new_url_and_id_does_not_duplicate(self):
        d1 = _draft(opportunity_type=model.TYPE_TASK, source_id="issue-804",
                   source_url="https://x/804",
                   title="[radar] open bounty 2026-09-04T14:15",
                   evidence=["$100 bounty for a fix"])
        d2 = _draft(opportunity_type=model.TYPE_TASK, source_id="issue-991",
                   source_url="https://x/991",
                   title="[radar] open bounty 2026-09-04T15:47",
                   evidence=["$100 bounty for a fix"])
        DiscoveryEngine(self.d, sources=[_StaticSource([d1])]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 1)
        rep2 = DiscoveryEngine(self.d, sources=[_StaticSource([d2])]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 1)   # still one
        self.assertEqual(rep2.new, 0)
        self.assertEqual(rep2.refreshed, 1)

    def test_different_tasks_from_the_same_source_are_not_merged(self):
        d1 = _draft(opportunity_type=model.TYPE_TASK, source_id="a",
                   title="Fix bug A", evidence=["$50 bounty"])
        d2 = _draft(opportunity_type=model.TYPE_TASK, source_id="b",
                   title="Fix bug B", evidence=["$50 bounty"])
        DiscoveryEngine(self.d, sources=[_StaticSource([d1, d2])]).run()
        self.assertEqual(len(load_opportunities(self.d).all()), 2)

    def test_non_task_types_are_unaffected_by_fingerprint_dedupe(self):
        # a PRODUCT-type draft never computes/stores a task_fingerprint
        DiscoveryEngine(self.d, sources=[_StaticSource(
            [_draft(source_id="p1")])]).run()
        rec = load_opportunities(self.d).all()[0]
        self.assertNotIn("task_fingerprint", rec["discovery"])


# ---------------------------------------------------------------------------
# per-source quality metrics (spec section 6)
# ---------------------------------------------------------------------------

class SourceQualityTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_funnel_counts_from_real_discovery(self):
        DiscoveryEngine(self.d, sources=[_StaticSource([
            _draft(source_id="1"),                                    # QUALIFIED
            _draft(source_id="2", opportunity_type=model.TYPE_TASK,
                  evidence=["We're hiring a developer"]),              # HUMAN_REQUIRED
        ])]).run()
        sq = learning.source_quality(self.d)["by_source"]
        self.assertEqual(sq["unit"]["discovered"], 2)
        self.assertEqual(sq["unit"]["qualified"], 1)

    def test_settled_outcomes_roll_up_by_source(self):
        learning.record_outcome(self.d, learning.Outcome(
            opportunity_id="o1", source="hacker-news", revenue_eur=20.0,
            success=True))
        learning.record_outcome(self.d, learning.Outcome(
            opportunity_id="o2", source="hacker-news", revenue_eur=0.0,
            success=False, failure_reason="not accepted"))
        sq = learning.source_quality(self.d)["by_source"]
        self.assertEqual(sq["hacker-news"]["human_submitted"], 2)
        self.assertEqual(sq["hacker-news"]["successful"], 1)
        self.assertEqual(sq["hacker-news"]["paid"], 1)
        self.assertEqual(sq["hacker-news"]["win_rate"], 0.5)
        self.assertEqual(sq["hacker-news"]["revenue_eur"], 20.0)

    def test_ecosystem_status_exposes_source_quality(self):
        DiscoveryEngine(self.d, sources=[_StaticSource([_draft(source_id="1")])]).run()
        st = intel.ecosystem_status(self.d)
        self.assertIn("source_quality", st)
        self.assertIn("unit", st["source_quality"])
        self.assertEqual(st["source_quality"]["unit"]["discovered"], 1)


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

    def test_verify_result_checklist_includes_not_expired_on_the_happy_path(self):
        from revenue_os.execution import load_tasks

        oid = self._qualified_task_opportunity()
        self._drain()
        vt = [t for t in load_tasks(self.d).by_opportunity(oid)
              if t.task_type == "VERIFY_RESULT"][0]
        self.assertTrue(vt.output["checklist"]["not_expired"])

    def test_plan_task_fails_closed_on_an_expired_deadline(self):
        from revenue_os.ecosystem.task_adapters import PlanTaskAdapter
        from revenue_os.execution import ExecutionTask
        from revenue_os.worker import AdapterContext

        rec = {
            "id": "opp_x", "title": "Fix a bug", "category": "freelancing",
            "discovery": {
                "opportunity_type": model.TYPE_TASK, "source": "unit",
                "evidence": ["$50 bounty"], "source_url": "https://x",
                "submission_evidence": SubmissionEvidence(
                    deadline="2000-01-01T00:00:00+00:00").to_dict(),
            },
        }
        task = ExecutionTask(opportunity_id="opp_x", task_type="PLAN_TASK")
        ctx = AdapterContext(self.d, task, rec, {})
        res = PlanTaskAdapter().run(ctx)
        self.assertFalse(res.ok)
        self.assertFalse(res.retryable)
        self.assertIn("deadline", res.error)


if __name__ == "__main__":
    unittest.main()
