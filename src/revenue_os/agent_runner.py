"""Generic dispatch for the deterministic roster agents.

`run_agent` routes ONE Task through the existing Orchestrator + team
registry, then persists a successful result to `agent_outputs.json` so
the output survives a restart. It is the thin coordination layer the
Operator/CEO or a human command calls; the agents themselves stay pure.

It refuses to auto-run a human-gated capability (same invariant as
`OperatorAgent.act`): the agent still produces its draft/spec, but the
result is tagged and never treated as an executed action.

No LLM, no network, no money.
"""

from __future__ import annotations

from pathlib import Path

from . import roster
from .agent_outputs import AgentOutputStore
from .messages import Result, Task
from .team import build_team


def run_agent(data_dir, capability: str, payload: dict | None = None, *,
              objective: str = "", persist: bool = True,
              sink=None) -> Result:
    """Dispatch `capability` to its agent and (by default) persist the output.

    Raises ValueError if no live roster agent owns the capability.
    """
    spec = roster.by_capability(capability)
    if spec is None:
        raise ValueError(f"unknown capability: {capability!r}")
    if spec.status != "live":
        raise ValueError(f"{spec.name} is not live yet (status={spec.status})")

    team = build_team(sink=sink)
    task = Task(objective=objective or f"run {spec.name}",
                capability=capability, payload=dict(payload or {}))
    team.add_task(task)
    results = team.run_cycle()
    result = results[0] if results else Result(
        task_id=task.id, agent="orchestrator", status="error",
        error="no result produced")

    if persist and result.status == "ok":
        out = dict(result.output)
        if spec.gate == "human":
            out.setdefault("human_gate_required", True)
            out["_gate"] = "human"
        store = AgentOutputStore.load(Path(data_dir) / "agent_outputs.json")
        store.put(capability, out, objective=task.objective)
        store.save()
    return result


def last_output(data_dir, capability: str) -> dict | None:
    """The most recent persisted output for a capability, or None."""
    store = AgentOutputStore.load(Path(data_dir) / "agent_outputs.json")
    return store.output(capability)
