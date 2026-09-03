"""Phase 11-real P1-1: `build-opportunity-checkout` CLI command.

Proves the opportunity checkout is built from real, persisted state (the
OpportunityStore + the opportunity's own successful PLAN task's frozen
offer - the SAME source `paypal_payments.PayPalPaymentAdapter` reads),
that `custom_id` on the generated page is exactly the opportunity id,
and that it fails closed on every ambiguous / invalid input rather than
falling back to any other opportunity or identifier.
"""

import os
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore

_CLIENT_ID = "AbC-live_client_123"
_ENV = dict(PAYPAL_CLIENT_ID=_CLIENT_ID, PAYPAL_ENV="live",
           PAYPAL_CLIENT_SECRET="secret")


class OpportunityCheckoutCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _make_opportunity(self, *, title="pack", price=29.90, currency="EUR",
                          with_plan=True):
        s = OpportunityStore.load(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title=title, category="saas",
                                   target_customer="indie hackers"))["id"]
        s.save()
        if with_plan:
            q = load_tasks(self.d)
            t = q.create(oid, "PLAN")
            q.resolve_dependencies()
            q.claim(t.task_id, "test")
            q.mark_succeeded(t.task_id, {"offer": {
                "price": price, "currency": currency,
                "what_is_sold": "pack"}})
            q.save()
        return oid

    def _run(self, *args, env=None):
        old = {k: os.environ.get(k) for k in
               ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in old})
        os.environ.update(env if env is not None else _ENV)
        try:
            return main(["build-opportunity-checkout", "--data-dir", str(self.d),
                        *args])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # --- A/B: valid opportunity id -> exact custom_id, exact order payload
    def test_A_B_valid_opportunity_produces_matching_custom_id_and_payload(self):
        oid = self._make_opportunity(price=29.90, currency="EUR")
        rc = self._run(oid)
        self.assertEqual(rc, 0)
        f = self.d / "deliverables" / oid / "checkout.html"
        self.assertTrue(f.exists())
        html = f.read_text(encoding="utf-8")
        self.assertIn(f'custom_id: "{oid}"', html)
        self.assertIn('value: "29.90", currency_code: "EUR"', html)
        self.assertIn(f"Order reference: <code>{oid}</code>", html)

    # --- C: unknown / not-yet-planned opportunities fail closed
    def test_C_unknown_opportunity_id_fails_closed(self):
        rc = self._run("opp_" + "a" * 12)
        self.assertEqual(rc, 1)
        self.assertFalse((self.d / "deliverables").exists())

    def test_C_missing_plan_task_fails_closed(self):
        oid = self._make_opportunity(with_plan=False)
        rc = self._run(oid)
        self.assertEqual(rc, 1)
        self.assertFalse((self.d / "deliverables" / oid).exists())

    def test_C_plan_task_not_succeeded_fails_closed(self):
        oid = self._make_opportunity(with_plan=False)
        q = load_tasks(self.d)
        q.create(oid, "PLAN")
        q.save()   # left PENDING
        rc = self._run(oid)
        self.assertEqual(rc, 1)

    def test_C_invalid_offer_in_plan_output_fails_closed(self):
        oid = self._make_opportunity(with_plan=False)
        q = load_tasks(self.d)
        t = q.create(oid, "PLAN")
        q.resolve_dependencies()
        q.claim(t.task_id, "test")
        q.mark_succeeded(t.task_id, {"offer": {"currency": "EUR"}})   # no price
        q.save()
        rc = self._run(oid)
        self.assertEqual(rc, 1)

    def test_C_not_live_paypal_env_fails_closed(self):
        oid = self._make_opportunity()
        env = {**_ENV, "PAYPAL_ENV": "sandbox"}
        rc = self._run(oid, env=env)
        self.assertEqual(rc, 1)

    def test_C_missing_client_id_fails_closed(self):
        oid = self._make_opportunity()
        env = {**_ENV, "PAYPAL_CLIENT_ID": ""}
        rc = self._run(oid, env=env)
        self.assertEqual(rc, 1)

    # --- D: no silent fallback to a different opportunity
    def test_D_two_opportunities_never_cross_attribute(self):
        oid_a = self._make_opportunity(title="pack-a", price=10.0)
        oid_b = self._make_opportunity(title="pack-b", price=20.0)
        self._run(oid_a)
        self._run(oid_b)
        html_a = (self.d / "deliverables" / oid_a / "checkout.html").read_text(
            encoding="utf-8")
        html_b = (self.d / "deliverables" / oid_b / "checkout.html").read_text(
            encoding="utf-8")
        self.assertIn(f'custom_id: "{oid_a}"', html_a)
        self.assertNotIn(f'custom_id: "{oid_b}"', html_a)
        self.assertIn(f'custom_id: "{oid_b}"', html_b)
        self.assertNotIn(f'custom_id: "{oid_a}"', html_b)

    # --- E: the existing candidate/manual checkout command is untouched
    def test_E_existing_build_checkout_command_still_works(self):
        from revenue_os.store import Candidate, CandidateStore

        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name="cand-x", description="d", status="launched"))
        store.save()
        old = {k: os.environ.get(k) for k in
               ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in old})
        os.environ.update(_ENV)
        try:
            rc = main(["build-checkout", "--data-dir", str(self.d),
                      "cand-x", "--price", "9.90"])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)
        html = (self.d / "deliverables" / "cand-x" / "checkout.html").read_text(
            encoding="utf-8")
        self.assertIn('custom_id: "cand-x"', html)

    # --- F: no secret ever reaches the generated file
    def test_F_no_secret_in_generated_checkout(self):
        oid = self._make_opportunity()
        self._run(oid)
        html = (self.d / "deliverables" / oid / "checkout.html").read_text(
            encoding="utf-8")
        for secret_marker in ("secret", "PAYPAL_CLIENT_SECRET", "CLIENT_SECRET"):
            self.assertNotIn(secret_marker, html)


if __name__ == "__main__":
    unittest.main()
