import re
import tempfile
import unittest
from pathlib import Path

from revenue_os.deliverable import (
    DeliverablePackagerAgent,
    render_landing_html,
    render_product_deliverable_md,
    render_readme,
)
from revenue_os.messages import Task
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import package_deliverables

_CAND = {"name": "alpha", "description": "an open-source automation platform"}
_OFFER = {
    "what_is_sold": "a self-hostable automation platform",
    "price": 29.0, "currency": "USD", "delivery": "digital",
    "call_to_action": "Join the waitlist", "positioning": "for small ops teams",
    "price_is_estimate": True,
}
_DRAFT = {
    "headline": "Automate your ops in a weekend",
    "subheadline": "for teams tired of glue code",
    "body": "Glue scripts rot.\n\nYou get one platform you can host yourself.",
    "primary_cta": "Join the waitlist",
    "faq": [
        {"question": "Price?", "answer": "$29/mo, estimated."},
        {"question": "Self-host?", "answer": "Yes, that's the point."},
        {"question": "Refund?", "answer": "It's a waitlist, nothing charged."},
    ],
}
_PLAN = {"hypothesis": "people will pay for this",
        "success_metric": "25 waitlist signups within 2 weeks"}


class RenderTests(unittest.TestCase):
    def test_landing_has_the_real_parts_and_is_self_contained(self):
        h = render_landing_html(_CAND, _OFFER, _DRAFT, _PLAN)
        self.assertIn("Automate your ops in a weekend", h)
        self.assertIn("29.0 USD", h)
        self.assertIn("Join the waitlist", h)
        self.assertIn("Self-host?", h)                        # faq
        self.assertIn("WAITLIST FORM PLACEHOLDER", h)          # honest placeholder
        self.assertIn("pre-launch demand test", h)             # disclaimer
        self.assertIn("25 waitlist signups", h)                # metric
        # self-contained: no script, no external resources
        self.assertNotIn("<script", h)
        self.assertNotIn("://", h)
        self.assertNotIn("url(http", h)
        # the sample input is disabled - it captures nothing
        self.assertIn("<input type='email' placeholder='you@example.com' disabled>", h)

    def test_landing_degrades_without_a_draft(self):
        h = render_landing_html(_CAND, _OFFER, {}, _PLAN)
        self.assertIn("self-hostable automation platform", h)  # falls back to offer
        self.assertIn("people will pay for this", h)           # falls back to plan
        self.assertNotIn("<details>", h)                       # no faq without a draft

    def test_landing_escapes_injected_text(self):
        # description is used as the headline when the offer has no what_is_sold
        h = render_landing_html(
            {"name": "x", "description": "<script>alert(1)</script>"},
            {"price": 5.0, "currency": "USD", "call_to_action": "go"}, {}, {},
        )
        self.assertNotIn("<script>alert(1)</script>", h)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h)
        # and offer text is escaped too
        h2 = render_landing_html(_CAND, {**_OFFER, "what_is_sold": "<b>x</b>"}, {}, {})
        self.assertNotIn("<b>x</b>", h2)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", h2)

    def test_readme_names_files_and_the_metric_and_warns(self):
        r = render_readme(_CAND, _PLAN, ["landing.html", "README.txt"])
        self.assertIn("landing.html", r)
        self.assertIn("25 waitlist signups within 2 weeks", r)
        self.assertIn("Do NOT record before you have a number", r)
        self.assertIn("captures nothing", r)


# ---------------------------------------------------------------------------
# Phase 11-real P1-6: the real, sellable product deliverable
# ---------------------------------------------------------------------------

_OPP = {"id": "opp_0123456789ab", "title": "Onboarding email pack",
       "target_customer": "SaaS founders",
       "required_work": "a 5-email onboarding sequence"}
_PRODUCT_OFFER = {
    "what_is_sold": "5-email SaaS onboarding sequence",
    "price": 29.9, "currency": "EUR", "delivery": "digital",
    "positioning": "Written for founders who onboard users with no team.",
    "includes": ["The 5-email sequence, ready to adapt",
                 "A short how-to-use guide"],
    "disclaimer": "A specific deliverable - not guaranteed signups or revenue.",
}


class ProductDeliverableTests(unittest.TestCase):
    def test_real_content_derived_from_offer(self):
        md = render_product_deliverable_md(_OPP, _PRODUCT_OFFER)
        self.assertIn("5-email SaaS onboarding sequence", md)
        self.assertIn("SaaS founders", md)
        self.assertIn("opp_0123456789ab", md)
        self.assertIn("The 5-email sequence, ready to adapt", md)
        self.assertIn("A short how-to-use guide", md)
        self.assertIn("How to use this deliverable", md)
        self.assertIn("not guaranteed signups", md)
        self.assertIn("No AI-generated claims, no fabricated data", md)

    def test_includes_become_a_numbered_how_to_guide_not_a_bullet_pitch(self):
        md = render_product_deliverable_md(_OPP, _PRODUCT_OFFER)
        self.assertIn("1. **The 5-email sequence, ready to adapt**", md)
        self.assertIn("2. **A short how-to-use guide**", md)
        # meaningfully different in purpose from the sales-page/checkout copy
        self.assertNotIn("Join the waitlist", md)
        self.assertNotIn("paypal", md.lower())
        self.assertNotIn("<html", md.lower())   # a document, not a web page

    def test_missing_what_is_sold_fails_closed(self):
        with self.assertRaises(ValueError):
            render_product_deliverable_md(_OPP, {**_PRODUCT_OFFER, "what_is_sold": ""})
        with self.assertRaises(ValueError):
            render_product_deliverable_md(_OPP, {})

    def test_missing_includes_falls_back_to_required_work_not_empty(self):
        offer = {**_PRODUCT_OFFER, "includes": []}
        md = render_product_deliverable_md(_OPP, offer)
        self.assertIn("a 5-email onboarding sequence", md)

    def test_deterministic_reproducible(self):
        a = render_product_deliverable_md(_OPP, _PRODUCT_OFFER)
        b = render_product_deliverable_md(_OPP, _PRODUCT_OFFER)
        self.assertEqual(a, b)

    def test_no_secret_or_credential_shaped_content(self):
        md = render_product_deliverable_md(_OPP, _PRODUCT_OFFER)
        for marker in ("CLIENT_SECRET", "PAYPAL_CLIENT_SECRET", "GITHUB_TOKEN",
                      "SMTP_PASSWORD", "api_key", "Bearer "):
            self.assertNotIn(marker, md)


class AgentTests(unittest.TestCase):
    def test_agent_returns_files_and_note(self):
        agent = DeliverablePackagerAgent(name="content_creator")
        r = agent.run(Task(objective="p", capability="package_deliverable",
                           payload={"candidate": _CAND, "offer": _OFFER,
                                    "draft": _DRAFT, "plan": _PLAN}))
        self.assertEqual(r.status, "ok")
        self.assertIn("landing.html", r.output["deliverable"]["files"])
        self.assertTrue(r.output["deliverable"]["has_copy"])
        self.assertIn("Automate your ops", r.output["landing_html"])

    def test_agent_needs_an_offer(self):
        agent = DeliverablePackagerAgent(name="content_creator")
        r = agent.run(Task(objective="p", capability="package_deliverable",
                           payload={"candidate": _CAND, "offer": {}}))
        self.assertEqual(r.status, "error")


class PackageDeliverablesTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_writes_files_attaches_note_and_is_idempotent(self):
        self.store.put(Candidate(name="alpha", status="validated",
                                 description="a platform", offer=dict(_OFFER),
                                 launch_draft=dict(_DRAFT), plan=dict(_PLAN)))
        seen = []
        out = package_deliverables(self.store, self.d,
                                   sink=lambda t, r: seen.append(r.agent))
        self.assertEqual([c.name for c in out], ["alpha"])
        self.assertEqual(seen, ["content_creator"])
        f = self.d / "deliverables" / "alpha" / "landing.html"
        self.assertTrue(f.exists())
        self.assertIn("WAITLIST FORM PLACEHOLDER", f.read_text(encoding="utf-8"))
        self.assertTrue((self.d / "deliverables" / "alpha" / "README.txt").exists())
        got = CandidateStore.load(self.d / "candidates.json").get("alpha")
        self.assertEqual(got.deliverable["dir"], "deliverables/alpha")
        self.assertEqual(got.status, "validated")              # gate not crossed
        # re-run does nothing
        self.assertEqual(package_deliverables(self.store, self.d), [])

    def test_skips_validated_without_an_offer(self):
        self.store.put(Candidate(name="no_offer", status="validated"))
        self.store.save()
        self.assertEqual(package_deliverables(self.store, self.d), [])

    def test_survives_a_rescore(self):
        self.store.put(Candidate(name="alpha", status="validated",
                                 offer=dict(_OFFER)))
        package_deliverables(self.store, self.d)
        self.store.upsert(Candidate(name="alpha", description="a", total=9.9))
        self.assertTrue(
            CandidateStore.load(self.d / "candidates.json").get("alpha").deliverable)


if __name__ == "__main__":
    unittest.main()
