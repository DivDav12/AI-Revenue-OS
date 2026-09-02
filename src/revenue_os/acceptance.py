"""Opportunity acceptance - the business decision that starts execution.

This is deliberately SEPARATE from the human-approval firewall
(`approvals.py`). Accepting an opportunity is "yes, pursue this idea"; it
moves the opportunity to SELECTED and enqueues a real ExecutionTask chain.
It never moves money, never performs a protected action - the money /
identity / legal gates still sit inside the chain (e.g. the DEPLOY task is
born `BLOCKED_APPROVAL` on a money approval).

  accept_opportunity(data_dir, opportunity_id)  -> SELECTED + task chain
  abandon_opportunity(data_dir, opportunity_id) -> ABANDONED + cancel tasks
  execution_view(data_dir[, opportunity_id])    -> read model for the UI

The chain (dependencies enforced by the TaskQueue):

  PLAN
  ├─ BUILD_PRODUCT ─ VALIDATE_PRODUCT
  └─ BUILD_PAGE ──── VALIDATE_PAGE
                         └─ DEPLOY (needs money approval)
                              ├─ DISTRIBUTE
                              ├─ CHECK_TRAFFIC
                              ├─ CHECK_LEADS
                              └─ CHECK_REVENUE

DISTRIBUTE / CHECK_* have no adapter yet (Phases 9 / 10) and DEPLOY has no
adapter yet (Phase 7) - they sit PENDING / BLOCKED until those phases land.
Nothing here fakes their completion.
"""

from __future__ import annotations

from pathlib import Path

from . import opportunity_state as ostate
from .events import load_events
from .execution import TASK_STATUSES, load_tasks
from .opportunity_store import load_opportunities

# (task_type, dependency task_types, approval_type or "")
CHAIN: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("PLAN",             (),                              ""),
    ("BUILD_PRODUCT",    ("PLAN",),                       ""),
    ("BUILD_PAGE",       ("PLAN",),                       ""),
    ("VALIDATE_PRODUCT", ("BUILD_PRODUCT",),              ""),
    ("VALIDATE_PAGE",    ("BUILD_PAGE",),                 ""),
    ("DEPLOY",           ("BUILD_PAGE", "VALIDATE_PAGE"), "money"),
    ("DISTRIBUTE",       ("DEPLOY",),                     ""),
    # the measurement checks depend on DISTRIBUTE (not DEPLOY) so DISTRIBUTE
    # is guaranteed to run first: DEPLOY -> LIVE -> DISTRIBUTE ->
    # ACQUIRING_TRAFFIC -> CHECK_*. Ordering is a real dependency, not a
    # priority hint. With no owned channel configured DISTRIBUTE is a no-op
    # (SUCCEEDED, nothing published) so the checks still proceed.
    ("CHECK_TRAFFIC",    ("DISTRIBUTE",),                 ""),
    ("CHECK_LEADS",      ("DISTRIBUTE",),                 ""),
    ("CHECK_REVENUE",    ("DISTRIBUTE",),                 ""),
)

_PRE_SELECT = ("DISCOVERED", "RESEARCHING", "SCORED")


class AcceptanceError(ValueError):
    """Accept / abandon could not be performed."""


# ---------------------------------------------------------------------------

def _ensure_selected(store, oid: str, actor: str) -> list[tuple[str, str]]:
    """Move the opportunity onto SELECTED by legal steps only. No-op if it
    is already at or beyond SELECTED. Returns the (from, to) moves made."""
    cur = store.get(oid).get("state") or ostate.INITIAL
    if cur == "SELECTED":
        return []
    # already in / through execution, or terminal - leave the state alone
    if cur not in _PRE_SELECT and cur not in ("BLOCKED", "FAILED"):
        return []
    want = []
    if cur in ("DISCOVERED", "RESEARCHING"):
        want.append("SCORED")
    want.append("SELECTED")
    moves: list[tuple[str, str]] = []
    for nxt in want:
        c = store.get(oid).get("state") or ostate.INITIAL
        if c == nxt or not ostate.can_transition(c, nxt):
            continue
        tr = store.transition(oid, nxt, reason="accepted for execution",
                              source="human", actor=actor)
        moves.append((tr["previous_state"], tr["next_state"]))
    return moves


def accept_opportunity(data_dir, opportunity_id: str, *, actor: str = "human",
                       priority: int = 5) -> dict:
    """Accept an opportunity: SELECTED + a persistent ExecutionTask chain.
    Idempotent - a second call re-uses the existing tasks."""
    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    rec = store.get(opportunity_id)
    if rec is None:
        raise AcceptanceError(f"unknown opportunity {opportunity_id!r}")
    if (rec.get("state") or "") == "ABANDONED":
        raise AcceptanceError(f"{opportunity_id} is ABANDONED - cannot accept")

    moves = _ensure_selected(store, opportunity_id, actor)

    q = load_tasks(data_dir)
    ev = load_events(data_dir)

    ids: dict[str, str] = {}
    created: list[str] = []
    reused: list[str] = []
    existing_ids = {t.task_id for t in q.all()}
    for ttype, deps, approval in CHAIN:
        dep_ids = [ids[d] for d in deps if d in ids]
        t = q.create(
            opportunity_id, ttype, depends_on=dep_ids, priority=priority,
            idempotency_key=f"accept:{opportunity_id}:{ttype}",
            requires_approval=bool(approval), approval_type=approval,
            input=({"channel": "owned_web"} if ttype == "DISTRIBUTE" else {})
            | {"title": rec.get("title", ""), "accepted_by": actor})
        ids[ttype] = t.task_id
        if t.task_id in existing_ids:
            reused.append(ttype)
        else:
            created.append(ttype)
            ev.emit("TASK_CREATED", task_id=t.task_id,
                    opportunity_id=opportunity_id, task_type=ttype, actor=actor,
                    depends_on=list(dep_ids), priority=priority)
            if ttype == "DISTRIBUTE":
                ev.emit("DISTRIBUTION_CREATED", task_id=t.task_id,
                        opportunity_id=opportunity_id, task_type="DISTRIBUTE",
                        actor=actor, channel="owned_web")

    res = q.resolve_dependencies()
    q.save()
    for a, b in moves:
        ev.emit("OPPORTUNITY_TRANSITIONED", opportunity_id=opportunity_id,
                actor=actor, **{"from": a, "to": b,
                                "reason": "accepted for execution"})
    for tid in res.get("blocked", []):
        tt = q.get(tid)
        ev.emit("TASK_BLOCKED", task_id=tid, opportunity_id=opportunity_id,
                task_type=tt.task_type, actor=actor,
                approval_type=tt.approval_type)
    for tid in res.get("promoted", []):
        tt = q.get(tid)
        ev.emit("TASK_READY", task_id=tid, opportunity_id=opportunity_id,
                task_type=tt.task_type, actor=actor)
    ev.save()

    store.mark_accepted(opportunity_id, by=actor, task_ids=list(ids.values()))
    store.save()

    return {
        "opportunity_id": opportunity_id,
        "state": store.get(opportunity_id).get("state"),
        "moves": [{"from": a, "to": b} for a, b in moves],
        "created": created,
        "reused": reused,
        "chain": [{"task_type": tt, "task_id": ids[tt],
                   "status": q.get(ids[tt]).status} for tt, _, _ in CHAIN],
    }


def abandon_opportunity(data_dir, opportunity_id: str, *, actor: str = "human",
                        reason: str = "") -> dict:
    """Abandon an opportunity: cancel its non-terminal tasks, move it to
    ABANDONED."""
    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    rec = store.get(opportunity_id)
    if rec is None:
        raise AcceptanceError(f"unknown opportunity {opportunity_id!r}")

    q = load_tasks(data_dir)
    ev = load_events(data_dir)
    cancelled: list[str] = []
    for t in q.by_opportunity(opportunity_id):
        if not t.is_terminal:
            q.cancel(t.task_id, reason=reason or "opportunity abandoned")
            ev.emit("TASK_CANCELLED", task_id=t.task_id,
                    opportunity_id=opportunity_id, task_type=t.task_type,
                    actor=actor, reason=reason)
            cancelled.append(t.task_id)
    q.save()

    cur = rec.get("state") or ostate.INITIAL
    moved = False
    if cur != "ABANDONED" and ostate.can_transition(cur, "ABANDONED"):
        tr = store.transition(opportunity_id, "ABANDONED",
                              reason=reason or "abandoned by human",
                              source="human", actor=actor)
        store.save()
        ev.emit("OPPORTUNITY_TRANSITIONED", opportunity_id=opportunity_id,
                actor=actor, **{"from": tr["previous_state"], "to": "ABANDONED",
                                "reason": tr["reason"]})
        moved = True
    ev.save()

    return {"opportunity_id": opportunity_id, "cancelled": cancelled,
            "state": store.get(opportunity_id).get("state"), "moved": moved}


def release_task(data_dir, task_id: str, *, actor: str = "human") -> dict:
    """Human releases a BLOCKED_APPROVAL task (e.g. DEPLOY). This is the
    satisfaction of the approval gate - it is an explicit, logged human
    action; it never runs the task, only makes it eligible. The worker
    still checks dependencies before it becomes READY."""
    data_dir = Path(data_dir)
    q = load_tasks(data_dir)
    t = q.get(task_id)
    if t is None:
        raise AcceptanceError(f"unknown task {task_id!r}")
    if t.status != "BLOCKED_APPROVAL":
        raise AcceptanceError(f"task {task_id} is {t.status}, not BLOCKED_APPROVAL")
    q.unblock(task_id)
    res = q.resolve_dependencies()
    q.save()
    ev = load_events(data_dir)
    ev.emit("TASK_UNBLOCKED", task_id=task_id, opportunity_id=t.opportunity_id,
            task_type=t.task_type, actor=actor,
            approval_type=t.approval_type)
    for tid in res.get("promoted", []):
        tt = q.get(tid)
        ev.emit("TASK_READY", task_id=tid, opportunity_id=tt.opportunity_id,
                task_type=tt.task_type, actor=actor)
    ev.save()
    return {"task_id": task_id, "task_type": t.task_type,
            "status": q.get(task_id).status}


def execution_view(data_dir, opportunity_id: str | None = None) -> list[dict]:
    """Read model: one row per accepted / task-bearing opportunity."""
    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    q = load_tasks(data_dir)

    recs = ([store.get(opportunity_id)] if opportunity_id is not None
            else store.all())
    rows: list[dict] = []
    for rec in recs:
        if rec is None:
            continue
        oid = rec["id"]
        tasks = sorted(q.by_opportunity(oid), key=lambda t: t.created_at)
        accepted = bool((rec.get("execution") or {}).get("accepted"))
        if not tasks and not accepted:
            continue

        running = next((t for t in tasks if t.status == "RUNNING"), None)
        ready = next((t for t in tasks if t.status == "READY"), None)
        nxt = next((t for t in tasks
                    if t.status in ("PENDING", "BLOCKED_APPROVAL")), None)
        blocked = next((t for t in tasks if t.status == "BLOCKED_APPROVAL"), None)
        cur = running or ready
        deployment = (rec.get("execution") or {}).get("deployment") or {}

        rows.append({
            "opportunity_id": oid,
            "title": rec.get("title", ""),
            "state": rec.get("state") or ostate.INITIAL,
            "accepted": accepted,
            "accepted_by": (rec.get("execution") or {}).get("accepted_by", ""),
            "current_task": cur.task_type if cur else "",
            "next_task": nxt.task_type if nxt else "",
            "blocked_task_id": blocked.task_id if blocked else "",
            "blocker": (f"{blocked.task_type} needs a {blocked.approval_type} "
                        "approval" if blocked else ""),
            "live_url": (rec.get("execution") or {}).get("live_url", ""),
            "deployment_provider": deployment.get("provider", ""),
            "counts": {s: n for s in TASK_STATUSES
                       if (n := sum(1 for t in tasks if t.status == s))},
            "tasks": [{"task_id": t.task_id, "task_type": t.task_type,
                       "status": t.status, "attempt": t.attempt_count,
                       "error": t.error} for t in tasks],
        })
    return rows
