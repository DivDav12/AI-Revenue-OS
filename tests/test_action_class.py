import unittest
from pathlib import Path

from revenue_os import action_class as ac


class ClassifyTests(unittest.TestCase):
    def test_safe_autonomous_kinds(self):
        for k in ("research", "write_code", "build_landing_page", "create_design",
                  "publish_website", "publish_github_repo", "publish_docs",
                  "seo_work", "draft_outreach_message", "analytics",
                  "change_strategy", "abandon_experiment"):
            self.assertTrue(ac.classify(k).autonomous, k)
            self.assertEqual(ac.classify(k).approval_kind, None)

    def test_money_kinds(self):
        for k in ("spend_money", "buy_ads", "buy_api_credits", "real_llm_call",
                  "activate_paid_checkout", "launch_paid_ad_campaign",
                  "purchase_domain", "subscribe_service", "record_real_payment",
                  "increase_budget", "financial_transfer"):
            v = ac.classify(k)
            self.assertEqual(v.action_class, ac.ActionClass.MONEY_APPROVAL_REQUIRED, k)
            self.assertEqual(v.approval_kind, "money")
            self.assertFalse(v.autonomous)

    def test_identity_and_legal_kinds(self):
        for k in ("kyc", "identity_verification", "age_verification",
                  "bank_verification", "paypal_identity_action",
                  "create_personal_account"):
            self.assertEqual(ac.classify(k).approval_kind, "identity", k)
        for k in ("sign_contract", "tax_submission", "binding_legal_agreement"):
            self.assertEqual(ac.classify(k).approval_kind, "legal", k)

    def test_safety_blocked_kinds(self):
        for k in ("spam", "solve_captcha", "bypass_authentication",
                  "fabricate_revenue", "fabricate_customer", "fabricate_review",
                  "deceive_user", "automated_post_tos_forbidden",
                  "mass_unsolicited_message"):
            self.assertEqual(ac.classify(k).action_class,
                             ac.ActionClass.SAFETY_BLOCKED, k)

    def test_unknown_kind_fails_closed(self):
        v = ac.classify("do_something_weird")
        self.assertEqual(v.action_class, ac.ActionClass.SAFETY_BLOCKED)
        self.assertIn("failing closed", v.reason)

    def test_deploy_page_is_context_sensitive(self):
        self.assertTrue(ac.classify("deploy_page", {"has_checkout": False}).autonomous)
        self.assertEqual(
            ac.classify("deploy_page", {"has_checkout": True}).action_class,
            ac.ActionClass.MONEY_APPROVAL_REQUIRED)
        self.assertEqual(
            ac.classify("deploy_page", {"collects_payment": True}).action_class,
            ac.ActionClass.MONEY_APPROVAL_REQUIRED)

    def test_posting_only_to_owned_channels(self):
        self.assertTrue(
            ac.classify("post_public_content", {"platform": "own_blog"}).autonomous)
        self.assertTrue(
            ac.classify("post_public_reply", {"platform": "github"}).autonomous)
        for plat in ("hacker news", "reddit", "lobsters", "twitter", "linkedin"):
            self.assertEqual(
                ac.classify("post_public_reply", {"platform": plat}).action_class,
                ac.ActionClass.SAFETY_BLOCKED, plat)
        self.assertFalse(ac.posting_permitted(""))
        self.assertFalse(ac.posting_permitted("hn"))

    def test_deterministic(self):
        self.assertEqual(ac.classify("buy_ads"), ac.classify("buy_ads"))


class FinancialEffectTests(unittest.TestCase):
    """A nominal 0 EUR alone is never a money action; hidden costs are."""

    _MAYBE = ("use_external_service", "sign_up_service", "create_service_account",
              "register_on_platform", "use_external_api", "publish_to_marketplace",
              "list_product_for_sale", "deploy_infra", "provision_hosting",
              "change_price", "optimize_paid_campaign", "connect_payment_provider")

    def test_zero_cost_no_obligation_runs_autonomously(self):
        for k in self._MAYBE:
            for ctx in (None, {}, {"amount": 0}, {"amount": 0.0}, {"cost_eur": 0},
                        {"free_tier": True}, {"price": 0}):
                v = ac.classify(k, ctx)
                self.assertTrue(v.autonomous, f"{k} {ctx} -> {v.action_class}")
                self.assertIn("EUR 0", v.reason)

    def test_a_real_price_makes_it_money(self):
        for k in self._MAYBE:
            v = ac.classify(k, {"amount": 5.0})
            self.assertEqual(v.action_class, ac.ActionClass.MONEY_APPROVAL_REQUIRED, k)

    def test_hidden_and_indirect_costs_are_caught(self):
        cases = [
            {"free_trial_converts_to_paid": True},
            {"trial_to_paid": True},
            {"requires_card": True},
            {"requires_payment_method": True},
            {"metered": True},
            {"usage_billing": True},
            {"recurring": True},
            {"subscription": True},
            {"monthly_cost": 6},
            {"setup_fee": 10},
            {"fees": True},
            {"processor_fees": True},
            {"commission": True},
            {"revenue_share": True},
            {"listing_fee": True},
            {"creates_payment_obligation": True},
            {"future_commitment": True},
            {"contractual_spend": True},
        ]
        for ctx in cases:
            v = ac.classify("use_external_service", ctx)
            self.assertEqual(v.action_class, ac.ActionClass.MONEY_APPROVAL_REQUIRED,
                             ctx)
            eff, why = ac.has_financial_effect(ctx)
            self.assertTrue(eff)
            self.assertTrue(why)

    def test_genuinely_paid_kinds_are_always_money(self):
        for k in ("buy_ads", "buy_api_credits", "real_llm_call", "purchase_domain",
                  "purchase_software", "subscribe_service", "start_subscription",
                  "financial_transfer", "withdraw_money", "increase_budget",
                  "activate_paid_checkout", "publish_paid_checkout",
                  "incur_processor_fees", "activate_revenue_share",
                  "pay_platform_commission"):
            # even with an explicit zero context they stay MONEY
            self.assertEqual(ac.classify(k, {"amount": 0, "free": True}).action_class,
                             ac.ActionClass.MONEY_APPROVAL_REQUIRED, k)

    def test_deploy_page_free_vs_paid_vs_checkout(self):
        self.assertTrue(ac.classify("deploy_page",
                                    {"has_checkout": False, "cost_eur": 0}).autonomous)
        self.assertEqual(
            ac.classify("deploy_page", {"has_checkout": True}).action_class,
            ac.ActionClass.MONEY_APPROVAL_REQUIRED)
        self.assertEqual(
            ac.classify("deploy_page", {"has_checkout": False, "monthly_cost": 5}).action_class,
            ac.ActionClass.MONEY_APPROVAL_REQUIRED)

    def test_identity_and_legal_are_independent_of_cost(self):
        for k in ("kyc", "age_verification", "bank_verification"):
            self.assertEqual(ac.classify(k, {"amount": 0}).approval_kind, "identity")
        for k in ("sign_contract", "tax_submission"):
            self.assertEqual(ac.classify(k, {"amount": 0}).approval_kind, "legal")


class ApprovalFilingTests(unittest.TestCase):
    def _store(self):
        import tempfile
        from revenue_os.approvals import ApprovalStore
        return ApprovalStore(Path(tempfile.mkdtemp()) / "approvals.json")

    def test_zero_amount_no_obligation_files_nothing(self):
        s = self._store()
        r = s.request_money(key="k", what="w", why="y", amount=0.0)
        self.assertEqual(r["status"], "not_required")
        self.assertEqual(s.pending("money"), [])

    def test_none_amount_no_obligation_files_nothing(self):
        s = self._store()
        r = s.request_money(key="k", what="w", why="y")
        self.assertEqual(r["status"], "not_required")

    def test_fees_or_obligation_or_amount_files_the_request(self):
        s = self._store()
        s.request_money(key="a", what="w", why="y", fees=True)
        s.request_money(key="b", what="w", why="y", creates_payment_obligation=True)
        s.request_money(key="c", what="w", why="y", amount=25.0)
        s.request_money(key="d", what="w", why="y", recurring=True)
        self.assertEqual(len(s.pending("money")), 4)
        for r in s.pending("money"):
            self.assertTrue(r.get("financial_effect"))


class FirewallContextTests(unittest.TestCase):
    def test_guard_is_a_noop_outside_context(self):
        ac.guard_no_money_in_autonomy("x")   # must not raise

    def test_guard_raises_inside_context(self):
        with ac.autonomous_context():
            self.assertTrue(ac.in_autonomous_context())
            with self.assertRaises(ac.ActionBlocked):
                ac.guard_no_money_in_autonomy("spend money")
        self.assertFalse(ac.in_autonomous_context())

    def test_context_is_reentrant(self):
        with ac.autonomous_context():
            with ac.autonomous_context():
                self.assertTrue(ac.in_autonomous_context())
            self.assertTrue(ac.in_autonomous_context())
        self.assertFalse(ac.in_autonomous_context())


if __name__ == "__main__":
    unittest.main()
