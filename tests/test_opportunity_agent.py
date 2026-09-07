"""Opportunity Agent: real public-signal discovery -> research -> score ->
select. Uses the curated-file source (`LocalSignalSource`, origin="real",
fully offline) instead of the network HN/RemoteOK sources so tests never
touch the network - the agent's contract is source-agnostic, and "file" is
one of its documented real sources, not a mock.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import affiliate_sources
from revenue_os.ecosystem.affiliate_model import AffiliateOffer, AffiliateOfferStore
from revenue_os.ecosystem import model
from revenue_os.messages import Task
from revenue_os.opportunity_agent import OpportunityAgent, discover_real_signals, select_opportunity, usable_offers


def _offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "network": "generic_saas_program",
        "program_name": "Acme Hosting Affiliates",
        "product_name": "Acme Cloud Hosting",
        "product_url": "https://acme.example/hosting?ref=base",
        "product_price": 200.0,
        "currency": "EUR",
        "commission_kind": "recurring_percent",
        "commission_rate": 0.30,
        "commission_evidence": ["Acme Affiliates dashboard: '30% recurring commission'"],
        "cookie_duration_days": 60,
        "evidence": ["Acme Cloud Hosting pricing page: EUR 200/month, 99.9% uptime SLA"],
        "category": "hosting",
        "keywords": ["hosting", "server", "cloud", "vps"],
        "human_confirmed_joined": True,
        "tracking_param": "ref",
    }
    base.update(overrides)
    return base


def _write_signal_file(path: Path, *, title: str, text: str) -> None:
    path.write_text(json.dumps([{
        "title": title, "text": text, "url": "https://example.com/t/1", "external_id": "sig-1",
    }]), encoding="utf-8")


class OpportunityAgentUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.signal_path = self.d / "signals.json"

    def test_no_offers_at_all_rejects_with_human_setup_reason(self):
        result = select_opportunity(self.d)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("HUMAN SETUP REQUIRED", result["reason"])
        self.assertIsNone(result["selected"])

    def test_human_setup_required_offer_is_never_usable_or_selected(self):
        store = AffiliateOfferStore.load(self.d)
        store.upsert(AffiliateOffer(
            offer_id="aff-pending", network="amazon_associates",
            program_name="Amazon Associates", product_name="Some Product",
            status=model.POLICY_HUMAN_SETUP_REQUIRED, keywords=("hosting",)))
        store.save()
        self.assertEqual(usable_offers(self.d), [])
        result = select_opportunity(self.d)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("HUMAN SETUP REQUIRED", result["reason"])

    def test_real_discovery_via_curated_file_then_selection(self):
        _write_signal_file(self.signal_path,
                           title="Is there a tool for cheap VPS hosting for a side project?",
                           text="I need cloud hosting that does not cost a fortune, ideally a VPS.")
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())

        report = discover_real_signals(self.d, source_names=("file",),
                                       source_kwargs={"file": {"path": str(self.signal_path)}})
        self.assertGreaterEqual(report.new, 1, report.to_dict())

        result = select_opportunity(self.d)
        self.assertEqual(result["status"], "SELECTED", result)
        self.assertTrue(result["selected"]["opportunity_id"])
        self.assertGreater(result["selected"]["score"], 0.0)

    def test_no_selection_when_signal_does_not_match_the_offer(self):
        _write_signal_file(self.signal_path,
                           title="Best recipe for sourdough bread",
                           text="Looking for baking tips and a good flour brand to try this weekend.")
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
        discover_real_signals(self.d, source_names=("file",),
                              source_kwargs={"file": {"path": str(self.signal_path)}})
        result = select_opportunity(self.d)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("clears the content bar", result["reason"])

    def test_idempotent_rediscovery_does_not_duplicate_records(self):
        _write_signal_file(self.signal_path,
                           title="Is there a tool for cheap VPS hosting?",
                           text="Need cloud hosting, ideally a VPS.")
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
        r1 = discover_real_signals(self.d, source_names=("file",),
                                   source_kwargs={"file": {"path": str(self.signal_path)}})
        r2 = discover_real_signals(self.d, source_names=("file",),
                                   source_kwargs={"file": {"path": str(self.signal_path)}})
        self.assertGreaterEqual(r1.new, 1)
        self.assertEqual(r2.new, 0)  # second run refreshes, never duplicates
        self.assertGreaterEqual(r2.refreshed, 1)


class OpportunityAgentTaskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.signal_path = self.d / "signals.json"

    def test_run_end_to_end_via_task_payload(self):
        _write_signal_file(self.signal_path,
                           title="Is there a tool for cheap VPS hosting for a side project?",
                           text="I need cloud hosting that does not cost a fortune, ideally a VPS.")
        affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())

        agent = OpportunityAgent(name="opportunity_agent")
        task = Task(objective="discover and select", payload={
            "data_dir": str(self.d), "source_names": ("file",),
            "source_kwargs": {"file": {"path": str(self.signal_path)}},
        })
        result = agent.run(task)
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(result.output["selection"]["status"], "SELECTED")

    def test_run_errors_without_data_dir(self):
        agent = OpportunityAgent(name="opportunity_agent")
        result = agent.run(Task(objective="x", payload={}))
        self.assertEqual(result.status, "error")

    def test_run_isolates_a_bad_source_without_crashing(self):
        agent = OpportunityAgent(name="opportunity_agent")
        task = Task(objective="discover", payload={
            "data_dir": str(self.d), "source_names": ("file",),
            "source_kwargs": {"file": {"path": str(self.d / "does-not-exist.json")}},
        })
        result = agent.run(task)
        # a missing curated file is a construction-time error inside
        # build_source() itself, not a per-source discovery failure -
        # the agent must still fail predictably (status=error), never crash
        # the process or fabricate a result.
        self.assertEqual(result.status, "error")
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
