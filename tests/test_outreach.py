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
        self.assertEqual(r.output, outreach_brief(_LEAD, checkout_url=_CHECKOUT))
        self.assertIn("?lead=abc123def456", r.output["checkout_link"])
        self.assertIn("never posts", r.output["human_approval"].lower())

    def test_rejects_a_payload_without_a_lead(self):
        self.assertEqual(self._run({"lead": None}).status, "error")
        self.assertEqual(self._run({"lead": {"no": "id"}}).status, "error")


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


if __name__ == "__main__":
    unittest.main()
