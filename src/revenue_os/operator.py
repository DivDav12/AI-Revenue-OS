"""The operator agent.

A single autonomous coordinator: it holds a Goal, observes the pipeline
state, decides the next non-human action, executes it via the existing
workflow functions, and repeats until only human-gated actions remain
(or a cycle cap is hit). Every decision is logged.

run() does one bounded pass; run_continuous() ticks forever - sleep,
re-observe, tick again - bounded by ticks / wall-clock / cumulative
cycles / spend, and resumable across restarts via agent_session.json.

Deterministic policy, deterministic leaf workers, no LLM, no money. It
has no code path to approve a candidate, launch an offer, set a budget,
or record a payment - it stops at those gates and hands off via the
digest.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .agent_log import AgentLog
from .discovery_log import DiscoveryLog
from .filtering import is_relevant
from .llm_spend import LlmSpendLog
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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_goal(data_dir: str | Path, goal: Goal) -> None:
    _atomic_write(Path(data_dir) / "goal.json", json.dumps(goal.to_dict(), indent=2))


@dataclass
class Session:
    """One continuous-run session. Persisted to agent_session.json so a
    restart resumes an unfinished session (counters continue)."""

    started_at: str = ""
    last_tick_at: str = ""
    ticks: int = 0
    cycles: int = 0
    spend_baseline_usd: float = 0.0
    ended_at: str | None = None
    end_reason: str | None = None


def _session_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "agent_session.json"


def load_session(data_dir: str | Path) -> tuple[Session, bool]:
    """Return (session, resumed). Resume an unfinished session; otherwise
    a fresh one."""
    path = _session_path(data_dir)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        session = Session(**raw)
        if session.ended_at is None:
            return session, True
    return Session(), False


def save_session(data_dir: str | Path, session: Session) -> None:
    _atomic_write(_session_path(data_dir), json.dumps(asdict(session), indent=2))


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

    def step(self, now: datetime | None = None, *, log_noop_stop: bool = True) -> AgentStep:
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
        last = log.latest()
        redundant_stop = (
            decision.action == "stop" and not result
            and last is not None
            and last.get("action") == "stop"
            and last.get("digest_after") == digest
        )
        if not (redundant_stop and not log_noop_stop):
            log.add(entry)
            log.save()
        return AgentStep(decision, result, digest, entry)

    def run(
        self, max_cycles: int = 20, now: datetime | None = None,
        *, log_noop_stop: bool = True,
    ) -> list[AgentStep]:
        self._discovery_exhausted = False
        steps: list[AgentStep] = []
        for _ in range(max(1, max_cycles)):
            step = self.step(now=now, log_noop_stop=log_noop_stop)
            steps.append(step)
            if step.decision.action == "stop":
                break
        return steps

    # --- continuous operation -----------------------------------------

    def _marker(self, action: str, reason: str, ts: str) -> None:
        log = AgentLog.load(self.data_dir / "agent_log.json")
        log.add({
            "ts": ts, "cycle": len(log), "action": action,
            "reason": reason, "detail": {}, "digest_after": "",
        })
        log.save()

    def _spent_since(self, baseline: float) -> float:
        total = LlmSpendLog.load(
            self.data_dir / "llm_spend.json"
        ).summary()["total_cost_usd"]
        return round(total - baseline, 4)

    def run_continuous(
        self,
        interval: float,
        *,
        max_ticks: int | None = None,
        max_runtime_s: float | None = None,
        max_total_cycles: int | None = None,
        max_spend_usd: float | None = None,
        fresh: bool = False,
        max_cycles: int = 20,
        on_tick=None,
        sleep_fn=time.sleep,
        clock_fn=time.monotonic,
        now_fn=None,
    ) -> Session:
        """Tick the agent to a fixed point, sleep, repeat - bounded and
        resumable. Never passes a human gate; stops cleanly on Ctrl-C."""
        now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        session, resumed = load_session(self.data_dir)
        if fresh or not resumed:
            session = Session(
                started_at=now_fn().isoformat(),
                spend_baseline_usd=LlmSpendLog.load(
                    self.data_dir / "llm_spend.json"
                ).summary()["total_cost_usd"],
            )
            save_session(self.data_dir, session)
            self._marker("session_start", f"interval={interval}s", now_fn().isoformat())

        start_clock = clock_fn()

        def _bound_hit() -> str | None:
            if max_ticks is not None and session.ticks >= max_ticks:
                return "max-ticks"
            if max_runtime_s is not None and clock_fn() - start_clock >= max_runtime_s:
                return "max-runtime"
            if max_total_cycles is not None and session.cycles >= max_total_cycles:
                return "max-total-cycles"
            if (
                max_spend_usd is not None
                and self._spent_since(session.spend_baseline_usd) >= max_spend_usd
            ):
                return "max-spend"
            return None

        reason: str | None = None
        try:
            while True:
                reason = _bound_hit()
                if reason:
                    break
                steps = self.run(max_cycles=max_cycles, log_noop_stop=False)
                session.ticks += 1
                session.cycles += len(steps)
                session.last_tick_at = now_fn().isoformat()
                save_session(self.data_dir, session)
                if on_tick is not None:
                    on_tick(steps)
                reason = _bound_hit()
                if reason:
                    break
                sleep_fn(interval)
        except (KeyboardInterrupt, SystemExit):
            reason = "interrupted"

        session.ended_at = now_fn().isoformat()
        session.end_reason = reason or "stopped"
        save_session(self.data_dir, session)
        self._marker("session_end", session.end_reason, session.ended_at)
        return session
