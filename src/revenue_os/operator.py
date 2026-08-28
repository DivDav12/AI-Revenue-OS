"""The operator agent.

A single autonomous coordinator: it holds a Goal, observes the pipeline
state, decides the next non-human action, executes it via the existing
workflow functions, and repeats until only human-gated actions remain
(or a cycle cap is hit). Every decision is logged.

Deterministic policy, deterministic leaf workers, no LLM, no money. It
has no code path to approve a candidate, launch an offer, set a budget,
or record a payment - it stops at those gates and hands off via the
digest.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .agent_log import AgentLog
from .discovery_log import DiscoveryLog
from .filtering import is_relevant
from .report import STALE_AFTER_DAYS, _age_days, digest_line, pipeline_report
from .revenue import RevenueLedger
from .sources import FilteredSource, build_source
from .spend import SpendLedger
from .store import CandidateStore
from .workflow import investigate_approved, prepare_launch, run_discovery_cycle

_ACTIONS = ("discover", "investigate", "prepare_launch", "stop")


@dataclass(frozen=True)
class Goal:
    sources: tuple[str, ...] = ("static",)
    filter: bool = False
    min_score: float = 0.0
    shortlist_n: int = 3
    limit: int = 10
    calibrated: bool = False
    target_validated: int | None = None
    discovery_stale_days: int = STALE_AFTER_DAYS

    def to_dict(self) -> dict:
        return {
            "sources": list(self.sources),
            "filter": self.filter,
            "min_score": self.min_score,
            "shortlist_n": self.shortlist_n,
            "limit": self.limit,
            "calibrated": self.calibrated,
            "target_validated": self.target_validated,
            "discovery_stale_days": self.discovery_stale_days,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Goal":
        return cls(
            sources=tuple(d.get("sources", ("static",))) or ("static",),
            filter=bool(d.get("filter", False)),
            min_score=float(d.get("min_score", 0.0)),
            shortlist_n=int(d.get("shortlist_n", 3)),
            limit=int(d.get("limit", 10)),
            calibrated=bool(d.get("calibrated", False)),
            target_validated=d.get("target_validated"),
            discovery_stale_days=int(d.get("discovery_stale_days", STALE_AFTER_DAYS)),
        )


def load_goal(data_dir: str | Path) -> Goal:
    path = Path(data_dir) / "goal.json"
    if not path.exists():
        return Goal()
    return Goal.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_goal(data_dir: str | Path, goal: Goal) -> None:
    path = Path(data_dir) / "goal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(goal.to_dict(), indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentStep:
    decision: Decision
    result: dict
    digest: str
    entry: dict


def _source_for(spec: str):
    if spec.startswith("file:"):
        return build_source("file", spec[len("file:"):])
    return build_source(spec)


def _validated_without_offer(report: dict) -> int:
    return sum(
        1 for c in report.get("candidates", [])
        if c["status"] == "validated" and not c.get("offer")
    )


def decide(obs: dict, goal: Goal, *, discovery_exhausted: bool = False) -> Decision:
    """Pure: pick the next non-human action from the observed state."""
    report = obs["report"]
    counts = report["status_counts"]
    queue = report["action_queue"]
    age = obs["last_discovery_age_days"]

    beyond = counts["validated"] + counts["launched"] + counts["earning"]
    if goal.target_validated is not None and beyond >= goal.target_validated:
        return Decision(
            "stop",
            f"target reached: {beyond} >= {goal.target_validated} validated",
        )

    if counts["approved"] > 0:
        return Decision(
            "investigate",
            f"{counts['approved']} approved candidate(s) need a validation plan",
            {"approved": counts["approved"]},
        )

    no_offer = _validated_without_offer(report)
    if no_offer > 0:
        return Decision(
            "prepare_launch",
            f"{no_offer} validated candidate(s) without an offer",
            {"validated_without_offer": no_offer},
        )

    total = report["totals"]["candidates"]
    if not discovery_exhausted:
        if total == 0 or age is None:
            return Decision("discover", "no discovery has run yet")
        if counts["shortlisted"] < goal.shortlist_n and counts["discovered"] == 0:
            return Decision(
                "discover",
                f"shortlist has {counts['shortlisted']} (< {goal.shortlist_n}) "
                "and nothing awaits triage",
            )
        if age >= goal.discovery_stale_days:
            return Decision(
                "discover", f"last discovery was {age}d ago (>= "
                f"{goal.discovery_stale_days}d)"
            )

    return Decision("stop", f"only human-gated actions remain: {digest_line(queue)}")


class OperatorAgent:
    def __init__(self, data_dir: str | Path, goal: Goal | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.goal = goal or Goal()
        self._discovery_exhausted = False

    # --- state -----------------------------------------------------------

    def _load(self):
        d = self.data_dir
        return (
            CandidateStore.load(d / "candidates.json"),
            RevenueLedger.load(d / "revenue.json"),
            SpendLedger.load(d / "spend.json"),
        )

    def observe(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        store, revenue, spend = self._load()
        dlog = DiscoveryLog.load(self.data_dir / "discovery_runs.json")
        report = pipeline_report(store, revenue, spend, dlog, now=now)
        last = dlog.latest()
        age = _age_days(last["ts"], now) if last and last.get("ts") else None
        return {"report": report, "last_discovery_age_days": age}

    # --- actions -------------------------------------------------------

    def act(self, decision: Decision) -> dict:
        if decision.action == "discover":
            store, _, _ = self._load()
            dlog = DiscoveryLog.load(self.data_dir / "discovery_runs.json")
            before = len(store.all())
            for spec in self.goal.sources:
                src = _source_for(spec)
                if self.goal.filter:
                    src = FilteredSource(src, is_relevant)
                run_discovery_cycle(
                    src, store,
                    limit=self.goal.limit,
                    shortlist_n=self.goal.shortlist_n,
                    min_score=self.goal.min_score,
                    log=dlog,
                    calibrated=self.goal.calibrated,
                )
            new = len(store.all()) - before
            return {"new_candidates": new, "total_candidates": len(store.all())}

        if decision.action == "investigate":
            store, _, _ = self._load()
            out = investigate_approved(store)
            return {"now_investigating": len(out)}

        if decision.action == "prepare_launch":
            store, _, _ = self._load()
            out = prepare_launch(store)
            return {"offers_attached": sum(1 for c in out if c.offer)}

        return {}

    # --- loop -------------------------------------------------------

    def step(self, now: datetime | None = None) -> AgentStep:
        now = now or datetime.now(timezone.utc)
        log = AgentLog.load(self.data_dir / "agent_log.json")
        cycle = len(log)

        decision = decide(
            self.observe(now), self.goal,
            discovery_exhausted=self._discovery_exhausted,
        )
        result: dict = {}
        if decision.action != "stop":
            result = self.act(decision)
            if decision.action == "discover" and result.get("new_candidates", 0) == 0:
                self._discovery_exhausted = True

        digest = digest_line(self.observe(now)["report"]["action_queue"])
        entry = {
            "ts": now.isoformat(),
            "cycle": cycle,
            "action": decision.action,
            "reason": decision.reason,
            "detail": {**decision.detail, **result},
            "digest_after": digest,
        }
        log.add(entry)
        log.save()
        return AgentStep(decision, result, digest, entry)

    def run(self, max_cycles: int = 20, now: datetime | None = None) -> list[AgentStep]:
        self._discovery_exhausted = False
        steps: list[AgentStep] = []
        for _ in range(max(1, max_cycles)):
            step = self.step(now=now)
            steps.append(step)
            if step.decision.action == "stop":
                break
        return steps
