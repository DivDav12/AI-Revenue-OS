import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from revenue_os import cli
from revenue_os.llm_normalize import (
    CostCeilingExceeded,
    CostMeter,
    LlmNormalizer,
    build_client,
    estimate_cost_usd,
    to_opportunity_llm,
)
from revenue_os.opportunity import CRITERIA
from revenue_os.sources import RawSignal
from revenue_os.store import CandidateStore
from revenue_os.discovery_log import DiscoveryLog
from revenue_os.workflow import run_discovery_cycle

_GOOD = {**{c: 3.0 for c in CRITERIA}, "demand": 1.0, "rationale": "plausible niche tool"}


class _FakeUsage:
    def __init__(self, i=500, o=120):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _FakeBlock:
    type = "tool_use"
    name = "record_scores"

    def __init__(self, payload):
        self.input = payload


class _FakeResponse:
    def __init__(self, payload, usage):
        self.content = [_FakeBlock(payload)]
        self.usage = usage


class _FakeClient:
    def __init__(self, payload=None, usage=(500, 120)):
        self.payload = _GOOD if payload is None else payload
        self.usage = usage
        self.calls = 0
        self.last_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return _FakeResponse(self.payload, _FakeUsage(*self.usage))


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class ToOpportunityLlmTests(unittest.TestCase):
    def test_maps_scores_and_marks_source(self):
        client = _FakeClient()
        opp = to_opportunity_llm(RawSignal(title="A niche SaaS"), client=client)
        self.assertEqual(opp.estimate_source, "llm")
        self.assertEqual(opp.demand, 1.0)
        self.assertEqual(opp.automation_potential, 3.0)
        self.assertEqual(opp.rationale, "plausible niche tool")
        self.assertEqual(client.calls, 1)
        # no tools/loop; single forced tool call
        self.assertEqual(client.last_kwargs["tool_choice"]["name"], "record_scores")

    def test_rationale_truncated_to_280(self):
        payload = {**_GOOD, "rationale": "x" * 400}
        opp = to_opportunity_llm(
            RawSignal(title="t"), client=_FakeClient(payload=payload)
        )
        self.assertEqual(len(opp.rationale), 280)

    def test_out_of_range_score_raises(self):
        payload = {**_GOOD, "profit_potential": 9.0}
        with self.assertRaises(ValueError):
            to_opportunity_llm(RawSignal(title="t"), client=_FakeClient(payload=payload))

    def test_missing_key_raises(self):
        payload = {k: v for k, v in _GOOD.items() if k != "scalability"}
        with self.assertRaises(ValueError):
            to_opportunity_llm(RawSignal(title="t"), client=_FakeClient(payload=payload))

    def test_no_tool_call_raises(self):
        class _Empty(_FakeClient):
            def create(self, **kwargs):
                self.calls += 1
                r = _FakeResponse(self.payload, _FakeUsage())
                r.content = []
                return r

        with self.assertRaises(ValueError):
            to_opportunity_llm(RawSignal(title="t"), client=_Empty())


class CostTests(unittest.TestCase):
    def test_cost_meter_math_sonnet(self):
        meter = CostMeter("claude-sonnet-5")
        meter.add(_FakeUsage(500_000, 120_000))
        # 0.5 * 2.0 + 0.12 * 10.0 = 1.0 + 1.2
        self.assertEqual(meter.cost_usd, 2.2)

    def test_cost_meter_unknown_model_uses_fallback(self):
        meter = CostMeter("mystery")
        meter.add(_FakeUsage(1_000_000, 0))
        self.assertEqual(meter.cost_usd, 5.0)

    def test_estimate_scales_with_signal_count(self):
        one = estimate_cost_usd([RawSignal(title="abc def")], "claude-sonnet-5")
        five = estimate_cost_usd(
            [RawSignal(title="abc def")] * 5, "claude-sonnet-5"
        )
        self.assertGreater(one, 0.0)
        self.assertAlmostEqual(five, one * 5, places=2)


class BuildClientTests(unittest.TestCase):
    def test_missing_package_raises_with_hint(self):
        # anthropic is not a core dependency
        with self.assertRaises(ValueError) as ctx:
            build_client()
        self.assertIn("anthropic", str(ctx.exception))


class LlmNormalizerTests(unittest.TestCase):
    def test_meters_each_call(self):
        norm = LlmNormalizer(client=_FakeClient(), model="claude-sonnet-5")
        norm(RawSignal(title="one"))
        norm(RawSignal(title="two"))
        self.assertGreater(norm.meter.cost_usd, 0.0)
        self.assertFalse(norm.ceiling_hit)

    def test_ceiling_stops_further_calls(self):
        client = _FakeClient(usage=(400_000, 0))  # 0.8 USD per call on sonnet
        norm = LlmNormalizer(client=client, model="claude-sonnet-5", max_cost_usd=1.0)
        norm(RawSignal(title="one"))  # 0.0 < 1.0 -> runs, cost -> 0.8
        norm(RawSignal(title="two"))  # 0.8 < 1.0 -> runs, cost -> 1.6
        with self.assertRaises(CostCeilingExceeded):
            norm(RawSignal(title="three"))  # 1.6 >= 1.0 -> blocked
        self.assertTrue(norm.ceiling_hit)
        self.assertEqual(client.calls, 2)


class RunCycleWithLlmTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _source(self, *titles):
        from revenue_os.sources import StaticSource

        return StaticSource([RawSignal(title=t) for t in titles])

    def test_llm_path_persists_source_and_rationale(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        norm = LlmNormalizer(client=_FakeClient(), model="claude-sonnet-5")
        run_discovery_cycle(
            self._source("A paid SaaS tool", "Another paid tool"),
            store, log=log, normalizer=norm, evaluator="llm", est_cost_usd=0.01,
        )
        cands = CandidateStore.load(self.d / "candidates.json").all()
        self.assertTrue(cands)
        self.assertTrue(all(c.estimate_source == "llm" for c in cands))
        self.assertTrue(all(c.rationale for c in cands))

        entry = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertEqual(entry["evaluator"], "llm")
        self.assertEqual(entry["est_cost_usd"], 0.01)
        self.assertGreater(entry["actual_cost_usd"], 0.0)
        self.assertFalse(entry["cost_ceiling_hit"])

    def test_bad_response_skips_signal_cycle_survives(self):
        class _BadClient(_FakeClient):
            def create(self, **kwargs):
                return _FakeResponse({**_GOOD, "demand": 99.0}, _FakeUsage())

        store = CandidateStore.load(self.d / "candidates.json")
        norm = LlmNormalizer(client=_BadClient(), model="claude-sonnet-5")
        result = run_discovery_cycle(
            self._source("A", "B"), store, normalizer=norm, evaluator="llm"
        )
        self.assertEqual(result, [])  # every signal skipped, no crash

    def test_ceiling_hit_recorded_in_log(self):
        store = CandidateStore.load(self.d / "candidates.json")
        log = DiscoveryLog.load(self.d / "discovery_runs.json")
        client = _FakeClient(usage=(400_000, 0))  # 0.8 USD per call
        norm = LlmNormalizer(client=client, model="claude-sonnet-5", max_cost_usd=1.0)
        run_discovery_cycle(
            self._source("A", "B", "C", "D"),
            store, log=log, normalizer=norm, evaluator="llm",
        )
        entry = DiscoveryLog.load(self.d / "discovery_runs.json").latest()
        self.assertTrue(entry["cost_ceiling_hit"])
        self.assertLess(client.calls, 4)


class CliLlmTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_run_evaluator_llm_end_to_end(self):
        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            code, out = _run([
                "run", "--source", "static", "--evaluator", "llm",
                "--data-dir", self.data,
            ])
        self.assertEqual(code, 0)
        self.assertIn("llm evaluator: est $", out)
        store = CandidateStore.load(Path(self.data) / "candidates.json")
        self.assertTrue(store.all())
        self.assertTrue(all(c.estimate_source == "llm" for c in store.all()))
        entry = DiscoveryLog.load(Path(self.data) / "discovery_runs.json").latest()
        self.assertEqual(entry["evaluator"], "llm")

    def test_preflight_over_ceiling_exits_1_no_calls(self):
        fake = _FakeClient()
        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=fake
        ):
            code, _ = _run([
                "run", "--source", "static", "--evaluator", "llm",
                "--max-eval-cost", "0.0000001", "--data-dir", self.data,
            ])
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, 0)
        self.assertFalse((Path(self.data) / "candidates.json").exists())

    def test_evaluator_llm_without_package_exits_1(self):
        code, err = _run([
            "run", "--source", "static", "--evaluator", "llm",
            "--data-dir", self.data,
        ])
        self.assertEqual(code, 1)

    def test_default_is_keyword_and_no_anthropic_import(self):
        import sys

        sys.modules.pop("anthropic", None)
        code, _ = _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertEqual(code, 0)
        store = CandidateStore.load(Path(self.data) / "candidates.json")
        self.assertTrue(all(c.estimate_source == "keyword" for c in store.all()))
        self.assertNotIn("anthropic", sys.modules)


class LiveLlmTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("REVENUE_OS_NET_TESTS"), "network tests disabled"
    )
    def test_real_call_scores_a_signal(self):
        opp = to_opportunity_llm(
            RawSignal(title="A subscription API for PDF invoice parsing"),
            client=build_client(),
        )
        self.assertEqual(opp.estimate_source, "llm")
        for name in CRITERIA:
            self.assertTrue(0.0 <= getattr(opp, name) <= 5.0)


if __name__ == "__main__":
    unittest.main()
