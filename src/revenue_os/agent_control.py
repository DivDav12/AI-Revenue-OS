"""The agent control plane - persisted enable/disable + global pause.

JARVIS (jarvis_server.py) and any other caller flips these switches; the
generic dispatcher (`agent_runner.run_agent`) and the pipeline read them
before running anything. This is the ONLY place a "paused" or "disabled"
decision lives.

State: <data-dir>/agent_control.json, atomically written.

  {
    "paused": false,
    "paused_reason": "",
    "agents": { "<roster id>": {"enabled": true, "note": "",
                                "updated_at": "...", "updated_by": "..."} },
    "updated_at": "..."
  }

An absent file / absent agent entry means **enabled and not paused** -
the control plane is opt-in and fails open, exactly like today's
behaviour. It never touches money, network, or an LLM: disabling an
agent only stops a local function call.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import roster
from .store import now_iso


class AgentPaused(ValueError):
    """Raised when a disabled / globally-paused agent is asked to run."""


MODES = ("manual", "auto", "autonomous", "paused")


class AgentControl:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.paused: bool = False
        self.paused_reason: str = ""
        self.agents: dict[str, dict] = {}
        self.updated_at: str = ""
        # fleet operating mode - "manual" (default, safe): nothing runs unless
        # a button is pressed. "auto": batch ops enabled. "paused": == paused.
        # manual (default, safe) | auto (batch buttons) | autonomous (the
        # revenue loop runs on its own) | paused
        self.mode: str = "manual"

    # --- persistence ---------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "AgentControl":
        ctrl = cls(path)
        if not ctrl.path.exists():
            return ctrl
        try:
            raw = json.loads(ctrl.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt agent control file {ctrl.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"agent control file {ctrl.path} must be a JSON object")
        ctrl.paused = bool(raw.get("paused", False))
        ctrl.paused_reason = str(raw.get("paused_reason", "") or "")
        agents = raw.get("agents")
        if isinstance(agents, dict):
            ctrl.agents = {
                str(k): dict(v) for k, v in agents.items() if isinstance(v, dict)
            }
        ctrl.updated_at = str(raw.get("updated_at", "") or "")
        m = str(raw.get("mode", "") or "").lower()
        ctrl.mode = m if m in MODES else ("paused" if ctrl.paused else "manual")
        return ctrl

    def save(self) -> None:
        self.updated_at = now_iso()
        payload = json.dumps(
            {
                "paused": self.paused,
                "paused_reason": self.paused_reason,
                "mode": self.mode,
                "agents": self.agents,
                "updated_at": self.updated_at,
            },
            indent=2,
            sort_keys=True,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- queries -----------------------------------------------------
    def is_paused(self) -> bool:
        return self.paused

    def is_enabled(self, agent_id: str) -> bool:
        entry = self.agents.get(str(agent_id))
        if entry is None:
            return True
        return bool(entry.get("enabled", True))

    def entry(self, agent_id: str) -> dict:
        """The stored control entry for an agent, with defaults filled in."""
        e = dict(self.agents.get(str(agent_id)) or {})
        e.setdefault("enabled", True)
        e.setdefault("note", "")
        e.setdefault("updated_at", "")
        e.setdefault("updated_by", "")
        return e

    def runnable(self, capability: str) -> tuple[bool, str]:
        """(may this capability run now?, human-readable reason if not).

        Checks the global pause first, then the owning agent's switch.
        An unknown / not-live capability is refused with a clear message.
        """
        spec = roster.by_capability(capability)
        if spec is None:
            return False, f"no roster agent owns capability {capability!r}"
        if spec.status != "live":
            return False, f"{spec.name} is not live (status={spec.status})"
        if self.paused:
            why = self.paused_reason or "operator hit the global pause"
            return False, f"ALL agents are paused - {why}"
        if not self.is_enabled(spec.id):
            note = (self.agents.get(spec.id) or {}).get("note") or ""
            tail = f" ({note})" if note else ""
            return False, f"{spec.name} is disabled in the control plane{tail}"
        return True, ""

    # --- human-gate acknowledgement -------------------------------
    def gate_acknowledged(self, agent_id: str, output_ts: str) -> bool:
        """True when a human has marked this agent's current output as
        handled. Keyed to the output timestamp, so a fresh run re-opens
        the gate."""
        e = self.agents.get(str(agent_id)) or {}
        return "gate_ack_ts" in e and e.get("gate_ack_ts") == str(output_ts or "")

    def gate_ack_info(self, agent_id: str) -> dict:
        e = self.agents.get(str(agent_id)) or {}
        return {k[len("gate_"):]: e[k] for k in e if k.startswith("gate_ack_")}

    def acknowledge_gate(self, agent_id: str, output_ts: str, *,
                         by: str = "jarvis", note: str = "") -> dict:
        spec = roster.get(agent_id)
        if spec is None:
            raise ValueError(f"unknown agent id: {agent_id!r}")
        if spec.gate != "human":
            raise ValueError(f"{spec.name} is not a human-gated agent")
        e = self.agents.get(agent_id) or {"enabled": True}
        e["gate_ack_ts"] = str(output_ts or "")
        e["gate_ack_by"] = str(by or "jarvis")
        e["gate_ack_at"] = now_iso()
        e["gate_ack_note"] = str(note or "")
        self.agents[agent_id] = e
        return e

    def reopen_gate(self, agent_id: str) -> None:
        e = self.agents.get(str(agent_id))
        if not e:
            return
        for k in ("gate_ack_ts", "gate_ack_by", "gate_ack_at", "gate_ack_note"):
            e.pop(k, None)

    # --- mutations --------------------------------------------------
    def set_agent(self, agent_id: str, enabled: bool, *, by: str = "jarvis",
                  note: str = "") -> dict:
        spec = roster.get(agent_id)
        if spec is None:
            raise ValueError(f"unknown agent id: {agent_id!r}")
        entry = self.agents.get(agent_id) or {}   # keep any gate-ack fields
        entry.update({
            "enabled": bool(enabled),
            "note": str(note or ""),
            "updated_at": now_iso(),
            "updated_by": str(by or "jarvis"),
        })
        self.agents[agent_id] = entry
        return entry

    def set_paused(self, paused: bool, *, by: str = "jarvis", reason: str = "") -> None:
        self.paused = bool(paused)
        self.paused_reason = (
            f"{reason} - {by}" if reason else (f"paused by {by}" if paused else "")
        )
        if paused:
            self.mode = "paused"
        elif self.mode == "paused":
            self.mode = "manual"

    def set_mode(self, mode: str, *, by: str = "jarvis") -> str:
        """manual | auto | paused. 'paused' pauses the fleet; leaving 'paused'
        unpauses. Never bypasses a human gate."""
        mode = str(mode or "").lower()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}")
        if mode == "paused":
            self.set_paused(True, by=by, reason="mode=paused")
        else:
            if self.paused:
                self.set_paused(False, by=by)
            self.mode = mode
        return self.mode


def load_agent_control(data_dir: str | Path) -> AgentControl:
    return AgentControl.load(Path(data_dir) / "agent_control.json")


def check_runnable(data_dir: str | Path, capability: str) -> tuple[bool, str]:
    """Disk-backed one-shot check used by the dispatcher and the pipeline."""
    return load_agent_control(data_dir).runnable(capability)


def gate(data_dir: str | Path, capability: str) -> None:
    """Raise AgentPaused if `capability` may not run right now."""
    ok, reason = check_runnable(data_dir, capability)
    if not ok:
        raise AgentPaused(reason)
