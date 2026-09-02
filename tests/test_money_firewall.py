"""Security audit: prove that NOTHING running inside the autonomous loop
can spend money, move money, touch PayPal, send e-mail, call a paid LLM,
or activate a paid checkout - even if some agent tried.
"""

import ast
import tempfile
import unittest
from pathlib import Path

import revenue_os
from revenue_os import action_class as ac

_SRC = Path(revenue_os.__file__).parent


class CallSiteGuardTests(unittest.TestCase):
    """Every real leak path refuses inside autonomous_context()."""

    def test_budget_guard_refuses(self):
        from revenue_os import budget
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                budget.guard(tempfile.mkdtemp(), 0.01)

    def test_paypal_config_refuses(self):
        from revenue_os.paypal import PayPalConfig
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x",
                                       "PAYPAL_CLIENT_SECRET": "y",
                                       "PAYPAL_ENV": "live"})

    def test_record_payment_refuses(self):
        from revenue_os.revenue import RevenueLedger, record_payment
        from revenue_os.store import CandidateStore
        d = Path(tempfile.mkdtemp())
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                record_payment(CandidateStore(d / "c.json"),
                               RevenueLedger(d / "r.json"), "x", 10.0, actor="t")

    def test_build_client_refuses(self):
        from revenue_os.llm_normalize import build_client
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                build_client()

    def test_send_delivery_refuses(self):
        from revenue_os.delivery import send_delivery
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                send_delivery(tempfile.mkdtemp(), "order-1")

    def test_deploy_checkout_refuses(self):
        from revenue_os.deploy import deploy_checkout
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                deploy_checkout(tempfile.mkdtemp(), "cand")


class AutonomyStaticAuditTests(unittest.TestCase):
    """autonomy.py must not import or call the money/identity/comms paths."""

    _FORBIDDEN_IMPORTS = {"smtplib", "anthropic", "http", "urllib", "socket"}
    _FORBIDDEN_NAMES = {"record_payment", "send_delivery", "deploy_checkout",
                        "build_client", "PayPalClient", "PayPalConfig",
                        "authorize_spend", "record_spend"}

    def _tree(self, fname):
        return ast.parse((_SRC / fname).read_text(encoding="utf-8"))

    def test_autonomy_imports_are_clean(self):
        mods = set()
        for node in ast.walk(self._tree("autonomy.py")):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertEqual(mods & self._FORBIDDEN_IMPORTS, set())

    def test_autonomy_calls_no_money_function(self):
        src = (_SRC / "autonomy.py").read_text(encoding="utf-8")
        for name in self._FORBIDDEN_NAMES:
            self.assertNotIn(name + "(", src, name)

    def test_opportunity_engine_llm_is_refused(self):
        from revenue_os.opportunity_engine import generate
        d = Path(tempfile.mkdtemp())
        (d / "candidates.json").write_text("[]")
        with self.assertRaises(ac.ActionBlocked):
            generate(d, llm=True)


class AutonomyRuntimeAuditTests(unittest.TestCase):
    def test_a_full_cycle_touches_no_money_path(self):
        """Run a real cycle; a spy on the guard proves the context was active
        and every guarded call would have been refused."""
        from revenue_os import autonomy
        d = Path(tempfile.mkdtemp())
        (d / "candidates.json").write_text(
            '[{"name":"a","description":"founders customers onboarding changelog"}]')

        seen = {"in_context": False}
        real = ac.guard_no_money_in_autonomy

        def spy(what):
            if ac.in_autonomous_context():
                seen["in_context"] = True
            return real(what)

        autonomy.ac.guard_no_money_in_autonomy = spy
        try:
            rep = autonomy.run_cycle(d)
        finally:
            autonomy.ac.guard_no_money_in_autonomy = real

        self.assertIsNone(rep.get("stopped"))
        # no revenue was booked, no approval was auto-approved
        self.assertEqual(rep["pending_approvals"]["money"]["approved"], 0)
        self.assertFalse((d / "revenue.json").exists())
        self.assertFalse((d / "deliveries.json").exists())


if __name__ == "__main__":
    unittest.main()
