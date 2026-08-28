"""Candidate lifecycle: allowed statuses and transitions.

Pure and deterministic. No I/O.
"""

from __future__ import annotations

from dataclasses import replace

from .store import Candidate, now_iso

STATUSES: tuple[str, ...] = (
    "discovered",
    "shortlisted",
    "approved",
    "investigating",
    "validated",
    "launched",
    "earning",
    "rejected",
)

TERMINAL: frozenset[str] = frozenset({"rejected"})

# forward transitions; "rejected" is reachable from every non-terminal status.
_FORWARD: dict[str, set[str]] = {
    "discovered": {"shortlisted"},
    "shortlisted": {"approved"},
    "approved": {"investigating"},
    "investigating": {"validated"},
    "validated": {"launched"},
    "launched": {"earning"},
    "earning": {"earning"},  # further payments keep the candidate earning
}


def can_transition(frm: str, to: str) -> bool:
    if frm not in STATUSES or to not in STATUSES:
        return False
    if frm in TERMINAL:
        return False
    if to == "rejected":
        return True
    return to in _FORWARD.get(frm, set())


def check_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise ValueError(f"illegal transition: {frm!r} -> {to!r}")


def advance(candidate: Candidate, to: str, *, note: str = "", actor: str = "system") -> Candidate:
    check_transition(candidate.status, to)
    entry = {
        "ts": now_iso(),
        "from": candidate.status,
        "to": to,
        "note": note,
        "actor": actor,
    }
    return replace(candidate, status=to, history=candidate.history + (entry,))
