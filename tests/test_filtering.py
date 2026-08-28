import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.filtering import is_relevant
from revenue_os.sources import FilteredSource, RawSignal, StaticSource
from revenue_os.store import CandidateStore
from revenue_os.workflow import run_discovery_cycle


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


_HIGH = RawSignal(title="automation automate no-code api saas platform revenue marketplace")
_LOW = RawSignal(title="a plain note about something entirely unrelated")
_NOISE = RawSignal(title="My weekend raytracer experiment written in Rust for fun")


class IsRelevantTests(unittest.TestCase):
    def test_commercial_keyword_makes_it_relevant(self):
        self.assertTrue(is_relevant(RawSignal(title="A SaaS tool for pricing teams")))
        self.assertTrue(
            is_relevant(
                RawSignal(title="Show HN: something", text="we charge customers monthly")
            )
        )

    def test_pure_noise_is_not_relevant(self):
        self.assertFalse(is_relevant(_NOISE))

    def test_too_short_is_not_relevant(self):
        self.assertFalse(is_relevant(RawSignal(title="saas")))  # has keyword but < 20

    def test_deterministic(self):
        self.assertEqual(is_relevant(_NOISE), is_relevant(_NOISE))


class FilteredSourceTests(unittest.TestCase):
    def test_keeps_only_matching_signals(self):
        src = FilteredSource(StaticSource([_HIGH, _NOISE]), is_relevant)
        got = src.fetch(10)
        self.assertEqual([s.title for s in got], [_HIGH.title])

    def test_empty_when_none_match(self):
        src = FilteredSource(StaticSource([_NOISE]), is_relevant)
        self.assertEqual(src.fetch(10), [])

    def test_respects_limit(self):
        src = FilteredSource(StaticSource([_HIGH, _HIGH, _HIGH]), is_relevant)
        self.assertEqual(len(src.fetch(2)), 2)


class MinScoreGateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "candidates.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_default_persists_all(self):
        store = CandidateStore.load(self.path)
        run_discovery_cycle(StaticSource([_HIGH, _LOW]), store, shortlist_n=1)
        self.assertEqual(len(store.all()), 2)

    def test_min_score_drops_low_candidates(self):
        store = CandidateStore.load(self.path)
        run_discovery_cycle(
            StaticSource([_HIGH, _LOW]), store, shortlist_n=1, min_score=3.0
        )
        names = [c.name for c in store.all()]
        self.assertEqual(len(names), 1)
        self.assertGreaterEqual(store.all()[0].total, 3.0)


class CliFilterTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def _count(self) -> int:
        return len(CandidateStore.load(Path(self.data) / "candidates.json").all())

    def test_run_filter_drops_noise_signal(self):
        # static sample has 4 signals; "A weekend project with no obvious
        # business model / Just something I made for fun." has no commercial term.
        code, _ = _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertEqual(self._count(), 4)

        self._dir.cleanup()
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name
        code, _ = _run(
            ["run", "--source", "static", "--filter", "--data-dir", self.data]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._count(), 3)

    def test_run_min_score_gates_candidates(self):
        code, _ = _run(
            ["run", "--source", "static", "--min-score", "3.1", "--data-dir", self.data]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._count(), 1)

    def test_run_filter_static_still_yields_shortlist(self):
        _run(["run", "--source", "static", "--filter", "--data-dir", self.data])
        store = CandidateStore.load(Path(self.data) / "candidates.json")
        self.assertTrue(any(c.status == "shortlisted" for c in store.all()))


if __name__ == "__main__":
    unittest.main()
