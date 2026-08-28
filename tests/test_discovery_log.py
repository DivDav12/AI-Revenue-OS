import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.discovery_log import DiscoveryLog
from revenue_os.filtering import is_relevant
from revenue_os.sources import FilteredSource, RawSignal, StaticSource
from revenue_os.store import CandidateStore
from revenue_os.workflow import run_discovery_cycle

_HIGH = RawSignal(title="automation automate no-code api saas platform revenue marketplace")
_LOW = RawSignal(title="a plain note about something entirely unrelated to money")
_NOISE = RawSignal(title="My weekend raytracer experiment written in Rust for fun")


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class DiscoveryLogTests(unittest.TestCase):
    def test_round_trip_and_latest(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "discovery_runs.json"
            log = DiscoveryLog.load(path)
            self.assertIsNone(log.latest())
            log.add({"ts": "t1", "kept": 1})
            log.add({"ts": "t2", "kept": 5})
            log.save()

            reloaded = DiscoveryLog.load(path)
            self.assertEqual(len(reloaded.entries()), 2)
            self.assertEqual(reloaded.latest()["ts"], "t2")

    def test_corrupt_file_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "discovery_runs.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                DiscoveryLog.load(path)


class RunCycleLoggingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_one_entry_per_run_with_counts(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(StaticSource([_HIGH, _LOW]), store, shortlist_n=1, log=log)

        entries = DiscoveryLog.load(self.d / "discovery_runs.json").entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["fetched"], 2)
        self.assertEqual(e["evaluated"], 2)
        self.assertEqual(e["kept"], 2)
        self.assertEqual(e["new"], 2)
        self.assertEqual(e["refreshed"], 0)
        self.assertEqual(e["shortlisted"], 1)
        self.assertEqual(e["filtered_out"], 0)
        self.assertEqual(e["source"], "static")

    def test_second_run_is_all_refreshed(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(StaticSource([_HIGH, _LOW]), store, log=log)
        run_discovery_cycle(StaticSource([_HIGH, _LOW]), store, log=log)

        entries = DiscoveryLog.load(self.d / "discovery_runs.json").entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["new"], 0)
        self.assertEqual(entries[1]["refreshed"], entries[1]["kept"])

    def test_filtered_out_recorded(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        source = FilteredSource(StaticSource([_HIGH, _NOISE, _NOISE]), is_relevant)
        run_discovery_cycle(source, store, log=log)

        e = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertEqual(e["filtered_out"], 2)
        self.assertEqual(e["fetched"], 1)
        self.assertEqual(e["source"], "static")

    def test_dropped_below_score_recorded(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(
            StaticSource([_HIGH, _LOW]), store, min_score=3.0, log=log
        )
        e = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertGreaterEqual(e["dropped_below_score"], 1)
        self.assertEqual(e["kept"], e["evaluated"] - e["dropped_below_score"])

    def test_no_log_means_no_file(self):
        store = CandidateStore.load(self.d / "candidates.json")
        run_discovery_cycle(StaticSource([_HIGH]), store)
        self.assertFalse((self.d / "discovery_runs.json").exists())


class DiscoveryLogCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_run_creates_log_and_report_shows_it(self):
        code, _ = _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertEqual(code, 0)
        path = Path(self.data) / "discovery_runs.json"
        self.assertTrue(path.exists())
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

        code, out = _run(["report", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("LAST DISCOVERY", out)
        self.assertIn("source=static", out)

    def test_report_without_runs_states_none(self):
        code, out = _run(["report", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("LAST DISCOVERY", out)
        self.assertIn("(no discovery run recorded)", out)

    def test_dashboard_shows_last_discovery(self):
        _run(["run", "--source", "static", "--data-dir", self.data])
        code, _ = _run(["dashboard", "--data-dir", self.data])
        self.assertEqual(code, 0)
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Last discovery", html)
        self.assertIn("filtered_out", html)

    def test_dashboard_without_runs_renders(self):
        code, _ = _run(["dashboard", "--data-dir", self.data])
        self.assertEqual(code, 0)
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("No discovery run recorded.", html)


if __name__ == "__main__":
    unittest.main()
