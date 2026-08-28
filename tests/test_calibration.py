import tempfile
import unittest
from pathlib import Path

from revenue_os.calibration import calibration_weights
from revenue_os.opportunity import CRITERIA
from revenue_os.store import Candidate, CandidateStore


def _bd(**over):
    b = {c: 2.5 for c in CRITERIA}
    b.update(over)
    return b


def _cand(name, outcome, breakdown):
    return Candidate(
        name=name, status="validated" if outcome == "validated" else "rejected",
        total=round(sum(breakdown.values()) / len(breakdown), 2),
        breakdown=breakdown,
        outcome={"outcome": outcome, "metric_value": "m"},
    )


class CalibrationWeightsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def _seed(self, n_validated, n_rejected, demand_split=True):
        for i in range(n_validated):
            self.store.put(_cand(f"v{i}", "validated",
                                 _bd(demand=4.5 if demand_split else 2.5)))
        for i in range(n_rejected):
            self.store.put(_cand(f"r{i}", "rejected",
                                 _bd(demand=1.0 if demand_split else 2.5)))

    def test_none_below_min_outcomes(self):
        self._seed(3, 3)
        self.assertIsNone(calibration_weights(self.store))

    def test_none_with_single_class(self):
        self._seed(10, 0)
        self.assertIsNone(calibration_weights(self.store))

    def test_weights_average_one_and_clamped(self):
        self._seed(6, 6)
        w = calibration_weights(self.store)
        self.assertIsNotNone(w)
        self.assertEqual(set(w), set(CRITERIA))
        self.assertAlmostEqual(sum(w.values()) / len(w), 1.0, places=2)
        for v in w.values():
            self.assertGreater(v, 0.0)
            self.assertLess(v, 3.0)

    def test_predictive_criterion_gets_the_largest_weight(self):
        self._seed(6, 6)
        w = calibration_weights(self.store)
        self.assertEqual(max(w, key=w.get), "demand")
        # a criterion equal on both sides stays near 1.0
        self.assertAlmostEqual(w["scalability"], 1.0, delta=0.15)

    def test_no_signal_gives_flat_weights(self):
        self._seed(6, 6, demand_split=False)  # identical breakdowns
        w = calibration_weights(self.store)
        for v in w.values():
            self.assertAlmostEqual(v, 1.0, places=4)

    def test_min_outcomes_override(self):
        self._seed(1, 1)
        self.assertIsNone(calibration_weights(self.store))
        self.assertIsNotNone(calibration_weights(self.store, min_outcomes=2))


class RunDiscoveryCalibratedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _store_with_outcomes(self):
        from revenue_os.sources import RawSignal, StaticSource

        store = CandidateStore.load(self.d / "candidates.json")
        for i in range(6):
            store.put(_cand(f"v{i}", "validated", _bd(demand=4.5)))
            store.put(_cand(f"r{i}", "rejected", _bd(demand=1.0)))
        store.save()
        src = StaticSource([
            RawSignal(title="high demand paid saas platform for customers"),
            RawSignal(title="a quiet plain note"),
        ])
        return store, src

    def test_uncalibrated_run_records_flags_false(self):
        from revenue_os.discovery_log import DiscoveryLog
        from revenue_os.workflow import run_discovery_cycle

        store, src = self._store_with_outcomes()
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(src, store, log=log)
        e = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertFalse(e["calibrated"])
        self.assertFalse(e["weights_applied"])

    def test_calibrated_run_applies_weights(self):
        from revenue_os.discovery_log import DiscoveryLog
        from revenue_os.workflow import run_discovery_cycle

        store, src = self._store_with_outcomes()
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(src, store, log=log, calibrated=True)
        e = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertTrue(e["calibrated"])
        self.assertTrue(e["weights_applied"])

    def test_calibrated_run_without_data_is_a_noop(self):
        from revenue_os.discovery_log import DiscoveryLog
        from revenue_os.sources import RawSignal, StaticSource
        from revenue_os.workflow import run_discovery_cycle

        store = CandidateStore.load(self.d / "candidates.json")
        src = StaticSource([RawSignal(title="a paid saas tool for teams")])
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        run_discovery_cycle(src, store, log=log, calibrated=True)
        e = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertTrue(e["calibrated"])
        self.assertFalse(e["weights_applied"])


if __name__ == "__main__":
    unittest.main()
