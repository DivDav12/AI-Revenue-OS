"""Digital-product secondary path - must never block or be required by
the affiliate pipeline (mandatory correction #3). Deterministic, $0,
template-only; every platform defaults to HUMAN_SETUP_REQUIRED."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from revenue_os.ecosystem import digital_products as dp
from revenue_os.ecosystem import model


class PlatformPolicyTests(unittest.TestCase):
    def test_gumroad_and_payhip_default_to_human_setup_required(self):
        for platform in (dp.PLATFORM_GUMROAD, dp.PLATFORM_PAYHIP):
            policy = dp.platform_policy(platform)
            self.assertEqual(policy["status"], model.POLICY_HUMAN_SETUP_REQUIRED)
            self.assertTrue(policy["setup_steps"])

    def test_unknown_platform_fails_closed(self):
        policy = dp.platform_policy("some-new-platform")
        self.assertEqual(policy["status"], model.POLICY_HUMAN_SETUP_REQUIRED)

    def test_no_platform_configured_by_default(self):
        import os
        env_backup = {k: os.environ.pop(k, None) for k in
                     ("GUMROAD_ACCOUNT_CONFIRMED", "PAYHIP_ACCOUNT_CONFIRMED")}
        try:
            self.assertFalse(dp.any_platform_configured())
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_no_api_credential_is_ever_read_or_requested(self):
        # structural honesty check: no platform policy note claims a live
        # credential/integration - every note says uploads are human.
        for platform in (dp.PLATFORM_GUMROAD, dp.PLATFORM_PAYHIP):
            note = dp.platform_policy(platform)["note"]
            self.assertIn("no", note.lower())
            self.assertIn("credential", note.lower())


class GenerateProductDraftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def test_generates_a_deterministic_checklist_from_real_evidence(self):
        draft = dp.generate_product_draft(
            self.d, opportunity_id="opp-1", topic="USB-Mikrofone für Streaming",
            evidence=("Rauscharm laut Herstellerangabe", "Plug-and-play über USB"),
            category="mikrofone", now_iso="t1")
        self.assertTrue(draft.product_id)
        self.assertIn("Rauscharm laut Herstellerangabe", draft.body_markdown)
        self.assertIn("Plug-and-play über USB", draft.body_markdown)
        self.assertIn("USB-Mikrofone für Streaming", draft.title)

    def test_never_invents_a_fact_beyond_supplied_evidence(self):
        draft = dp.generate_product_draft(
            self.d, opportunity_id="opp-2", topic="Sourdough Bread", evidence=())
        self.assertIn("keine weiteren belegten Punkte", draft.body_markdown)

    def test_idempotent_per_opportunity(self):
        first = dp.generate_product_draft(self.d, opportunity_id="opp-3", topic="A",
                                          evidence=("x",))
        second = dp.generate_product_draft(self.d, opportunity_id="opp-3", topic="different topic",
                                           evidence=("y",))
        self.assertEqual(first.product_id, second.product_id)
        self.assertEqual(len(dp.DigitalProductStore.load(self.d).all()), 1)

    def test_requires_no_network_no_llm_no_platform_configured(self):
        # generation must succeed with zero env vars set and zero network -
        # the parallel-path independence requirement.
        import os
        env_backup = {k: os.environ.pop(k, None) for k in
                     ("GUMROAD_ACCOUNT_CONFIRMED", "PAYHIP_ACCOUNT_CONFIRMED",
                      "ANTHROPIC_API_KEY")}
        try:
            draft = dp.generate_product_draft(self.d, opportunity_id="opp-4", topic="X",
                                              evidence=("fact",))
            self.assertTrue(draft.product_id)
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
