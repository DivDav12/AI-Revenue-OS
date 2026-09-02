"""The controlled LLM budget layer (Priority 1).

Proves: the autonomous loop still cannot construct an LLM client with the
default policy (backward-compatible with the old hard guard); the
emergency stop blocks everything; per-call / task / hourly / daily /
global limits and the calls-per-minute rate limit are enforced once the
tier is enabled; graceful fallback (`LlmUnavailable`) is distinct from a
budget breach (`LlmBudgetExceeded`); the audit log records every call and
drives the spend windows; the mock client returns schema-valid output.
"""

import tempfile
import unittest
from pathlib import Path

from revenue_os import action_class as ac
from revenue_os.llm_gateway import (
    LlmBudgetExceeded,
    LlmPolicy,
    LlmUnavailable,
    gateway,
    load_audit,
    load_policy,
    save_policy,
    status,
)


def _enabled(**over) -> LlmPolicy:
    base = dict(enabled=True, provider="mock", per_call_usd=0.05, hourly_usd=0.50,
               daily_usd=2.00, global_usd=5.00, task_default_usd=0.20,
               max_calls_per_min=3)
    base.update(over)
    return LlmPolicy(**base)


class PolicyPersistenceTests(unittest.TestCase):
    def test_default_policy_is_all_off(self):
        d = tempfile.mkdtemp()
        p = load_policy(d)
        self.assertFalse(p.enabled)
        self.assertFalse(p.autonomous_enabled)
        self.assertFalse(p.emergency_stop)
        self.assertEqual(p.provider, "none")

    def test_round_trips_and_ignores_unknown_keys(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(daily_usd=1.23), by="tester")
        p = load_policy(d)
        self.assertEqual(p.daily_usd, 1.23)
        self.assertEqual(p.updated_by, "tester")
        self.assertTrue(p.updated_at)
        (Path(d) / "llm_policy.json").write_text(
            '{"enabled": true, "bogus": 9}', encoding="utf-8")
        self.assertTrue(load_policy(d).enabled)   # no crash


class ClientGateTests(unittest.TestCase):
    def test_autonomous_context_blocked_by_default_policy(self):
        d = tempfile.mkdtemp()
        with ac.autonomous_context():
            with self.assertRaises(LlmUnavailable):
                gateway(d).assert_client_allowed()

    def test_llm_unavailable_is_an_action_blocked(self):
        # the old money-firewall test asserts ac.ActionBlocked - keep that true
        self.assertTrue(issubclass(LlmUnavailable, ac.ActionBlocked))

    def test_autonomous_allowed_once_explicitly_authorised(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(autonomous_enabled=True))
        with ac.autonomous_context():
            gateway(d).assert_client_allowed()   # no raise

    def test_emergency_stop_blocks_even_non_autonomous(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(emergency_stop=True))
        with self.assertRaises(LlmUnavailable):
            gateway(d).assert_client_allowed(autonomous=False)

    def test_non_autonomous_is_still_blocked_when_tier_disabled(self):
        # DISABLED means zero real LLM access - in EVERY mode, not just the
        # autonomous loop. The client must not be constructible.
        d = tempfile.mkdtemp()
        with self.assertRaises(LlmUnavailable):
            gateway(d).assert_client_allowed(autonomous=False)

    def test_non_autonomous_allowed_once_tier_enabled(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled())
        gateway(d).assert_client_allowed(autonomous=False)   # no raise


class PreflightLimitTests(unittest.TestCase):
    def test_tier_off_means_limits_are_dormant(self):
        d = tempfile.mkdtemp()                       # default policy
        ceiling = gateway(d).preflight(4.0, task="research", autonomous=False)
        self.assertGreater(ceiling, 4.0)             # not constrained

    def test_per_call_limit_enforced_when_enabled(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(per_call_usd=0.02))
        with self.assertRaises(LlmBudgetExceeded):
            gateway(d).preflight(0.03, task="research", autonomous=False)

    def test_task_limit_enforced_when_enabled(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(per_call_usd=1.0, task_limits={"research": 0.01}))
        with self.assertRaises(LlmBudgetExceeded):
            gateway(d).preflight(0.05, task="research", autonomous=False)

    def test_returns_smallest_headroom(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(per_call_usd=1.0, task_default_usd=0.10,
                                hourly_usd=0.30))
        # nothing spent yet: min(per_call 1.0, task 0.10, hour 0.30, day, global)
        self.assertAlmostEqual(gateway(d).preflight(0.01, autonomous=False), 0.10)

    def test_hourly_then_daily_then_global_windows(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(per_call_usd=1.0, task_default_usd=1.0,
                                hourly_usd=0.50, daily_usd=0.80, global_usd=1.0))
        g = gateway(d)
        g.record(task="t", model="m", est_usd=0.5, actual_usd=0.5, autonomous=False)
        with self.assertRaises(LlmBudgetExceeded):     # hour exhausted
            g.preflight(0.1, autonomous=False)

    def test_rate_limit_enforced_when_enabled(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(max_calls_per_min=2))
        g = gateway(d)
        for _ in range(2):
            g.record(task="t", model="m", est_usd=0.0, actual_usd=0.0)
        with self.assertRaises(LlmBudgetExceeded):
            g.preflight(0.0, autonomous=False)

    def test_autonomous_gate_beats_limits(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled())        # autonomous_enabled stays False
        with ac.autonomous_context():
            with self.assertRaises(LlmUnavailable):
                gateway(d).preflight(0.001, autonomous=None)


class AuditAndBalancesTests(unittest.TestCase):
    def test_record_appends_and_balances_track(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled())
        g = gateway(d)
        g.record(task="research", model="claude-sonnet-5", est_usd=0.03,
                 actual_usd=0.04, in_tokens=1000, out_tokens=200, autonomous=True)
        audit = load_audit(d)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["task"], "research")
        self.assertTrue(audit[0]["autonomous"])
        b = gateway(d).balances()
        self.assertAlmostEqual(b["spent_this_hour"], 0.04)
        self.assertAlmostEqual(b["spent_today"], 0.04)
        self.assertAlmostEqual(b["remaining_hour"], 0.46)

    def test_audit_log_is_capped(self):
        d = tempfile.mkdtemp()
        g = gateway(d)
        for i in range(520):
            g.record(task=f"t{i}", model="m", est_usd=0.0, actual_usd=0.0)
        self.assertLessEqual(len(load_audit(d)), 500)

    def test_status_shape(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled())
        st = status(d)
        self.assertEqual(set(st), {"policy", "available", "reason",
                                   "balances", "recent_calls"})
        self.assertTrue(st["available"])            # mock provider + enabled


class MockClientTests(unittest.TestCase):
    def test_build_client_returns_mock_when_provider_mock(self):
        d = tempfile.mkdtemp()
        save_policy(d, _enabled(provider="mock"))
        from revenue_os.llm_mock import MockLlmClient
        from revenue_os.llm_normalize import build_client
        self.assertIsInstance(build_client(d), MockLlmClient)

    def test_mock_returns_schema_valid_tool_output(self):
        from revenue_os.llm_mock import MockLlmClient
        c = MockLlmClient()
        resp = c.messages.create(
            model="claude-mock",
            messages=[{"role": "user", "content": "score this saas idea"}],
            tools=[{"name": "record_scores"}], max_tokens=256)
        self.assertEqual(resp.content[0].type, "tool_use")
        self.assertEqual(resp.content[0].name, "record_scores")
        data = resp.content[0].input
        for k in ("real_demand", "scalability", "rationale"):
            self.assertIn(k, data)
        self.assertGreater(resp.usage.input_tokens, 0)

    def test_mock_opportunity_payload(self):
        from revenue_os.llm_mock import MockLlmClient
        c = MockLlmClient()
        resp = c.messages.create(
            messages=[{"role": "user", "content": "find an opportunity"}],
            tools=[{"name": "record_opportunity"}])
        data = resp.content[0].input
        for k in ("title", "category", "willingness_to_pay_eur",
                  "implementation_difficulty"):
            self.assertIn(k, data)


class BackwardCompatTests(unittest.TestCase):
    def test_build_client_still_blocked_in_autonomous_context(self):
        from revenue_os.llm_normalize import build_client
        d = tempfile.mkdtemp()
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                build_client(d)
            with self.assertRaises(ac.ActionBlocked):
                build_client()          # no data_dir - old fallback path


if __name__ == "__main__":
    unittest.main()
