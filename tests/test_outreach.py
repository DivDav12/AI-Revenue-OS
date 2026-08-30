import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.outreach import (
    OutreachStore,
    outreach_brief,
    tracked_checkout_link,
)


_LEAD = {
    "lead_id": "abc123def456",
    "canonical_url": "https://news.ycombinator.com/item?id=42",
    "url": "https://news.ycombinator.com/item?id=42",
    "platform": "Hacker News",
    "source": "hn-algolia",
    "posted_at": "2026-08-27T00:00:00+00:00",
    "age_days": 2,
    "age_bucket": "extremely_fresh",
    "prospect_type": "active_problem",
    "prospect_quality": "high",
    "problem_summary": "I launched my SaaS two weeks ago and have 0 paying customers.",
    "why": ["posted 2 day(s) ago (extremely_fresh)",
            "explicit zero/low-customer state (matched ['0 paying customer'])",
            "first-person founder (matched ['my saas', 'i launched'])"],
    "matched_phrases": ["0 paying customer", "my saas", "i launched"],
    "title": "Ask HN: how do I get my first customers?",
    "promo_allowed": "caution",
    "promo_note": "HN bans cold pitching - reply only if you add real value.",
    "human_review_status": "new",
}
_CHECKOUT = "https://example.test/checkout.html"


class BriefTests(unittest.TestCase):
    def test_tracked_link_carries_the_lead_id(self):
        self.assertEqual(tracked_checkout_link(_CHECKOUT, "abc123"),
                         _CHECKOUT + "?lead=abc123")
        self.assertEqual(
            tracked_checkout_link(_CHECKOUT + "?x=1", "abc123"),
            _CHECKOUT + "?x=1&lead=abc123")
        self.assertEqual(tracked_checkout_link(_CHECKOUT, ""), _CHECKOUT)

    def test_brief_uses_only_the_leads_own_words(self):
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT)
        self.assertEqual(b["their_words"], _LEAD["problem_summary"])
        self.assertEqual(b["why_relevant"], _LEAD["why"])
        # the whole brief mentions no business fact not in the lead text
        blob = json.dumps(b).lower()
        self.assertNotIn("their product is", blob)
        self.assertNotIn("they sell", blob)

    def test_cta_is_secondary_and_link_is_tracked(self):
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT)
        self.assertIn("first", b["help_first"].lower())
        self.assertIn("optional", b["help_first"].lower())
        self.assertIn("?lead=abc123def456", b["optional_cta"])
        self.assertIn("?lead=abc123def456", b["checkout_link"])
        self.assertIn("29.90", b["optional_cta"])
        self.assertIn("fine to ignore", b["optional_cta"])

    def test_promo_policy_comes_from_the_lead(self):
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT)
        self.assertEqual(b["promo_allowed"], "caution")
        self.assertEqual(b["promo_note"], _LEAD["promo_note"])

    def test_brief_says_it_never_posts(self):
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT)
        self.assertIn("never posts", b["human_approval"].lower())
        self.assertIn("draft only", b["human_approval"].lower())

    def test_angle_varies_by_signal(self):
        freelance = outreach_brief(
            {**_LEAD, "matched_phrases": ["first client"],
             "problem_summary": "how do I find my first freelance client"},
            checkout_url=_CHECKOUT)
        self.assertIn("freelance", freelance["answer_angle"].lower())
        marketing = outreach_brief(
            {**_LEAD, "matched_phrases": ["how do i market"],
             "problem_summary": "how do I market my launched product"},
            checkout_url=_CHECKOUT)
        self.assertIn("distribution", marketing["answer_angle"].lower())


class OutreachDrafterAgentTests(unittest.TestCase):
    """Phase 2.3: the roster `outreach_drafter` wrapper. Delegates to
    outreach_brief; produces a draft only; never posts."""

    def _run(self, payload):
        from revenue_os.messages import Task
        from revenue_os.outreach_agent import OutreachDrafterAgent
        return OutreachDrafterAgent(name="outreach_drafter").run(
            Task(objective="draft", capability="draft_outreach", payload=payload))

    def test_wraps_outreach_brief_and_tracks_the_link(self):
        r = self._run({"lead": _LEAD, "checkout_url": _CHECKOUT})
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.agent, "outreach_drafter")
        expected = outreach_brief(_LEAD, checkout_url=_CHECKOUT)
        # identical bar the wall-clock `generated_at`
        self.assertEqual({k: v for k, v in r.output.items() if k != "generated_at"},
                         {k: v for k, v in expected.items() if k != "generated_at"})
        self.assertIn("?lead=abc123def456", r.output["checkout_link"])
        self.assertIn("never posts", r.output["human_approval"].lower())

    def test_rejects_a_payload_without_a_lead(self):
        self.assertEqual(self._run({"lead": None}).status, "error")
        self.assertEqual(self._run({"lead": {"no": "id"}}).status, "error")


_GOOD_DRAFT = {
    "reply_draft": (
        "You've got 200 signups but no buyers - that's usually a "
        "conversation gap, not a traffic gap. Pick the 20 signups who "
        "match your ideal user and DM each one this week asking what "
        "stopped them from paying. Do that before touching ads.\n\n"
        "If a structured version would help, I put together a 14-day "
        "first-customers plan - totally fine to ignore: "
        "https://example.test/checkout.html?lead=abc123def456"),
    "help_summary": "talk to 20 matching signups 1:1 before spending on ads",
    "cta_included": True,
    "caveats_for_the_human": ["HN bans cold pitching - keep the last line "
                              "very soft or drop it"],
}


class _FakeAnthropic:
    """Minimal Anthropic-client stand-in for the outreach drafter."""

    class _Usage:
        input_tokens = 900
        output_tokens = 260
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    class _Block:
        type = "tool_use"
        name = "record_reply_draft"

        def __init__(self, payload):
            self.input = payload

    class _Resp:
        def __init__(self, payload):
            self.content = [_FakeAnthropic._Block(payload)]
            self.usage = _FakeAnthropic._Usage()
            self.stop_reason = "tool_use"

    def __init__(self, payload=None):
        self.payload = payload or _GOOD_DRAFT
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return _FakeAnthropic._Resp(self.payload)


class OutreachLlmTests(unittest.TestCase):
    def _drafter(self, payload=None, **kw):
        from revenue_os.outreach_llm import OutreachDrafter
        return OutreachDrafter(client=_FakeAnthropic(payload),
                               checkout_url=_CHECKOUT, **kw)

    def test_drafts_a_tailored_reply_attached_as_draft_reply(self):
        d = self._drafter()
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT, drafter=d)
        dr = b["draft_reply"]
        self.assertIn("200 signups", dr["reply_draft"])
        self.assertIn("?lead=abc123def456", dr["reply_draft"])
        self.assertTrue(dr["cta_included"])
        self.assertEqual(dr["promise_language_flagged"], [])
        self.assertIn("never posts", dr["human_approval"].lower())
        self.assertEqual(d.meter.output_tokens, 260)   # metered

    def test_promise_language_is_flagged_not_dropped(self):
        bad = {**_GOOD_DRAFT,
               "reply_draft": "Do this and you will get customers, guaranteed."}
        d = self._drafter(bad)
        dr = d(_LEAD)
        self.assertTrue(dr["promise_language_flagged"])

    def test_negated_guarantee_is_not_flagged(self):
        ok = {**_GOOD_DRAFT,
              "reply_draft": "This is not a guarantee of customers - just a plan."}
        dr = self._drafter(ok)(_LEAD)
        self.assertEqual(dr["promise_language_flagged"], [])

    def test_cached_lead_costs_nothing_and_makes_no_second_call(self):
        from revenue_os.llm_cache import LlmCache
        cache = LlmCache(Path(self._tmp()) / "c.json")
        client = _FakeAnthropic()
        from revenue_os.outreach_llm import OutreachDrafter
        d = OutreachDrafter(client=client, checkout_url=_CHECKOUT, cache=cache)
        d(_LEAD)
        d(_LEAD)
        self.assertEqual(client.calls, 1)
        self.assertEqual(d.cache_hits, 1)

    def test_drafter_failure_never_breaks_the_brief(self):
        def boom(_lead):
            raise RuntimeError("api down")
        b = outreach_brief(_LEAD, checkout_url=_CHECKOUT, drafter=boom)
        self.assertIn("api down", b["draft_reply"]["error"])
        self.assertIn(b["answer_angle"], b["answer_angle"])   # rest of brief intact

    def test_no_posting_functions_in_outreach_llm_module(self):
        import inspect

        import revenue_os.outreach_llm as m
        src = inspect.getsource(m)
        for banned in ("def post", "def send", "def dm", "def comment",
                       "requests.post", "urlopen", "smtplib",
                       'method="POST"', "method='POST'"):
            self.assertNotIn(banned, src)

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.p = Path(self._dir.name) / "outreach.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_put_is_idempotent_and_keeps_the_human_status(self):
        s = OutreachStore.load(self.p)
        s.put(outreach_brief(_LEAD, checkout_url=_CHECKOUT))
        s.set_status("abc123def456", "approved")
        s.put(outreach_brief(_LEAD, checkout_url=_CHECKOUT))   # re-prepared
        self.assertEqual(len(s.all()), 1)
        self.assertEqual(s.get("abc123def456")["status"], "approved")
        self.assertIn("first_prepared_at", s.get("abc123def456"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        from revenue_os.acquisition import AcquisitionStore
        st = AcquisitionStore.load(self.d / "acquisition.json")
        st.upsert(_LEAD)
        st.save()

    def tearDown(self):
        self._dir.cleanup()

    def test_outreach_brief_command_writes_a_draft(self):
        rc = main(["outreach-brief", "abc123", "--checkout-url", _CHECKOUT,
                   "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        briefs = OutreachStore.load(self.d / "outreach.json")
        b = briefs.get("abc123def456")
        self.assertIsNotNone(b)
        self.assertEqual(b["status"], "draft")
        self.assertIn("?lead=abc123def456", b["brief"]["checkout_link"])

    def test_no_posting_functions_in_outreach_module(self):
        import inspect

        import revenue_os.outreach as o
        src = inspect.getsource(o)
        for banned in ("def post", "def send", "def dm", "requests.post",
                       "urlopen", "smtplib", "def reply", "def comment"):
            self.assertNotIn(banned, src)

    def test_draft_llm_is_budget_gated_not_a_crash(self):
        # --draft llm with an impossible budget -> refused before any client
        rc = main(["outreach-brief", "abc123", "--checkout-url", _CHECKOUT,
                   "--draft", "llm", "--max-cost", "0.00001",
                   "--data-dir", str(self.d)])
        self.assertEqual(rc, 1)
        sp = self.d / "llm_spend.json"
        if sp.exists():
            self.assertEqual(json.loads(sp.read_text())[-1]["cost_usd"], 0.0)

    def test_default_draft_is_template_no_llm_key(self):
        main(["outreach-brief", "abc123", "--checkout-url", _CHECKOUT,
              "--data-dir", str(self.d)])
        b = OutreachStore.load(self.d / "outreach.json").get("abc123def456")
        self.assertNotIn("draft_reply", b["brief"])


if __name__ == "__main__":
    unittest.main()
