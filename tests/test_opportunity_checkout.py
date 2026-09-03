"""Phase 11-real P1-1: `build-opportunity-checkout` CLI command.
Phase 11-real P1-4: `deploy-opportunity-checkout` CLI command.

Proves the opportunity checkout is built from real, persisted state (the
OpportunityStore + the opportunity's own successful PLAN task's frozen
offer - the SAME source `paypal_payments.PayPalPaymentAdapter` reads),
that `custom_id` on the generated page is exactly the opportunity id,
and that it fails closed on every ambiguous / invalid input rather than
falling back to any other opportunity or identifier.

P1-4 additionally proves the built checkout.html can be published (via a
mocked GitHubPagesDeploymentAdapter.deploy - no real network) to a real
URL derived from the deploy mechanism's own result, never fabricated,
and that the whole path refuses to run inside autonomous_context()
before the deploy adapter is ever reached.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from revenue_os import action_class as ac
from revenue_os.cli import main
from revenue_os.deployment import DeploymentResult
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


class OpportunityCheckoutDeployCliTests(unittest.TestCase):
    """Phase 11-real P1-4: `deploy-opportunity-checkout`.

    No real GitHub call in any test here - `GitHubPagesDeploymentAdapter.
    deploy()` itself is mocked (the external boundary), exactly like the
    real command would call it. This proves the CLI command's OWN logic:
    which file it reads, what artifact/slug it builds, how it reports the
    adapter's result, and that it never even constructs the request
    (let alone reaches the adapter) while inside autonomous_context()."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()
        ac._local.__dict__.pop("depth", None)   # never leak a stuck context

    def _write_checkout(self, oid: str, content: bytes = b"<html>checkout</html>"):
        out = self.d / "deliverables" / oid / "checkout.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        return out

    def _run(self, oid: str):
        return main(["deploy-opportunity-checkout", "--data-dir", str(self.d), oid])

    # --- successful deploy, mocked adapter -----------------------------
    def test_successful_deploy_uses_correct_slug_and_file_and_returns_real_url(self):
        oid = "opp_0123456789ab"
        content = b"<html>real checkout content</html>"
        self._write_checkout(oid, content)

        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
        ) as mock_deploy:
            mock_deploy.return_value = DeploymentResult(
                success=True, provider="github_pages",
                live_url="https://divdav12.github.io/site/opp-0123456789ab/checkout.html",
                deployment_id="ghp:divdav12/site:opp-0123456789ab",
                commit_sha="deadbeef")
            rc = self._run(oid)

        self.assertEqual(rc, 0)
        mock_deploy.assert_called_once()
        artifact = mock_deploy.call_args.args[1]   # (self, artifact)
        self.assertEqual(artifact.opportunity_id, oid)
        self.assertEqual(artifact.slug, "opp-0123456789ab")
        self.assertEqual(set(artifact.files), {"checkout.html"})   # ONLY this file
        self.assertEqual(artifact.files["checkout.html"], content)  # exact bytes

    def test_returned_url_is_exactly_the_adapters_url_never_fabricated(self):
        oid = "opp_0123456789ab"
        self._write_checkout(oid)
        real_url = "https://divdav12.github.io/site/opp-0123456789ab/checkout.html"

        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
            return_value=DeploymentResult(success=True, provider="github_pages",
                                          live_url=real_url),
        ):
            with patch("builtins.print") as mock_print:
                rc = self._run(oid)

        self.assertEqual(rc, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn(real_url, printed)

    # --- missing file -> fail closed, adapter never reached ------------
    def test_missing_checkout_html_fails_closed_without_reaching_the_adapter(self):
        oid = "opp_0123456789ab"   # never written
        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
        ) as mock_deploy:
            rc = self._run(oid)
        self.assertEqual(rc, 1)
        mock_deploy.assert_not_called()

    # --- deploy failure / blocked never fabricates a URL ----------------
    def test_blocked_deploy_result_is_reported_as_failure_not_a_url(self):
        oid = "opp_0123456789ab"
        self._write_checkout(oid)
        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
            return_value=DeploymentResult(success=False, blocked=True,
                                          provider="github_pages",
                                          error="set GITHUB_TOKEN and "
                                                "GITHUB_PAGES_REPO"),
        ):
            rc = self._run(oid)
        self.assertEqual(rc, 1)

    # --- autonomous_context() blocks BEFORE the adapter is ever reached -
    def test_autonomous_context_blocks_before_reaching_the_adapter(self):
        oid = "opp_0123456789ab"
        self._write_checkout(oid)
        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
        ) as mock_deploy:
            with ac.autonomous_context():
                rc = self._run(oid)
        self.assertEqual(rc, 1)
        mock_deploy.assert_not_called()

    def test_guard_is_the_same_precedent_as_the_candidate_deploy_checkout_path(self):
        # exercises the real, unmodified guard function directly - no new
        # firewall mechanism was introduced for this command
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy(
                    "activate a paid opportunity checkout page")
        self.assertIsNone(ac.guard_no_money_in_autonomy(
            "activate a paid opportunity checkout page"))   # no-op outside

    # --- no secret is ever read into an artifact / printed --------------
    def test_no_secret_read_or_printed(self):
        oid = "opp_0123456789ab"
        self._write_checkout(oid)
        old = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "ghp_should_never_appear_anywhere"
        try:
            with patch(
                "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
                autospec=True,
                return_value=DeploymentResult(
                    success=True, provider="github_pages",
                    live_url="https://x.github.io/site/opp-0123456789ab/checkout.html"),
            ):
                with patch("builtins.print") as mock_print:
                    self._run(oid)
        finally:
            if old is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertNotIn("ghp_should_never_appear_anywhere", printed)

    # --- intake.html is never deployed by this command ------------------
    def test_intake_html_is_never_touched_or_deployed(self):
        oid = "opp_0123456789ab"
        self._write_checkout(oid)
        intake = self.d / "deliverables" / oid / "intake.html"
        intake.write_bytes(b"<html>intake - must never be deployed</html>")

        with patch(
            "revenue_os.deployment.GitHubPagesDeploymentAdapter.deploy",
            autospec=True,
        ) as mock_deploy:
            mock_deploy.return_value = DeploymentResult(
                success=True, provider="github_pages",
                live_url="https://x.github.io/site/opp-0123456789ab/checkout.html")
            self._run(oid)

        artifact = mock_deploy.call_args.args[1]
        self.assertNotIn("intake.html", artifact.files)

    # --- unrelated existing CLI/checkout paths are unaffected -----------
    def test_build_opportunity_checkout_command_still_works_unaffected(self):
        old = {k: os.environ.get(k) for k in
              ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in old})
        os.environ.update(_ENV)
        try:
            s = OpportunityStore.load(self.d / "opportunities.json")
            oid = s.upsert(Opportunity(title="pack", category="saas"))["id"]
            s.save()
            q = load_tasks(self.d)
            t = q.create(oid, "PLAN")
            q.resolve_dependencies()
            q.claim(t.task_id, "test")
            q.mark_succeeded(t.task_id, {"offer": {"price": 9.9, "currency": "EUR"}})
            q.save()
            rc = main(["build-opportunity-checkout", "--data-dir", str(self.d), oid])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)
        self.assertTrue((self.d / "deliverables" / oid / "checkout.html").exists())

    def test_build_checkout_candidate_command_still_works_unaffected(self):
        from revenue_os.store import Candidate, CandidateStore

        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name="cand-y", description="d", status="launched"))
        store.save()
        old = {k: os.environ.get(k) for k in
              ("PAYPAL_CLIENT_ID", "PAYPAL_ENV", "PAYPAL_CLIENT_SECRET")}
        os.environ.update({k: "" for k in old})
        os.environ.update(_ENV)
        try:
            rc = main(["build-checkout", "--data-dir", str(self.d),
                      "cand-y", "--price", "9.90"])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
