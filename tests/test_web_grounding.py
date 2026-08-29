import tempfile
import unittest
from pathlib import Path

from revenue_os.competition import (
    CompetitionWorker,
    analyze_competition_web,
    estimate_competition_cost_usd,
)
from revenue_os.llm_cache import LlmCache
from revenue_os.llm_normalize import CostMeter, SEARCH_PRICE_USD
from revenue_os.research import (
    ResearchWorker,
    estimate_research_cost_usd,
    research_candidate_web,
)
from revenue_os.store import Candidate

_RESEARCH_NOTE = {
    "competition": "Zapier, Make, n8n cover this",
    "demand_evidence": "steady search volume for 'self-hosted automation'",
    "legal_flags": "none apparent",
    "verdict": "caution",
    "rationale": "crowded but real demand",
    "sources": [
        {"url": "https://zapier.com/pricing", "title": "Zapier Pricing"},
        {"url": "https://n8n.io", "title": "n8n"},
    ],
}
_COMP_NOTE = {
    "named_competitors": "Zapier, Make, n8n",
    "pricing_landscape": "$0-50/mo tiered",
    "differentiation_angle": "vertical templates",
    "saturation": "several funded incumbents",
    "verdict": "crowded",
    "rationale": "mature category",
    "sources": [{"url": "https://make.com/pricing", "title": "Make Pricing"}],
}


class _Usage:
    input_tokens = 900
    output_tokens = 300
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Search:
    type = "web_search_tool_result"

    def __init__(self, ok=True):
        self.content = [{"title": "r", "url": "https://x"}] if ok else {"error_code": "x"}


class _ToolUse:
    type = "tool_use"

    def __init__(self, name, payload):
        self.name = name
        self.input = payload


class _WebResp:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.usage = _Usage()
        self.stop_reason = stop_reason


class _WebClient:
    """Returns a queued list of responses, one per create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


def _cand(name="alpha"):
    return Candidate(name=name, description=f"{name} opportunity", status="shortlisted")


class ResearchWebTests(unittest.TestCase):
    def test_web_note_has_sources_and_basis(self):
        client = _WebClient([_WebResp(
            [_Search(), _Search(), _ToolUse("record_research", _RESEARCH_NOTE)])])
        meter = CostMeter("claude-sonnet-5")
        note = research_candidate_web(_cand(), client=client, meter=meter)
        self.assertEqual(note["verdict"], "caution")
        self.assertEqual(note["basis"], "web search, 2 sources")
        self.assertEqual(len(note["sources"]), 2)
        # cost includes 2 search fees + tokens
        self.assertEqual(meter.searches, 2)
        self.assertGreater(meter.cost_usd, 2 * SEARCH_PRICE_USD)

    def test_search_error_yields_partial_not_a_raise(self):
        client = _WebClient([_WebResp(
            [_Search(ok=False), _ToolUse("record_research", _RESEARCH_NOTE)])])
        note = research_candidate_web(_cand(), client=client, meter=CostMeter("m"))
        self.assertTrue(note["basis"].startswith("web search (partial)"))

    def test_missing_sources_raises(self):
        bad = {**_RESEARCH_NOTE, "sources": []}
        client = _WebClient([_WebResp([_Search(), _ToolUse("record_research", bad)])])
        with self.assertRaises(ValueError):
            research_candidate_web(_cand(), client=client, meter=CostMeter("m"))

    def test_pause_turn_is_resumed(self):
        client = _WebClient([
            _WebResp([_Search()], stop_reason="pause_turn"),
            _WebResp([_Search(), _ToolUse("record_research", _RESEARCH_NOTE)]),
        ])
        meter = CostMeter("claude-sonnet-5")
        note = research_candidate_web(_cand(), client=client, meter=meter)
        self.assertEqual(client.calls, 2)
        self.assertEqual(meter.searches, 2)
        self.assertIn("2 sources", note["basis"])

    def test_worker_mode_web_uses_a_mode_tagged_cache_key(self):
        with tempfile.TemporaryDirectory() as d:
            cache = LlmCache(Path(d) / "c.json")
            client = _WebClient([
                _WebResp([_Search(), _ToolUse("record_research", _RESEARCH_NOTE)]),
                _WebResp([_Search(), _ToolUse("record_research", _RESEARCH_NOTE)]),
            ])
            w = ResearchWorker(client=client, model="claude-sonnet-5",
                               cache=cache, mode="web")
            w(_cand())
            w(_cand())                       # second call is a cache hit
            self.assertEqual(client.calls, 1)
            self.assertEqual(w.cache_hits, 1)
            # an llm-mode worker does NOT hit the web cache entry
            from revenue_os.research import research_cache_key
            self.assertNotEqual(
                research_cache_key(_cand(), "claude-sonnet-5", "web"),
                research_cache_key(_cand(), "claude-sonnet-5", "llm"),
            )


class CompetitionWebTests(unittest.TestCase):
    def test_web_note_and_cost(self):
        client = _WebClient([_WebResp(
            [_Search(), _ToolUse("record_competition_analysis", _COMP_NOTE)])])
        meter = CostMeter("claude-sonnet-5")
        note = analyze_competition_web(_cand(), client=client, meter=meter)
        self.assertEqual(note["verdict"], "crowded")
        self.assertEqual(note["basis"], "web search, 1 sources")
        self.assertEqual(meter.searches, 1)

    def test_worker_web_mode(self):
        client = _WebClient([_WebResp(
            [_Search(), _ToolUse("record_competition_analysis", _COMP_NOTE)])])
        w = CompetitionWorker(client=client, model="m", mode="web")
        self.assertEqual(w(_cand())["verdict"], "crowded")


class CostEstimateTests(unittest.TestCase):
    def test_web_estimate_exceeds_llm_estimate_by_search_fees(self):
        cands = [_cand("a"), _cand("b")]
        llm = estimate_research_cost_usd(cands, "claude-sonnet-5", mode="llm")
        web = estimate_research_cost_usd(cands, "claude-sonnet-5", mode="web")
        self.assertGreater(web, llm + 2 * 4 * SEARCH_PRICE_USD - 0.001)
        clm = estimate_competition_cost_usd(cands, "claude-sonnet-5", mode="llm")
        cweb = estimate_competition_cost_usd(cands, "claude-sonnet-5", mode="web")
        self.assertGreater(cweb, clm)


if __name__ == "__main__":
    unittest.main()
