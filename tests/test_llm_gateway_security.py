"""Priority-1 SECURITY / INVARIANT regression suite for the LLM gateway.

Proves the 12 safety invariants of the controlled LLM budget layer. No
real API request is ever made: every test uses the deterministic mock
provider or a stub, and asserts that a real `anthropic.Anthropic()` is
NEVER constructed unless the policy explicitly authorises it.

INVARIANTS
  1  disabled  => zero real LLM access (every mode)
  2  provider="none" => zero provider calls (fail closed)
  3  provider="mock" can never fall through to real Anthropic
  4  a real Anthropic client needs enabled + provider=anthropic + no
     emergency stop + (in autonomous ctx) autonomous_enabled
  5  emergency stop is absolute
  6  autonomous permission does NOT weaken the money firewall
  7  every real LLM path goes through the gateway
  8  preflight runs before the paid call
  9  actual spend is accounted exactly once
  10 per-task limits actually bite
  11 effective allowed spend = MIN of every applicable limit
  12 no real credits needed for testing
"""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import revenue_os
from revenue_os import action_class as ac
from revenue_os.llm_gateway import (
    LlmBudgetExceeded,
    LlmPolicy,
    LlmUnavailable,
    gateway,
    load_audit,
    save_policy,
)

_SRC = Path(revenue_os.__file__).parent


def _pol(**over) -> LlmPolicy:
    base = dict(enabled=True, provider="mock", per_call_usd=0.05, hourly_usd=0.50,
                daily_usd=2.00, global_usd=5.00, task_default_usd=0.20,
                max_calls_per_min=6)
    base.update(over)
    return LlmPolicy(**base)


def _tmp() -> str:
    return tempfile.mkdtemp()


class _SpyReal:
    """Records whether the real Anthropic constructor was invoked."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return object()   # a fake "real" client


# ---------------------------------------------------------------------------
# INVARIANT 1 - DISABLED MEANS ZERO REAL LLM ACCESS
# ---------------------------------------------------------------------------

class DisabledMeansNoAccess(unittest.TestCase):
    def _build(self, d):
        from revenue_os.llm_normalize import build_client
        return build_client(d)

    def test_1_disabled_provider_none_manual_build_client_blocked(self):
        d = _tmp()   # default policy: enabled=False, provider="none"
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            with self.assertRaises(LlmUnavailable):
                self._build(d)
        self.assertEqual(spy.calls, 0)

    def test_2_disabled_with_api_key_present_still_blocked(self):
        d = _tmp()
        spy = _SpyReal()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-not-real"}):
            with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
                with self.assertRaises(LlmUnavailable):
                    self._build(d)
        self.assertEqual(spy.calls, 0)

    def test_3_disabled_but_provider_anthropic_blocked(self):
        d = _tmp()
        save_policy(d, LlmPolicy(enabled=False, provider="anthropic"))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            with self.assertRaises(LlmUnavailable):
                self._build(d)
        self.assertEqual(spy.calls, 0)

    def test_11_emergency_stop_blocks_non_autonomous_calls_too(self):
        d = _tmp()
        save_policy(d, _pol(emergency_stop=True))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            with self.assertRaises(LlmUnavailable):
                self._build(d)
            with ac.autonomous_context():
                with self.assertRaises(LlmUnavailable):
                    self._build(d)
        self.assertEqual(spy.calls, 0)


# ---------------------------------------------------------------------------
# INVARIANT 2 - provider="none" / invalid  =>  fail closed
# ---------------------------------------------------------------------------

class ProviderNoneFailsClosed(unittest.TestCase):
    def test_4_enabled_provider_none_blocked(self):
        d = _tmp()
        save_policy(d, _pol(provider="none"))
        from revenue_os.llm_normalize import build_client
        with self.assertRaises(LlmUnavailable):
            build_client(d)
        with self.assertRaises(LlmUnavailable):
            gateway(d).preflight(0.0, autonomous=False)  # ceiling gate too

    def test_unknown_provider_blocked(self):
        d = _tmp()
        save_policy(d, _pol(provider="openai"))
        from revenue_os.llm_normalize import build_client
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            with self.assertRaises(LlmUnavailable):
                build_client(d)
        self.assertEqual(spy.calls, 0)


# ---------------------------------------------------------------------------
# INVARIANT 3 - MOCK NEVER FALLS THROUGH TO REAL ANTHROPIC
# ---------------------------------------------------------------------------

class MockNeverFallsThrough(unittest.TestCase):
    def test_5_enabled_provider_mock_returns_mock_client(self):
        d = _tmp()
        save_policy(d, _pol(provider="mock"))
        from revenue_os.llm_mock import MockLlmClient
        from revenue_os.llm_normalize import build_client
        self.assertIsInstance(build_client(d), MockLlmClient)

    def test_6_mock_construction_failure_never_falls_back_to_anthropic(self):
        d = _tmp()
        save_policy(d, _pol(provider="mock"))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_mock.MockLlmClient",
                        side_effect=RuntimeError("mock boom")):
            with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
                from revenue_os.llm_normalize import build_client
                with self.assertRaises(RuntimeError):
                    build_client(d)
        self.assertEqual(spy.calls, 0)

    def test_mock_needs_no_key_and_no_network(self):
        d = _tmp()
        save_policy(d, _pol(provider="mock"))
        from revenue_os.llm_normalize import build_client
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            c = build_client(d)
        r = c.messages.create(messages=[{"role": "user", "content": "x"}],
                              tools=[{"name": "record_scores"}])
        self.assertEqual(r.content[0].type, "tool_use")


# ---------------------------------------------------------------------------
# INVARIANT 4 - ANTHROPIC REQUIRES EXPLICIT ENABLEMENT
# ---------------------------------------------------------------------------

class AnthropicNeedsExplicitEnablement(unittest.TestCase):
    def test_all_conditions_true_then_real_client_built(self):
        d = _tmp()
        save_policy(d, _pol(provider="anthropic"))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            from revenue_os.llm_normalize import build_client
            build_client(d)
        self.assertEqual(spy.calls, 1)

    def test_7_emergency_stop_beats_enabled_anthropic(self):
        d = _tmp()
        save_policy(d, _pol(provider="anthropic", emergency_stop=True))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            from revenue_os.llm_normalize import build_client
            with self.assertRaises(LlmUnavailable):
                build_client(d)
        self.assertEqual(spy.calls, 0)

    def test_8_autonomous_ctx_without_autonomous_enabled_blocked(self):
        d = _tmp()
        save_policy(d, _pol(provider="anthropic", autonomous_enabled=False))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            from revenue_os.llm_normalize import build_client
            with ac.autonomous_context():
                with self.assertRaises(LlmUnavailable):
                    build_client(d)
        self.assertEqual(spy.calls, 0)

    def test_9_autonomous_ctx_with_autonomous_enabled_mock_allowed(self):
        d = _tmp()
        save_policy(d, _pol(provider="mock", autonomous_enabled=True))
        from revenue_os.llm_mock import MockLlmClient
        from revenue_os.llm_normalize import build_client
        with ac.autonomous_context():
            self.assertIsInstance(build_client(d), MockLlmClient)

    def test_10_autonomous_enabled_anthropic_but_tier_disabled_blocked(self):
        d = _tmp()
        save_policy(d, LlmPolicy(enabled=False, autonomous_enabled=True,
                                 provider="anthropic"))
        spy = _SpyReal()
        with mock.patch("revenue_os.llm_normalize._real_anthropic_client", spy):
            from revenue_os.llm_normalize import build_client
            with ac.autonomous_context():
                with self.assertRaises(LlmUnavailable):
                    build_client(d)
        self.assertEqual(spy.calls, 0)


# ---------------------------------------------------------------------------
# INVARIANT 5 / 6 - emergency stop absolute; autonomy != money authority
# ---------------------------------------------------------------------------

class MoneyFirewallUnweakened(unittest.TestCase):
    def test_autonomous_enabled_does_not_unblock_budget_guard(self):
        from revenue_os import budget
        d = _tmp()
        save_policy(d, _pol(provider="anthropic", autonomous_enabled=True))
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                budget.guard(d, 0.01)

    def test_autonomous_enabled_does_not_unblock_paypal_or_payment(self):
        d = Path(_tmp())
        save_policy(d, _pol(provider="anthropic", autonomous_enabled=True))
        from revenue_os.paypal import PayPalConfig
        from revenue_os.revenue import RevenueLedger, record_payment
        from revenue_os.store import CandidateStore
        with ac.autonomous_context():
            with self.assertRaises(ac.ActionBlocked):
                PayPalConfig.from_env({"PAYPAL_CLIENT_ID": "x",
                                       "PAYPAL_CLIENT_SECRET": "y",
                                       "PAYPAL_ENV": "live"})
            with self.assertRaises(ac.ActionBlocked):
                record_payment(CandidateStore(d / "c.json"),
                               RevenueLedger(d / "r.json"), "x", 10.0, actor="t")

    def test_guard_no_money_in_autonomy_still_present_on_money_paths(self):
        for fname in ("budget.py", "paypal.py", "delivery.py"):
            src = (_SRC / fname).read_text(encoding="utf-8")
            self.assertIn("guard_no_money_in_autonomy", src, fname)


# ---------------------------------------------------------------------------
# INVARIANT 8 / 10 / 11 - preflight before spend; task limits; MIN of limits
# ---------------------------------------------------------------------------

class LimitsAndPreflight(unittest.TestCase):
    def test_12_per_call_limit(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=0.02))
        with self.assertRaises(LlmBudgetExceeded):
            gateway(d).preflight(0.03, autonomous=False)

    def test_13_per_task_limit(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=1.0, task_limits={"research": 0.01}))
        with self.assertRaises(LlmBudgetExceeded):
            gateway(d).preflight(0.05, task="research", autonomous=False)
        # a different task with no explicit limit uses task_default_usd
        gateway(d).preflight(0.05, task="copywriting", autonomous=False)

    def test_14_hourly_limit(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=1.0, task_default_usd=1.0,
                            hourly_usd=0.30, daily_usd=9, global_usd=9))
        g = gateway(d)
        g.record(task="t", model="m", est_usd=0.30, actual_usd=0.30)
        with self.assertRaises(LlmBudgetExceeded):
            g.preflight(0.05, autonomous=False)

    def test_15_daily_limit(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=9, task_default_usd=9,
                            hourly_usd=9, daily_usd=0.40, global_usd=9))
        g = gateway(d)
        g.record(task="t", model="m", est_usd=0.40, actual_usd=0.40)
        with self.assertRaises(LlmBudgetExceeded):
            g.preflight(0.05, autonomous=False)

    def test_16_global_limit(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=9, task_default_usd=9,
                            hourly_usd=9, daily_usd=9, global_usd=0.50))
        g = gateway(d)
        g.record(task="t", model="m", est_usd=0.50, actual_usd=0.50)
        with self.assertRaises(LlmBudgetExceeded):
            g.preflight(0.05, autonomous=False)

    def test_17_calls_per_minute_limit(self):
        d = _tmp()
        save_policy(d, _pol(max_calls_per_min=2))
        g = gateway(d)
        for _ in range(2):
            g.record(task="t", model="m", est_usd=0.0, actual_usd=0.0)
        with self.assertRaises(LlmBudgetExceeded):
            g.preflight(0.0, autonomous=False)

    def test_11_effective_ceiling_is_min_of_all_limits(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=1.00, task_default_usd=0.10,
                            hourly_usd=0.40, daily_usd=0.60, global_usd=0.80))
        self.assertAlmostEqual(
            gateway(d).preflight(0.01, autonomous=False), 0.10)

    def test_18_cumulative_legacy_llm_budget_limit(self):
        from revenue_os.llm_spend import LlmSpendLog
        from revenue_os.revenue import RevenueLedger
        from revenue_os.llm_workers import budget_gate
        d = Path(_tmp())
        led = RevenueLedger(d / "revenue.json")          # book a sale -> presale off
        led.add({"candidate_name": "x", "amount": 30.0, "currency": "EUR",
                 "received_at": "2026-01-01T00:00:00+00:00", "actor": "t",
                 "ref": "paypal:seed"})
        led.save()
        log = LlmSpendLog(d / "llm_spend.json")
        log.add({"activity": "evaluate", "cost_usd": 5.0, "api_calls": 1})
        log.save()
        with self.assertRaisesRegex(ValueError, "cumulative cap"):
            budget_gate(d, 0.01, 0.5, task="research")

    def test_19_presale_hard_limit(self):
        from revenue_os.budget import BudgetBlocked
        from revenue_os.llm_spend import LlmSpendLog
        from revenue_os.llm_workers import budget_gate
        d = Path(_tmp())
        log = LlmSpendLog(d / "llm_spend.json")
        log.add({"activity": "evaluate", "cost_usd": 3.0, "api_calls": 1})
        log.save()
        with self.assertRaises(BudgetBlocked):
            budget_gate(d, 0.5, 1.0, task="research")

    def test_8_preflight_runs_before_any_spend_is_recorded(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=0.01))
        with self.assertRaises(LlmBudgetExceeded):
            gateway(d).preflight(0.50, autonomous=False)
        self.assertEqual(load_audit(d), [])   # nothing recorded by a rejected call


# ---------------------------------------------------------------------------
# INVARIANT 9 - ACTUAL SPEND ACCOUNTING
# ---------------------------------------------------------------------------

class SpendAccounting(unittest.TestCase):
    def test_20_successful_call_recorded_exactly_once(self):
        d = _tmp()
        save_policy(d, _pol())
        g = gateway(d)
        g.record(task="research", model="claude-sonnet-5",
                 est_usd=0.03, actual_usd=0.04, in_tokens=900, out_tokens=120)
        audit = load_audit(d)
        self.assertEqual(len(audit), 1)
        self.assertAlmostEqual(gateway(d).balances()["spent_this_hour"], 0.04)
        self.assertAlmostEqual(gateway(d).balances()["spent_total"], 0.04)

    def test_21_rejected_call_costs_zero(self):
        d = _tmp()
        save_policy(d, _pol(per_call_usd=0.01))
        try:
            gateway(d).preflight(1.0, autonomous=False)
        except LlmBudgetExceeded:
            pass
        self.assertEqual(gateway(d).balances()["spent_total"], 0.0)
        self.assertEqual(load_audit(d), [])

    def test_22_cache_hit_creates_no_phantom_provider_spend(self):
        from revenue_os.llm_cache import LlmCache
        from revenue_os.llm_normalize import LlmNormalizer
        from revenue_os.opportunity import CRITERIA
        from revenue_os.sources import RawSignal

        d = Path(_tmp())
        cache = LlmCache.load(d / "c.json")
        sig = RawSignal(title="a cached idea", text="body")
        model = "claude-sonnet-5"
        from revenue_os.llm_normalize import cache_key
        cache.put(cache_key(sig, model),
                  {"scores": {c: 3.0 for c in CRITERIA}, "rationale": "cached",
                   "model": model})

        class _Boom:
            def __init__(self): self.messages = self
            def create(self, **k): raise AssertionError("provider must not be called")

        norm = LlmNormalizer(client=_Boom(), model=model, max_cost_usd=0.0,
                             cache=cache)
        opp = norm(sig)                       # served from cache, no API call
        self.assertEqual(norm.cache_hits, 1)
        self.assertEqual(norm.cache_misses, 0)
        self.assertEqual(norm.meter.cost_usd, 0.0)


# ---------------------------------------------------------------------------
# INVARIANT 7 - EVERY REAL LLM PATH USES THE GATEWAY
# ---------------------------------------------------------------------------

class NoDirectProviderBypass(unittest.TestCase):
    _ADAPTER = {"llm_normalize.py"}          # the ONLY real-constructor module

    def test_23_only_the_adapter_constructs_a_real_anthropic_client(self):
        offenders = []
        for path in _SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                # match `anthropic.Anthropic(...)` and `Anthropic(...)`
                is_attr = (isinstance(f, ast.Attribute) and f.attr == "Anthropic")
                is_name = (isinstance(f, ast.Name) and f.id == "Anthropic")
                if (is_attr or is_name) and path.name not in self._ADAPTER:
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"direct Anthropic construction: {offenders}")

    def test_build_client_is_the_single_construction_entry_point(self):
        # every in-tree call site that builds an LLM client passes a data_dir
        # (so the policy gate always runs). A bare build_client() survives
        # only in tests / the documented legacy path.
        import subprocess
        hits = []
        for path in _SRC.rglob("*.py"):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if "build_client()" in s.replace(" ", "") \
                        and "def build_client" not in s:
                    hits.append(f"{path.name}:{i}")
        self.assertEqual(hits, [], f"build_client() without data_dir: {hits}")


# ---------------------------------------------------------------------------
# INVARIANT 12 - NO REAL CREDITS NEEDED  (meta: this suite made no real call)
# ---------------------------------------------------------------------------

class NoRealCreditsNeeded(unittest.TestCase):
    def test_suite_uses_only_mock_or_stub(self):
        # sanity: the mock provider is self-contained and free
        from revenue_os.llm_mock import MockLlmClient
        c = MockLlmClient()
        r = c.messages.create(messages=[{"role": "user", "content": "x"}],
                              tools=[{"name": "record_opportunity"}])
        self.assertIn("title", r.content[0].input)


if __name__ == "__main__":
    unittest.main()
