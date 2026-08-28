import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.dashboard import render_html
from revenue_os.report import pipeline_report
from revenue_os.revenue import RevenueLedger, mark_launched, record_payment
from revenue_os.spend import SpendLedger
from revenue_os.store import Candidate, CandidateStore

_FIXED_TS = "2026-08-28T00:00:00+00:00"


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _report(store, d):
    return pipeline_report(
        store,
        RevenueLedger(Path(d) / "revenue.json"),
        SpendLedger(Path(d) / "spend.json"),
    )


class RenderHtmlTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.store = CandidateStore(Path(self.d) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_empty_store_renders(self):
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("No candidates.", html)
        self.assertIn("Nothing awaiting a human.", html)
        self.assertIn(_FIXED_TS, html)

    def test_populated_store_shows_counts_and_names(self):
        self.store.put(Candidate(name="alpha", status="shortlisted", total=3.1, verdict="hold"))
        self.store.put(Candidate(name="beta", status="investigating", total=2.0, verdict="hold"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("alpha", html)
        self.assertIn("beta", html)
        self.assertIn("approve or reject", html)
        self.assertIn("record validation outcome", html)

    def test_no_external_resource_references(self):
        self.store.put(Candidate(name="alpha", status="discovered"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"src\s*=")
        self.assertNotIn("<script", html)

    def test_candidate_text_is_html_escaped(self):
        self.store.put(
            Candidate(name="<script>alert(1)</script>", status="discovered")
        )
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_deterministic_given_fixed_timestamp(self):
        self.store.put(Candidate(name="alpha", status="shortlisted", total=3.1))
        r = _report(self.store, self.d)
        self.assertEqual(render_html(r, _FIXED_TS), render_html(r, _FIXED_TS))

    def test_candidate_breakdown_in_details_block(self):
        from revenue_os.opportunity import CRITERIA

        breakdown = {name: 3.0 for name in CRITERIA}
        breakdown["demand"] = 1.0
        self.store.put(
            Candidate(name="alpha", status="shortlisted", total=2.75,
                      verdict="hold", breakdown=breakdown)
        )
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("<details>", html)
        self.assertIn("<summary>", html)
        for name in CRITERIA:
            self.assertIn(name, html)
        self.assertNotIn("<script", html)
        self.assertNotIn("https://", html)

    def test_candidate_with_empty_breakdown_renders(self):
        self.store.put(Candidate(name="alpha", status="discovered"))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("No score breakdown.", html)
        self.assertIn("No rationale.", html)
        self.assertIn("[keyword]", html)

    def test_candidate_shows_llm_source_and_rationale(self):
        self.store.put(Candidate(
            name="alpha", status="shortlisted", total=3.0, verdict="hold",
            estimate_source="llm", rationale="niche but real demand",
        ))
        html = render_html(_report(self.store, self.d), _FIXED_TS)
        self.assertIn("[llm]", html)
        self.assertIn("niche but real demand", html)
        self.assertNotIn("<script", html)

    def test_roi_table_appears_after_payment(self):
        self.store.put(Candidate(name="alpha", status="validated"))
        mark_launched(self.store, "alpha", actor="o")
        rev = RevenueLedger(Path(self.d) / "revenue.json")
        record_payment(self.store, rev, "alpha", 29.0, actor="o")
        report = pipeline_report(
            self.store, rev, SpendLedger(Path(self.d) / "spend.json")
        )
        html = render_html(report, _FIXED_TS)
        self.assertIn("29.0", html)


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_writes_default_path_and_leaves_stores_untouched(self):
        _run(["run", "--source", "static", "--data-dir", self.data])
        before = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        code, out = _run(["dashboard", "--data-dir", self.data])
        after = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        html_path = Path(self.data) / "dashboard.html"
        self.assertTrue(html_path.exists())
        self.assertIn("<!doctype html>", html_path.read_text(encoding="utf-8"))
        self.assertIn("dashboard written", out)
        self.assertEqual(before, after)

    def test_out_flag_honored_from_clean_data_dir(self):
        target = Path(self.data) / "nested" / "page.html"
        code, _ = _run(
            ["dashboard", "--data-dir", self.data, "--out", str(target)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        self.assertNotIn("https://", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
