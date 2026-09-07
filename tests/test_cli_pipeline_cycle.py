"""CLI test for `pipeline-cycle` - the schedulable entry point for the
whole affiliate/content pipeline. No real GitHub credential is present in
the test environment, so this exercises the real (non-injected)
credential-gated path end to end and must report a human action, never
fabricate a deploy or crash.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.ecosystem import affiliate_sources


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _offer_json(**overrides) -> dict:
    base = {
        "schema_version": 1, "network": "generic_saas_program",
        "program_name": "Acme Hosting Affiliates", "product_name": "Acme Cloud Hosting",
        "product_url": "https://acme.example/hosting?ref=base", "product_price": 200.0,
        "currency": "EUR", "commission_kind": "recurring_percent", "commission_rate": 0.30,
        "commission_evidence": ["Acme dashboard: 30% recurring commission"],
        "cookie_duration_days": 60,
        "evidence": ["Acme Cloud Hosting pricing page: EUR 200/month, 99.9% uptime SLA"],
        "category": "hosting", "keywords": ["hosting", "server", "cloud", "vps"],
        "human_confirmed_joined": True, "tracking_param": "ref",
    }
    base.update(overrides)
    return base


class PipelineCycleCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.signal_path = self.d / "signals.json"
        self.signal_path.write_text(json.dumps([{
            "title": "Is there a tool for cheap VPS hosting for a side project?",
            "text": "I need cloud hosting that does not cost a fortune, ideally a VPS.",
            "url": "https://example.com/1", "external_id": "sig-1",
        }]), encoding="utf-8")

    def test_no_offer_reports_human_setup_required(self):
        code, out = _run(["pipeline-cycle", "--data-dir", str(self.d),
                          "--source", "file", "--source-path", str(self.signal_path)])
        self.assertEqual(code, 0)
        self.assertIn("HUMAN SETUP REQUIRED", out)

    def test_with_offer_but_no_github_credential_reports_deployment_human_action(self):
        import os
        backup = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GITHUB_PAGES_REPO")}
        try:
            affiliate_sources.ingest_affiliate_offer(self.d, _offer_json())
            code, out = _run(["pipeline-cycle", "--data-dir", str(self.d),
                              "--source", "file", "--source-path", str(self.signal_path)])
            self.assertEqual(code, 0)
            self.assertIn("SELECTED", out)
            self.assertIn("DEPLOYMENT", out)
            self.assertIn("GITHUB_TOKEN", out)
            # never claims a live URL when nothing was actually deployed
            self.assertNotIn("live at", out)
        finally:
            for k, v in backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_json_output_is_valid_json(self):
        code, out = _run(["pipeline-cycle", "--data-dir", str(self.d),
                          "--source", "file", "--source-path", str(self.signal_path),
                          "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIn("selection", parsed)

    def test_bad_source_path_fails_predictably_not_silently(self):
        # cli.main() catches FileNotFoundError/ValueError at the top level
        # and returns 1 (error printed to stderr) - it never silently
        # succeeds or fabricates a result for a missing signal file.
        code, out = _run(["pipeline-cycle", "--data-dir", str(self.d), "--source", "file",
                          "--source-path", str(self.d / "missing.json")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
