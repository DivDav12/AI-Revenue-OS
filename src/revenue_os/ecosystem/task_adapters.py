"""ExecutionTask adapters for the ecosystem task types (spec section 24).

DISCOVER / VERIFY / EVALUATE / SELECT_STRATEGY all map to read-only,
deterministic ecosystem functions. They spend nothing, post nothing,
contact no one - `task_class.classify_task` classes every one as
SAFE_AUTONOMOUS via its `action_class` kind, and the worker still runs
them inside `autonomous_context()`.

Registered by `register_ecosystem_adapters()` into the default registry so
`revenue_os worker` can drain an ecosystem chain like any other.
"""

from __future__ import annotations

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


def register_ecosystem_adapters(registry: AdapterRegistry) -> AdapterRegistry:
    registry.register(DiscoverTaskAdapter())
    registry.register(VerifyTaskAdapter())
    registry.register(EvaluateTaskAdapter())
    registry.register(SelectStrategyTaskAdapter())
    return registry
