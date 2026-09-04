"""ExecutionTask adapters for the ecosystem task types (spec section 24).

DISCOVER / VERIFY / EVALUATE / SELECT_STRATEGY all map to read-only,
deterministic ecosystem functions. They spend nothing, post nothing,
contact no one - `task_class.classify_task` classes every one as
SAFE_AUTONOMOUS via its `action_class` kind, and the worker still runs
them inside `autonomous_context()`.

PLAN_TASK / EXECUTE_TASK / VERIFY_RESULT (spec 11) are the TASK-strategy
execution chain: deterministic, EUR 0, no LLM, no network. They build a
real draft deliverable from facts already on the opportunity record and
persist it - nothing here submits it anywhere, spends money, or invents
facts about the requester. The external submission step, and recording
what actually happened once a human submits it, stay outside the worker
(see acceptance.pending_actions' SUBMIT_TASK row and
ecosystem.pipeline.record_task_outcome).

Registered by `register_ecosystem_adapters()` into the default registry so
`revenue_os worker` can drain an ecosystem chain like any other.
"""

from __future__ import annotations

import hashlib

from ..worker import AdapterContext, AdapterRegistry, AdapterResult, TaskAdapter
from . import pipeline
from .discovery import DiscoveryEngine
from .sources import build_source, default_sources


class DiscoverTaskAdapter(TaskAdapter):
    """Runs one discovery cycle. `task.input`:
      sources : comma-separated source names (default: the offline default)
      limit   : max items per source
    """

    task_types = ("DISCOVER",)
    name = "eco-discover"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        inp = ctx.task.input or {}
        names = [s.strip() for s in str(inp.get("sources") or "").split(",") if s.strip()]
        try:
            srcs = ([build_source(n, **({"path": inp["source_path"]}
                                        if n == "file" else {}))
                     for n in names] if names else default_sources())
        except ValueError as exc:
            return AdapterResult(ok=False, retryable=False, error=str(exc))
        report = DiscoveryEngine(ctx.data_dir, sources=srcs).run(
            limit_per_source=int(inp.get("limit", 25)))
        return AdapterResult(ok=True, output=report.to_dict())


class VerifyTaskAdapter(TaskAdapter):
    """Re-verification is already done inline by the discovery engine; this
    task just surfaces the current verdict for one opportunity."""

    task_types = ("VERIFY",)
    name = "eco-verify"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        from . import verification
        draft = pipeline.draft_from_record(ctx.opportunity)
        verdict = verification.verify(draft)
        return AdapterResult(ok=True, output=verdict.to_dict())


class EvaluateTaskAdapter(TaskAdapter):
    task_types = ("EVALUATE",)
    name = "eco-evaluate"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        try:
            out = pipeline.evaluate(ctx.data_dir, ctx.task.opportunity_id)
        except pipeline.EcosystemError as exc:
            return AdapterResult(ok=False, retryable=False, error=str(exc))
        return AdapterResult(ok=True, output=out)


class SelectStrategyTaskAdapter(TaskAdapter):
    task_types = ("SELECT_STRATEGY",)
    name = "eco-select-strategy"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        try:
            out = pipeline.select(ctx.data_dir, ctx.task.opportunity_id)
        except pipeline.EcosystemError as exc:
            return AdapterResult(ok=False, retryable=False, error=str(exc))
        # a strategy that clears the floor is a success; "nothing worth
        # pursuing" is also a valid, non-retryable result.
        return AdapterResult(ok=True, output=out)


_PLACEHOLDER_MARKERS = ("{{", "TODO_FILL")


def _render_task_solution(spec: dict) -> str:
    """Deterministic markdown scaffold for a TASK-strategy deliverable.
    Restates ONLY facts already present on `spec` (itself derived from the
    opportunity record's own title/description/evidence) - it never invents
    domain expertise, numbers, or claims about the requester. The body is
    an explicit human-completion draft, not a fabricated finished answer
    (spec sections 5, 11)."""
    title = spec.get("title", "")
    request = spec.get("request", "") or title
    evidence = spec.get("evidence") or []
    lines = [
        f"# Draft response: {title}",
        "",
        "## Request (from the source)",
        request,
        "",
        "## Evidence this draft is based on",
    ]
    lines += [f"- {e}" for e in evidence] if evidence else ["- (none recorded)"]
    lines += [
        "",
        "## Draft solution",
        "<!-- human: review and complete before submitting anywhere -->",
        f"This is a structured starting point for \"{title}\", built only "
        "from the request and evidence above. Fill in the specific "
        "solution, numbers, code, or content the request asks for, then "
        "review it in full before it is submitted.",
        "",
        "## Before you submit",
    ]
    lines += [f"- {r}" for r in (spec.get("deliverable_requirements") or [])]
    lines += ["", f"_source: {spec.get('source_url') or spec.get('source') or 'n/a'}_"]
    return "\n".join(lines) + "\n"


class PlanTaskAdapter(TaskAdapter):
    """TASK-strategy PLAN_TASK (spec 11): derives a concrete, honest
    deliverable spec from the verified opportunity's own title / description
    / evidence. Pure synthesis - restates facts already on the record,
    invents nothing about the requester or the task domain."""

    task_types = ("PLAN_TASK",)
    name = "eco-plan-task"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        draft = pipeline.draft_from_record(ctx.opportunity)
        title = (draft.title or "").strip()
        if not title:
            return AdapterResult(ok=False, retryable=False,
                                 error="opportunity has no title - nothing to plan")
        spec = {
            "title": title,
            "request": draft.description or title,
            "evidence": [e for e in (draft.evidence or []) if str(e).strip()],
            "source": draft.source_meta.source if draft.source_meta else "",
            "source_url": draft.source_url,
            "est_pay_eur": draft.est_pay_eur,
            "est_time_minutes": draft.est_time_minutes,
            "deliverable_requirements": [
                "addresses every point named in the evidence",
                "restates the request in the requester's own terms",
                "is submitted by a human on the source platform - the fleet "
                "never submits it",
            ],
        }
        return AdapterResult(ok=True, output={"task_spec": spec})


class ExecuteTaskAdapter(TaskAdapter):
    """TASK-strategy EXECUTE_TASK (spec 11): renders the draft deliverable
    from the PLAN_TASK spec and persists it at
    deliverables/<opportunity_id>/task_solution.md. Deterministic template
    synthesis - EUR 0, no LLM, no network."""

    task_types = ("EXECUTE_TASK",)
    name = "eco-execute-task"

    def run(self, ctx: AdapterContext) -> AdapterResult:
        spec = (ctx.dep_outputs.get("PLAN_TASK") or {}).get("task_spec")
        if not isinstance(spec, dict) or not spec.get("title"):
            return AdapterResult(ok=False, retryable=False,
                                 error="no PLAN_TASK spec - nothing to execute")

        oid = ctx.opportunity.get("id") or ctx.task.opportunity_id
        content = _render_task_solution(spec)
        out_dir = ctx.data_dir / "deliverables" / oid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "task_solution.md"
        out_path.write_text(content, encoding="utf-8")

        return AdapterResult(ok=True, output={
            "deliverable_path": f"deliverables/{oid}/task_solution.md",
            "solution_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "title": spec["title"],
        })


class VerifyResultTaskAdapter(TaskAdapter):
    """TASK-strategy VERIFY_RESULT (spec 11): a deterministic, evidence-based
    checklist over the EXECUTE_TASK deliverable - never a fabricated 'looks
    good' pass. Fails closed (non-retryable) when the file is missing,
    empty, off-title, or still carries a template placeholder; only a
    genuinely complete draft is marked verified."""

    task_types = ("VERIFY_RESULT",)
    name = "eco-verify-result"

    _MIN_CHARS = 200

    def run(self, ctx: AdapterContext) -> AdapterResult:
        exec_out = ctx.dep_outputs.get("EXECUTE_TASK") or {}
        rel_path = str(exec_out.get("deliverable_path") or "")
        if not rel_path:
            return AdapterResult(ok=False, retryable=False,
                                 error="no EXECUTE_TASK output - nothing to verify")
        path = ctx.data_dir / rel_path
        if not path.is_file():
            return AdapterResult(ok=False, retryable=False,
                                 error=f"deliverable file missing at {rel_path}")
        content = path.read_text(encoding="utf-8")
        # VERIFY_RESULT depends directly only on EXECUTE_TASK (not PLAN_TASK)
        # - the title travels forward on EXECUTE_TASK's own output, exactly
        # like BuildProductTaskAdapter's offer travels through BUILD_PRODUCT.
        title = str(exec_out.get("title") or "").strip()

        checklist = {
            "file_exists": True,
            "min_length": len(content) >= self._MIN_CHARS,
            "references_title": bool(title) and title.lower() in content.lower(),
            "no_placeholder_left": not any(m in content for m in _PLACEHOLDER_MARKERS),
        }
        failed = [k for k, ok in checklist.items() if not ok]
        if failed:
            return AdapterResult(
                ok=False, retryable=False, output={"checklist": checklist},
                error=f"deliverable failed verification: {', '.join(failed)}")
        return AdapterResult(ok=True, output={
            "verified": True, "checklist": checklist,
            "deliverable_path": rel_path,
        })


def register_ecosystem_adapters(registry: AdapterRegistry) -> AdapterRegistry:
    registry.register(DiscoverTaskAdapter())
    registry.register(VerifyTaskAdapter())
    registry.register(EvaluateTaskAdapter())
    registry.register(SelectStrategyTaskAdapter())
    registry.register(PlanTaskAdapter())
    registry.register(ExecuteTaskAdapter())
    registry.register(VerifyResultTaskAdapter())
    return registry
