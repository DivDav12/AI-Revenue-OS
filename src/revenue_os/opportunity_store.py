"""The Revenue Opportunity database.

A richer superset of the discovery `Candidate`: one record per money-making
idea the fleet is considering, across every category (digital product,
micro-SaaS, affiliate, marketplace, freelancing, lead-gen, e-commerce,
POD, data product, content, B2B, arbitrage, ...).

  file : <data-dir>/opportunities.json  (list of records, atomic write)

Status lifecycle:
  discovered -> evaluating -> building -> testing -> active -> successful
  (any state) -> abandoned  (with a reason)

`score()` is deterministic: expected value per unit of effort, penalised by
risk and time-to-first-revenue, so the strategist can rank without an LLM.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import opportunity_state as ostate
from .store import now_iso

CATEGORIES = (
    "digital_product", "micro_saas", "saas", "ai_service", "automation_service",
    "affiliate", "marketplace", "freelancing", "lead_generation",
    "information_product", "template_pack", "developer_tool", "api_product",
    "website", "ecommerce", "print_on_demand", "data_product", "niche_service",
    "content_business", "b2b_service", "b2c_service", "arbitrage",
    "emerging_platform", "other",
)

STATUSES = ("discovered", "evaluating", "building", "testing", "active",
            "successful", "abandoned")

_MEASURE_SERIES_CAP = 200

_COMP = {"low": 1.0, "medium": 0.6, "high": 0.3, "unknown": 0.5}
_RISK = {"low": 1.0, "medium": 0.7, "high": 0.35, "unknown": 0.6}


def _oid(title: str, category: str) -> str:
    import hashlib
    return "opp_" + hashlib.sha1(f"{category}:{title}".lower().encode()).hexdigest()[:12]


@dataclass
class Opportunity:
    id: str = ""
    title: str = ""
    category: str = "other"
    target_customer: str = ""
    est_revenue_eur: float = 0.0        # realistic monthly, first 90 days
    est_cost_eur: float = 0.0           # cash cost to launch (excludes fleet time)
    required_work: str = ""             # one line
    effort_points: int = 3             # 1 (trivial) .. 5 (weeks of build)
    competition: str = "unknown"       # low | medium | high | unknown
    difficulty: int = 3               # 1 .. 5
    probability: float = 0.15          # 0..1 chance of ANY revenue in 90d
    time_to_first_revenue_days: int = 30
    scalability: int = 3              # 1 .. 5
    legal_platform_risk: str = "low"  # low | medium | high
    required_human_actions: list = field(default_factory=list)
    status: str = "discovered"        # legacy 7-value field (kept working)
    state: str = ostate.INITIAL       # canonical lifecycle state (opportunity_state)
    score: float = 0.0
    source: str = "engine"            # engine | llm | manual | adjacent
    parent_id: str = ""               # if spawned as an adjacent opportunity
    experiments: list = field(default_factory=list)   # [{ts, kind, note, result}]
    transitions: list = field(default_factory=list)   # append-only state history
    execution: dict = field(default_factory=dict)     # {accepted, accepted_by, chain}
    results: dict = field(default_factory=dict)       # {revenue_eur, leads, signups, ...}
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = _oid(self.title, self.category)
        if not self.created_at:
            self.created_at = now_iso()

    def to_dict(self) -> dict:
        return asdict(self)


def score_opportunity(o: Opportunity) -> float:
    """Deterministic 0-100. Reward expected value; punish effort, cost,
    competition, risk and slow time-to-revenue."""
    ev = max(0.0, float(o.est_revenue_eur)) * max(0.0, min(1.0, float(o.probability)))
    effort = max(1, int(o.effort_points)) * max(1, int(o.difficulty))
    comp = _COMP.get(o.competition, 0.5)
    risk = _RISK.get(o.legal_platform_risk, 0.6)
    ttfr = 1.0 / (1.0 + max(0, int(o.time_to_first_revenue_days)) / 30.0)
    cost_pen = 1.0 / (1.0 + max(0.0, float(o.est_cost_eur)) / 20.0)
    scale = 0.7 + 0.06 * max(1, min(5, int(o.scalability)))
    raw = (ev / effort) * comp * risk * ttfr * cost_pen * scale
    # squash into 0..100 (raw ~0..40 for realistic micro-opportunities)
    return round(min(100.0, raw * 2.5), 2)


class OpportunityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_id: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "OpportunityStore":
        s = cls(path)
        if not s.path.exists():
            return s
        try:
            raw = json.loads(s.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return s
        for r in raw if isinstance(raw, list) else []:
            if isinstance(r, dict) and r.get("id"):
                s._migrate(r)
                s._by_id[r["id"]] = r
        return s

    @staticmethod
    def _migrate(r: dict) -> None:
        """Backfill the canonical state machine on a record written before
        it existed. Derives the state from the legacy status and seeds one
        bootstrap transition; never invents history."""
        r.setdefault("transitions", [])
        if not r.get("state"):
            st = ostate.state_for_legacy_status(r.get("status", "discovered"))
            r["state"] = st
            r["transitions"].append(ostate.Transition(
                ts=now_iso(), previous_state="", next_state=st,
                reason=f"migrated from legacy status {r.get('status', 'discovered')!r}",
                source="migration", actor="system").to_dict())

    def save(self) -> None:
        payload = json.dumps(sorted(self._by_id.values(),
                                    key=lambda r: -float(r.get("score", 0))),
                             indent=2)
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

    # --- mutation ------------------------------------------------
    def upsert(self, opp: Opportunity) -> dict:
        opp.score = score_opportunity(opp)
        opp.updated_at = now_iso()
        existing = self._by_id.get(opp.id)
        if existing is None:
            rec = opp.to_dict()
            if not rec.get("transitions"):
                rec["state"] = rec.get("state") or ostate.INITIAL
                rec["transitions"] = [ostate.Transition(
                    ts=now_iso(), previous_state="", next_state=rec["state"],
                    reason="opportunity discovered",
                    source=f"discovery:{rec.get('source', 'engine')}",
                    actor="system").to_dict()]
            self._by_id[opp.id] = rec
            return self._by_id[opp.id]
        # preserve lifecycle + history; refresh the estimates
        keep = {k: existing[k] for k in ("status", "state", "experiments",
                                         "transitions", "execution", "results",
                                         "created_at", "notes")
                if k in existing}
        merged = {**opp.to_dict(), **keep}
        self._by_id[opp.id] = merged
        return merged

    def get(self, oid: str) -> dict | None:
        return self._by_id.get(oid)

    def set_status(self, oid: str, status: str, *, note: str = "",
                   actor: str = "system") -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        r = self._by_id.get(oid)
        if r is None:
            raise ValueError(f"unknown opportunity {oid!r}")
        r["status"] = status
        r["updated_at"] = now_iso()
        if note:
            r.setdefault("experiments", []).append(
                {"ts": now_iso(), "kind": "status", "note": note, "result": status})
        # mirror into the canonical state machine (permissive legacy bridge:
        # it records history even where the legacy path skipped an
        # intermediate state, tagged so it is distinguishable from a
        # result-driven transition).
        target = ostate.state_for_legacy_status(status)
        cur = r.get("state") or ostate.INITIAL
        if target and target != cur:
            r.setdefault("transitions", []).append(ostate.Transition(
                ts=now_iso(), previous_state=cur, next_state=target,
                reason=note or f"legacy status -> {status}",
                source="legacy_status_sync", actor=actor,
                forced=not ostate.can_transition(cur, target)).to_dict())
            r["state"] = target
        return r

    def transition(self, oid: str, to: str, *, reason: str, source: str,
                   actor: str = "system", task_id: str = "", error: str = "",
                   force: bool = False) -> dict:
        """Move an opportunity to state `to`, recording a full transition.

        Refuses a move that is not in the legal table unless `force=True`
        (an explicit human override), which is still recorded, flagged
        `forced: true`. This is the entry point real task results use -
        e.g. DEPLOYING -> LIVE only after a deploy adapter returns a URL.
        """
        r = self._by_id.get(oid)
        if r is None:
            raise ValueError(f"unknown opportunity {oid!r}")
        if to not in ostate.STATES:
            raise ValueError(f"unknown state {to!r}")
        frm = r.get("state") or ostate.state_for_legacy_status(
            r.get("status", "discovered"))
        forced = False
        if not ostate.can_transition(frm, to):
            if not force:
                raise ostate.IllegalTransition(
                    f"{oid}: {frm} -> {to} is not a legal transition")
            forced = True
        rec = ostate.Transition(
            ts=now_iso(), previous_state=frm, next_state=to, reason=reason,
            source=source, actor=actor, task_id=task_id, error=error,
            forced=forced).to_dict()
        r["state"] = to
        r.setdefault("transitions", []).append(rec)
        r["updated_at"] = now_iso()
        return rec

    def mark_accepted(self, oid: str, *, by: str, task_ids: list) -> dict:
        """Breadcrumb: this opportunity was accepted for execution and owns
        the given ExecutionTask chain. The autonomy loop skips accepted
        opportunities so the two systems do not fight over the record."""
        r = self._by_id[oid]
        r["execution"] = {"accepted": True, "accepted_by": str(by),
                          "accepted_at": now_iso(), "chain": list(task_ids)}
        r["updated_at"] = now_iso()
        return r

    def is_accepted(self, oid: str) -> bool:
        return bool((self._by_id.get(oid) or {}).get("execution", {}).get("accepted"))

    def record_measurement(self, oid: str, kind: str, metrics: dict, *,
                           cycle: int = 0) -> dict:
        """Append one measurement to the opportunity's persistent time series
        (`execution.measurement_series`, capped) and refresh the rolling
        `execution.metrics[<kind>]` snapshot."""
        r = self._by_id[oid]
        ex = r.setdefault("execution", {})
        series = ex.setdefault("measurement_series", [])
        clean = {k: v for k, v in (metrics or {}).items()
                 if isinstance(v, (int, float, str, bool)) or v is None}
        series.append({"ts": now_iso(), "kind": str(kind), "cycle": int(cycle),
                       "metrics": clean})
        del series[:-_MEASURE_SERIES_CAP]
        ex.setdefault("metrics", {})[str(kind)] = clean
        r["updated_at"] = now_iso()
        return r

    def record_delivery(self, oid: str, payment_ref: str, delivery: dict) -> dict:
        """Persist a CONFIRMED delivery keyed by the payment reference under
        `execution.deliveries`. A later DELIVER re-run for the same payment
        reads this and no-ops instead of sending again (survives restart)."""
        r = self._by_id[oid]
        r.setdefault("execution", {}).setdefault("deliveries", {})[
            str(payment_ref)] = dict(delivery)
        r["updated_at"] = now_iso()
        return r

    def record_deployment(self, oid: str, deployment: dict) -> dict:
        """Persist the confirmed deployment (provider, live_url, deployment_id,
        commit_sha) under `execution`. A later DEPLOY re-run reads this and
        no-ops instead of re-publishing."""
        r = self._by_id[oid]
        r.setdefault("execution", {})["deployment"] = dict(deployment)
        if deployment.get("live_url"):
            r["execution"]["live_url"] = deployment["live_url"]
        r["updated_at"] = now_iso()
        return r

    def add_experiment(self, oid: str, kind: str, note: str,
                       result: str = "") -> dict:
        r = self._by_id[oid]
        r.setdefault("experiments", []).append(
            {"ts": now_iso(), "kind": kind, "note": note, "result": result})
        r["updated_at"] = now_iso()
        return r

    def prune_abandoned(self, *, trigger: int = 150, keep: int = 10) -> int:
        """Batch sweep: do nothing until the abandoned pile reaches
        `trigger`, then delete the oldest ones down to `keep` (e.g. hits
        150 -> wipe 140, keep the 10 newest). Never touches an active
        lifecycle state."""
        ab = sorted((r for r in self._by_id.values()
                     if r.get("status") == "abandoned"),
                    key=lambda r: str(r.get("updated_at", "")))
        if len(ab) < trigger:
            return 0
        drop = ab[:-keep] if keep > 0 else ab
        for r in drop:
            self._by_id.pop(r["id"], None)
        return len(drop)

    def record_result(self, oid: str, **metrics) -> dict:
        r = self._by_id[oid]
        r.setdefault("results", {}).update(
            {k: v for k, v in metrics.items()
             if isinstance(v, (int, float, str, bool))})
        r["updated_at"] = now_iso()
        return r

    # --- views -------------------------------------------------
    def all(self) -> list[dict]:
        return list(self._by_id.values())

    def by_status(self, *statuses: str) -> list[dict]:
        return sorted((r for r in self._by_id.values()
                       if r.get("status") in statuses),
                      key=lambda r: -float(r.get("score", 0)))

    def by_state(self, *states: str) -> list[dict]:
        """Records in any of the given canonical lifecycle states."""
        return sorted((r for r in self._by_id.values()
                       if (r.get("state") or ostate.INITIAL) in states),
                      key=lambda r: -float(r.get("score", 0)))

    def state_counts(self) -> dict:
        c: dict = {}
        for r in self._by_id.values():
            s = r.get("state") or ostate.INITIAL
            c[s] = c.get(s, 0) + 1
        return c

    def board(self) -> dict:
        b = {s: [] for s in STATUSES}
        for r in self._by_id.values():
            b.setdefault(r.get("status", "discovered"), []).append(r)
        return b

    def counts(self) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in self._by_id.values():
            c[r.get("status", "discovered")] = c.get(r.get("status", "discovered"), 0) + 1
        return c

    def __len__(self) -> int:
        return len(self._by_id)


def load_opportunities(data_dir: str | Path) -> OpportunityStore:
    return OpportunityStore.load(Path(data_dir) / "opportunities.json")
