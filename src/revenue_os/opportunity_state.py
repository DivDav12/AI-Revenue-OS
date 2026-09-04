"""The Opportunity lifecycle state machine.

Pure and deterministic - no I/O, no wall-clock, no randomness. The
persistent store (`opportunity_store.OpportunityStore`) owns the records;
this module owns the vocabulary, the legal transitions, and the shape of a
transition record.

A state changes ONLY when:

  * the real action behind it actually succeeded (a task result), or
  * a human explicitly confirmed it (`force=True` on the store's
    `transition()`, recorded with `forced: true` and a human actor).

Nothing here ever "presents" a state - a UI reads `state`, it never sets
it. `DEPLOYING -> LIVE`, for example, is only legal once a deployment
adapter has returned a real URL; the store call that records it must be
driven by that result, not by a button.

State record (persisted on each opportunity as `transitions: [ ... ]`):

  ts              ISO timestamp
  previous_state  state before the move
  next_state      state after the move
  reason          one line: why it moved
  source          what caused it ("task" | "loop:<phase>" | "human" |
                  "legacy_status_sync" | "migration")
  actor           "system" | "jarvis" | "strategist" | "<human>" | ...
  task_id         optional - the ExecutionTask that produced the result
  error           optional - present when moving to FAILED / BLOCKED
  forced          optional - true when a human overrode the legal table
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OpportunityState(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHING = "RESEARCHING"
    SCORED = "SCORED"
    SELECTED = "SELECTED"
    PLANNING = "PLANNING"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    READY_TO_DEPLOY = "READY_TO_DEPLOY"
    DEPLOYING = "DEPLOYING"
    LIVE = "LIVE"
    ACQUIRING_TRAFFIC = "ACQUIRING_TRAFFIC"
    MEASURING = "MEASURING"
    FIRST_VISITOR = "FIRST_VISITOR"
    FIRST_LEAD = "FIRST_LEAD"
    FIRST_SALE = "FIRST_SALE"
    DELIVERING = "DELIVERING"
    ACTIVE = "ACTIVE"
    OPTIMIZING = "OPTIMIZING"
    PROFITABLE = "PROFITABLE"
    SCALING = "SCALING"
    NO_TRACTION = "NO_TRACTION"
    ABANDONED = "ABANDONED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


STATES: tuple[str, ...] = tuple(s.value for s in OpportunityState)

#: no forward move out of these (ABANDONED is the only truly terminal one)
TERMINAL: frozenset[str] = frozenset({"ABANDONED"})

#: a stumble that can be cleared back onto the path (see can_transition)
RECOVERABLE: frozenset[str] = frozenset({"BLOCKED", "FAILED"})

INITIAL: str = OpportunityState.DISCOVERED.value


class IllegalTransition(ValueError):
    """Raised when an unforced transition is not in the legal table."""


# ---------------------------------------------------------------------------
# the legal table - forward + branch edges only. ABANDONED and BLOCKED are
# reachable from every non-terminal state (handled in can_transition), so
# they are omitted here.
# ---------------------------------------------------------------------------

_FORWARD: dict[str, set[str]] = {
    "DISCOVERED":        {"RESEARCHING", "SCORED"},
    "RESEARCHING":       {"SCORED", "FAILED"},
    "SCORED":            {"SELECTED"},
    "SELECTED":          {"PLANNING"},
    "PLANNING":          {"BUILDING", "FAILED"},
    "BUILDING":          {"VALIDATING", "FAILED"},
    # FIRST_SALE from VALIDATING: the TASK-strategy chain (spec 11) has no
    # DEPLOY step - a validated deliverable is submitted by a human on the
    # source platform, and a confirmed real payment
    # (ecosystem.pipeline.record_task_outcome) lands here directly, exactly
    # like the existing LIVE -> FIRST_SALE precedent above for the PRODUCT
    # chain. Never set except from a human-confirmed payment.
    "VALIDATING":        {"READY_TO_DEPLOY", "BUILDING", "FAILED", "FIRST_SALE"},
    "READY_TO_DEPLOY":   {"DEPLOYING"},
    "DEPLOYING":         {"LIVE", "READY_TO_DEPLOY", "FAILED"},
    # a confirmed first payment can land on a live page before any separate
    # measurement phase is entered (Phase 11).
    "LIVE":              {"ACQUIRING_TRAFFIC", "MEASURING", "FIRST_SALE"},
    "ACQUIRING_TRAFFIC": {"MEASURING", "LIVE"},
    "MEASURING":         {"FIRST_VISITOR", "FIRST_LEAD", "FIRST_SALE",
                          "NO_TRACTION", "OPTIMIZING", "ACQUIRING_TRAFFIC"},
    "FIRST_VISITOR":     {"FIRST_LEAD", "FIRST_SALE", "MEASURING",
                          "NO_TRACTION", "OPTIMIZING"},
    "FIRST_LEAD":        {"FIRST_SALE", "MEASURING", "NO_TRACTION", "OPTIMIZING"},
    "FIRST_SALE":        {"DELIVERING", "ACTIVE", "MEASURING"},
    "DELIVERING":        {"ACTIVE", "FIRST_SALE", "FAILED"},
    "ACTIVE":            {"OPTIMIZING", "MEASURING", "PROFITABLE",
                          "NO_TRACTION", "DELIVERING"},
    "OPTIMIZING":        {"MEASURING", "ACTIVE", "BUILDING", "ACQUIRING_TRAFFIC",
                          "PROFITABLE", "NO_TRACTION"},
    "PROFITABLE":        {"SCALING", "ACTIVE", "OPTIMIZING", "NO_TRACTION"},
    "SCALING":           {"PROFITABLE", "ACTIVE", "OPTIMIZING"},
    "NO_TRACTION":       {"OPTIMIZING", "ACQUIRING_TRAFFIC"},
}


def can_transition(frm: str, to: str) -> bool:
    """True when `frm -> to` is a legal move without a human override."""
    if frm not in STATES or to not in STATES or frm == to:
        return False
    if frm in TERMINAL:
        return False
    if to in ("ABANDONED", "BLOCKED"):
        return True                      # give up / hit a blocker from anywhere
    if frm in RECOVERABLE:
        return to not in TERMINAL         # a cleared blocker/failure rejoins the path
    return to in _FORWARD.get(frm, set())


def check_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(f"illegal transition: {frm!r} -> {to!r}")


def next_states(frm: str) -> tuple[str, ...]:
    """Every state reachable from `frm` in one legal move."""
    return tuple(s for s in STATES if can_transition(frm, s))


# ---------------------------------------------------------------------------
# transition record
# ---------------------------------------------------------------------------

@dataclass
class Transition:
    ts: str
    previous_state: str
    next_state: str
    reason: str
    source: str
    actor: str = "system"
    task_id: str = ""
    error: str = ""
    forced: bool = False

    def to_dict(self) -> dict:
        d = {
            "ts": self.ts,
            "previous_state": self.previous_state,
            "next_state": self.next_state,
            "reason": self.reason,
            "source": self.source,
            "actor": self.actor,
        }
        if self.task_id:
            d["task_id"] = self.task_id
        if self.error:
            d["error"] = self.error
        if self.forced:
            d["forced"] = True
        return d


# ---------------------------------------------------------------------------
# legacy bridge - the store keeps the old 7-value `status` field working and
# mirrors every change into this machine. The map is deliberately
# conservative: a staged local page is READY_TO_DEPLOY, never LIVE; a
# strategist "promote" is ACTIVE, never PROFITABLE (no verified revenue).
# ---------------------------------------------------------------------------

LEGACY_STATUS_TO_STATE: dict[str, str] = {
    "discovered": "DISCOVERED",
    "evaluating": "SCORED",
    "building": "BUILDING",
    "testing": "READY_TO_DEPLOY",
    "active": "ACTIVE",
    "successful": "ACTIVE",
    "abandoned": "ABANDONED",
}


def state_for_legacy_status(status: str) -> str:
    return LEGACY_STATUS_TO_STATE.get(status, INITIAL)
