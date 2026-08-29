import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition_web import WebSearchSource
from revenue_os.llm_cache import LlmCache
from revenue_os.llm_spend import entry_from


class _Usage:
    input_tokens = 1500
    output_tokens = 200
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _SearchResultItem:
    type = "web_search_result"

    def __init__(self, url, title, page_age=None):
        self.url = url
        self.title = title
        self.page_age = page_age


class _SearchResultBlock:
    type = "web_search_tool_result"

    def __init__(self, items):
        self.content = list(items)


class _ToolUse:
    type = "tool_use"
    name = "record_web_leads"

    def __init__(self, leads):
        self.input = {"leads": leads}


class _Resp:
    def __init__(self, blocks):
        self.content = list(blocks)
        self.usage = _Usage()
        self.stop_reason = "tool_use"


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return self._resp


_REAL = [
    _SearchResultItem("https://www.reddit.com/r/SaaS/comments/abc/no_customers/",
                      "Launched, 0 customers, help", "2026-08-25"),
    _SearchResultItem("https://www.indiehackers.com/post/xyz",
                      "How to get first users", None),
]


def _source(client, cache=None, **kw):
    return WebSearchSource(client=client, cache=cache, **kw)


class WebSearchSourceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_keeps_only_urls_that_are_in_the_real_search_results(self):
        resp = _Resp([
            _SearchResultBlock(_REAL),
            _ToolUse([
                {"url": "https://www.reddit.com/r/SaaS/comments/abc/no_customers/",
                 "title": "Launched, 0 customers, help", "why_relevant": "0 customers"},
                {"url": "https://www.reddit.com/r/SaaS/comments/FAKE/invented/",
                 "title": "Made up", "why_relevant": "hallucinated"},
            ]),
        ])
        recs = _source(_FakeClient(resp)).search("first customers", 10)
        self.assertEqual(len(recs), 1)                      # the fabricated URL dropped
        self.assertIn("reddit.com/r/SaaS/comments/abc", recs[0].url)
        self.assertEqual(recs[0].source, "web")
        self.assertEqual(recs[0].posted_at, "2026-08-25T00:00:00+00:00")  # from page_age
        self.assertIn("web_search", recs[0].meta["via"])

    def test_no_date_when_page_age_missing(self):
        resp = _Resp([
            _SearchResultBlock([_REAL[1]]),
            _ToolUse([{"url": "https://www.indiehackers.com/post/xyz",
                       "title": "t", "why_relevant": "asking for users"}]),
        ])
        rec = _source(_FakeClient(resp)).search("q", 5)[0]
        self.assertEqual(rec.posted_at, "")                 # not fabricated
        self.assertIn("no date", rec.meta["date_basis"])

    def test_cache_avoids_a_second_api_call(self):
        resp = _Resp([_SearchResultBlock(_REAL),
                      _ToolUse([{"url": _REAL[0].url, "title": "t",
                                 "why_relevant": "r"}])])
        c = _FakeClient(resp)
        s = _source(c, cache=self.cache)
        s.search("q", 5)
        s.search("q", 5)
        self.assertEqual(c.calls, 1)
        self.assertEqual(s.cache_hits, 1)

    def test_ceiling_blocks_the_call(self):
        s = _source(_FakeClient(_Resp([])), max_cost_usd=0.0)
        s.meter.input_tokens = 10_000_000
        with self.assertRaises(Exception):
            s.search("q", 5)

    def test_spend_entry_reads_the_meter(self):
        resp = _Resp([_SearchResultBlock(_REAL),
                      _ToolUse([{"url": _REAL[0].url, "title": "t",
                                 "why_relevant": "r"}])])
        s = _source(_FakeClient(resp))
        s.search("q", 5)
        e = entry_from("acquisition", s)
        self.assertEqual(e["activity"], "acquisition")
        self.assertGreater(e["cost_usd"], 0)

    def test_empty_when_model_returns_no_leads(self):
        resp = _Resp([_SearchResultBlock(_REAL), _ToolUse([])])
        self.assertEqual(_source(_FakeClient(resp)).search("q", 5), [])

    def test_source_failure_is_isolated_by_composite(self):
        from revenue_os.acquisition_sources import CompositeAcqSource

        class _Boom:
            def create(self, **kw):
                raise RuntimeError("api error")
            messages = property(lambda self: self)

        class _HN:
            name = "hn-algolia"

            def search(self, q, n, *, since_ts=None):
                from revenue_os.acquisition_sources import AcqRecord
                return [AcqRecord(title="how do I get my first customers",
                                  url="https://news.ycombinator.com/item?id=1",
                                  text="0 customers", source="hn-algolia")]

        web = _source(_Boom())
        comp = CompositeAcqSource([web, _HN()])
        out = comp.search("q", 5)
        self.assertEqual(len(out), 1)                       # HN survived
        self.assertEqual(comp.errors[0][0], "web")


if __name__ == "__main__":
    unittest.main()
