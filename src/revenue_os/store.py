"""Local persistence for revenue candidates.

One JSON file, atomically written. Standard library only. No database.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

INITIAL_STATUS = "discovered"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str = ""
    source: str = ""
    raw_ref: str = ""
    total: float = 0.0
    verdict: str = ""
    breakdown: dict = field(default_factory=dict)
    status: str = INITIAL_STATUS
    first_seen: str = ""
    last_scored: str = ""
    history: tuple = ()
    plan: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    offer: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    competition: dict = field(default_factory=dict)
    launch_draft: dict = field(default_factory=dict)
    deliverable: dict = field(default_factory=dict)
    rationale: str = ""
    estimate_source: str = "keyword"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "raw_ref": self.raw_ref,
            "total": self.total,
            "verdict": self.verdict,
            "breakdown": dict(self.breakdown),
            "status": self.status,
            "first_seen": self.first_seen,
            "last_scored": self.last_scored,
            "history": [dict(h) for h in self.history],
            "plan": dict(self.plan),
            "outcome": dict(self.outcome),
            "offer": dict(self.offer),
            "research": dict(self.research),
            "competition": dict(self.competition),
            "launch_draft": dict(self.launch_draft),
            "deliverable": dict(self.deliverable),
            "rationale": self.rationale,
            "estimate_source": self.estimate_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            source=data.get("source", ""),
            raw_ref=data.get("raw_ref", ""),
            total=data.get("total", 0.0),
            verdict=data.get("verdict", ""),
            breakdown=dict(data.get("breakdown", {})),
            status=data.get("status", INITIAL_STATUS),
            first_seen=data.get("first_seen", ""),
            last_scored=data.get("last_scored", ""),
            history=tuple(data.get("history", ())),
            plan=dict(data.get("plan", {})),
            outcome=dict(data.get("outcome", {})),
            offer=dict(data.get("offer", {})),
            research=dict(data.get("research", {})),
            competition=dict(data.get("competition", {})),
            launch_draft=dict(data.get("launch_draft", {})),
            deliverable=dict(data.get("deliverable", {})),
            rationale=data.get("rationale", ""),
            estimate_source=data.get("estimate_source", "keyword"),
        )


class CandidateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_name: dict[str, Candidate] = {}

    @classmethod
    def load(cls, path: str | Path) -> "CandidateStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt candidate store {store.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"candidate store {store.path} must contain a JSON list")
        for item in raw:
            cand = Candidate.from_dict(item)
            store._by_name[cand.name] = cand
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([c.to_dict() for c in self.all()], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, name: str) -> Candidate | None:
        return self._by_name.get(name)

    def all(self) -> list[Candidate]:
        return sorted(self._by_name.values(), key=lambda c: c.total, reverse=True)

    def put(self, candidate: Candidate) -> None:
        """Overwrite a candidate's full record (used for lifecycle updates)."""
        self._by_name[candidate.name] = candidate

    def upsert(self, candidate: Candidate) -> Candidate:
        """Insert a new candidate, or refresh the score of an existing one.

        A human-set status and history are never overwritten here.
        """
        existing = self._by_name.get(candidate.name)
        ts = now_iso()
        if existing is None:
            fresh = replace(
                candidate,
                status=INITIAL_STATUS,
                first_seen=ts,
                last_scored=ts,
                history=(),
            )
            self._by_name[candidate.name] = fresh
            return fresh
        merged = replace(
            existing,
            description=candidate.description or existing.description,
            source=candidate.source or existing.source,
            raw_ref=candidate.raw_ref or existing.raw_ref,
            total=candidate.total,
            verdict=candidate.verdict,
            breakdown=dict(candidate.breakdown),
            rationale=candidate.rationale or existing.rationale,
            estimate_source=candidate.estimate_source or existing.estimate_source,
            last_scored=ts,
        )
        self._by_name[candidate.name] = merged
        return merged
