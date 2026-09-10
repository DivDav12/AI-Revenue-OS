"""`ecosystem.affiliate_discovery` - the offer-discovery layer that stages
real product-search results as candidates for human completion.

Covers: candidate discovery, deterministic dedup, "an unauthorized
network is never contacted", candidate -> human completion -> a usable
offer, that a completed offer makes `affiliate-night` produce a real
action, and that every existing safety/money/approval firewall is
untouched.

No network: the one authorized source used here is the curated,
network-call-free `WondershareOfferSource` (Awin advertiser 20202),
driven entirely by an injected `environ` dict.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from revenue_os.acquisition_sources import AcqRecord
from revenue_os.ecosystem import affiliate_pipeline as AP
from revenue_os.ecosystem import demand_sources, model
from revenue_os.ecosystem.affiliate_discovery import discover_offer_candidates
from revenue_os.ecosystem.affiliate_model import (
    CANDIDATE_COMPLETED,
    CANDIDATE_NEEDS_COMPLETION,
    AffiliateOfferCandidateStore,
    AffiliateOfferStore,
)
from revenue_os.ecosystem.discovery import DiscoveryEngine
from revenue_os.deployment import FakeDeploymentAdapter

_NOW = "2026-09-09T00:00:00+00:00"

_PDF_TITLE = ("Which PDF editor would you recommend for OCR and editing scanned "
              "contracts? I will pay, Acrobat is too expensive")
_PDF_TEXT = "Acrobat is too expensive, I would pay ~120 EUR for a one-time PDF editor with OCR."
_WEAK_TITLE = "Thoughts on the PDF format and Adobe"
_WEAK_TEXT = "Just a general discussion about PDF history."

# a real-shaped Awin publisher identity + the exact cread.php deep link
# Awin issues for advertiser 20202 (PDFelement). Never a live credential.
_WS_ENV = {
    "WONDERSHARE_AWIN_PUBLISHER_ID": "998877",
    "WONDERSHARE_AWIN_TRACKING_URL": (
        "https://www.awin1.com/cread.php?awinmid=20202&awinaffid=998877"
        "&ued=https%3A%2F%2Fpdf.wondershare.com%2Fpdfelement.html"),
}


class _FixtureSource:
    def __init__(self, records):
        self._records = records

    def search(self, query, limit, *, since_ts=None):
        return list(self._records)[:limit]


def _rec(title, text, oid):
    return AcqRecord(title=title, url=f"https://news.ycombinator.com/item?id={oid}",
                     text=text, author="x", posted_at="2026-09-01T00:00:00+00:00",
                     platform="HN", source="hn-algolia", query="q")


def _demand_src(name, records):
    return demand_sources.DemandDiscoverySource(
        name, _FixtureSource(records), queries=("q",), now_iso=_NOW)


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _seed_demand(self, records):
        DiscoveryEngine(self.d, sources=[_demand_src("demand-hn", records)]).run(
            limit_per_source=20)

    def _candidates(self):
        return AffiliateOfferCandidateStore.load(self.d).all()

    def _offers(self):
        return AffiliateOfferStore.load(self.d).all()


class Discovery(_Base):
    def test_authorized_source_stages_a_candidate_linked_to_the_opportunity(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        rep = discover_offer_candidates(self.d, networks=["awin"], environ=_WS_ENV, now_iso=_NOW)

        self.assertEqual(rep["networks_live"], ["awin"])
        self.assertEqual(len(rep["new_candidates"]), 1)
        self.assertGreaterEqual(rep["opportunities_scanned"], 1)

        cands = self._candidates()
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.network, "awin")
        self.assertEqual(c.product_name, "Wondershare PDFelement")
        self.assertEqual(c.category_phrase, "pdf editor")
        self.assertTrue(c.opportunity_id)
        self.assertEqual(c.status, CANDIDATE_NEEDS_COMPLETION)
        # discovery must NEVER create a usable offer on its own
        self.assertEqual(self._offers(), [])

    def test_no_product_intent_no_candidate(self):
        self._seed_demand([_rec(_WEAK_TITLE, _WEAK_TEXT, "1")])
        rep = discover_offer_candidates(self.d, networks=["awin"], environ=_WS_ENV)
        self.assertEqual(rep["new_candidates"], [])
        self.assertEqual(self._candidates(), [])

    def test_dedup_is_deterministic_across_runs(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        first = discover_offer_candidates(self.d, networks=["awin"], environ=_WS_ENV, now_iso=_NOW)
        cid = first["new_candidates"][0]
        second = discover_offer_candidates(
            self.d, networks=["awin"], environ=_WS_ENV, now_iso="2026-09-10T00:00:00+00:00")

        self.assertEqual(second["new_candidates"], [])
        self.assertEqual(second["refreshed_candidates"], [cid])
        self.assertEqual(len(self._candidates()), 1)
        self.assertEqual(self._candidates()[0].last_seen_at, "2026-09-10T00:00:00+00:00")


class UnauthorizedNetworksAreNeverContacted(_Base):
    def test_unconfigured_cj_is_skipped_and_search_is_never_called(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        with mock.patch(
                "revenue_os.ecosystem.cj_offer_source.CjOfferSource.search") as cj_search:
            rep = discover_offer_candidates(self.d, networks=["cj_affiliate"], environ={})
        cj_search.assert_not_called()
        self.assertEqual(rep["networks_live"], [])
        self.assertEqual([s["network"] for s in rep["networks_skipped"]], ["cj_affiliate"])
        self.assertEqual(self._candidates(), [])

    def test_unknown_network_name_is_surfaced_not_silently_dropped(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        rep = discover_offer_candidates(self.d, networks=["awn_typo"], environ={})
        self.assertEqual(rep["networks_live"], [])
        self.assertEqual(
            [s for s in rep["networks_skipped"] if s["network"] == "awn_typo"],
            [{"network": "awn_typo", "reason": "unknown offer-source network"}])
        self.assertEqual(self._candidates(), [])

    def test_default_run_with_no_config_contacts_nothing(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        rep = discover_offer_candidates(self.d, environ={})
        self.assertEqual(rep["networks_live"], [])
        self.assertEqual(rep["opportunities_scanned"], 0)
        self.assertEqual(self._candidates(), [])
        self.assertEqual(self._offers(), [])


class _CliMixin:
    def _cli(self, argv):
        from revenue_os import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv + ["--data-dir", str(self.d)])
        return code, buf.getvalue()


class CompleteOffer(_Base, _CliMixin):
    def _stage_one(self):
        self._seed_demand([_rec(_PDF_TITLE, _PDF_TEXT, "1")])
        rep = discover_offer_candidates(self.d, networks=["awin"], environ=_WS_ENV, now_iso=_NOW)
        return rep["new_candidates"][0]

    def test_refuses_without_confirm_joined(self):
        cid = self._stage_one()
        code, out = self._cli([
            "affiliate-complete-offer", cid, "--program-name", "Wondershare/Awin",
            "--commission-kind", "fixed", "--commission-fixed", "150",
            "--commission-evidence", "Awin dashboard: 'EUR 150 per sale'"])
        self.assertEqual(code, 1)
        self.assertEqual(self._offers(), [])
        self.assertEqual(self._candidates()[0].status, CANDIDATE_NEEDS_COMPLETION)

    def test_refuses_without_commission_evidence(self):
        cid = self._stage_one()
        code, out = self._cli([
            "affiliate-complete-offer", cid, "--program-name", "Wondershare/Awin",
            "--commission-kind", "fixed", "--commission-fixed", "150", "--confirm-joined"])
        self.assertEqual(code, 1)
        self.assertEqual(self._offers(), [])

    def test_human_completion_produces_a_usable_offer(self):
        cid = self._stage_one()
        code, out = self._cli([
            "affiliate-complete-offer", cid, "--program-name", "Wondershare/Awin",
            "--commission-kind", "fixed", "--commission-fixed", "150",
            "--commission-evidence", "Awin dashboard: 'EUR 150 per sale'",
            "--confirm-joined"])
        self.assertEqual(code, 0)

        offers = self._offers()
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].usable)
        self.assertEqual(offers[0].network, "awin")
        self.assertEqual(offers[0].commission.fixed_amount, 150.0)
        self.assertFalse(offers[0].commission.is_estimate)

        cand = self._candidates()[0]
        self.assertEqual(cand.status, CANDIDATE_COMPLETED)
        self.assertEqual(cand.completed_offer_id, offers[0].offer_id)

    def test_unknown_candidate_is_an_error(self):
        code, out = self._cli([
            "affiliate-complete-offer", "cand-deadbeefdeadbeef",
            "--program-name", "x", "--commission-kind", "fixed",
            "--commission-fixed", "1", "--commission-evidence", "q", "--confirm-joined"])
        self.assertEqual(code, 1)


class NightLoopIntegration(_Base, _CliMixin):
    def _night(self, **kw):
        with mock.patch(
                "revenue_os.ecosystem.affiliate_assets.default_deployment_adapter",
                return_value=FakeDeploymentAdapter()):
            return AP.run_affiliate_night(
                self.d, sources=[_demand_src("demand-hn", [_rec(_PDF_TITLE, _PDF_TEXT, "1")])],
                environ=_WS_ENV, now_iso=_NOW, **kw)

    def test_night_stages_candidate_then_no_action_until_completed(self):
        out = self._night()
        self.assertIsNotNone(out["offer_discovery"])
        self.assertEqual(len(out["offer_discovery"]["new_candidates"]), 1)
        # a bare candidate is not a usable offer -> nothing routes yet
        self.assertEqual(out["action"], "NO_ACTION")
        self.assertEqual(self._offers(), [])

    def test_completed_offer_makes_the_next_night_run_produce_an_action(self):
        first = self._night()
        cid = first["offer_discovery"]["new_candidates"][0]
        code, _ = self._cli([
            "affiliate-complete-offer", cid, "--program-name", "Wondershare/Awin",
            "--commission-kind", "fixed", "--commission-fixed", "150",
            "--commission-evidence", "Awin dashboard: 'EUR 150 per sale'",
            "--confirm-joined"])
        self.assertEqual(code, 0)

        second = self._night()
        self.assertEqual(second["action"], "AFFILIATE_ACTION")
        self.assertEqual(second["new_affiliate_actions"], 1)
        oid = second["tick"]["planned"][0]
        from revenue_os.opportunity_store import load_opportunities
        rec = load_opportunities(self.d).get(oid)
        self.assertEqual(rec["discovery"]["opportunity_type"], model.TYPE_AFFILIATE)
        self.assertEqual(rec["strategy"]["plan"]["kind"], "affiliate_chain")
        self.assertEqual(rec["strategy"]["plan"]["status"], "completed")

    def test_offer_discovery_runs_inside_autonomous_context(self):
        from revenue_os.action_class import in_autonomous_context

        seen = {}
        import revenue_os.ecosystem.affiliate_discovery as AD
        real_fn = AD.discover_offer_candidates

        def spy(*a, **k):
            seen["in_ctx"] = in_autonomous_context()
            return real_fn(*a, **k)

        with mock.patch.object(AD, "discover_offer_candidates", side_effect=spy):
            self._night()
        self.assertTrue(seen["in_ctx"])

    def test_discover_offers_false_skips_the_step(self):
        out = self._night(discover_offers=False)
        self.assertIsNone(out["offer_discovery"])
        self.assertEqual(self._candidates(), [])


class FirewallsUntouched(unittest.TestCase):
    def test_no_new_action_class_was_added(self):
        from revenue_os.action_class import ActionClass

        self.assertEqual(
            [c.value for c in ActionClass],
            ["SAFE_AUTONOMOUS", "MONEY_APPROVAL_REQUIRED", "IDENTITY_APPROVAL_REQUIRED",
             "LEGAL_APPROVAL_REQUIRED", "SAFETY_BLOCKED"])

    def test_discovery_module_makes_no_network_call_and_no_auto_join(self):
        import revenue_os.ecosystem.affiliate_discovery as AD

        src = Path(AD.__file__).read_text(encoding="utf-8")
        for banned in ("import urllib", "import requests", "import http",
                       "import socket", "def search(", "join_program", "apply_to_program"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
