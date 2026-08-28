import unittest

from revenue_os.decide_llm import (
    LlmDecisionPolicy,
    decide_llm,
    summarize_for_decision,
)
from revenue_os.lifecycle import STATUSES
from revenue_os.operator import Goal, decide


class _Block:
    type = "tool_use"
    name = "choose_action"

    def __init__(self, payload):
        self.input = payload


class _Usage:
    input_tokens = 200
    output_tokens = 40
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(payload)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = payload or {"action": "stop", "rationale": "enough queued"}
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.payload)


def _obs(*, counts=None, queue=None, age=0, total=None):
    sc = {s: 0 for s in STATUSES}
    sc.update(counts or {})
    return {
        "report": {
            "status_counts": sc,
            "action_queue": queue or [],
            "candidates": [],
            "totals": {"candidates": total if total is not None else sum(sc.values())},
            "outcomes": {"ready": False, "most_predictive": []},
        },
        "last_discovery_age_days": age,
    }


class DecideLlmTests(unittest.TestCase):
    def test_returns_action_and_rationale(self):
        a, r = decide_llm({}, client=_FakeClient({"action": "discover", "rationale": "thin"}))
        self.assertEqual(a, "discover")
        self.assertEqual(r, "thin")

    def test_bad_action_raises(self):
        with self.assertRaises(ValueError):
            decide_llm({}, client=_FakeClient({"action": "launch", "rationale": "x"}))

    def test_policy_swallows_bad_response(self):
        pol = LlmDecisionPolicy(client=_FakeClient({"action": "nope", "rationale": "x"}))
        self.assertIsNone(pol({}))

    def test_policy_ceiling_returns_none(self):
        pol = LlmDecisionPolicy(client=_FakeClient(), max_cost_usd=0.0)
        self.assertIsNone(pol({}))
        self.assertTrue(pol.ceiling_hit)

    def test_policy_returns_choice_and_counts(self):
        pol = LlmDecisionPolicy(client=_FakeClient({"action": "stop", "rationale": "ok"}))
        self.assertEqual(pol({}), ("stop", "ok"))
        self.assertEqual(pol.calls, 1)
        self.assertEqual(pol.cache_misses, 1)

    def test_summarize_shape(self):
        s = summarize_for_decision(_obs(counts={"shortlisted": 2}), Goal(shortlist_n=3))
        self.assertEqual(s["shortlisted"], 2)
        self.assertEqual(s["shortlist_target"], 3)
        self.assertIn("last_discovery_age_days", s)


class DecideWithPolicyTests(unittest.TestCase):
    def test_policy_discover_at_discretionary_point(self):
        d = decide(
            _obs(counts={"shortlisted": 3}, total=3, age=1),
            Goal(shortlist_n=3), policy=lambda s: ("discover", "keep hunting"),
        )
        self.assertEqual(d.action, "discover")
        self.assertIn("keep hunting", d.reason)

    def test_policy_stop(self):
        d = decide(
            _obs(counts={"shortlisted": 3}, total=3, age=1),
            Goal(shortlist_n=3), policy=lambda s: ("stop", "enough"),
        )
        self.assertEqual(d.action, "stop")

    def test_policy_none_falls_back_to_rules(self):
        # stale discovery -> deterministic discover
        d = decide(
            _obs(counts={"shortlisted": 3}, total=3, age=99),
            Goal(shortlist_n=3, discovery_stale_days=7), policy=lambda s: None,
        )
        self.assertEqual(d.action, "discover")
        self.assertIn("99d ago", d.reason)

    def test_policy_not_called_on_forced_branches(self):
        called = []

        def spy(s):
            called.append(1)
            return ("discover", "x")

        decide(_obs(counts={"approved": 1}, total=1), Goal(), policy=spy)   # investigate
        decide(_obs(counts={"validated": 5}, total=5), Goal(target_validated=1), policy=spy)
        decide(_obs(), Goal(), llm_capped=True, policy=spy)                  # capped
        self.assertEqual(called, [])

    def test_policy_not_called_when_discovery_exhausted(self):
        called = []
        decide(
            _obs(counts={"shortlisted": 1}, total=1, age=99), Goal(shortlist_n=5),
            discovery_exhausted=True, policy=lambda s: called.append(1) or ("discover", "x"),
        )
        self.assertEqual(called, [])

    def test_policy_not_called_on_cold_start(self):
        called = []
        d = decide(_obs(total=0, age=None), Goal(),
                   policy=lambda s: called.append(1) or ("stop", "x"))
        self.assertEqual(d.action, "discover")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
