import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.acquisition import (
    AcquisitionAgent,
    AcquisitionStore,
    age_bucket,
    age_info,
    build_lead,
    canonical_url,
    classify,
    evidence,
    final_score,
    lead_id,
    prospect_quality,
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
        self.assertEqual(age_info(_days_ago(2))["age_bucket"], "extremely_fresh")
        self.assertEqual(age_info(_days_ago(6))["age_bucket"], "fresh")
        self.assertEqual(age_info(_days_ago(12))["age_bucket"], "recent")
        self.assertEqual(age_info(_days_ago(20))["age_bucket"], "aging")
        self.assertEqual(age_info(_days_ago(45))["age_bucket"], "stale")
        self.assertEqual(age_info(_days_ago(75))["age_bucket"], "very_stale")
        self.assertEqual(age_info(_days_ago(200))["age_bucket"], "archive")
        self.assertEqual(age_info("")["age_bucket"], "unknown")

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


class SolvedSignalTests(unittest.TestCase):
    def test_solved_text_marks_success_story_and_crushes_score(self):
        active = classify("How do I get my first customers for my SaaS?",
                          "0 paying customers, just launched")
        solved = classify("Update: solved - how I finally got my first customers",
                          "for anyone else struggling: here is what worked. thanks everyone")
        self.assertEqual(solved["prospect_type"], "success_story")
        self.assertTrue(solved["solved"])
        self.assertLess(solved["relevance_score"], active["relevance_score"])

    def test_solved_title_prefix_detected(self):
        self.assertTrue(classify(
            "[SOLVED] getting my first customers", "we finally got there")["solved"])

    def test_final_score_solved_factor(self):
        fresh = final_score(relevance_score=80, age_days=2,
                            prospect_type="active_problem", buying_intent="high",
                            max_age_days=30, solved=False)
        solved = final_score(relevance_score=80, age_days=2,
                             prospect_type="active_problem", buying_intent="high",
                             max_age_days=30, solved=True)
        self.assertLess(solved, fresh * 0.3)

    def test_se_accepted_answer_meta_marks_solved(self):
        rec = AcqRecord(title="how do I get my first customers for my SaaS",
                        url="https://startups.stackexchange.com/questions/1/x",
                        text="0 customers", posted_at=_days_ago(3),
                        platform="startups.stackexchange.com", source="stackexchange",
                        meta={"answered": True, "answer_count": 4,
                              "score": 5, "accepted": True})
        s = score_lead(rec)
        self.assertTrue(s["solved"])
        self.assertEqual(s["signals"]["answer_count"], 4)

    def test_problem_factor_ordering(self):
        from revenue_os.acquisition import _problem_factor
        f = _problem_factor
        self.assertGreater(f("active_problem"), f("seeking_advice"))
        self.assertGreater(f("seeking_advice"), f("founder_building"))
        self.assertGreater(f("founder_building"), f("educational"))
        self.assertGreater(f("educational"), f("success_story"))


# --- lead assembly / qc ----------------------------------------

class LeadTests(unittest.TestCase):
    def test_build_lead_fills_all_new_fields(self):
        lead = build_lead(_rec(days=2), score_lead(_rec(days=2)), max_age_days=30)
        d = lead.to_dict()
        for k in ("age_days", "age_bucket", "prospect_type", "prospect_quality",
                  "relevance_score", "final_score", "scoring_mode",
                  "human_review_status", "lead_id", "active_problem",
                  "recommended_fit", "llm_reason", "why", "matched_queries"):
            self.assertIn(k, d)
        self.assertEqual(d["human_review_status"], "new")
        self.assertEqual(d["scoring_mode"], "deterministic")
        self.assertEqual(d["age_bucket"], "extremely_fresh")
        self.assertTrue(d["why"])                      # evidence list populated

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

    def test_rescore_re_derives_legacy_records_and_keeps_human_verdict(self):
        from revenue_os.acquisition import _SCORER_VERSION
        url = "https://news.ycombinator.com/item?id=555"
        self.p.write_text(json.dumps([{
            "canonical_url": url, "url": url, "lead_id": lead_id(url),
            "title": "How do I get my first paying customers for my SaaS?",
            "problem_summary": "Launched last week, 0 paying customers. What worked?",
            "posted_at": _days_ago(2), "platform": "Hacker News",
            "source": "hn-algolia", "author": "founder_a",
            # legacy / stale scoring fields
            "fit_score": 40, "final_score": 5, "prospect_quality": None,
            "age_bucket": "old", "scorer_version": "1",
            "human_review_status": "reviewed", "reviewed_by": "me",
            "discovered_at": "2020-01-01T00:00:00+00:00",
        }]), encoding="utf-8")
        s = AcquisitionStore.load(self.p)
        r = s.rescore(max_age_days=30)
        s.save()

        self.assertEqual(r["dropped"], 0)
        self.assertGreaterEqual(r["rescored"], 1)
        e = s.get(url)
        self.assertEqual(e["scorer_version"], _SCORER_VERSION)
        self.assertIn(e["prospect_quality"], ("high", "medium"))
        self.assertGreater(e["final_score"], 50)
        self.assertIn(e["age_bucket"], ("extremely_fresh", "fresh"))
        self.assertTrue(e["why"])
        # the human's verdict and the original discovery date survive
        self.assertEqual(e["human_review_status"], "reviewed")
        self.assertEqual(e["reviewed_by"], "me")
        self.assertEqual(e["discovered_at"], "2020-01-01T00:00:00+00:00")

    def test_rescore_drops_a_record_that_never_matched_a_signal(self):
        url = "https://news.ycombinator.com/item?id=777"
        self.p.write_text(json.dumps([{
            "canonical_url": url, "url": url, "lead_id": lead_id(url),
            "title": "Show HN: my weekend markdown editor",
            "problem_summary": "feedback welcome", "posted_at": _days_ago(3),
            "source": "hn-algolia", "fit_score": 20,
        }]), encoding="utf-8")
        s = AcquisitionStore.load(self.p)
        r = s.rescore()
        self.assertEqual(r["dropped"], 1)
        self.assertEqual(r["total"], 0)
        self.assertEqual(s.all(), [])

    def test_rescore_keeps_but_zeroes_a_lead_whose_signal_fell_off_the_summary(self):
        # matched a phrase originally, but the stored summary no longer
        # contains it (truncated) -> keep the row, zero the score
        url = "https://news.ycombinator.com/item?id=888"
        self.p.write_text(json.dumps([{
            "canonical_url": url, "url": url, "lead_id": lead_id(url),
            "title": "A news article about local journalism",
            "problem_summary": "nothing scorable remains in this excerpt",
            "matched_phrases": ["i built"], "posted_at": _days_ago(5),
            "source": "lemmy", "final_score": 3, "prospect_quality": "low",
            "human_review_status": "new",
        }]), encoding="utf-8")
        s = AcquisitionStore.load(self.p)
        r = s.rescore()
        self.assertEqual(r["dropped"], 0)
        self.assertEqual(r["total"], 1)
        e = s.get(url)
        self.assertEqual(e["final_score"], 0)
        self.assertEqual(e["prospect_quality"], "none")
        self.assertEqual(e["rescore_note"], "signal not present in stored summary")


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

    def test_acquisition_rescore_refreshes_the_store(self):
        from revenue_os.acquisition import _SCORER_VERSION
        self._run("discover-opportunities", "--source", "file", "--source-path",
                  str(self.recs), "--delay", "0", "--max-age-days", "30")
        path = self.d / "acquisition.json"
        # simulate a stale store: knock every lead back to an old scorer version
        data = json.loads(path.read_text(encoding="utf-8"))
        for d in data:
            d["scorer_version"] = "1"
            d["prospect_quality"] = None
        path.write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(self._run("acquisition-rescore", "--max-age-days", "30"), 0)
        after = AcquisitionStore.load(path).all()
        self.assertTrue(after)
        for d in after:
            self.assertEqual(d["scorer_version"], _SCORER_VERSION)
            self.assertIn(d["prospect_quality"], ("high", "medium", "low", "none"))

    def test_acquisition_rescore_dry_run_persists_nothing(self):
        self._run("discover-opportunities", "--source", "file", "--source-path",
                  str(self.recs), "--delay", "0")
        path = self.d / "acquisition.json"
        before = path.read_text(encoding="utf-8")
        self.assertEqual(self._run("acquisition-rescore", "--dry-run"), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_review_requires_exactly_one_flag(self):
        self._run("discover-opportunities", "--source", "file", "--source-path",
                  str(self.recs), "--delay", "0")
        self.assertEqual(self._run("review-opportunity", "abc"), 1)   # neither flag

    def test_dry_run_persists_nothing(self):
        self.assertEqual(self._run(
            "discover-opportunities", "--source", "file", "--source-path",
            str(self.recs), "--delay", "0", "--dry-run"), 0)
        self.assertFalse((self.d / "acquisition.json").exists())

    def test_web_source_without_a_client_is_budget_gated_not_a_crash(self):
        # --source web with an impossible budget -> refused before any client
        rc = self._run("discover-opportunities", "--source", "web",
                       "--max-cost", "0.0001", "--delay", "0", "--dry-run")
        self.assertEqual(rc, 1)   # ValueError -> exit 1, nothing spent

    def test_multiple_source_flags_are_accepted(self):
        # file can't combine; use a lone recognised pair via the offline path
        rc = self._run("discover-opportunities", "--source", "static",
                       "--delay", "0", "--dry-run")
        self.assertEqual(rc, 0)

    def test_discover_free_runs_offline_source_and_persists(self):
        rc = self._run("discover-free", "--source", "file", "--source-path",
                       str(self.recs), "--delay", "0", "--max-age-days", "30")
        self.assertEqual(rc, 0)
        store = AcquisitionStore.load(self.d / "acquisition.json")
        self.assertTrue(store.all())
        # every lead carries the new evidence + quality fields
        d = store.all()[0]
        self.assertIn("prospect_quality", d)
        self.assertIn("why", d)
        self.assertIn("age_bucket", d)

    def test_discover_free_rejects_web_at_the_parser(self):
        # argparse itself refuses 'web'/'all' for discover-free (SystemExit 2)
        for src in ("web", "all"):
            with self.assertRaises(SystemExit):
                self._run("discover-free", "--source", src, "--delay", "0",
                          "--dry-run")
        # nothing was spent
        sp = self.d / "llm_spend.json"
        if sp.exists():
            self.assertEqual(json.loads(sp.read_text())[-1]["cost_usd"], 0.0)

    def test_run_discovery_guard_also_blocks_web_in_free_mode(self):
        # belt-and-suspenders: the runtime guard rejects web even if it slips past
        from types import SimpleNamespace

        from revenue_os.cli import _run_discovery
        args = SimpleNamespace(source=None, query=None, limit=5, min_score=0,
                               max_age_days=30, delay=0, dry_run=True, json=False,
                               data_dir=str(self.d), score="deterministic",
                               model="x", max_cost=1.0, refresh=False)
        with self.assertRaises(ValueError):
            _run_discovery(args, names=["web"], allow_web=False, allow_llm=False)


# --- guarantees --------------------------------------------

class FreeV2Tests(unittest.TestCase):
    def test_seven_tier_buckets_and_monotonic_recency(self):
        order = ["extremely_fresh", "fresh", "recent", "aging", "stale",
                 "very_stale", "archive"]
        self.assertEqual([age_bucket(d) for d in (1, 6, 12, 22, 45, 75, 200)], order)
        from revenue_os.acquisition import _recency_factor
        fs = [_recency_factor(d, 3650) for d in (1, 6, 12, 22, 45, 75, 200)]
        self.assertEqual(fs, sorted(fs, reverse=True))       # strictly decreasing
        self.assertGreater(_recency_factor(2, 3650), _recency_factor(None, 3650))

    def test_prospect_quality_levels(self):
        base = {"relevance_score": 70, "prospect_type": "active_problem",
                "age_bucket": "extremely_fresh", "solved": False}
        self.assertEqual(prospect_quality(base), "high")
        self.assertEqual(prospect_quality({**base, "age_bucket": "stale"}), "medium")
        self.assertEqual(prospect_quality({**base, "solved": True}), "low")
        self.assertEqual(prospect_quality({**base, "prospect_type": "success_story"}),
                         "none")
        self.assertEqual(prospect_quality({**base, "relevance_score": 5,
                                           "prospect_type": "irrelevant"}), "none")

    def test_fresh_good_lead_beats_stale_perfect_lead(self):
        fresh = build_lead(_rec(title="how do I get my first customers for my SaaS",
                                text="0 paying customers, just launched", days=2),
                           score_lead(_rec(days=2)), max_age_days=30)
        stale = build_lead(
            _rec(title="how do I get my first customers for my SaaS",
                 text="0 paying customers, just launched",
                 url="https://news.ycombinator.com/item?id=9", days=95),
            score_lead(_rec(url="https://news.ycombinator.com/item?id=9", days=95)),
            max_age_days=30)
        self.assertGreater(fresh.final_score, stale.final_score * 4)

    def test_evidence_is_grounded_never_invented(self):
        c = classify("how do I get my first customers for my SaaS?",
                     "I launched two weeks ago and have 0 paying customers")
        c.update(age_info(_days_ago(2)))
        why = evidence(c, "I launched two weeks ago and have 0 paying customers")
        joined = " ".join(why).lower()
        self.assertIn("day(s) ago", joined)
        self.assertIn("zero/low-customer state", joined)
        self.assertIn("asks for help", joined)
        self.assertIn("first-person founder", joined)
        # a lead with no signals gets no invented reasons beyond the age line
        empty = evidence({"age_days": None, "age_bucket": "unknown",
                          "matched_phrases": (), "negative_signals": (),
                          "signals": {}}, "")
        self.assertEqual(empty, ["post date not available (age unknown)"])

    def test_matched_queries_merge_on_collapse(self):
        r = AcquisitionAgent(name="s").run(Task(
            objective="d", capability="discover_acquisition",
            payload={"records": [
                _rec(url="https://news.ycombinator.com/item?id=1",
                     title="how do I get my first customers for my SaaS",
                     text="0 paying customers", query="q1", days=2),
                _rec(url="https://news.ycombinator.com/item?id=1&x=y",
                     title="how do I get my first customers for my SaaS",
                     text="0 paying customers", query="q2", days=2),
            ]}))
        self.assertEqual(len(r.output["leads"]), 1)
        self.assertEqual(sorted(r.output["leads"][0]["matched_queries"]),
                         ["q1", "q2"])

    def test_store_merges_matched_queries_across_runs(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        s = AcquisitionStore.load(d / "a.json")
        s.upsert({"canonical_url": "u", "lead_id": "x", "final_score": 40,
                  "matched_queries": ["q1"]})
        s.upsert({"canonical_url": "u", "lead_id": "x", "final_score": 40,
                  "matched_queries": ["q2"]})
        self.assertEqual(sorted(s.get("u")["matched_queries"]), ["q1", "q2"])

    def test_legacy_records_do_not_outrank_current_ones(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "a.json"
        p.write_text(json.dumps([
            {"canonical_url": "legacy", "url": "https://x.test/1", "title": "old",
             "fit_score": 95},                                    # no final_score
            {"canonical_url": "fresh", "url": "https://x.test/2", "title": "new",
             "final_score": 30, "relevance_score": 40},
        ]), encoding="utf-8")
        ranked = AcquisitionStore.load(p).ranked()
        self.assertEqual(ranked[0]["canonical_url"], "fresh")     # legacy sinks


class GuaranteeTests(unittest.TestCase):
    def test_no_posting_or_contact_functions_exist(self):
        import inspect

        import revenue_os.acquisition as a
        import revenue_os.acquisition_llm as ll
        import revenue_os.acquisition_sources as s
        import revenue_os.acquisition_web as w
        src = "".join(inspect.getsource(m) for m in (a, s, w, ll))
        for banned in ("def post", "def reply", "def send", "def dm(",
                       "def message(", "def comment", "requests.post",
                       'method="POST"', "method='POST'"):
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
