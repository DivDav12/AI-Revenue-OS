"""Developer AI (#12, build cluster, HUMAN-GATED) - implementation planner.

Turns a build specification into an ordered implementation plan. It
PLANS ONLY: it never edits a file, so `files_changed` and `tests_added`
are always empty and `implementation_status` is always "planned" (or
"blocked" when the spec asks for something forbidden). Every proposed
file gets a matching required test - an implementation without tests is
a blocking issue. It never touches payment credentials and never exposes
secrets.
"""

from __future__ import annotations

import re

from .agent import Agent
from .messages import Result, Task

_FORBIDDEN_PATTERNS = (
    (re.compile(r"paypal.*(secret|client[_-]?id|credential)", re.I),
     "modifies PayPal credentials"),
    (re.compile(r"(rm -rf|drop table|delete from|force push|--force)", re.I),
     "destructive operation without a human gate"),
    (re.compile(r"(print|echo|log).*(secret|api[_-]?key|token|password)", re.I),
     "would expose a secret"),
    (re.compile(r"\b(daemon|while True|cron|schedule)\b", re.I),
     "introduces an autonomous daemon / scheduler"),
)

_SECRET_HINT = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]\s*\S+|password\s*[:=]\s*\S+)", re.I)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return s or "component"


def _redact(text: str) -> str:
    return _SECRET_HINT.sub("***redacted***", str(text))


def build_implementation_plan(spec: dict, *, technical_requirements: list | None = None) -> dict:
    spec = spec or {}
    raw_reqs = [str(r).strip() for r in (technical_requirements
                                         or spec.get("requirements") or []) if str(r).strip()]
    reqs = [_redact(r) for r in raw_reqs]
    haystack = _redact(" ".join([str(spec), " ".join(raw_reqs)]))

    blocking = [reason for pat, reason in _FORBIDDEN_PATTERNS if pat.search(haystack)]
    had_secret = any(r != rr for r, rr in zip(reqs, raw_reqs)) or \
        _SECRET_HINT.search(str(spec))
    if had_secret:
        blocking.append("a secret value appeared in the spec - redacted; do not proceed")

    steps, proposed_files, required_tests = [], [], []
    if not blocking:
        for i, req in enumerate(reqs, 1):
            module = _slug(req)[:40]
            steps.append(f"{i}. implement: {req}")
            proposed_files.append(f"src/revenue_os/{module}.py")
            required_tests.append(f"tests/test_{module}.py")

    status = "blocked" if blocking else ("planned" if steps else "empty")

    return {
        "component": str(spec.get("component") or spec.get("opportunity") or ""),
        "implementation_plan": steps,
        "proposed_files": proposed_files,
        "files_changed": [],
        "tests_added": [],
        "required_tests": required_tests,
        "tests_cover_every_file": len(required_tests) == len(proposed_files) and bool(proposed_files),
        "implementation_status": status,
        "blocking_issues": blocking,
        "human_gate_required": True,
        "forbidden": ["modify payment credentials", "destructive change without a gate",
                      "expose secrets", "autonomous daemon"],
        "note": "plan only - no file was created or changed",
    }


class DeveloperAgent(Agent):
    role = "developer"
    objective = "Plan a technical implementation; never execute it."
    capabilities = ("develop",)

    def run(self, task: Task) -> Result:
        spec = task.payload.get("build_specification")
        if not isinstance(spec, dict) or not spec:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['build_specification'] must be a non-empty dict")
        tr = task.payload.get("technical_requirements")
        if tr is not None and not isinstance(tr, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['technical_requirements'] must be a list when given")
        plan = build_implementation_plan(spec, technical_requirements=tr)
        return Result(task_id=task.id, agent=self.name, status="ok", output=plan)
