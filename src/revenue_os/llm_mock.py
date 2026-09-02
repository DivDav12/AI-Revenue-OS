"""A deterministic MOCK LLM client.

Same call shape as `anthropic.Anthropic()` for the code paths this
project uses (`client.messages.create(...)` returning `.content` blocks
with `type == "tool_use"` and `.usage`). It returns schema-valid canned
tool output so the LLM-backed agents (research, competition, offer,
copy, launch plan, opportunity discovery) can be exercised end-to-end in
tests and in a `provider: "mock"` dry run - with NO network and $0.

Selected when `llm_policy.json` has `provider == "mock"` or the env var
`REVENUE_OS_LLM_MOCK=1` is set (test convenience).
"""

from __future__ import annotations

import hashlib
import os


def mock_selected(policy=None) -> bool:
    if os.environ.get("REVENUE_OS_LLM_MOCK") in ("1", "true", "yes"):
        return True
    return bool(policy is not None and getattr(policy, "provider", "") == "mock")


def _seed(*parts) -> int:
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:6], 16)


class _Block:
    type = "tool_use"

    def __init__(self, name: str, data: dict) -> None:
        self.name = name
        self.input = data


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o


class _Response:
    def __init__(self, blocks: list, usage: _Usage) -> None:
        self.content = blocks
        self.usage = usage
        self.stop_reason = "tool_use"


# canned, schema-shaped output per tool name -------------------------------

def _tool_payload(tool: str, prompt: str) -> dict:
    n = _seed(tool, prompt[:200])
    verdicts = ("proceed", "caution", "avoid")
    comp = ("open", "contested", "crowded")
    if tool == "record_scores":
        base = 2.5 + (n % 20) / 10.0            # 2.5 .. 4.4
        return {c: round(min(5.0, base + (i % 3) * 0.2), 1)
                for i, c in enumerate(
                    ("startup_affordability", "automation_potential", "real_demand",
                     "competition_gap", "legal_feasibility", "time_to_revenue",
                     "profit_potential", "scalability"))} | {
                    "rationale": f"[mock] plausible read of: {prompt[:60]}"}
    if tool == "record_research":
        return {"summary": f"[mock] {prompt[:80]}",
                "key_findings": ["mock finding A", "mock finding B"],
                "risks": ["mock risk"], "verdict": verdicts[n % 3],
                "confidence": ["low", "medium", "high"][n % 3],
                "sources": []}
    if tool == "record_competition_analysis":
        return {"named_competitors": ["MockCo", "SampleTools"],
                "pricing_landscape": "[mock] $9-$49/mo range",
                "differentiation_angle": "[mock] faster onboarding",
                "saturation": comp[n % 3], "verdict": comp[n % 3],
                "rationale": "[mock]", "sources": []}
    if tool == "record_offer":
        return {"what_is_sold": f"[mock] {prompt[:40]}",
                "price": 19 + (n % 5) * 10, "currency": "EUR",
                "delivery": "digital",
                "includes": ["core deliverable", "quick-start guide"],
                "call_to_action": "Get it",
                "positioning": "[mock] for a specific customer"}
    if tool == "record_launch_copy":
        return {"headline": f"[mock] {prompt[:50]}",
                "subheadline": "[mock] made for you",
                "body": "[mock] body copy paragraph.",
                "primary_cta": "Get it",
                "faq": [{"question": "Refunds?", "answer": "Yes."},
                        {"question": "Format?", "answer": "PDF."},
                        {"question": "For whom?", "answer": "Specific buyers."}]}
    if tool == "record_relevance":
        return {"relevance_score": 40 + (n % 55),
                "is_active_problem": bool(n % 2),
                "buying_intent": ("low", "medium", "high")[n % 3],
                "prospect_type": "active_problem",
                "reason": "[mock] matches the stated problem",
                "recommended_fit": "medium"}
    if tool == "record_launch_plan":
        return {"business_analysis": "[mock] analysis",
                "ideal_customer": "[mock] ICP",
                "acquisition_opportunities": [f"[mock] channel {i}" for i in range(5)],
                "prioritized_strategy": "[mock] do channel 1 first",
                "action_plan_14_day": [f"day {i}: [mock] step" for i in range(1, 15)],
                "outreach_templates": ["[mock] template 1", "[mock] template 2"],
                "next_steps": ["[mock] next step"], "sources": []}
    if tool == "record_opportunity":
        return {"title": f"[mock] {prompt[:50]}", "category": "micro_saas",
                "target_customer": "[mock] segment",
                "problem": "[mock] a real pain point",
                "willingness_to_pay_eur": 15 + (n % 6) * 10,
                "competition": comp[n % 3],
                "implementation_difficulty": 1 + n % 5,
                "distribution_difficulty": 1 + (n // 3) % 5,
                "rationale": "[mock]"}
    return {"note": f"[mock] no canned payload for tool {tool!r}"}


class MockLlmClient:
    """Drop-in for anthropic.Anthropic()."""

    def __init__(self) -> None:
        self.messages = self._Messages()

    class _Messages:
        def create(self, *, model="claude-mock", messages=None, tools=None,
                   max_tokens=512, **kw):
            prompt = ""
            for m in (messages or []):
                c = m.get("content")
                prompt += c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in c if isinstance(b, dict))
            tool = ((tools or [{}])[0].get("name")
                    if isinstance((tools or [None])[0], dict) else None) or "record_scores"
            data = _tool_payload(tool, prompt)
            i_tok = max(1, len(prompt) // 4)
            o_tok = max(1, len(str(data)) // 4)
            return _Response([_Block(tool, data)], _Usage(i_tok, o_tok))
