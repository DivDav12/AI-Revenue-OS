import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from revenue_os import cli
from revenue_os.llm_budget import DEFAULT_CAP_USD, LlmBudget
from revenue_os.llm_spend import LlmSpendLog
from revenue_os.opportunity import CRITERIA
from revenue_os.store import CandidateStore

_SCORES = {**{c: 3.0 for c in CRITERIA}, "rationale": "ok"}


class _Usage:
    def __init__(self, i):
        self.input_tokens = i
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"
    name = "record_scores"
    input = _SCORES


class _Resp:
    def __init__(self, i):
        self.content = [_Block()]
        self.usage = _Usage(i)


class _FakeClient:
    def __init__(self, input_tokens=4000):
        self.calls = 0
        self.input_tokens = input_tokens
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.input_tokens)


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue() + err.getvalue()


def _seed_spend(data_dir, total_cost):
    log = LlmSpendLog(Path(data_dir) / "llm_spend.json")
    log.add({"activity": "evaluate", "cost_usd": total_cost, "api_calls": 1})
    log.save()


class LlmBudgetFileTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "llm_budget.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_file_uses_default(self):
        self.assertEqual(LlmBudget.load(self.path).cap, DEFAULT_CAP_USD)

    def test_set_cap_persists_with_history(self):
        b = LlmBudget.load(self.path)
        b.set_cap(12.5, actor="human-owner")
        again = LlmBudget.load(self.path)
        self.assertEqual(again.cap, 12.5)
        self.assertEqual(again.history()[-1]["actor"], "human-owner")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            LlmBudget.load(self.path).set_cap(-1, actor="o")

    def test_corrupt_and_shapeless_raise(self):
        self.path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmBudget.load(self.path)
        self.path.write_text(json.dumps({"x": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmBudget.load(self.path)


class BudgetGateCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def _spend_entries(self):
        return LlmSpendLog.load(Path(self.data) / "llm_spend.json").entries()

    def test_keyword_run_not_gated_no_budget_file(self):
        code, _ = _run(["run", "--source", "static", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertFalse((Path(self.data) / "llm_budget.json").exists())

    def test_run_refused_when_cap_exhausted(self):
        _seed_spend(self.data, DEFAULT_CAP_USD)  # nothing left
        fake = _FakeClient()
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=fake):
            code, err = _run([
                "run", "--source", "static", "--evaluator", "llm",
                "--data-dir", self.data,
            ])
        self.assertEqual(code, 1)
        self.assertIn("cumulative cap", err)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(len(self._spend_entries()), 1)  # only the seed

    def test_raising_cap_lets_the_run_proceed(self):
        _seed_spend(self.data, DEFAULT_CAP_USD)
        code, out = _run(["llm-budget", "50", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("-> $50", out)

        fake = _FakeClient(input_tokens=100)
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=fake):
            code, _ = _run([
                "run", "--source", "static", "--evaluator", "llm",
                "--data-dir", self.data,
            ])
        self.assertEqual(code, 0)
        self.assertGreater(fake.calls, 0)
        self.assertEqual(len(self._spend_entries()), 2)

    def test_effective_ceiling_is_capped_by_remaining(self):
        # remaining just above the pre-flight estimate but far below --max-eval-cost
        _seed_spend(self.data, DEFAULT_CAP_USD - 0.02)
        fake = _FakeClient(input_tokens=4000)  # 0.008 USD per call on sonnet
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=fake):
            code, _ = _run([
                "run", "--source", "static", "--evaluator", "llm",
                "--data-dir", self.data,
            ])
        self.assertEqual(code, 0)
        entry = self._spend_entries()[-1]
        self.assertTrue(entry["ceiling_hit"])
        self.assertLess(entry["api_calls"], 4)

    def test_llm_budget_no_arg_shows_status(self):
        _seed_spend(self.data, 1.25)
        code, out = _run(["llm-budget", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn(f"cap ${DEFAULT_CAP_USD}", out)
        self.assertIn("spent $1.25", out)
        self.assertIn(f"remaining ${round(DEFAULT_CAP_USD - 1.25, 4)}", out)

    def test_report_shows_cap_and_remaining(self):
        _seed_spend(self.data, 0.5)
        code, out = _run(["report", "--data-dir", self.data])
        self.assertIn("LLM SPEND", out)
        self.assertIn(f"cap ${DEFAULT_CAP_USD}", out)
        self.assertIn(f"remaining ${round(DEFAULT_CAP_USD - 0.5, 4)}", out)


if __name__ == "__main__":
    unittest.main()
