import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from revenue_os import cli
from revenue_os.llm_spend import LlmSpendLog, entry_from
from revenue_os.opportunity import CRITERIA
from revenue_os.store import Candidate, CandidateStore

_PAYLOADS = {
    "record_scores": {**{c: 3.0 for c in CRITERIA}, "rationale": "ok"},
    "record_plan": {
        "hypothesis": "people pay", "cheapest_test": "call 10", "success_metric": "3 yes",
        "effort": "low", "estimated_cost_usd": 0.0, "needs_human_budget": False,
    },
    "record_offer": {
        "what_is_sold": "a thing", "price": 99.0, "currency": "USD",
        "delivery": "digital", "call_to_action": "buy now", "positioning": "for X",
    },
}


class _Usage:
    input_tokens = 400
    output_tokens = 90
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"

    def __init__(self, name, payload):
        self.name = name
        self.input = payload


class _Resp:
    def __init__(self, name):
        self.content = [_Block(name, _PAYLOADS[name])]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(kwargs["tool_choice"]["name"])


class _FakeWorker:
    model = "claude-sonnet-5"
    cache_hits = 2
    cache_misses = 3
    ceiling_hit = False

    class meter:
        input_tokens = 1000
        output_tokens = 200
        cost_usd = 0.004


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class LlmSpendLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "llm_spend.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(LlmSpendLog.load(self.path).entries(), [])

    def test_entry_from_worker(self):
        e = entry_from("plan", _FakeWorker())
        self.assertEqual(e["activity"], "plan")
        self.assertEqual(e["model"], "claude-sonnet-5")
        self.assertEqual(e["api_calls"], 3)
        self.assertEqual(e["input_tokens"], 1000)
        self.assertEqual(e["cost_usd"], 0.004)
        self.assertEqual(e["cache_hits"], 2)

    def test_entry_from_rejects_bad_activity(self):
        with self.assertRaises(ValueError):
            entry_from("bogus", _FakeWorker())

    def test_round_trip_and_summary(self):
        log = LlmSpendLog.load(self.path)
        log.add(entry_from("evaluate", _FakeWorker()))
        log.add({"activity": "offer", "cost_usd": 0.01, "api_calls": 1})
        log.save()

        s = LlmSpendLog.load(self.path).summary()
        self.assertEqual(s["runs"], 2)
        self.assertEqual(s["total_cost_usd"], round(0.004 + 0.01, 4))
        self.assertEqual(s["total_api_calls"], 4)
        self.assertEqual(s["by_activity"]["evaluate"], 0.004)
        self.assertEqual(s["by_activity"]["offer"], 0.01)
        self.assertEqual(s["by_activity"]["plan"], 0.0)
        self.assertEqual(s["by_activity"]["competition"], 0.0)

    def test_competition_and_copy_are_valid_activities(self):
        log = LlmSpendLog.load(self.path)
        log.add(entry_from("competition", _FakeWorker()))
        log.add(entry_from("copy", _FakeWorker()))
        log.save()
        acts = {e["activity"] for e in LlmSpendLog.load(self.path).entries()}
        self.assertIn("competition", acts)
        self.assertIn("copy", acts)

    def test_corrupt_and_non_list_raise(self):
        self.path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmSpendLog.load(self.path)
        self.path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmSpendLog.load(self.path)


class CliSpendRecordingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def _entries(self):
        return LlmSpendLog.load(Path(self.data) / "llm_spend.json").entries()

    def test_keyword_path_records_nothing(self):
        _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertFalse((Path(self.data) / "llm_spend.json").exists())

    def test_full_llm_pipeline_records_three_activities(self):
        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            _run(["run", "--source", "static", "--evaluator", "llm",
                  "--data-dir", self.data])
            store = CandidateStore.load(Path(self.data) / "candidates.json")
            name = store.all()[0].name
            _run(["approve", name, "--data-dir", self.data])
            _run(["investigate", "--planner", "llm", "--data-dir", self.data])
            # force a validated candidate so prepare-launch has work
            store = CandidateStore.load(Path(self.data) / "candidates.json")
            from dataclasses import replace
            cand = store.get(name)
            store.put(replace(cand, status="validated"))
            store.save()
            _run(["prepare-launch", "--proposer", "llm", "--data-dir", self.data])

        acts = [e["activity"] for e in self._entries()]
        self.assertEqual(acts, ["evaluate", "plan", "offer"])
        self.assertTrue(all(e["cost_usd"] > 0 for e in self._entries()))

    def test_fully_cached_run_still_records_zero(self):
        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            _run(["run", "--source", "static", "--evaluator", "llm",
                  "--data-dir", self.data])
            _run(["run", "--source", "static", "--evaluator", "llm",
                  "--data-dir", self.data])
        entries = self._entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["cost_usd"], 0.0)
        self.assertEqual(entries[1]["api_calls"], 0)
        self.assertGreater(entries[1]["cache_hits"], 0)

    def test_llm_costs_command(self):
        code, out = _run(["llm-costs", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("no LLM runs recorded", out)

        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            _run(["run", "--source", "static", "--evaluator", "llm",
                  "--data-dir", self.data])
        code, out = _run(["llm-costs", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("evaluate", out)
        self.assertIn("total $", out)

    def test_report_shows_llm_spend_section(self):
        code, out = _run(["report", "--data-dir", self.data])
        self.assertIn("LLM SPEND", out)
        self.assertIn("(no LLM runs recorded)", out)

        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            _run(["run", "--source", "static", "--evaluator", "llm",
                  "--data-dir", self.data])
        code, out = _run(["report", "--data-dir", self.data])
        self.assertIn("LLM SPEND", out)
        self.assertIn("evaluate $", out)


if __name__ == "__main__":
    unittest.main()
