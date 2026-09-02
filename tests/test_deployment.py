"""Deployment adapters + DEPLOY task integration (Phase 7)."""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import acceptance
from revenue_os.acceptance import accept_opportunity, execution_view, release_task
from revenue_os.deployment import (
    DeploymentArtifact,
    FakeDeploymentAdapter,
    GitHubPagesDeploymentAdapter,
    valid_live_url,
)
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.task_adapters import DeployTaskAdapter, default_registry
from revenue_os.worker import AdapterContext, Worker, run_worker


def _artifact(oid="opp_x", html="<h1>hi</h1>"):
    return DeploymentArtifact(opportunity_id=oid, slug="opp-x",
                              files={"index.html": html})


class _CountingFake(FakeDeploymentAdapter):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def deploy(self, artifact):
        self.calls += 1
        return super().deploy(artifact)


# ---------------------------------------------------------------------------
# adapter unit tests
# ---------------------------------------------------------------------------

class FakeAdapterTests(unittest.TestCase):
    def test_success(self):
        r = FakeDeploymentAdapter().deploy(_artifact())
        self.assertTrue(r.success)
        self.assertEqual(r.provider, "fake")
        self.assertTrue(valid_live_url(r.live_url))
        self.assertTrue(r.deployment_id)
        self.assertTrue(r.commit_sha)
        self.assertEqual(r.error, "")

    def test_failure(self):
        r = FakeDeploymentAdapter(fail=True, error="boom").deploy(_artifact())
        self.assertFalse(r.success)
        self.assertFalse(r.blocked)
        self.assertEqual(r.live_url, "")
        self.assertIn("boom", r.error)

    def test_blocked_missing_config(self):
        r = FakeDeploymentAdapter(blocked=True).deploy(_artifact())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertEqual(r.live_url, "")

    def test_success_without_url_is_still_not_usable(self):
        r = FakeDeploymentAdapter(drop_url=True).deploy(_artifact())
        self.assertFalse(valid_live_url(r.live_url))

    def test_idempotent_same_content(self):
        a = _CountingFake()
        r1 = a.deploy(_artifact(html="<x>"))
        r2 = a.deploy(_artifact(html="<x>"))
        self.assertEqual(r1.deployment_id, r2.deployment_id)
        r3 = a.deploy(_artifact(html="<different>"))
        self.assertNotEqual(r1.deployment_id, r3.deployment_id)


class GitHubPagesAdapterTests(unittest.TestCase):
    def test_missing_credentials_fails_closed(self):
        r = GitHubPagesDeploymentAdapter(environ={}).deploy(_artifact())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertIn("GITHUB_TOKEN", r.error)
        self.assertEqual(r.live_url, "")

    def test_real_path_with_injected_client(self):
        from revenue_os.deploy import GitHubPagesConfig

        class _FakeGH:
            def __init__(self):
                self.puts = []

            def get_file(self, repo_path):
                return None

            def put_file(self, repo_path, content, *, message, sha=None):
                self.puts.append(repo_path)
                return {"commit": {"sha": "abc123"}}

        cfg = GitHubPagesConfig(token="t", owner="me", repo="site", branch="main")
        gh = _FakeGH()
        r = GitHubPagesDeploymentAdapter(config=cfg, client=gh).deploy(_artifact())
        self.assertTrue(r.success)
        self.assertEqual(r.live_url, "https://me.github.io/site/opp-x/index.html")
        self.assertEqual(r.commit_sha, "abc123")
        self.assertIn("opp-x/index.html", gh.puts)


# ---------------------------------------------------------------------------
# DEPLOY task adapter + worker/queue integration
# ---------------------------------------------------------------------------

class DeployBase(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _accepted_opp(self):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="Onboarding email pack", category="saas",
                                   est_revenue_eur=140,
                                   target_customer="SaaS founders"))["id"]
        for st in ("SCORED", "SELECTED"):
            s.transition(oid, st, reason="setup", source="test")
        s.save()
        accept_opportunity(self.d, oid, actor="owner")
        return oid

    def _registry(self, deploy_adapter):
        reg = default_registry()
        reg.register(DeployTaskAdapter(deploy_adapter))
        return reg

    def _deploy_task(self, oid):
        q = load_tasks(self.d)
        return next(t for t in q.by_opportunity(oid) if t.task_type == "DEPLOY")


class DeployTaskAdapterTests(DeployBase):
    def test_deploy_stays_blocked_until_released(self):
        oid = self._accepted_opp()
        run_worker(self.d, registry=self._registry(FakeDeploymentAdapter()),
                   max_ticks=50)
        dep = self._deploy_task(oid)
        self.assertEqual(dep.status, "BLOCKED_APPROVAL")
        self.assertNotEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")

    def test_release_then_successful_deploy_makes_opportunity_live(self):
        oid = self._accepted_opp()
        reg = self._registry(FakeDeploymentAdapter(base_url="https://x.pages.test"))
        run_worker(self.d, registry=reg, max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id, actor="owner")
        run_worker(self.d, registry=reg, max_ticks=50)

        dep = self._deploy_task(oid)
        self.assertEqual(dep.status, "SUCCEEDED")
        self.assertTrue(valid_live_url(dep.output["live_url"]))
        s = load_opportunities(self.d).get(oid)
        self.assertEqual(s["state"], "LIVE")
        self.assertEqual(s["execution"]["live_url"], dep.output["live_url"])
        types = [e["type"] for e in load_events(self.d).all()]
        self.assertIn("TASK_UNBLOCKED", types)
        self.assertIn("DEPLOYMENT_COMPLETE", types)
        trans = [e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "LIVE"]
        self.assertEqual(len(trans), 1)

    def test_failed_deploy_does_not_make_opportunity_live(self):
        oid = self._accepted_opp()
        reg = self._registry(FakeDeploymentAdapter(fail=True, error="pages 500"))
        run_worker(self.d, registry=reg, max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id)
        run_worker(self.d, registry=reg, max_ticks=50)

        dep = self._deploy_task(oid)
        self.assertIn(dep.status, ("FAILED_RETRYABLE", "FAILED_FINAL"))
        s = load_opportunities(self.d).get(oid)
        self.assertNotEqual(s["state"], "LIVE")
        self.assertNotIn("LIVE", [t["next_state"] for t in s["transitions"]])
        self.assertNotIn("DEPLOYMENT_COMPLETE",
                         [e["type"] for e in load_events(self.d).all()])

    def test_missing_credentials_blocks_deploy_no_fake_url(self):
        oid = self._accepted_opp()
        reg = self._registry(GitHubPagesDeploymentAdapter(environ={}))
        run_worker(self.d, registry=reg, max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id)
        run_worker(self.d, registry=reg, max_ticks=50)

        dep = self._deploy_task(oid)
        self.assertIn(dep.status, ("FAILED_RETRYABLE", "FAILED_FINAL"))
        self.assertIn("BLOCKED", dep.error)
        self.assertNotEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")
        self.assertFalse(dep.output.get("live_url"))

    def test_success_without_url_is_not_live(self):
        oid = self._accepted_opp()
        reg = self._registry(FakeDeploymentAdapter(drop_url=True))
        run_worker(self.d, registry=reg, max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id)
        run_worker(self.d, registry=reg, max_ticks=50)

        dep = self._deploy_task(oid)
        self.assertEqual(dep.status, "FAILED_FINAL")
        self.assertIn("no valid live_url", dep.error)
        self.assertNotEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")

    def test_deploy_is_idempotent(self):
        oid = self._accepted_opp()
        fake = _CountingFake()
        reg = self._registry(fake)
        run_worker(self.d, registry=reg, max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id)
        run_worker(self.d, registry=reg, max_ticks=50)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")

        # a fresh DEPLOY task for the same opportunity + same page must not
        # re-publish - the recorded deployment short-circuits it
        q = load_tasks(self.d)
        first = self._deploy_task(oid)
        prior_deploy_id = first.output["deployment_id"]
        t2 = q.create(oid, "DEPLOY", priority=9, depends_on=list(first.depends_on))
        q.resolve_dependencies()
        q.save()
        run_worker(self.d, registry=reg, max_ticks=10)
        self.assertEqual(fake.calls, 1)                  # not called again
        t2b = load_tasks(self.d).get(t2.task_id)
        self.assertEqual(t2b.status, "SUCCEEDED")
        self.assertTrue(t2b.output.get("idempotent"))
        self.assertEqual(t2b.output["deployment_id"], prior_deploy_id)

    def test_no_page_to_deploy_fails_non_retryably(self):
        s = OpportunityStore(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title="x", category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY"):
            s.transition(oid, st, reason="s", source="t")
        s.save()
        q = load_tasks(self.d)
        dep = q.create(oid, "DEPLOY", priority=5)
        q.resolve_dependencies()
        q.save()
        run_worker(self.d, registry=self._registry(FakeDeploymentAdapter()),
                   max_ticks=5)
        self.assertEqual(load_tasks(self.d).get(dep.task_id).status, "FAILED_FINAL")

    def test_restart_between_release_and_deploy(self):
        oid = self._accepted_opp()
        adapter = FakeDeploymentAdapter()
        run_worker(self.d, registry=self._registry(adapter), max_ticks=50)
        release_task(self.d, self._deploy_task(oid).task_id, actor="owner")
        # "restart": brand-new worker + registry instance reads state from disk
        run_worker(self.d, registry=self._registry(FakeDeploymentAdapter()),
                   max_ticks=50)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")

    def test_execution_view_exposes_live_url_and_blocked_task(self):
        oid = self._accepted_opp()
        reg = self._registry(FakeDeploymentAdapter())
        run_worker(self.d, registry=reg, max_ticks=50)
        row = execution_view(self.d)[0]
        self.assertTrue(row["blocked_task_id"])
        self.assertEqual(row["live_url"], "")

        release_task(self.d, row["blocked_task_id"])
        run_worker(self.d, registry=reg, max_ticks=50)
        row = execution_view(self.d)[0]
        self.assertTrue(valid_live_url(row["live_url"]))
        self.assertEqual(row["state"], "LIVE")


class DeployApprovalFirewallTests(DeployBase):
    def test_release_requires_blocked_state(self):
        oid = self._accepted_opp()
        dep_id = self._deploy_task(oid).task_id
        release_task(self.d, dep_id)
        with self.assertRaises(acceptance.AcceptanceError):
            release_task(self.d, dep_id)          # already released

    def test_worker_never_runs_deploy_before_release(self):
        oid = self._accepted_opp()
        fake = _CountingFake()
        # drain many times - DEPLOY must never execute
        for _ in range(3):
            run_worker(self.d, registry=self._registry(fake), max_ticks=50)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self._deploy_task(oid).status, "BLOCKED_APPROVAL")

    def test_jarvis_release_action(self):
        from revenue_os.jarvis_server import apply_control, jarvis_snapshot

        oid = self._accepted_opp()
        # run the chain via the real registry through the background job path
        # (simplest: call worker directly here with the real registry)
        run_worker(self.d, max_ticks=50)          # real registry: DEPLOY blocked
        dep_id = self._deploy_task(oid).task_id

        msg = apply_control(self.d, "owner",
                            {"action": ["release-task"], "task": [dep_id]})
        self.assertIn("released", msg)
        self.assertEqual(load_tasks(self.d).get(dep_id).status, "READY")
        snap = jarvis_snapshot(self.d)
        self.assertTrue(snap["execution"])


if __name__ == "__main__":
    unittest.main()
