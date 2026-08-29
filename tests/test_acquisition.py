import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.acquisition import (
    AcquisitionAgent,
    AcquisitionStore,
    build_lead,
    canonical_url,
    qc_lead,
    score_lead,
)
from revenue_os.acquisition_sources import (
    AcqRecord,
    CompositeAcqSource,
    build_acquisition_source,
)
from revenue_os.cli import main
from revenue_os.messages import Task
from revenue_os.workflow import discover_acquisition_opportunities


def _rec(title="Ask HN: how do I get my first paying customer?",
         url="https://news.ycombinator.com/item?id=1",
         text="Launched my SaaS, 0 paying customers. How did you find your first clients?",
         author="founder_a", posted_at="2026-08-15T00:00:00+00:00",
         platform="Hacker News", source="hn-algolia", query="q"):
    return AcqRecord(title=title, url=url, text=text, author=author,
                     posted_at=posted_at, platform=platform, source=source,
                     query=query)


class CanonicalUrlTests(unittest.TestCase):
    def test_strips_www_fragment_and_tracking(self):
        self.assertEqual(
            canonical_url("https://www.Example-Blog.com/post/?utm_source=x&ref=y#top"),
            "https://example-blog.com/post")

    def test_keeps_hn_id_query(self):
        self.assertEqual(
            canonical_url("https://news.ycombinator.com/item?id=42&foo=bar"),
            "https://news.ycombinator.com/item?id=42")

    def test_reddit_permalink_truncates_to_comment_id(self):
        a = canonical_url("https://www.reddit.com/r/SaaS/comments/abc/some_slug/")
        b = canonical_url("https://old.reddit.com/r/SaaS/comments/abc/other_slug/?x=1")
        self.assertEqual(a, "https://reddit.com/r/saas/comments/abc")
        self.assertEqual(a, b)

    def test_empty(self):
        self.assertEqual(canonical_url(""), "")
        self.assertEqual(canonical_url(None), "")


class ScoreTests(unittest.TestCase):
    def test_no_intent_phrase_returns_none(self):
        self.assertIsNone(score_lead(_rec(
            title="Show HN: my new markdown editor", text="feedback welcome")))

    def test_matches_and_buckets(self):
        s = score_lead(_rec())
        self.assertGreater(s["fit_score"], 60)
        self.assertEqual(s["buying_intent"], "high")
        self.assertIn("first paying customer", s["matched_phrases"])
        self.assertIn("matched", s["match_reason"])

    def test_title_match_scores_higher_than_body_only(self):
        in_title = score_lead(_rec(title="how do I get my first paying customer",
                                   text="hello"))
        body_only = score_lead(_rec(title="a question about launch",
                                    text="how do I get my first paying customer"))
        self.assertGreater(in_title["fit_score"], body_only["fit_score"])

    def test_score_clamped_0_100(self):
        s = score_lead(_rec(
            title="first paying customer first clients my first users",
            text="0 paying customers no customers after launch nobody is buying"))
        self.assertLessEqual(s["fit_score"], 100)
        self.assertGreaterEqual(s["fit_score"], 0)


class QcTests(unittest.TestCase):
    def test_placeholder_host_is_rejected(self):
        lead = build_lead(_rec(url="https://example.com/thread"), score_lead(_rec()))
        self.assertIn("placeholder host 'example.com'", qc_lead(lead))

    def test_missing_url_is_rejected(self):
        lead = build_lead(_rec(url=""), score_lead(_rec()))
        self.assertIn("no url", qc_lead(lead))

    def test_valid_lead_passes(self):
        self.assertEqual(qc_lead(build_lead(_rec(), score_lead(_rec()))), [])


class NoFabricationTests(unittest.TestCase):
    def test_missing_author_and_timestamp_stay_empty(self):
        rec = _rec(author="", posted_at="")
        lead = build_lead(rec, score_lead(rec))
        self.assertEqual(lead.author, "")
        self.assertEqual(lead.posted_at, "")

    def test_lead_url_is_exactly_the_record_url(self):
        rec = _rec(url="https://news.ycombinator.com/item?id=99")
        lead = build_lead(rec, score_lead(rec))
        self.assertEqual(lead.url, "https://news.ycombinator.com/item?id=99")

    def test_agent_never_emits_a_lead_without_a_real_url(self):
        agent = AcquisitionAgent(name="s")
        r = agent.run(Task(objective="d", capability="discover_acquisition",
                           payload={"records": [
                               _rec(url=""),                       # no url
                               _rec(url="https://example.com/x"),  # fake host
                               _rec(title="unrelated", text="nothing here"),  # no intent
                           ]}))
        self.assertEqual(r.output["leads"], [])
        self.assertEqual(len(r.output["dropped"]), 2)   # the 3rd is a no-match, not "dropped"


class AgentTests(unittest.TestCase):
    def test_scores_ranks_and_reports_dropped(self):
        agent = AcquisitionAgent(name="s")
        r = agent.run(Task(objective="d", capability="discover_acquisition",
                           payload={"records": [
                               _rec(url="https://news.ycombinator.com/item?id=1",
                                    title="how do I get users", text="just curious"),
                               _rec(url="https://news.ycombinator.com/item?id=2",
                                    title="can't get my first customer",
                                    text="0 paying customers after launch"),
                               _rec(url="https://example.com/x"),
                           ]}))
        self.assertEqual(r.status, "ok")
        leads = r.output["leads"]
        self.assertEqual(len(leads), 2)
        self.assertGreater(leads[0]["fit_score"], leads[1]["fit_score"])
        self.assertEqual(len(r.output["dropped"]), 1)
        self.assertEqual(r.output["considered"], 3)

    def test_dedupes_within_a_batch_keeping_higher_score(self):
        agent = AcquisitionAgent(name="s")
        weak = _rec(url="https://news.ycombinator.com/item?id=5",
                    title="how do I get users", text="hmm")
        strong = _rec(url="https://news.ycombinator.com/item?id=5&utm=x",
                      title="how do I get my first paying customer",
                      text="0 paying customers")
        r = agent.run(Task(objective="d", capability="discover_acquisition",
                           payload={"records": [weak, strong]}))
        self.assertEqual(len(r.output["leads"]), 1)
        self.assertEqual(r.output["leads"][0]["matched_phrases"][0],
                         "first paying customer")

    def test_bad_payload_errors(self):
        r = AcquisitionAgent(name="s").run(
            Task(objective="d", capability="discover_acquisition", payload={}))
        self.assertEqual(r.status, "error")


class _FakeSource:
    name = "fake"

    def __init__(self, by_query, fail_on=()):
        self._by_query = by_query
        self._fail_on = set(fail_on)

    def search(self, query, limit):
        if query in self._fail_on:
            raise RuntimeError("boom")
        return list(self._by_query.get(query, []))[:limit]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _store(self):
        return AcquisitionStore.load(self.d / "acquisition.json")

    def test_discovers_persists_and_dedupes_across_runs(self):
        src = _FakeSource({
            "q1": [_rec(url="https://news.ycombinator.com/item?id=1",
                        title="can't get my first customer")],
            "q2": [_rec(url="https://news.ycombinator.com/item?id=1"),   # dup of q1
                   _rec(url="https://www.reddit.com/r/SaaS/comments/z9/x/",
                        title="nobody is buying my product", platform="r/SaaS")],
        })
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["q1", "q2"], politeness_delay=0)
        self.assertEqual(r["new"], 2)
        self.assertEqual(r["collapsed"], 1)      # the id=1 dup within the batch
        self.assertEqual(len(r["leads"]), 2)

        again = discover_acquisition_opportunities(
            self._store(), src, queries=["q1", "q2"], politeness_delay=0)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["duplicates"], 2)   # both already persisted
        self.assertEqual(len(again["leads"]), 2)

    def test_a_failing_query_does_not_kill_the_run(self):
        src = _FakeSource(
            {"good": [_rec(url="https://news.ycombinator.com/item?id=7")]},
            fail_on=["bad"])
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["bad", "good"], politeness_delay=0)
        self.assertEqual(r["new"], 1)
        self.assertEqual(r["query_errors"][0]["query"], "bad")

    def test_min_score_filters(self):
        src = _FakeSource({"q": [
            _rec(url="https://news.ycombinator.com/item?id=1",
                 title="how do I get users", text="curious"),           # weak
            _rec(url="https://news.ycombinator.com/item?id=2",
                 title="can't get my first customer",
                 text="0 paying customers"),                            # strong
        ]})
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["q"], min_score=50, politeness_delay=0)
        self.assertEqual(len(r["leads"]), 1)
        self.assertGreaterEqual(r["leads"][0]["fit_score"], 50)

    def test_malformed_records_are_skipped(self):
        src = _FakeSource({"q": [
            _rec(url="https://news.ycombinator.com/item?id=1", title=""),   # no title
            _rec(url="not a url", title="first paying customer"),           # bad url
        ]})
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["q"], politeness_delay=0)
        self.assertEqual(r["leads"], [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.p = Path(self._dir.name) / "acquisition.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_add_dedupes_and_persists_ranked(self):
        s = AcquisitionStore.load(self.p)
        self.assertTrue(s.add({"canonical_url": "u1", "fit_score": 40, "title": "a"}))
        self.assertTrue(s.add({"canonical_url": "u2", "fit_score": 90, "title": "b"}))
        self.assertFalse(s.add({"canonical_url": "u1", "fit_score": 99}))
        s.save()
        loaded = json.loads(self.p.read_text(encoding="utf-8"))
        self.assertEqual([d["canonical_url"] for d in loaded], ["u2", "u1"])

    def test_add_without_canonical_url_raises(self):
        with self.assertRaises(ValueError):
            AcquisitionStore.load(self.p).add({"fit_score": 10})


class SourceFactoryTests(unittest.TestCase):
    def test_static_source_is_offline_and_scoreable(self):
        src = build_acquisition_source("static")
        recs = src.search("anything", 10)
        self.assertTrue(any(score_lead(r) for r in recs))

    def test_file_source_requires_path(self):
        with self.assertRaises(ValueError):
            build_acquisition_source("file")

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            build_acquisition_source("bing")

    def test_composite_isolates_a_dead_sub_source(self):
        class _Dead:
            name = "dead"

            def search(self, q, n):
                raise RuntimeError("HTTP Error 403: Blocked")

        class _Live:
            name = "live"

            def search(self, q, n):
                return [_rec()]

        c = CompositeAcqSource([_Dead(), _Live()])
        out = c.search("q", 5)
        self.assertEqual(len(out), 1)                    # live source still returned
        self.assertEqual(c.errors, [("dead", "HTTP Error 403: Blocked")])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.recs = self.d / "recs.json"
        self.recs.write_text(json.dumps([
            {"title": "Ask HN: how do I get my first paying customer?",
             "url": "https://news.ycombinator.com/item?id=100",
             "text": "0 paying customers", "author": "a",
             "posted_at": "2026-08-20T00:00:00+00:00", "platform": "Hacker News"},
            {"title": "Show HN: my side project", "url": "https://x.example/1",
             "text": "no intent here"},
        ]), encoding="utf-8")

    def tearDown(self):
        self._dir.cleanup()

    def test_file_source_ranks_and_persists(self):
        rc = main(["discover-opportunities", "--source", "file",
                   "--source-path", str(self.recs), "--delay", "0",
                   "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        store = AcquisitionStore.load(self.d / "acquisition.json")
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.all()[0]["url"],
                         "https://news.ycombinator.com/item?id=100")

    def test_dry_run_persists_nothing(self):
        rc = main(["discover-opportunities", "--source", "file",
                   "--source-path", str(self.recs), "--delay", "0", "--dry-run",
                   "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        self.assertFalse((self.d / "acquisition.json").exists())


if __name__ == "__main__":
    unittest.main()
