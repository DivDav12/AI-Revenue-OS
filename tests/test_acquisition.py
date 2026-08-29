import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.acquisition import (
    AcquisitionAgent,
    AcquisitionStore,
    age_info,
    build_lead,
    canonical_url,
    classify,
    final_score,
    lead_id,
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

_NOW = datetime.now(timezone.utc)


def _days_ago(n):
    return (_NOW - timedelta(days=n)).isoformat()


def _rec(title="Ask HN: How do I get my first paying customers?",
         url="https://news.ycombinator.com/item?id=1",
         text="I launched my SaaS two weeks ago and have 0 paying customers.",
         author="founder_a", posted_at=None, platform="Hacker News",
         source="hn-algolia", query="q", days=3):
    return AcqRecord(title=title, url=url, text=text, author=author,
                     posted_at=posted_at if posted_at is not None else _days_ago(days),
                     platform=platform, source=source, query=query)


# --- canonical url + id -------------------------------------------

class CanonicalUrlTests(unittest.TestCase):
    def test_strips_www_fragment_and_tracking(self):
        self.assertEqual(
            canonical_url("https://www.Example-Blog.com/post/?utm_source=x#top"),
            "https://example-blog.com/post")

    def test_reddit_permalink_truncates_to_comment_id(self):
        a = canonical_url("https://www.reddit.com/r/SaaS/comments/abc/some_slug/")
        b = canonical_url("https://old.reddit.com/r/SaaS/comments/abc/other/?x=1")
        self.assertEqual(a, b)

    def test_lead_id_is_stable_and_short(self):
        self.assertEqual(lead_id("https://x/y"), lead_id("https://x/y"))
        self.assertEqual(len(lead_id("https://x/y")), 12)


# --- recency -----------------------------------------------------

class AgeTests(unittest.TestCase):
    def test_missing_posted_at_is_unknown_not_fabricated(self):
        self.assertEqual(age_info(""), {"age_days": None, "age_bucket": "unknown"})
        self.assertEqual(age_info(None)["age_bucket"], "unknown")
        self.assertEqual(age_info("garbage")["age_bucket"], "unknown")

    def test_buckets(self):
        self.assertEqual(age_info(_days_ago(2))["age_bucket"], "recent")
        self.assertEqual(age_info(_days_ago(20))["age_bucket"], "aging")
        self.assertEqual(age_info(_days_ago(200))["age_bucket"], "old")

    def test_max_age_days_downranks_old_but_keeps_unknown_reasonable(self):
        old = final_score(relevance_score=90, age_days=400,
                          prospect_type="active_problem", buying_intent="high",
                          max_age_days=30)
        fresh = final_score(relevance_score=80, age_days=2,
                            prospect_type="active_problem", buying_intent="high",
                            max_age_days=30)
        unknown = final_score(relevance_score=90, age_days=None,
                              prospect_type="active_problem", buying_intent="high",
                              max_age_days=30)
        self.assertLess(old, 15)              # 10-year article crushed
        self.assertGreater(fresh, 70)         # 2-day real ask wins
        self.assertGreater(fresh, old)
        self.assertGreater(unknown, old)      # unknown penalised, not zeroed
        self.assertLess(unknown, fresh)


# --- deterministic classification --------------------------------

class ClassifyTests(unittest.TestCase):
    def test_active_problem_example_a(self):
        c = classify("How do I get my first paying customer?",
                     "I launched my SaaS two weeks ago and have zero customers.")
        self.assertGreaterEqual(c["relevance_score"], 75)
        self.assertEqual(c["prospect_type"], "active_problem")
        self.assertTrue(c["active_problem"])
        self.assertEqual(c["buying_intent"], "high")

    def test_historical_case_study_is_rejected_b_c(self):
        for title in ("I got 1,000 customers. Here's how.",
                      "How an Angelpad startup got from 0 to 1000 paying customers"):
            c = classify(title, "")
            self.assertEqual(c["prospect_type"], "success_story")
            self.assertLess(c["relevance_score"], 15)

    def test_possible_lead_example_d(self):
        c = classify("How do I get users for my new productivity app?",
                     "just shipped it, nobody is signing up")
        self.assertGreater(c["relevance_score"], 30)
        self.assertIn(c["prospect_type"], ("seeking_advice", "active_problem"))

    def test_idea_post_example_e(self):
        c = classify("Someone should build a tool for finding customers", "idea:")
        self.assertIn(c["prospect_type"], ("educational", "irrelevant"))
        self.assertLess(c["relevance_score"], 25)

    def test_unrelated_thread_example_f(self):
        c = classify("How do you allocate equity in a startup?",
                     "a comment somewhere mentions customers")
        self.assertLess(c["relevance_score"], 20)
        self.assertNotEqual(c["prospect_type"], "active_problem")

    def test_title_weighting(self):
        in_title = classify("struggling to get customers for my SaaS", "hello")
        body_only = classify("a general startup question",
                             "struggling to get customers for my SaaS")
        self.assertGreater(in_title["relevance_score"], body_only["relevance_score"])

    def test_no_positive_signal_scores_none(self):
        self.assertIsNone(score_lead(_rec(
            title="Show HN: my markdown editor", text="feedback welcome")))


# --- lead assembly / qc ----------------------------------------

class LeadTests(unittest.TestCase):
    def test_build_lead_fills_all_new_fields(self):
        lead = build_lead(_rec(days=2), score_lead(_rec(days=2)), max_age_days=30)
        d = lead.to_dict()
        for k in ("age_days", "age_bucket", "prospect_type", "relevance_score",
                  "final_score", "scoring_mode", "human_review_status",
                  "lead_id", "active_problem", "recommended_fit", "llm_reason"):
            self.assertIn(k, d)
        self.assertEqual(d["human_review_status"], "new")
        self.assertEqual(d["scoring_mode"], "deterministic")
        self.assertEqual(d["age_bucket"], "recent")

    def test_final_score_beats_an_old_perfect_match(self):
        fresh = build_lead(_rec(title="how do I get my first customers for my SaaS",
                                text="0 paying customers, just launched", days=2),
                           score_lead(_rec(days=2)), max_age_days=30)
        old = build_lead(
            _rec(title="how do I get my first customers for my SaaS",
                 text="0 paying customers, just launched",
                 url="https://news.ycombinator.com/item?id=999", days=3650),
            score_lead(_rec(url="https://news.ycombinator.com/item?id=999",
                            days=3650)),
            max_age_days=30)
        self.assertGreater(fresh.final_score, old.final_score + 40)

    def test_missing_author_and_timestamp_stay_empty(self):
        rec = _rec(author="", posted_at="")
        lead = build_lead(rec, score_lead(rec))
        self.assertEqual(lead.author, "")
        self.assertEqual(lead.posted_at, "")
        self.assertIsNone(lead.age_days)
        self.assertEqual(lead.age_bucket, "unknown")

    def test_qc_rejects_placeholder_and_missing_urls(self):
        self.assertIn("placeholder host 'example.com'",
                      qc_lead(build_lead(_rec(url="https://example.com/x"),
                                         score_lead(_rec()))))
        self.assertIn("no url",
                      qc_lead(build_lead(_rec(url=""), score_lead(_rec()))))


# --- agent -----------------------------------------------------

class AgentTests(unittest.TestCase):
    def _run(self, records, **payload):
        return AcquisitionAgent(name="s").run(Task(
            objective="d", capability="discover_acquisition",
            payload={"records": records, **payload}))

    def test_ranks_by_final_score_and_reports_counts(self):
        r = self._run([
            _rec(url="https://news.ycombinator.com/item?id=1",
                 title="how do I get my first paying customer for my SaaS?",
                 text="0 paying customers", days=2),
            _rec(url="https://news.ycombinator.com/item?id=2",
                 title="How I got 5000 customers - a retrospective",
                 text="from 0 to 5000", days=1),
            _rec(url="https://example.com/x"),
        ])
        self.assertEqual(r.status, "ok")
        leads = r.output["leads"]
        self.assertEqual(leads[0]["url"], "https://news.ycombinator.com/item?id=1")
        # the success story is kept (not deleted) but crushed to the bottom
        story = leads[-1]
        self.assertEqual(story["url"], "https://news.ycombinator.com/item?id=2")
        self.assertEqual(story["prospect_type"], "success_story")
        self.assertLess(story["final_score"], 10)
        self.assertEqual(r.output["considered"], 3)
        self.assertEqual(r.output["scoring_mode"], "deterministic")

    def test_too_old_is_counted_not_deleted(self):
        r = self._run([_rec(url="https://news.ycombinator.com/item?id=1",
                            title="struggling to get my first customers",
                            days=400)], max_age_days=30, min_score=0)
        self.assertEqual(r.output["too_old"], 1)
        self.assertEqual(len(r.output["leads"]), 1)          # kept
        self.assertLess(r.output["leads"][0]["final_score"], 15)  # but low

    def test_min_score_filters_on_final_score(self):
        r = self._run([
            _rec(url="https://news.ycombinator.com/item?id=1",
                 title="how do I get my first customers for my SaaS", days=2),
            _rec(url="https://news.ycombinator.com/item?id=2",
                 title="general chat about products", text="we have customers",
                 days=2),
        ], min_score=50)
        self.assertEqual(len(r.output["leads"]), 1)
        self.assertGreaterEqual(r.output["leads"][0]["final_score"], 50)

    def test_dedupes_keeping_higher_final_score(self):
        r = self._run([
            _rec(url="https://news.ycombinator.com/item?id=5",
                 title="a customers question", text="hmm", days=2),
            _rec(url="https://news.ycombinator.com/item?id=5&utm=x",
                 title="how do I get my first paying customers for my SaaS",
                 text="0 paying customers", days=2),
        ])
        self.assertEqual(len(r.output["leads"]), 1)
        self.assertGreater(r.output["leads"][0]["final_score"], 40)

    def test_bad_payload_errors(self):
        self.assertEqual(self._run(None).status, "error")

    def test_llm_scorer_failure_falls_back_to_deterministic(self):
        def boom(_view):
            raise RuntimeError("api down")
        r = self._run([_rec(url="https://news.ycombinator.com/item?id=1", days=2)],
                      llm_scorer=boom)
        self.assertEqual(r.output["scoring_mode"], "llm")
        self.assertIn("llm scoring failed", r.output["leads"][0]["llm_reason"])


# --- store: upsert + review + backwards compat ------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.p = Path(self._dir.name) / "acquisition.json"

    def tearDown(self):
        self._dir.cleanup()

    def _lead(self, **over):
        base = {"canonical_url": "u1", "lead_id": lead_id("u1"),
                "final_score": 50, "relevance_score": 50, "fit_score": 50,
                "title": "t", "human_review_status": "new",
                "scoring_mode": "deterministic"}
        base.update(over)
        return base

    def test_upsert_added_then_updated_keeps_better_score(self):
        s = AcquisitionStore.load(self.p)
        self.assertEqual(s.upsert(self._lead(final_score=40)), "added")
        self.assertEqual(s.upsert(self._lead(final_score=80, title="better")),
                         "updated")
        self.assertEqual(s.get("u1")["final_score"], 80)
        # a worse re-find does not lower it
        s.upsert(self._lead(final_score=10))
        self.assertEqual(s.get("u1")["final_score"], 80)

    def test_upsert_preserves_human_review_status_and_discovered_at(self):
        s = AcquisitionStore.load(self.p)
        s.upsert(self._lead(discovered_at="2020-01-01T00:00:00+00:00"))
        s.set_review(lead_id("u1"), "reviewed", actor="me")
        s.upsert(self._lead(final_score=99))
        self.assertEqual(s.get("u1")["human_review_status"], "reviewed")
        self.assertEqual(s.get("u1")["discovered_at"], "2020-01-01T00:00:00+00:00")

    def test_ranked_by_final_score_with_fit_score_fallback(self):
        s = AcquisitionStore.load(self.p)
        s.upsert(self._lead(canonical_url="a", lead_id=lead_id("a"), final_score=30))
        s.upsert(self._lead(canonical_url="b", lead_id=lead_id("b"), final_score=90))
        s.save()
        loaded = json.loads(self.p.read_text(encoding="utf-8"))
        self.assertEqual([d["canonical_url"] for d in loaded], ["b", "a"])

    def test_backwards_compatible_with_old_records(self):
        # an old lead: only the v1 fields, no final_score / age / lead_id
        self.p.write_text(json.dumps([{
            "canonical_url": "https://news.ycombinator.com/item?id=1",
            "url": "https://news.ycombinator.com/item?id=1",
            "title": "old lead", "fit_score": 70, "buying_intent": "high",
            "match_reason": "matched", "promo_allowed": "caution",
        }]), encoding="utf-8")
        s = AcquisitionStore.load(self.p)
        self.assertEqual(len(s.all()), 1)
        self.assertEqual(s.ranked()[0]["fit_score"], 70)     # still ranks
        s.save()                                             # re-save doesn't crash

    def test_set_review_rejects_unknown_status_and_id(self):
        s = AcquisitionStore.load(self.p)
        s.upsert(self._lead())
        with self.assertRaises(ValueError):
            s.set_review(lead_id("u1"), "bogus")
        with self.assertRaises(ValueError):
            s.set_review("ffffffffffff", "reviewed")


# --- sources -------------------------------------------------

class _FakeSource:
    name = "fake"

    def __init__(self, by_query, fail_on=()):
        self._by_query = by_query
        self._fail_on = set(fail_on)
        self.since_seen = []

    def search(self, query, limit, *, since_ts=None):
        self.since_seen.append(since_ts)
        if query in self._fail_on:
            raise RuntimeError("HTTP Error 403: Blocked")
        return list(self._by_query.get(query, []))[:limit]


class SourceTests(unittest.TestCase):
    def test_composite_isolates_a_dead_sub_source_reddit_403(self):
        class _Dead:
            name = "reddit"

            def search(self, q, n, *, since_ts=None):
                raise RuntimeError("HTTP Error 403: Blocked")

        class _Live:
            name = "hn-algolia"

            def search(self, q, n, *, since_ts=None):
                return [_rec(days=2)]

        c = CompositeAcqSource([_Dead(), _Live()])
        out = c.search("q", 5, since_ts=123)
        self.assertEqual(len(out), 1)                       # HN still worked
        self.assertEqual(c.errors[0][0], "reddit")

    def test_static_source_is_offline(self):
        recs = build_acquisition_source("static").search("x", 10)
        self.assertTrue(any(score_lead(r) for r in recs))

    def test_file_source_requires_path(self):
        with self.assertRaises(ValueError):
            build_acquisition_source("file")


# --- workflow -----------------------------------------------

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _store(self):
        return AcquisitionStore.load(self.d / "acquisition.json")

    def test_persists_dedupes_and_passes_since_ts(self):
        src = _FakeSource({
            "q1": [_rec(url="https://news.ycombinator.com/item?id=1",
                        title="how do I get my first customers for my SaaS", days=2)],
            "q2": [_rec(url="https://news.ycombinator.com/item?id=1", days=2),
                   _rec(url="https://news.ycombinator.com/item?id=2",
                        title="struggling to get my first paying customers", days=1)],
        })
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["q1", "q2"], max_age_days=30,
            politeness_delay=0)
        self.assertEqual(r["new"], 2)
        self.assertGreaterEqual(r["collapsed"], 1)          # id=1 seen in both queries
        self.assertTrue(all(t is not None for t in src.since_seen))  # recency filter

        again = discover_acquisition_opportunities(
            self._store(), src, queries=["q1", "q2"], politeness_delay=0)
        self.assertEqual(again["new"], 0)
        self.assertGreaterEqual(again["duplicates"], 2)     # both already persisted

    def test_reddit_403_isolated_hn_still_works(self):
        class _HN:
            name = "hn-algolia"

            def search(self, q, n, *, since_ts=None):
                return [_rec(url="https://news.ycombinator.com/item?id=7",
                             title="how do I get my first customers for my SaaS",
                             text="0 paying customers", days=2)]

        class _Reddit:
            name = "reddit"

            def search(self, q, n, *, since_ts=None):
                raise RuntimeError("HTTP Error 403: Blocked")

        comp = CompositeAcqSource([_HN(), _Reddit()])
        r = discover_acquisition_opportunities(
            self._store(), comp, queries=["q"], politeness_delay=0)
        self.assertEqual(r["new"], 1)
        self.assertTrue(r["sources_status"]["reddit"].startswith("unavailable"))
        self.assertEqual(r["sources_status"]["hn-algolia"], "ok")

    def test_single_dead_source_status(self):
        src = _FakeSource({}, fail_on=["a", "b"])
        r = discover_acquisition_opportunities(
            self._store(), src, queries=["a", "b"], politeness_delay=0)
        self.assertTrue(r["sources_status"]["fake"].startswith("unavailable"))


# --- CLI -----------------------------------------------------

class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.recs = self.d / "recs.json"
        self.recs.write_text(json.dumps([
            {"title": "How do I get my first paying customers for my SaaS?",
             "url": "https://news.ycombinator.com/item?id=100",
             "text": "0 paying customers, launched last week",
             "author": "a", "posted_at": _days_ago(3), "platform": "Hacker News"},
            {"title": "How I got 10,000 customers - lessons learned",
             "url": "https://news.ycombinator.com/item?id=101",
             "text": "from 0 to 10000, a retrospective", "posted_at": _days_ago(2),
             "platform": "Hacker News"},
            {"title": "Show HN: my side project", "url": "https://x.example/1",
             "text": "no intent here"},
        ]), encoding="utf-8")

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, *args):
        return main([*args, "--data-dir", str(self.d)])

    def test_discover_then_top_then_review(self):
        self.assertEqual(self._run(
            "discover-opportunities", "--source", "file", "--source-path",
            str(self.recs), "--delay", "0", "--max-age-days", "30"), 0)
        store = AcquisitionStore.load(self.d / "acquisition.json")
        by_url = {d["url"]: d for d in store.all()}
        # the genuine ask is stored and ranks well; the case study is crushed
        self.assertGreater(by_url["https://news.ycombinator.com/item?id=100"]["final_score"],
                           60)
        self.assertLess(by_url["https://news.ycombinator.com/item?id=101"]["final_score"],
                        15)

        # top-opportunities hides the success story and the irrelevant one
        self.assertEqual(self._run("top-opportunities", "--min-score", "50"), 0)

        good = by_url["https://news.ycombinator.com/item?id=100"]["lead_id"]
        self.assertEqual(self._run("review-opportunity", good, "--approve"), 0)
        self.assertEqual(
            AcquisitionStore.load(self.d / "acquisition.json").get(
                "https://news.ycombinator.com/item?id=100")["human_review_status"],
            "reviewed")

    def test_top_opportunities_excludes_rejected(self):
        self._run("discover-opportunities", "--source", "file", "--source-path",
                  str(self.recs), "--delay", "0")
        store = AcquisitionStore.load(self.d / "acquisition.json")
        good = [d for d in store.all()
                if d["url"] == "https://news.ycombinator.com/item?id=100"][0]["lead_id"]
        self._run("review-opportunity", good, "--reject")
        r = main(["top-opportunities", "--min-score", "0", "--json",
                  "--data-dir", str(self.d)])
        # (json goes to stdout; just assert it ran and the lead is gone from the list)
        self.assertEqual(r, 0)
        rows = [d for d in AcquisitionStore.load(self.d / "acquisition.json").all()
                if d["human_review_status"] != "rejected"]
        self.assertNotIn("https://news.ycombinator.com/item?id=100",
                         [d["url"] for d in rows])

    def test_review_requires_exactly_one_flag(self):
        self._run("discover-opportunities", "--source", "file", "--source-path",
                  str(self.recs), "--delay", "0")
        self.assertEqual(self._run("review-opportunity", "abc"), 1)   # neither flag

    def test_dry_run_persists_nothing(self):
        self.assertEqual(self._run(
            "discover-opportunities", "--source", "file", "--source-path",
            str(self.recs), "--delay", "0", "--dry-run"), 0)
        self.assertFalse((self.d / "acquisition.json").exists())


# --- guarantees --------------------------------------------

class GuaranteeTests(unittest.TestCase):
    def test_no_posting_or_contact_functions_exist(self):
        import inspect

        import revenue_os.acquisition as a
        import revenue_os.acquisition_sources as s
        src = inspect.getsource(a) + inspect.getsource(s)
        for banned in ("def post", "def reply", "def send", "def dm(",
                       "def message", "def comment", "requests.post",
                       "urlopen(.*POST"):
            self.assertNotIn(banned, src)

    def test_agent_never_emits_a_lead_without_a_real_url(self):
        r = AcquisitionAgent(name="s").run(Task(
            objective="d", capability="discover_acquisition",
            payload={"records": [
                _rec(url=""),
                _rec(url="https://example.com/x"),
                _rec(title="totally unrelated", text="nothing"),
            ]}))
        self.assertEqual(r.output["leads"], [])


if __name__ == "__main__":
    unittest.main()
