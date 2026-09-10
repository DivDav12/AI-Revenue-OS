"""`affiliate_pipeline.run_affiliate_night` - one recurring-safe affiliate
cycle over a single existing entry point.

Covers: discovery + tick composition, the MAX_AUTONOMOUS_AFFILIATE_
ACTIONS_PER_RUN cap, per-source error isolation, per-opportunity error
isolation, NO_ACTION when nothing qualifies, "one failure never stops the
run", and that the whole cycle runs inside autonomous_context().

No network: fixture AcqSearchable sources only; deploy is faked.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import demand_sources, model
from revenue_os.ecosystem import affiliate_pipeline as AP
from revenue_os.ecosystem.affiliate_sources import ingest_affiliate_offer
from revenue_os.ecosystem.model import SourceMeta
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.opportunity_store import load_opportunities

_NOW = "2026-09-06T00:00:00+00:00"

_PDF_TITLE = ("Which PDF editor would you recommend for OCR and editing scanned "
              "contracts? I will pay, Acrobat is too expensive")
_PDF_TEXT = "Acrobat is too expensive, I would pay ~100 EUR for a one-time PDF editor with OCR."
_VPN_TITLE = ("What VPN would you recommend for streaming? I will pay for a good one, "
              "my current provider is too slow")
_VPN_TEXT = "My current VPN is too slow, I would pay ~60 EUR/year for a fast streaming VPN."
_WEAK_TITLE = "Thoughts on the PDF format and Adobe"
_WEAK_TEXT = "Just a general discussion about PDF history."


class _FixtureSource:
    def __init__(self, records): self._records = records

    def search(self, query, limit, *, since_ts=None):
        return list(self._records)[:limit]


class _BadSource:
    meta = SourceMeta(source="bad-src", source_type="demand_signal",
                      access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
                      requires_login=False, policy_status=model.POLICY_OK)

    def discover(self, limit):
        raise ConnectionError("simulated source outage")


def _rec(title, text, oid):
    return AcqRecord(title=title, url=f"https://news.ycombinator.com/item?id={oid}",
                     text=text, author="x", posted_at="2026-09-01T00:00:00+00:00",
                     platform="HN", source="hn-algolia", query="q")


def _demand_src(name, records):
    return demand_sources.DemandDiscoverySource(
        name, _FixtureSource(records), queries=("q",), now_iso=_NOW)


def _pdf_offer(**over):
    base = {
        "schema_version": 1, "network": "generic_saas_program",
        "program_name": "ProPDF Partner Program", "product_name": "ProPDF Editor",
        "product_url": "https://propdf.example/buy?ref=base", "product_price": 129.99,
        "currency": "USD", "commission_kind": "fixed", "commission_fixed_amount": 80.0,
        "commission_evidence": ["ProPDF dashboard: 'USD 80 bounty per sale'"],
        "evidence": ["ProPDF product page: PDF editor with OCR, conversion, forms"],
        "category": "pdf-editor",
        "keywords": ["pdf", "pdf editor", "edit pdf", "ocr", "convert pdf",
                     "acrobat", "acrobat alternative", "pdf forms"],
        "human_confirmed_joined": True, "tracking_param": "ref",
    }
    base.update(over)
    return base


def _vpn_offer():
    return {
        "schema_version": 1, "network": "generic_saas_program",
        "program_name": "FastVPN Partner Program", "product_name": "FastVPN",
        "product_url": "https://fastvpn.example/buy?ref=base", "product_price": 59.0,
        "currency": "USD", "commission_kind": "fixed", "commission_fixed_amount": 80.0,
        "commission_evidence": ["FastVPN dashboard: 'USD 80 per sale'"],
        "evidence": ["FastVPN product page: fast streaming VPN, no-logs"],
        "category": "vpn",
        "keywords": ["vpn", "streaming", "no-logs", "privacy", "fast vpn"],
        "human_confirmed_joined": True, "tracking_param": "ref",
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _night(self, sources, **kw):
        with mock.patch(
                "revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                return_value=FakeDeploymentAdapter()):
            return AP.run_affiliate_night(self.d, sources=sources, now_iso=_NOW, **kw)


class HappyPath(_Base):
    def test_discovery_plus_tick_produces_one_affiliate_action(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])

        self.assertIsNotNone(out["discovery"])
        self.assertGreaterEqual(out["discovery"]["new"], 1)
        self.assertEqual(out["new_affiliate_actions"], 1)
        self.assertEqual(out["action"], "AFFILIATE_ACTION")
        self.assertEqual(out["errors"], [])

        oid = out["tick"]["planned"][0]
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_AFFILIATE)
        plan = rec["strategy"]["plan"]
        self.assertEqual(plan["kind"], "affiliate_chain")
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["next_step_class"], "SAFE_AUTONOMOUS")


class MaxActionsCap(_Base):
    def test_default_cap_is_one(self):
        self.assertEqual(AP.MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN, 1)

    def test_two_qualifying_demands_yield_only_one_action_per_run(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        ingest_affiliate_offer(self.d, _vpn_offer())
        out = self._night([_demand_src("demand-hn", [
            _rec(_PDF_TITLE, _PDF_TEXT, "1"),
            _rec(_VPN_TITLE, _VPN_TEXT, "2"),
        ])])
        tick = out["tick"]
        self.assertEqual(len(tick["planned"]), 1)
        self.assertEqual(len(tick["capped"]), 1)          # deferred, not dropped
        self.assertEqual(tick["max_actions"], 1)
        self.assertEqual(out["new_affiliate_actions"], 1)

    def test_next_run_picks_up_the_deferred_one(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        ingest_affiliate_offer(self.d, _vpn_offer())
        recs = [_rec(_PDF_TITLE, _PDF_TEXT, "1"), _rec(_VPN_TITLE, _VPN_TEXT, "2")]
        first = self._night([_demand_src("demand-hn", recs)])
        second = self._night([_demand_src("demand-hn", recs)])
        planned_ids = set(first["tick"]["planned"]) | set(second["tick"]["planned"])
        self.assertEqual(len(planned_ids), 2)             # both, across two runs
        self.assertEqual(second["tick"]["capped"], [])

    def test_max_actions_zero_means_no_autonomous_action(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])],
                          max_actions=0)
        self.assertEqual(out["new_affiliate_actions"], 0)
        self.assertEqual(out["action"], "NO_ACTION")
        self.assertEqual(len(out["tick"]["capped"]), 1)


class ErrorIsolation(_Base):
    def test_one_bad_source_does_not_stop_the_run(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        out = self._night([
            _BadSource(),
            _demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")]),
        ])
        self.assertTrue(any("simulated source outage" in e for e in out["errors"]))
        # the healthy source's signal still became an affiliate action
        self.assertEqual(out["new_affiliate_actions"], 1)

    def test_discovery_crash_is_isolated_tick_still_runs(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        # seed one qualifying opportunity via a clean run first
        self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])
        # now a run whose discovery blows up entirely - tick must still work
        with mock.patch("revenue_os.ecosystem.discovery.DiscoveryEngine.run",
                        side_effect=RuntimeError("discovery boom")):
            out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])
        self.assertTrue(any("discovery boom" in e for e in out["errors"]))
        self.assertIsNotNone(out["tick"])                 # tick still ran

    def test_one_bad_opportunity_does_not_stop_the_tick(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        with mock.patch("revenue_os.ecosystem.pipeline.evaluate",
                        side_effect=RuntimeError("eval boom")):
            out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])
        self.assertTrue(any("eval boom" in str(e) for e in out["errors"]))
        self.assertEqual(out["action"], "NO_ACTION")


class NoActionWhenNothingQualifies(_Base):
    def test_weak_discussion_only_is_no_action(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        out = self._night([_demand_src("demand-hn", [_rec(_WEAK_TITLE, _WEAK_TEXT, "1")])])
        self.assertEqual(out["new_affiliate_actions"], 0)
        self.assertEqual(out["action"], "NO_ACTION")
        self.assertEqual(out["tick"]["planned"], [])

    def test_strong_demand_but_no_offer_is_no_action(self):
        out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])
        self.assertEqual(out["action"], "NO_ACTION")


class RunsInsideAutonomousContext(_Base):
    def test_cycle_body_runs_inside_autonomous_context(self):
        from revenue_os.action_class import in_autonomous_context

        seen = {}
        real_tick = AP.run_affiliate_tick

        def spy(*a, **k):
            seen["in_ctx"] = in_autonomous_context()
            return real_tick(*a, **k)

        with mock.patch.object(AP, "run_affiliate_tick", side_effect=spy):
            self._night([_demand_src("demand-hn", [_rec(_WEAK_TITLE, _WEAK_TEXT, "1")])])
        self.assertTrue(seen["in_ctx"])


class SafetyAndIdempotency(_Base):
    def test_unprofitable_offer_is_no_action(self):
        # matches strongly on keywords, but the EXISTING
        # affiliate_profitability projection is <= 0 (tiny bounty vs. the
        # fixed content cost)
        ingest_affiliate_offer(self.d, _pdf_offer(
            commission_kind="fixed", commission_fixed_amount=1.0,
            commission_evidence=["ProPDF dashboard: 'USD 1 per sale'"]))
        out = self._night([_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])])
        self.assertEqual(out["action"], "NO_ACTION")
        self.assertEqual(out["tick"]["planned"], [])

    def test_safety_rejected_demand_never_reaches_the_chain(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        # a strong keyword match to the PDF offer but a discussion post
        # with no purchase intent -> verify REJECTED -> not PLANNABLE
        out = self._night([_demand_src("demand-hn", [
            _rec("Open letter to Adobe: please free our PDF files",
                 "Adobe Acrobat controls the PDF format; a discussion.", "1")])])
        self.assertEqual(out["action"], "NO_ACTION")
        oids = {r["id"] for r in load_opportunities(self.d).all()}
        for oid in oids:
            rec = load_opportunities(self.d).get(oid)
            self.assertNotIn(rec["discovery"]["verification"]["status"], model.PLANNABLE)
            self.assertNotEqual(rec["discovery"]["opportunity_type"], model.TYPE_AFFILIATE)

    def test_amazon_associates_stays_excluded_in_the_night_loop(self):
        ingest_affiliate_offer(self.d, {
            "schema_version": 1, "network": "amazon_associates",
            "program_name": "Amazon.de PartnerNet", "product_name": "StreamMic USB Microphone",
            "product_url": "https://www.amazon.de/dp/B0TEST0001", "product_asin": "B0TEST0001",
            "product_price": 79.99, "currency": "EUR", "commission_kind": "fixed",
            "commission_fixed_amount": 90.0,
            "commission_evidence": ["Amazon PartnerNet fee schedule"],
            "evidence": ["Amazon listing: USB condenser microphone for streaming"],
            "category": "usb-microphone",
            "keywords": ["microphone", "usb microphone", "streaming", "podcast", "mic"],
            "human_confirmed_joined": True, "tracking_param": "tag", "tracking_value": "test-21",
        })
        out = self._night([_demand_src("demand-hn", [_rec(
            "What USB microphone would you recommend for streaming? I will pay, my old one died",
            "My USB mic died, I would pay ~90 EUR for a good streaming microphone.", "1")])])
        self.assertEqual(out["action"], "NO_ACTION")
        self.assertEqual(out["tick"]["planned"], [])

    def test_repeated_tick_is_idempotent(self):
        ingest_affiliate_offer(self.d, _pdf_offer())
        recs = [_rec(_PDF_TITLE, _PDF_TEXT, "1")]
        first = self._night([_demand_src("demand-hn", recs)])
        self.assertEqual(first["new_affiliate_actions"], 1)
        oid = first["tick"]["planned"][0]

        second = self._night([_demand_src("demand-hn", recs)])
        self.assertEqual(second["new_affiliate_actions"], 0)      # already completed
        self.assertNotIn(oid, second["tick"]["attempted"])        # skipped, not re-run
        self.assertEqual(second["action"], "NO_ACTION")

        # the completed chain row is unchanged (idempotent, no duplicate)
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["strategy"]["plan"]["status"], "completed")
        from revenue_os.ecosystem.affiliate_model import AffiliateAssetStore, AffiliateLinkStore
        self.assertEqual(len(AffiliateAssetStore.load(self.d).by_opportunity(oid)), 1)
        self.assertEqual(len(AffiliateLinkStore.load(self.d).by_opportunity(oid)), 1)


class ExistingFunctionsUnchanged(unittest.TestCase):
    def test_run_affiliate_tick_default_signature_is_back_compatible(self):
        import inspect

        sig = inspect.signature(AP.run_affiliate_tick)
        self.assertEqual(sig.parameters["max_actions"].default,
                         AP.MAX_AUTONOMOUS_AFFILIATE_ACTIONS_PER_RUN)
        # limit / now_iso still present with their original defaults
        self.assertEqual(sig.parameters["limit"].default, 20)
        self.assertEqual(sig.parameters["now_iso"].default, "")

    def test_prefer_affiliate_if_matched_discovery_kwarg_is_optional(self):
        from revenue_os.ecosystem import pipeline as P

        sig = inspect.signature(P._prefer_affiliate_if_matched)
        self.assertIsNone(sig.parameters["discovery"].default)


class LoopRunnerCli(unittest.TestCase):
    """The thin --loop CLI: single start, never waits for human input,
    bounded, honours the existing global pause. Deterministic - the cycle
    and sleep are stubbed."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _run(self, argv):
        import io
        from contextlib import redirect_stdout

        from revenue_os import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv + ["--data-dir", str(self.d)])
        return code, buf.getvalue()

    def test_loop_runs_bounded_number_of_ticks_without_human_input(self):
        calls = {"n": 0}

        def fake_cycle(*a, **k):
            calls["n"] += 1
            return {"action": "NO_ACTION", "new_affiliate_actions": 0,
                    "errors": [], "discovery": {"new": 0}, "tick": {}}

        with mock.patch("revenue_os.ecosystem.affiliate_pipeline.run_affiliate_night",
                        side_effect=fake_cycle), \
             mock.patch("time.sleep") as sleep_mock:
            code, out = self._run(["affiliate-night", "--loop", "--max-ticks", "3",
                                   "--interval", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(calls["n"], 3)
        self.assertIn("stopped: max-ticks (3 tick(s))", out)
        self.assertTrue(sleep_mock.called)

    def test_loop_skips_ticks_while_the_fleet_is_paused(self):
        from revenue_os.agent_control import load_agent_control

        ctrl = load_agent_control(self.d)
        ctrl.set_paused(True, by="test", reason="manual stop for the night")
        ctrl.save()

        with mock.patch("revenue_os.ecosystem.affiliate_pipeline.run_affiliate_night") as cyc, \
             mock.patch("time.sleep"):
            code, out = self._run(["affiliate-night", "--loop", "--max-ticks", "2",
                                   "--interval", "0", "--max-runtime", "5"])
        self.assertEqual(code, 0)
        cyc.assert_not_called()                       # paused -> no cycle ran
        self.assertIn("fleet paused", out)

    def test_single_cycle_without_loop_flag(self):
        with mock.patch("revenue_os.ecosystem.affiliate_pipeline.run_affiliate_night",
                        return_value={"action": "NO_ACTION"}) as cyc:
            code, out = self._run(["affiliate-night"])
        self.assertEqual(code, 0)
        cyc.assert_called_once()
        self.assertIn("NO_ACTION", out)


if __name__ == "__main__":
    unittest.main()
