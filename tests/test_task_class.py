"""Phase 6 - the mandatory task classifier (unit level)."""

import unittest

from revenue_os import task_class as tc
from revenue_os.execution import TASK_TYPES


class ClassifyTaskTests(unittest.TestCase):
    def test_safe_autonomous_task_types(self):
        for tt in ("RESEARCH", "SCORE", "PLAN", "BUILD_PRODUCT", "BUILD_PAGE",
                   "CREATE_CONTENT", "VALIDATE_PRODUCT", "VALIDATE_PAGE",
                   "ANALYZE", "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE",
                   "OPTIMIZE", "SPAWN_VARIANT", "SCALE", "DELIVER"):
            v = tc.classify_task(tt)
            self.assertEqual(v.task_class, tc.SAFE_AUTONOMOUS, tt)
            self.assertTrue(v.autonomous, tt)
            self.assertEqual(v.approval_type, "", tt)

    def test_every_task_type_has_a_verdict(self):
        for tt in TASK_TYPES:
            v = tc.classify_task(tt)
            self.assertIn(v.task_class, tc.TASK_CLASSES, tt)

    def test_deploy_is_external_authorized_money_gated_by_default(self):
        v = tc.classify_task("DEPLOY")
        self.assertEqual(v.task_class, tc.EXTERNAL_AUTHORIZED)
        self.assertTrue(v.needs_authorization)
        self.assertEqual(v.approval_type, "money")
        self.assertFalse(v.autonomous)

    def test_deploy_with_checkout_is_money(self):
        v = tc.classify_task("DEPLOY", {"has_checkout": True})
        self.assertEqual(v.task_class, tc.MONEY)
        self.assertTrue(v.needs_approval)
        self.assertEqual(v.approval_type, "money")

    def test_distribute_owned_channel_is_external_authorized_safe_noop(self):
        for ch in ("owned_web", "owned_content"):
            v = tc.classify_task("DISTRIBUTE", {"channel": ch})
            self.assertEqual(v.task_class, tc.EXTERNAL_AUTHORIZED, ch)
            self.assertTrue(v.safe_when_unauthorized, ch)

    def test_distribute_draft_channel_is_safe(self):
        for ch in ("community_draft", "social_draft"):
            v = tc.classify_task("DISTRIBUTE", {"channel": ch})
            self.assertEqual(v.task_class, tc.SAFE_AUTONOMOUS, ch)

    def test_distribute_third_party_platform_is_tos_blocked(self):
        v = tc.classify_task("DISTRIBUTE", {"channel": "hacker news"})
        self.assertEqual(v.task_class, tc.TOS_BLOCKED)
        self.assertTrue(v.blocked_forever)

    def test_unknown_task_type_fails_closed(self):
        for bad in ("", "WAT", "DROP_TABLE", "SEND_MONEY"):
            v = tc.classify_task(bad)
            self.assertEqual(v.task_class, tc.SAFETY_BLOCKED, bad)
            self.assertTrue(v.blocked_forever, bad)

    def test_identity_and_legal_translation_from_action_class(self):
        # no execution task_type maps to these yet, but the translation the
        # Worker relies on must be correct.
        cls, _ = tc._from_action_class("kyc", {})
        self.assertEqual(cls, tc.IDENTITY)
        cls, _ = tc._from_action_class("sign_contract", {})
        self.assertEqual(cls, tc.LEGAL)
        cls, _ = tc._from_action_class("spend_money", {})
        self.assertEqual(cls, tc.MONEY)
        cls, _ = tc._from_action_class("solve_captcha", {})
        self.assertEqual(cls, tc.SAFETY_BLOCKED)

    def test_reuses_action_class_not_a_second_policy(self):
        # classify_task must go through action_class for the generic kinds
        import revenue_os.action_class as ac
        seen = []
        orig = ac.classify

        def spy(kind, context=None):
            seen.append(kind)
            return orig(kind, context)

        ac.classify = spy
        try:
            tc.classify_task("BUILD_PAGE")
            tc.classify_task("CHECK_TRAFFIC")
        finally:
            ac.classify = orig
        self.assertIn("build_landing_page", seen)
        self.assertIn("analytics", seen)

    def test_verdict_is_immutable(self):
        v = tc.classify_task("PLAN")
        with self.assertRaises(Exception):
            v.task_class = "MONEY"      # frozen dataclass


if __name__ == "__main__":
    unittest.main()
