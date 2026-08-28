import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.sources import (
    HackerNewsSource,
    LocalFileSource,
    RawSignal,
    StaticSource,
    build_source,
)
from revenue_os.store import CandidateStore
from revenue_os.workflow import run_discovery_cycle


class _FailingSource:
    def fetch(self, limit):
        raise RuntimeError("network down")


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class BuildSourceTests(unittest.TestCase):
    def test_static_and_hn_and_unknown(self):
        self.assertIsInstance(build_source("static"), StaticSource)
        self.assertIsInstance(build_source("hn"), HackerNewsSource)
        with self.assertRaises(ValueError):
            build_source("reddit")

    def test_file_source_requires_existing_path(self):
        with self.assertRaises(ValueError):
            build_source("file")
        with self.assertRaises(ValueError):
            build_source("file", "no-such-file.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "leads.json"
            path.write_text(json.dumps([{"title": "A lead"}]), encoding="utf-8")
            self.assertIsInstance(build_source("file", str(path)), LocalFileSource)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_run_static_executes_cycle_and_prints_report(self):
        code, out = _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("PIPELINE STATUS", out)
        self.assertIn("ACTION QUEUE", out)
        self.assertTrue((Path(self.data) / "candidates.json").exists())

    def test_run_file_source_executes_cycle(self):
        leads = Path(self.data) / "leads.json"
        leads.write_text(
            json.dumps(
                [
                    {"title": "Paid API for invoice automation", "text": "SaaS pricing"},
                    {"title": "Marketplace for freelance consulting", "text": "b2b"},
                ]
            ),
            encoding="utf-8",
        )
        code, out = _run(
            ["run", "--source", "file", "--source-path", str(leads),
             "--data-dir", self.data]
        )
        self.assertEqual(code, 0)
        self.assertIn("PIPELINE STATUS", out)
        self.assertTrue((Path(self.data) / "candidates.json").exists())

    def test_run_file_source_with_filter_and_min_score(self):
        leads = Path(self.data) / "leads.json"
        leads.write_text(
            json.dumps(
                [
                    {"title": "Paid SaaS platform for customer invoicing"},
                    {"title": "a weekend toy with no business model"},
                ]
            ),
            encoding="utf-8",
        )
        code, _ = _run(
            ["run", "--source", "file", "--source-path", str(leads),
             "--filter", "--min-score", "0.1", "--data-dir", self.data]
        )
        self.assertEqual(code, 0)
        store = CandidateStore.load(Path(self.data) / "candidates.json")
        names = [c.name for c in store.all()]
        self.assertTrue(any("invoic" in n for n in names))
        self.assertFalse(any("weekend-toy" in n for n in names))

    def test_run_file_source_missing_path_exits_1(self):
        code, _ = _run(["run", "--source", "file", "--data-dir", self.data])
        self.assertEqual(code, 1)

    def test_run_file_source_absent_file_exits_1(self):
        code, _ = _run(
            ["run", "--source", "file", "--source-path", "nope.json",
             "--data-dir", self.data]
        )
        self.assertEqual(code, 1)
        self.assertFalse((Path(self.data) / "candidates.json").exists())

    def test_run_file_source_untitled_entry_exits_1(self):
        leads = Path(self.data) / "leads.json"
        leads.write_text(
            json.dumps([{"title": "ok"}, {"url": "http://x"}]), encoding="utf-8"
        )
        code, _ = _run(
            ["run", "--source", "file", "--source-path", str(leads),
             "--data-dir", self.data]
        )
        self.assertEqual(code, 1)

    def test_report_only_does_not_change_store(self):
        _run(["run", "--source", "static", "--data-dir", self.data])
        before = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        code, out = _run(["report", "--data-dir", self.data])
        after = (Path(self.data) / "candidates.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("PIPELINE STATUS", out)
        self.assertEqual(before, after)

    def test_no_subcommand_defaults_to_report(self):
        code, out = _run(["--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("PIPELINE STATUS", out)

    def test_demo_runs(self):
        code, out = _run(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("ROI summary", out)

    def test_data_dir_env_var_honored(self):
        prev = os.environ.get("REVENUE_OS_DATA_DIR")
        os.environ["REVENUE_OS_DATA_DIR"] = self.data
        try:
            code, _ = _run(["run"])
        finally:
            if prev is None:
                os.environ.pop("REVENUE_OS_DATA_DIR", None)
            else:
                os.environ["REVENUE_OS_DATA_DIR"] = prev
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.data) / "candidates.json").exists())


class FailingSourceTests(unittest.TestCase):
    def test_cycle_survives_source_failure(self):
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore.load(Path(d) / "candidates.json")
            result = run_discovery_cycle(_FailingSource(), store, shortlist_n=3)
            self.assertEqual(result, [])


class LiveHackerNewsTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("REVENUE_OS_NET_TESTS"), "network tests disabled"
    )
    def test_hn_fetch_returns_signals(self):
        signals = HackerNewsSource().fetch(3)
        self.assertGreaterEqual(len(signals), 1)
        self.assertIsInstance(signals[0], RawSignal)


if __name__ == "__main__":
    unittest.main()
