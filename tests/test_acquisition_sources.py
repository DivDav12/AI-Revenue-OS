import unittest
import urllib.error
from unittest import mock

from revenue_os import acquisition_sources as S
from revenue_os.acquisition import canonical_url, score_lead
from revenue_os.acquisition_sources import (
    BlueskySource,
    CompositeAcqSource,
    LemmySource,
    LobstersSource,
    StackExchangeSource,
    build_acquisition_source,
)


class _FakeHTTP:
    """Monkeypatches acquisition_sources._http_json for one test."""

    def __init__(self, by_url):
        self.by_url = by_url
        self.calls = []
        self._orig = S._http_json

    def __enter__(self):
        def fake(url, *, headers=None):
            self.calls.append(url)
            for frag, resp in self.by_url.items():
                if frag in url:
                    if isinstance(resp, Exception):
                        raise resp
                    return resp
            raise AssertionError(f"unexpected URL: {url}")
        S._http_json = fake
        return self

    def __exit__(self, *a):
        S._http_json = self._orig


# --- Stack Exchange -------------------------------------------

def _se_item(qid=1, title="How do I find my first client as a freelancer?",
             site="freelancing", created=1_760_000_000, answers=0, accepted=False,
             score=2, body="I just started freelancing and have zero clients."):
    d = {
        "question_id": qid, "title": title,
        "link": f"https://{site}.stackexchange.com/questions/{qid}/{title[:20]}",
        "creation_date": created, "answer_count": answers,
        "is_answered": answers > 0, "score": score,
        "owner": {"display_name": "founder_x"}, "body": body,
    }
    if accepted:
        d["accepted_answer_id"] = 999
    return d


class StackExchangeTests(unittest.TestCase):
    def test_parses_items_across_sites_with_meta(self):
        with _FakeHTTP({
            "site=freelancing": {"items": [_se_item(1)]},
            "site=webmasters": {"items": [_se_item(2, site="webmasters",
                                                  title="How do I get customers for my SaaS?")]},
        }) as http:
            recs = StackExchangeSource().search("first customers", 10)
        self.assertEqual(len(recs), 2)
        r = recs[0]
        self.assertEqual(r.source, "stackexchange")
        self.assertTrue(r.url.startswith("https://freelancing.stackexchange.com/questions/1"))
        self.assertTrue(r.posted_at.startswith("20"))
        self.assertEqual(r.author, "founder_x")
        self.assertEqual(r.meta, {"answered": False, "answer_count": 0,
                                  "score": 2, "accepted": False})
        self.assertEqual(len(http.calls), 2)   # one request per site (2 sites)

    def test_fromdate_passed_when_since_ts_given(self):
        with _FakeHTTP({"stackexchange.com": {"items": []}}) as http:
            StackExchangeSource(sites=("freelancing",)).search(
                "q", 5, since_ts=1_700_000_000)
        self.assertIn("fromdate=1700000000", http.calls[0])

    def test_http_error_propagates_for_isolation(self):
        with _FakeHTTP({"stackexchange.com":
                        urllib.error.HTTPError("u", 502, "bad", {}, None)}):
            with self.assertRaises(urllib.error.HTTPError):
                StackExchangeSource(sites=("freelancing",)).search("q", 5)

    def test_accepted_answer_becomes_solved_in_scoring(self):
        with _FakeHTTP({"site=freelancing": {"items": [
            _se_item(1, answers=3, accepted=True)]}}):
            rec = StackExchangeSource(sites=("freelancing",)).search("q", 5)[0]
        s = score_lead(rec)
        self.assertTrue(s["solved"])

    def test_unanswered_recent_question_gets_a_relevance_boost(self):
        import time
        recent = int(time.time()) - 3 * 86400
        with _FakeHTTP({"site=freelancing": {"items": [_se_item(1, created=recent,
                                                               answers=0)]}}):
            rec = StackExchangeSource(sites=("freelancing",)).search("q", 5)[0]
        s = score_lead(rec)
        self.assertIn("unanswered SE question", s["match_reason"])

    def test_second_site_hits_the_in_run_cache_not_the_network(self):
        src = StackExchangeSource(sites=("freelancing", "webmasters"))
        with _FakeHTTP({"site=freelancing": {"items": [_se_item(1)]},
                        "site=webmasters": {"items": [_se_item(2, site="webmasters")]},
                        }) as http:
            src.search("first customers", 10)
            src.search("first customers", 10)          # same query again
        self.assertEqual(len(http.calls), 2)           # 2 sites, 2nd call cached
        self.assertEqual(src.requests, 2)

    def test_429_is_retried_once_after_backoff_then_succeeds(self):
        calls = {"n": 0}

        def flaky(url, *, headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(url, 429, "slow down",
                                            {"Retry-After": "1"}, None)
            return {"items": [_se_item(1)]}

        with mock.patch.object(S, "_http_json", flaky), \
                mock.patch.object(S.time, "sleep") as slept:
            recs = StackExchangeSource(sites=("freelancing",)).search("q", 5)
        self.assertEqual(len(recs), 1)
        self.assertEqual(calls["n"], 2)               # one retry
        self.assertTrue(slept.called)                 # honoured the backoff

    def test_repeated_429_propagates_for_failure_isolation(self):
        def always_429(url, *, headers=None):
            raise urllib.error.HTTPError(url, 429, "no", {}, None)

        with mock.patch.object(S, "_http_json", always_429), \
                mock.patch.object(S.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                StackExchangeSource(sites=("freelancing",)).search("q", 5)


# --- Lobsters -----------------------------------------------

def _lob_story(short_id="abc123", title="How do I get my first customers for my SaaS?",
               created="2026-08-25T10:00:00.000-06:00",
               desc="Launched last month, 0 paying customers, what worked for you?"):
    return {
        "short_id": short_id, "title": title, "created_at": created,
        "url": "https://example.com/blog", "score": 3, "comment_count": 4,
        "comments_url": f"https://lobste.rs/s/{short_id}",
        "description_plain": desc,
        "submitter_user": {"username": "founder_l"},
    }


class LobstersTests(unittest.TestCase):
    def test_keyword_filters_the_recent_feed(self):
        keep = _lob_story(short_id="k1")
        drop = _lob_story(short_id="d1", title="A new Rust web framework",
                          desc="benchmarks inside")
        with _FakeHTTP({"lobste.rs/newest.json": [keep, drop]}) as http:
            recs = LobstersSource().search("first customers", 10)
        self.assertEqual([r.url for r in recs], ["https://lobste.rs/s/k1"])
        r = recs[0]
        self.assertEqual(r.source, "lobsters")
        self.assertEqual(r.platform, "Lobsters")
        self.assertEqual(r.author, "founder_l")
        self.assertTrue(r.posted_at.startswith("2026-08-25"))
        self.assertTrue(score_lead(r))
        self.assertIn("newest.json", http.calls[0])

    def test_feed_is_fetched_once_and_reused_across_queries(self):
        with _FakeHTTP({"lobste.rs/newest.json": [_lob_story()]}) as http:
            src = LobstersSource()
            src.search("first customers", 10)
            src.search("paying customers", 10)
        self.assertEqual(len(http.calls), 1)

    def test_since_ts_filters_old_stories(self):
        old = _lob_story(short_id="old1", created="2020-01-01T00:00:00.000Z")
        new = _lob_story(short_id="new1", created="2026-08-28T00:00:00.000Z")
        with _FakeHTTP({"lobste.rs/newest.json": [old, new]}):
            recs = LobstersSource().search("customers", 10, since_ts=1_760_000_000)
        self.assertEqual([r.url for r in recs], ["https://lobste.rs/s/new1"])

    def test_http_error_propagates_for_isolation(self):
        with _FakeHTTP({"lobste.rs": urllib.error.URLError("down")}):
            with self.assertRaises(urllib.error.URLError):
                LobstersSource().search("q", 5)


# --- Lemmy --------------------------------------------------

def _lemmy_row(pid=1, name="How do I find my first clients as a freelancer?",
               published="2026-08-26T09:00:00.500000",
               body="just started, zero clients, need help finding customers"):
    return {
        "post": {"id": pid, "name": name, "published": published, "body": body,
                 "ap_id": f"https://lemmy.world/post/{pid}"},
        "creator": {"name": "founder_m"},
        "community": {"name": "Entrepreneur"},
    }


class LemmyTests(unittest.TestCase):
    def test_parses_a_post_into_a_record(self):
        with _FakeHTTP({"lemmy.world/api/v3/search": {"posts": [_lemmy_row()]}}):
            recs = LemmySource().search("first clients", 10)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.source, "lemmy")
        self.assertEqual(r.url, "https://lemmy.world/post/1")
        self.assertEqual(r.author, "founder_m")
        self.assertIn("c/Entrepreneur", r.platform)
        self.assertTrue(r.posted_at.startswith("2026-08-26"))
        self.assertTrue(score_lead(r))

    def test_since_ts_filters_old_posts(self):
        with _FakeHTTP({"lemmy.world": {"posts": [
            _lemmy_row(1, published="2019-01-01T00:00:00"),
            _lemmy_row(2, published="2026-08-27T00:00:00"),
        ]}}):
            recs = LemmySource().search("q", 10, since_ts=1_760_000_000)
        self.assertEqual([r.url for r in recs], ["https://lemmy.world/post/2"])

    def test_empty_or_malformed_rows_are_skipped(self):
        with _FakeHTTP({"lemmy.world": {"posts": [
            {"post": {"id": 3, "name": ""}}, {"creator": {"name": "x"}},
        ]}}):
            self.assertEqual(LemmySource().search("q", 5), [])

    def test_http_error_propagates_for_isolation(self):
        with _FakeHTTP({"lemmy.world": urllib.error.URLError("down")}):
            with self.assertRaises(urllib.error.URLError):
                LemmySource().search("q", 5)


# --- Bluesky -------------------------------------------------

def _bsky_post(rkey="abc123", handle="dev.bsky.social", created="2026-08-27T10:00:00Z",
               text="Launched my SaaS 2 weeks ago and have 0 paying customers. "
                    "How do I get my first customers?"):
    return {
        "uri": f"at://did:plc:xxxx/app.bsky.feed.post/{rkey}",
        "author": {"handle": handle, "displayName": "Dev Person"},
        "record": {"text": text, "createdAt": created},
        "replyCount": 1, "likeCount": 4,
    }


class BlueskyTests(unittest.TestCase):
    def test_at_uri_becomes_canonical_bsky_url(self):
        with _FakeHTTP({"searchPosts": {"posts": [_bsky_post()]}}):
            recs = BlueskySource().search("first customers", 10)
        self.assertEqual(len(recs), 1)
        self.assertEqual(
            recs[0].url, "https://bsky.app/profile/dev.bsky.social/post/abc123")
        self.assertEqual(recs[0].posted_at, "2026-08-27T10:00:00Z")
        self.assertEqual(recs[0].platform, "Bluesky")
        self.assertEqual(recs[0].source, "bluesky")

    def test_since_param_from_since_ts(self):
        with _FakeHTTP({"searchPosts": {"posts": []}}) as http:
            BlueskySource().search("q", 5, since_ts=1_700_000_000)
        self.assertIn("since=2023", http.calls[0])

    def test_malformed_uri_or_empty_text_is_skipped(self):
        with _FakeHTTP({"searchPosts": {"posts": [
            {"uri": "at://did/not-a-post/x", "author": {"handle": "h"},
             "record": {"text": "hi", "createdAt": "2026-08-27T00:00:00Z"}},
            _bsky_post(text=""),
        ]}}):
            self.assertEqual(BlueskySource().search("q", 5), [])

    def test_http_error_propagates_for_isolation(self):
        with _FakeHTTP({"searchPosts": urllib.error.URLError("down")}):
            with self.assertRaises(urllib.error.URLError):
                BlueskySource().search("q", 5)


# --- composite / factory ------------------------------------

class CompositeAndFactoryTests(unittest.TestCase):
    def test_one_dead_source_does_not_kill_the_other_two(self):
        class _Dead:
            name = "stackexchange"

            def search(self, q, n, *, since_ts=None):
                raise RuntimeError("HTTP Error 502")

        class _Ok:
            def __init__(self, name):
                self.name = name

            def search(self, q, n, *, since_ts=None):
                from revenue_os.acquisition_sources import AcqRecord
                return [AcqRecord(title="how do I get my first customers",
                                  url=f"https://{self.name}.test/x",
                                  text="0 customers", source=self.name)]

        c = CompositeAcqSource([_Dead(), _Ok("bluesky"), _Ok("hn-algolia")])
        out = c.search("q", 5)
        self.assertEqual(len(out), 2)
        self.assertEqual(c.errors[0][0], "stackexchange")

    def test_free_expands_to_the_keyless_sources(self):
        # `free` = the keyless set only (no Reddit, no Bluesky - both gated)
        src = build_acquisition_source("free")
        self.assertEqual(sorted(s.name for s in src._sources),
                         ["hn-algolia", "lemmy", "lobsters", "stackexchange"])
        se = [s for s in src._sources if s.name == "stackexchange"][0]
        self.assertEqual(se.sites, ("freelancing", "webmasters"))

    def test_multiple_source_flags_build_a_composite(self):
        src = build_acquisition_source(["stackexchange", "bluesky"])
        self.assertEqual(sorted(s.name for s in src._sources),
                         ["bluesky", "stackexchange"])

    def test_single_source_returns_it_directly(self):
        self.assertIsInstance(build_acquisition_source(["bluesky"]), BlueskySource)

    def test_web_needs_a_client(self):
        with self.assertRaises(ValueError):
            build_acquisition_source(["web"])

    def test_file_cannot_combine_with_others(self):
        with self.assertRaises(ValueError):
            build_acquisition_source(["file", "bluesky"], path="x")

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            build_acquisition_source(["twitter"])


class HNFreshnessTests(unittest.TestCase):
    def test_since_ts_switches_to_search_by_date_with_ask_hn(self):
        from revenue_os.acquisition_sources import HNAlgoliaSource
        with _FakeHTTP({"search_by_date": {"hits": []}}) as http:
            HNAlgoliaSource().search("first customers", 10, since_ts=1_700_000_000)
        url = http.calls[0]
        self.assertIn("search_by_date", url)
        self.assertIn("ask_hn", url)
        self.assertIn("created_at_i", url)

    def test_no_since_ts_uses_relevance_search(self):
        from revenue_os.acquisition_sources import HNAlgoliaSource
        with _FakeHTTP({"api/v1/search?": {"hits": []}}) as http:
            HNAlgoliaSource().search("q", 5)
        self.assertIn("/api/v1/search?", http.calls[0])
        self.assertNotIn("search_by_date", http.calls[0])


class RegistryTests(unittest.TestCase):
    def test_registry_classifies_sources(self):
        from revenue_os.acquisition_sources import FREE_SOURCES, SOURCE_REGISTRY
        self.assertEqual(SOURCE_REGISTRY["hn-algolia"]["tier"], "free")
        self.assertEqual(SOURCE_REGISTRY["web"]["tier"], "paid")
        self.assertTrue(SOURCE_REGISTRY["reddit"]["auth"])
        self.assertTrue(SOURCE_REGISTRY["bluesky"]["auth"])
        self.assertEqual(FREE_SOURCES,
                         ("hn-algolia", "stackexchange", "lobsters", "lemmy"))
        for n in FREE_SOURCES:
            self.assertFalse(SOURCE_REGISTRY[n]["auth"])
            self.assertEqual(SOURCE_REGISTRY[n]["tier"], "free")


class CanonicalUrlSETests(unittest.TestCase):
    def test_stackexchange_question_normalises_to_id(self):
        a = canonical_url(
            "https://startups.stackexchange.com/questions/123/how-do-i-get-customers")
        b = canonical_url("https://startups.stackexchange.com/questions/123/other-slug/")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://startups.stackexchange.com/questions/123")


if __name__ == "__main__":
    unittest.main()
