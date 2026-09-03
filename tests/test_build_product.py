"""Phase 11-real P1-6: BUILD_PRODUCT generates and persists the real
digital deliverable.

Unit-level tests construct `AdapterContext` directly (fast, isolated).
Integration tests drive the real chain (opportunity_engine.generate +
accept_opportunity + Worker) - the same architecture every other Phase
11-real test uses - to prove BUILD_PRODUCT actually runs, actually
writes a file, and that P1-1 through P1-5 remain unaffected.
"""

import os
import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_engine
from revenue_os.acceptance import accept_opportunity, release_task
from revenue_os.deployment import FakeDeploymentAdapter
from revenue_os.execution import ExecutionTask, load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.task_adapters import BuildProductTaskAdapter, DeployTaskAdapter, default_registry
from revenue_os.worker import AdapterContext, Worker

_OFFER = {
    "what_is_sold": "5-email SaaS onboarding sequence",
    "price": 29.9, "currency": "EUR", "delivery": "digital",
    "positioning": "Written for founders who onboard users with no team.",
    "includes": ["The 5-email sequence, ready to adapt",
                 "A short how-to-use guide"],
    "disclaimer": "A specific deliverable - not guaranteed signups or revenue.",
}


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _opportunity(self, *, title="Onboarding email pack"):
        s = OpportunityStore.load(self.d / "opportunities.json")
        oid = s.upsert(Opportunity(title=title, category="saas",
                                   target_customer="SaaS founders",
                                   required_work="a 5-email onboarding sequence"))["id"]
        s.save()
        return oid

    def _ctx(self, oid, *, plan_offer=_OFFER):
        task = ExecutionTask(opportunity_id=oid, task_type="BUILD_PRODUCT")
        opp = load_opportunities(self.d).get(oid)
        dep_outputs = {"PLAN": {"offer": plan_offer}} if plan_offer is not None else {}
        return AdapterContext(self.d, task, opp, dep_outputs)


class UnitTests(_Base):
    def test_A_writes_a_real_file_under_the_opportunity(self):
        oid = self._opportunity()
        result = BuildProductTaskAdapter().run(self._ctx(oid))
        self.assertTrue(result.ok, result.error)
        path = self.d / "deliverables" / oid / "product.md"
        self.assertTrue(path.is_file())
        self.assertEqual(result.output["deliverable_path"],
                         f"deliverables/{oid}/product.md")

    def test_D_content_is_the_real_product_not_marketing_copy(self):
        oid = self._opportunity()
        BuildProductTaskAdapter().run(self._ctx(oid))
        content = (self.d / "deliverables" / oid / "product.md").read_text(
            encoding="utf-8")
        self.assertIn("5-email SaaS onboarding sequence", content)
        self.assertIn("The 5-email sequence, ready to adapt", content)
        self.assertNotIn("<html", content.lower())
        self.assertNotIn("paypal", content.lower())

    def test_B_two_opportunities_never_cross_write(self):
        oid_a = self._opportunity(title="pack-a")
        oid_b = self._opportunity(title="pack-b")
        BuildProductTaskAdapter().run(self._ctx(oid_a))
        BuildProductTaskAdapter().run(self._ctx(oid_b))
        content_a = (self.d / "deliverables" / oid_a / "product.md").read_text(
            encoding="utf-8")
        content_b = (self.d / "deliverables" / oid_b / "product.md").read_text(
            encoding="utf-8")
        # different opportunities -> different target_customer/id in the doc
        self.assertIn(oid_a, content_a)
        self.assertNotIn(oid_a, content_b)
        self.assertIn(oid_b, content_b)
        self.assertNotIn(oid_b, content_a)

    def test_F_no_frozen_offer_fails_closed_no_file(self):
        oid = self._opportunity()
        result = BuildProductTaskAdapter().run(self._ctx(oid, plan_offer=None))
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertFalse((self.d / "deliverables" / oid / "product.md").exists())

    def test_F_missing_price_fails_closed_no_file(self):
        oid = self._opportunity()
        bad = {**_OFFER, "price": 0}
        result = BuildProductTaskAdapter().run(self._ctx(oid, plan_offer=bad))
        self.assertFalse(result.ok)
        self.assertFalse((self.d / "deliverables" / oid / "product.md").exists())

    def test_F_missing_what_is_sold_fails_closed_no_file(self):
        oid = self._opportunity()
        bad = {**_OFFER, "what_is_sold": ""}
        result = BuildProductTaskAdapter().run(self._ctx(oid, plan_offer=bad))
        self.assertFalse(result.ok)
        self.assertFalse((self.d / "deliverables" / oid / "product.md").exists())

    def test_H_deterministic_regeneration_is_idempotent_content(self):
        oid = self._opportunity()
        BuildProductTaskAdapter().run(self._ctx(oid))
        first = (self.d / "deliverables" / oid / "product.md").read_text(encoding="utf-8")
        BuildProductTaskAdapter().run(self._ctx(oid))
        second = (self.d / "deliverables" / oid / "product.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)


class RealChainIntegrationTests(_Base):
    """Drives the real architecture: opportunity_engine -> accept_opportunity
    -> Worker. Proves BUILD_PRODUCT actually runs as part of the real
    chain and P1-1..P1-5 (checkout attribution, deploy) are unaffected."""

    def setUp(self):
        super().setUp()
        self._old_env = {k: os.environ.get(k) for k in
                         ("PAYPAL_CLIENT_ID", "PAYPAL_ENV")}
        os.environ["PAYPAL_CLIENT_ID"] = "test-client-id"
        os.environ["PAYPAL_ENV"] = "live"

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_real_chain_produces_a_real_product_file(self):
        opportunity_engine.generate(self.d, n=8)
        oid = load_opportunities(self.d).by_status("discovered")[0]["id"]
        accept_opportunity(self.d, oid, actor="founder")

        reg = default_registry()
        reg.register(DeployTaskAdapter(FakeDeploymentAdapter(
            base_url="https://p16.pages.test")))
        Worker(self.d, registry=reg, name="p16").run(max_ticks=100)

        build_product = next(t for t in load_tasks(self.d).by_opportunity(oid)
                             if t.task_type == "BUILD_PRODUCT")
        self.assertEqual(build_product.status, "SUCCEEDED")
        product_path = self.d / "deliverables" / oid / "product.md"
        self.assertTrue(product_path.is_file())
        content = product_path.read_text(encoding="utf-8")
        self.assertIn(oid, content)

        # P1-5's checkout deploy is unaffected: DEPLOY still needs release,
        # still builds a real checkout once released
        deploy = next(t for t in load_tasks(self.d).by_opportunity(oid)
                      if t.task_type == "DEPLOY")
        self.assertEqual(deploy.status, "BLOCKED_APPROVAL")
        release_task(self.d, deploy.task_id, actor="founder")
        Worker(self.d, registry=reg, name="p16").run(max_ticks=100)
        self.assertEqual(load_opportunities(self.d).get(oid)["state"], "LIVE")


if __name__ == "__main__":
    unittest.main()
