"""Pinterest pin drafting - the organic-distribution layer added on top of
the affiliate asset pipeline (business-model research phase 1/2, see
docs/BUSINESS_MODEL_RESEARCH.md). No network, no LLM call: pure template
rendering + local JSON persistence, exactly like affiliate_assets.py.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import action_class
from revenue_os.ecosystem.affiliate_model import AffiliateAsset, PIN_DRAFT, PIN_POSTED, PIN_SKIPPED
from revenue_os.ecosystem.pinterest_pins import (
    PIN_DISCLOSURE,
    PinDraftError,
    draft_pin,
    mark_posted,
    pending_pins,
)
from revenue_os.messages import Task
from revenue_os.pinterest_agent import PinterestDistributorAgent


def _asset(**overrides) -> AffiliateAsset:
    defaults = dict(
        asset_id="asset-1", opportunity_id="opp-1", offer_id="aff-1",
        title="USB-Mikrofon für Streaming", guide_title="",
        live_url="https://example.github.io/mikrofone/usb-mic/",
    )
    defaults.update(overrides)
    return AffiliateAsset(**defaults)


class DraftPinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_drafts_from_a_deployed_asset(self):
        pin = draft_pin(self.data_dir, asset=_asset(), product_name="RØDE NT-USB Mini",
                        category_label="Mikrofone")
        self.assertTrue(pin.pin_id)
        self.assertEqual(pin.asset_id, "asset-1")
        self.assertEqual(pin.opportunity_id, "opp-1")
        self.assertEqual(pin.dest_url, "https://example.github.io/mikrofone/usb-mic/")
        self.assertIn("RØDE NT-USB Mini", pin.title)
        self.assertIn(PIN_DISCLOSURE, pin.description)
        self.assertEqual(pin.status, PIN_DRAFT)
        self.assertLessEqual(len(pin.title), 100)
        self.assertLessEqual(len(pin.description), 500)
        self.assertLessEqual(len(pin.alt_text), 500)

    def test_refuses_an_asset_with_no_live_url(self):
        with self.assertRaises(PinDraftError):
            draft_pin(self.data_dir, asset=_asset(live_url=""), product_name="X")

    def test_idempotent_per_asset(self):
        first = draft_pin(self.data_dir, asset=_asset(), product_name="RØDE NT-USB Mini")
        second = draft_pin(self.data_dir, asset=_asset(), product_name="a different name entirely")
        self.assertEqual(first.pin_id, second.pin_id)
        raw = json.loads((self.data_dir / "pinterest_pins.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw), 1)

    def test_category_falls_back_to_board_suggestion(self):
        pin = draft_pin(self.data_dir, asset=_asset(), product_name="RØDE NT-USB Mini",
                        category_label="Mikrofone")
        self.assertEqual(pin.board_suggestion, "Mikrofone")

    def test_mark_posted_records_the_human_action(self):
        pin = draft_pin(self.data_dir, asset=_asset(), product_name="RØDE NT-USB Mini")
        updated = mark_posted(self.data_dir, pin.pin_id, now_iso="2026-09-07T00:00:00Z")
        self.assertEqual(updated.status, PIN_POSTED)
        self.assertEqual(updated.posted_at, "2026-09-07T00:00:00Z")
        self.assertEqual(pending_pins(self.data_dir), [])

    def test_mark_skipped_with_a_note(self):
        pin = draft_pin(self.data_dir, asset=_asset(), product_name="RØDE NT-USB Mini")
        updated = mark_posted(self.data_dir, pin.pin_id, status=PIN_SKIPPED, note="off-topic board")
        self.assertEqual(updated.status, PIN_SKIPPED)
        self.assertEqual(updated.note, "off-topic board")

    def test_mark_posted_rejects_unknown_pin_or_status(self):
        with self.assertRaises(ValueError):
            mark_posted(self.data_dir, "no-such-id")
        pin = draft_pin(self.data_dir, asset=_asset(), product_name="X")
        with self.assertRaises(ValueError):
            mark_posted(self.data_dir, pin.pin_id, status="bogus")

    def test_pending_pins_excludes_posted_and_skipped(self):
        a1 = draft_pin(self.data_dir, asset=_asset(), product_name="A")
        draft_pin(self.data_dir, asset=_asset(asset_id="asset-2"), product_name="B")
        mark_posted(self.data_dir, a1.pin_id)
        pending = pending_pins(self.data_dir)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].asset_id, "asset-2")


class SafetyGateTests(unittest.TestCase):
    def test_drafting_is_safe_autonomous(self):
        verdict = action_class.classify("prepare_pinterest_pin")
        self.assertTrue(verdict.autonomous)

    def test_posting_to_pinterest_is_not_permitted_automation(self):
        # Pinterest is a third-party platform, not an owned channel - the
        # fleet drafts, a human posts (fail closed, same as every other
        # community/social platform).
        self.assertFalse(action_class.posting_permitted("pinterest"))


class PinterestDistributorAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.agent = PinterestDistributorAgent(name="pinterest_distributor")

    def test_run_drafts_a_pin(self):
        task = Task(objective="draft a pin", payload={
            "data_dir": str(self.data_dir),
            "asset": _asset().to_dict(),
            "product_name": "RØDE NT-USB Mini",
            "category_label": "Mikrofone",
        })
        result = self.agent.run(task)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.output.get("pin_id"))
        self.assertEqual(result.output.get("dest_url"), _asset().live_url)

    def test_run_errors_without_an_asset(self):
        task = Task(objective="draft a pin", payload={"data_dir": str(self.data_dir)})
        result = self.agent.run(task)
        self.assertEqual(result.status, "error")

    def test_run_errors_when_asset_has_no_live_url(self):
        task = Task(objective="draft a pin", payload={
            "data_dir": str(self.data_dir),
            "asset": _asset(live_url="").to_dict(),
        })
        result = self.agent.run(task)
        self.assertEqual(result.status, "error")
        self.assertIn("live_url", result.error)


if __name__ == "__main__":
    unittest.main()
