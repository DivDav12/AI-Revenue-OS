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
from .decide_llm import summarize_for_decision
from .llm_workers import (
    build_decider,
    build_evaluator,
    build_planner,
    build_proposer,
    build_researcher,
    record_llm_spend,
)
from .normalize import to_opportunity
from .offer import propose_offer
from .report import STALE_AFTER_DAYS, _age_days, digest_line, pipeline_report
from .revenue import RevenueLedger
from .sources import FilteredSource, build_source
from .spend import SpendLedger
from .store import CandidateStore
from .validation import plan_validation
from .workflow import (
    investigate_approved,
    prepare_launch,
    research_shortlisted,
    run_discovery_cycle,
)

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
    # opt-in LLM leaf workers (deterministic by default)
    evaluator: str = "keyword"          # keyword | llm
    planner: str = "template"           # template | llm
    proposer: str = "template"          # template | llm
    research: str = "off"               # off | llm
    decision_policy: str = "rules"      # rules | llm
    model: str = "claude-sonnet-5"
    max_llm_cost_per_action: float = 0.5
    max_decision_cost: float = 0.05

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
            "evaluator": self.evaluator,
            "planner": self.planner,
            "proposer": self.proposer,
            "research": self.research,
            "decision_policy": self.decision_policy,
            "model": self.model,
            "max_llm_cost_per_action": self.max_llm_cost_per_action,
            "max_decision_cost": self.max_decision_cost,
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
            evaluator=d.get("evaluator", "keyword"),
            planner=d.get("planner", "template"),
            proposer=d.get("proposer", "template"),
            research=d.get("research", "off"),
            decision_policy=d.get("decision_policy", "rules"),
            model=d.get("model", "claude-sonnet-5"),
            max_llm_cost_per_action=float(d.get("max_llm_cost_per_action", 0.5)),
            max_decision_cost=float(d.get("max_decision_cost", 0.05)),
        )

    @property
    def uses_llm(self) -> bool:
        return "llm" in (
            self.evaluator, self.planner, self.proposer,
            self.research, self.decision_policy,
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


def session_dict(data_dir: str | Path) -> dict | None:
    """The raw agent_session.json, or None if there is no session."""
    path = _session_path(data_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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


def _unresearched_shortlisted(report: dict) -> int:
    return sum(
        1 for c in report.get("candidates", [])
        if c["status"] == "shortlisted" and not c.get("research")
    )


def decide(
    obs: dict, goal: Goal, *,
    discovery_exhausted: bool = False, llm_capped: bool = False,
    research_done: bool = False, policy=None,
) -> Decision:
    """Pure: pick the next non-human action from the observed state."""
    report = obs["report"]
    counts = report["status_counts"]
    queue = report["action_queue"]
    age = obs["last_discovery_age_days"]

    if llm_capped:
        return Decision(
            "stop",
            "llm budget exhausted - raise it with `llm-budget` or set the "
            "goal's worker to keyword/template",
        )

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

    if goal.research == "llm" and not research_done:
        unresearched = _unresearched_shortlisted(report)
        if unresearched > 0:
            return Decision(
                "research",
                f"{unresearched} shortlisted candidate(s) not yet researched",
                {"unresearched": unresearched},
            )

    total = report["totals"]["candidates"]
    if not discovery_exhausted:
        if total == 0 or age is None:
            return Decision("discover", "no discovery has run yet")

        if policy is not None:
            choice = policy(summarize_for_decision(obs, goal))
            if choice is not None:
                action, rationale = choice
                return Decision(action, f"llm policy: {rationale}", {"policy": "llm"})

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
        self._llm_capped = False
        self._research_done = False

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
        g = self.goal

        if decision.action == "discover":
            store, _, _ = self._load()
            dlog = DiscoveryLog.load(self.data_dir / "discovery_runs.json")
            before = len(store.all())
            spent = 0.0
            for spec in g.sources:
                src = _source_for(spec)
                if g.filter:
                    src = FilteredSource(src, is_relevant)
                normalizer, ev_name, est, cache = to_opportunity, "keyword", 0.0, None
                if g.evaluator == "llm":
                    try:
                        normalizer, ev_name, est, cache = build_evaluator(
                            mode="llm", source=src, limit=g.limit, model=g.model,
                            max_cost_usd=g.max_llm_cost_per_action, refresh=False,
                            data_dir=self.data_dir,
                        )
                    except ValueError as exc:
                        self._llm_capped = True
                        return {"skipped": str(exc),
                                "new_candidates": len(store.all()) - before}
                run_discovery_cycle(
                    src, store, limit=g.limit, shortlist_n=g.shortlist_n,
                    min_score=g.min_score, log=dlog, normalizer=normalizer,
                    evaluator=ev_name, est_cost_usd=est, calibrated=g.calibrated,
                )
                if cache is not None:
                    cache.save()
                if ev_name == "llm":
                    record_llm_spend(self.data_dir, "evaluate", normalizer)
                    spent = round(spent + normalizer.meter.cost_usd, 4)
            out = {
                "new_candidates": len(store.all()) - before,
                "total_candidates": len(store.all()),
            }
            if spent:
                out["llm_cost"] = spent
            return out

        if decision.action == "research":
            store, _, _ = self._load()
            try:
                worker, cache = build_researcher(
                    mode="llm", store=store, model=g.model,
                    max_cost_usd=g.max_llm_cost_per_action, refresh=False,
                    data_dir=self.data_dir,
                )
            except ValueError as exc:
                self._llm_capped = True
                return {"skipped": str(exc)}
            noted = research_shortlisted(store, worker)
            if cache is not None:
                cache.save()
            record_llm_spend(self.data_dir, "research", worker)
            if not noted:
                self._research_done = True
            return {"researched": len(noted), "llm_cost": round(worker.meter.cost_usd, 4)}

        if decision.action == "investigate":
            store, _, _ = self._load()
            planner, cache = plan_validation, None
            if g.planner == "llm":
                try:
                    planner, cache = build_planner(
                        mode="llm", store=store, model=g.model,
                        max_cost_usd=g.max_llm_cost_per_action, refresh=False,
                        data_dir=self.data_dir,
                    )
                except ValueError as exc:
                    self._llm_capped = True
                    return {"skipped": str(exc)}
            out = investigate_approved(store, planner=planner)
            res = {"now_investigating": len(out)}
            if cache is not None:
                cache.save()
            if g.planner == "llm":
                record_llm_spend(self.data_dir, "plan", planner)
                res["llm_cost"] = round(planner.meter.cost_usd, 4)
            return res

        if decision.action == "prepare_launch":
            store, _, _ = self._load()
            proposer, cache = propose_offer, None
            if g.proposer == "llm":
                try:
                    proposer, cache = build_proposer(
                        mode="llm", store=store, model=g.model,
                        max_cost_usd=g.max_llm_cost_per_action, refresh=False,
                        data_dir=self.data_dir,
                    )
                except ValueError as exc:
                    self._llm_capped = True
                    return {"skipped": str(exc)}
            out = prepare_launch(store, proposer=proposer)
            res = {"offers_attached": sum(1 for c in out if c.offer)}
            if cache is not None:
                cache.save()
            if g.proposer == "llm":
                record_llm_spend(self.data_dir, "offer", proposer)
                res["llm_cost"] = round(proposer.meter.cost_usd, 4)
            return res

        return {}

    # --- loop -------------------------------------------------------

    def step(self, now: datetime | None = None, *, log_noop_stop: bool = True) -> AgentStep:
        now = now or datetime.now(timezone.utc)
        log = AgentLog.load(self.data_dir / "agent_log.json")
        cycle = len(log)

        policy = None
        if self.goal.decision_policy == "llm" and not self._llm_capped:
            try:
                policy = build_decider(
                    mode="llm", model=self.goal.model,
                    max_cost_usd=self.goal.max_decision_cost, data_dir=self.data_dir,
                )
            except ValueError:
                # decision policy is starved of budget -> fall back to the
                # deterministic rules; leaf workers still gate on _llm_capped
                policy = None

        decision = decide(
            self.observe(now), self.goal,
            discovery_exhausted=self._discovery_exhausted,
            llm_capped=self._llm_capped,
            research_done=self._research_done,
            policy=policy,
        )
        result: dict = {}
        if decision.action != "stop":
            result = self.act(decision)
            if decision.action == "discover" and result.get("new_candidates", 0) == 0:
                self._discovery_exhausted = True

        if policy is not None and getattr(policy, "calls", 0) > 0:
            record_llm_spend(self.data_dir, "decide", policy)
            result = {**result, "decide_cost": round(policy.meter.cost_usd, 4)}

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
        self._llm_capped = False
        self._research_done = False
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
