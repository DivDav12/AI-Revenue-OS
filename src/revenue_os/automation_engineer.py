"""Automation Engineer (#13, build cluster, HUMAN-GATED) - workflow wiring.

Connects finished components into ONE repeatable workflow graph over the
existing one-cycle orchestrator model. It never creates a daemon: the
only triggers it emits are "manual" and "one-cycle autopilot". It marks
every human gate and a failure path for every node, and preserves
restart-safe state (the workflow is re-derivable each cycle).
"""

from __future__ import annotations

import re

from . import roster
from .agent import Agent
from .messages import Result, Task

_DAEMON_HINT = re.compile(r"\b(daemon|cron|schedule|every \d+ (min|hour)|24/7|forever)\b", re.I)

_ALWAYS_HUMAN = (
    "external contact", "posting", "publishing", "spending", "payment",
    "irreversible actions",
)


def build_workflow_graph(steps: list, *, workflow_specification: dict | None = None) -> dict:
    spec = workflow_specification or {}
    norm = []
    for i, s in enumerate(steps or [], 1):
        if isinstance(s, dict):
            node = {"id": str(s.get("id") or s.get("capability") or f"step_{i}"),
                    "capability": s.get("capability"),
                    "agent": s.get("agent")}
        else:
            node = {"id": f"step_{i}", "capability": str(s), "agent": None}
        norm.append(node)

    ids = [n["id"] for n in norm]
    dependencies = [{"from": a, "to": b} for a, b in zip(ids, ids[1:])]

    human_gates = list(_ALWAYS_HUMAN)
    for n in norm:
        rspec = roster.by_capability(n["capability"]) if n["capability"] else None
        if rspec is not None and rspec.gate == "human":
            human_gates.append(n["id"])

    failure_paths = [
        {"node": n["id"],
         "on_error": "isolate, record to task_log, continue the cycle; "
                     "pause the autopilot only for a payment/network fault"}
        for n in norm
    ]

    daemon_requested = bool(_DAEMON_HINT.search(str(spec)))

    return {
        "workflow_graph": {"nodes": norm, "edges": dependencies},
        "dependencies": dependencies,
        "triggers": ["manual", "one-cycle autopilot (revenue_os autopilot cycle)"],
        "actions": norm,
        "human_gates": sorted(set(human_gates)),
        "failure_paths": failure_paths,
        "restart_safe": True,
        "restart_safe_note": "no in-process state; the graph is re-derived each cycle "
                             "and progress lives in the existing JSON stores",
        "daemon": False,
        "blocking_issues": (["workflow spec requests a daemon / schedule - refused"]
                            if daemon_requested else []),
        "human_gate_required": True,
    }


class AutomationEngineerAgent(Agent):
    role = "automation_engineer"
    objective = "Wire components into one restart-safe, human-gated workflow."
    capabilities = ("automate",)

    def run(self, task: Task) -> Result:
        steps = task.payload.get("steps")
        if steps is None:
            steps = task.payload.get("agent_outputs")
        if not isinstance(steps, list) or not steps:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs a non-empty list under 'steps' or 'agent_outputs'")
        ws = task.payload.get("workflow_specification")
        if ws is not None and not isinstance(ws, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['workflow_specification'] must be a dict when given")
        graph = build_workflow_graph(steps, workflow_specification=ws)
        return Result(task_id=task.id, agent=self.name, status="ok", output=graph)
