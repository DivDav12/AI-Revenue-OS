import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.sources import HackerNewsSource, RawSignal, StaticSource, build_source
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
