import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition import AcquisitionAgent, _scoring_view
from revenue_os.acquisition_llm import (
    AcquisitionLlmScorer,
    estimate_score_cost_usd,
    score_view_llm,
)
from revenue_os.acquisition_sources import AcqRecord
from revenue_os.llm_cache import LlmCache
from revenue_os.llm_spend import _ACTIVITIES, entry_from
from revenue_os.messages import Task

_GOOD = {
    "relevance_score": 88, "is_active_problem": True, "buying_intent": "high",
    "prospect_type": "active_problem",
    "reason": "founder launched a SaaS two weeks ago, zero customers, asking for help",
    "recommended_fit": 82,
}


class _Usage:
    input_tokens = 700
    output_tokens = 90
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Block:
    type = "tool_use"
    name = "record_relevance"

    def __init__(self, payload):
        self.input = payload


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(payload)]
        self.usage = _Usage()
        self.stop_reason = "tool_use"


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = payload or _GOOD
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.payload)


def _view(title="How do I get my first paying customers?",
          text="I launched my SaaS two weeks ago and have zero customers."):
    rec = AcqRecord(title=title, url="https://news.ycombinator.com/item?id=1",
                    text=text, posted_at="2026-08-27T00:00:00+00:00",
                    platform="Hacker News", source="hn-algolia")
    from revenue_os.acquisition import score_lead
    return _scoring_view(rec, score_lead(rec))


class ScoreViewTests(unittest.TestCase):
    def test_structured_verdict(self):
        out = score_view_llm(_view(), client=_FakeClient())
        self.assertEqual(out["relevance_score"], 88)
        self.assertTrue(out["active_problem"])
        self.assertEqual(out["buying_intent"], "high")
        self.assertEqual(out["prospect_type"], "active_problem")
        self.assertEqual(out["recommended_fit"], 82)
        self.assertIn("founder", out["llm_reason"])

    def test_bad_enum_values_are_coerced_safely(self):
        out = score_view_llm(_view(), client=_FakeClient({
            **_GOOD, "buying_intent": "sky-high", "prospect_type": "???"}))
        self.assertEqual(out["buying_intent"], "low")
        self.assertEqual(out["prospect_type"], "unknown")

    def test_missing_numbers_raise(self):
        with self.assertRaises(ValueError):
            score_view_llm(_view(), client=_FakeClient({"reason": "x"}))

    def test_activity_is_registered_for_spend(self):
        self.assertIn("acquisition", _ACTIVITIES)


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_cache_means_one_api_call_for_a_repeated_lead(self):
        c = _FakeClient()
        s = AcquisitionLlmScorer(client=c, cache=self.cache)
        s(_view())
        s(_view())
        self.assertEqual(c.calls, 1)
        self.assertEqual(s.cache_hits, 1)

    def test_ceiling_blocks_further_calls(self):
        s = AcquisitionLlmScorer(client=_FakeClient(), max_cost_usd=0.0)
        s.meter.input_tokens = 10_000_000
        with self.assertRaises(Exception):
            s(_view())

    def test_estimate_skips_cached_views(self):
        v = _view()
        first = estimate_score_cost_usd([v], "claude-sonnet-5", cache=self.cache)
        self.assertGreater(first, 0)
        AcquisitionLlmScorer(client=_FakeClient(), cache=self.cache)(v)
        second = estimate_score_cost_usd([v], "claude-sonnet-5", cache=self.cache)
        self.assertEqual(second, 0.0)

    def test_spend_entry_reads_the_meter(self):
        s = AcquisitionLlmScorer(client=_FakeClient(), cache=self.cache)
        s(_view())
        e = entry_from("acquisition", s)
        self.assertEqual(e["activity"], "acquisition")
        self.assertGreater(e["cost_usd"], 0)


class AgentIntegrationTests(unittest.TestCase):
    def test_agent_uses_the_llm_score_and_marks_mode(self):
        rec = AcqRecord(title="How do I get my first customers for my app?",
                        url="https://news.ycombinator.com/item?id=1",
                        text="just launched, 0 users", posted_at="2026-08-27T00:00:00+00:00",
                        platform="Hacker News", source="hn-algolia")
        scorer = AcquisitionLlmScorer(client=_FakeClient())
        r = AcquisitionAgent(name="s").run(Task(
            objective="d", capability="discover_acquisition",
            payload={"records": [rec], "llm_scorer": scorer, "max_age_days": 30}))
        lead = r.output["leads"][0]
        self.assertEqual(lead["scoring_mode"], "llm")
        self.assertEqual(lead["relevance_score"], 88)
        self.assertEqual(lead["recommended_fit"], 82)
        self.assertIn("founder", lead["llm_reason"])
        self.assertEqual(r.output["scoring_mode"], "llm")

    def test_the_view_handed_to_the_llm_carries_no_extra_data(self):
        rec = AcqRecord(title="t", url="https://news.ycombinator.com/item?id=1",
                        text="x" * 5000, author="secret_handle",
                        posted_at="2026-08-27T00:00:00+00:00",
                        platform="Hacker News", source="hn-algolia")
        from revenue_os.acquisition import score_lead
        v = _scoring_view(rec, score_lead(rec) or {"relevance_score": 0,
                          "prospect_type": "unknown", "buying_intent": "low"})
        self.assertNotIn("author", v)
        self.assertLessEqual(len(v["text"]), 500)


if __name__ == "__main__":
    unittest.main()
