"""Human decision point.

record_decision() is the explicit human go/no-go required before a
candidate can progress. It touches no money and takes no external
action: it only records a decision and moves lifecycle status.
"""

from __future__ import annotations

from . import lifecycle
from .store import Candidate, CandidateStore

_DECISIONS = {"approve": "approved", "reject": "rejected"}


def record_decision(
    store: CandidateStore,
    name: str,
    decision: str,
    *,
    approver: str,
    note: str = "",
) -> Candidate:
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {sorted(_DECISIONS)}")
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    updated = lifecycle.advance(
        candidate, _DECISIONS[decision], note=note, actor=approver
    )
    store.put(updated)
    store.save()
    return updated
