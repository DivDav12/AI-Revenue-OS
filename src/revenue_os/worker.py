"""The synchronous Worker Executor.

One worker, one task at a time. Each `tick()`:

  1. reload the TaskQueue + EventLog from disk (restart-safe)
  2. housekeeping: reclaim stale RUNNING leases, requeue due retries,
     resolve dependencies (PENDING -> READY / FAILED_FINAL / BLOCKED)
  3. take the single highest-priority READY task and claim it
     (RUNNING + started_at + worker + lease + attempt_count++)
  4. move the opportunity into its "-ing" state if that is a legal move
  5. dispatch to the registered adapter for the task_type
  6. record the real result:
       ok    -> mark_succeeded(output, actual_cost); legal forward
                opportunity transition; TASK_SUCCEEDED (+ OPPORTUNITY_TRANSITIONED)
       fail  -> mark_failed(error, retryable); the queue's own backoff /
                final-failure machinery; TASK_RETRY_SCHEDULED or TASK_FAILED.
                NEVER an opportunity transition.
  7. resolve dependencies again (a success may unlock dependents)
  8. save queue + events

`run(max_ticks=N)` loops `tick()` until the queue has nothing READY or the
bound is hit. The bound + "the worker never reads the event log" are the
two guarantees against a runaway loop.

A task becomes SUCCEEDED only when the adapter returns `AdapterResult(ok=
True)` AND (for agent-backed work) the agent's own result was `ok` and not
a human-gated / quality-blocked output. An adapter exception is caught and
recorded as a retryable failure; queue state is never left inconsistent
because every mutation goes through a TaskQueue method that enforces the
status machine.

No money, no PayPal, no paid API, no identity/legal action, no external
posting - adapters that would need any of those are simply not registered
yet (later phases); their task_type fails cleanly as "no adapter".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import opportunity_state as ostate
from .action_class import ActionBlocked
from .events import EventLog, load_events
from .execution import ExecutionTask, TaskQueue, load_tasks
from .opportunity_store import load_opportunities

# ---------------------------------------------------------------------------
# adapter interface
# ---------------------------------------------------------------------------

@dataclass
class AdapterResult:
    ok: bool
    output: dict = field(default_factory=dict)
    actual_cost: float = 0.0
    error: str = ""
    retryable: bool = True


@dataclass
class AdapterContext:
    data_dir: Path
    task: ExecutionTask
    opportunity: dict                 # the opportunity record (never None)
    dep_outputs: dict                 # {task_type: output_dict} of SUCCEEDED deps


class TaskAdapter:
    """Base class. An adapter turns one task into a real result. It must
    not move money, call a paid API, or take an identity/legal action."""

    task_types: tuple[str, ...] = ()
    name: str = "adapter"

    def run(self, ctx: AdapterContext) -> AdapterResult:   # pragma: no cover
        raise NotImplementedError


class AdapterRegistry:
    def __init__(self) -> None:
        self._by_type: dict[str, TaskAdapter] = {}

    def register(self, adapter: TaskAdapter) -> None:
        for t in adapter.task_types:
            self._by_type[t] = adapter

    def get(self, task_type: str) -> TaskAdapter | None:
        return self._by_type.get(task_type)

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_type))


# ---------------------------------------------------------------------------
# task_type -> opportunity state, on task START and on task SUCCESS. Every
# entry is only applied when `opportunity_state.can_transition` allows it -
# the worker never forces. A missing key = no transition for that step.
# ---------------------------------------------------------------------------

_START_STATE: dict[str, str] = {
    "RESEARCH": "RESEARCHING",
    "PLAN": "PLANNING",
    "BUILD_PRODUCT": "BUILDING",
    "BUILD_PAGE": "BUILDING",
    "CREATE_CONTENT": "BUILDING",
    "VALIDATE_PRODUCT": "VALIDATING",
    "VALIDATE_PAGE": "VALIDATING",
    "DEPLOY": "DEPLOYING",
    # DELIVER intentionally has NO start transition: DELIVERING is reached
    # only from a confirmed successful delivery (see _on_success).
    "OPTIMIZE": "OPTIMIZING",
}

_SUCCESS_STATE: dict[str, str] = {
    "RESEARCH": "SCORED",
    "SCORE": "SCORED",
    "VALIDATE_PRODUCT": "READY_TO_DEPLOY",
    "VALIDATE_PAGE": "READY_TO_DEPLOY",
    "DEPLOY": "LIVE",
    # DELIVER drives FIRST_SALE -> DELIVERING -> ACTIVE in _on_success, only
    # on a confirmed successful delivery - not through this generic path.
}


# ---------------------------------------------------------------------------
# helpers callers (Phase 3 etc.) use to feed the queue + keep the log honest
# ---------------------------------------------------------------------------

def enqueue(data_dir, opportunity_id: str, task_type: str, **kw) -> ExecutionTask:
    """Add a task and emit TASK_CREATED. Returns the task (or the existing
    one if an idempotency_key matched)."""
    data_dir = Path(data_dir)
    q = load_tasks(data_dir)
    before = {t.task_id for t in q.all()}
    t = q.create(opportunity_id, task_type, **kw)
    q.save()
    if t.task_id not in before:
        ev = load_events(data_dir)
        ev.emit("TASK_CREATED", task_id=t.task_id, opportunity_id=t.opportunity_id,
                task_type=t.task_type, actor="orchestrator",
                depends_on=list(t.depends_on), priority=t.priority)
        ev.save()
    return t


def cancel_task(data_dir, task_id: str, *, reason: str = "") -> None:
    data_dir = Path(data_dir)
    q = load_tasks(data_dir)
    t = q.cancel(task_id, reason=reason)
    q.save()
    ev = load_events(data_dir)
    ev.emit("TASK_CANCELLED", task_id=t.task_id, opportunity_id=t.opportunity_id,
            task_type=t.task_type, actor="orchestrator", reason=reason)
    ev.save()


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, data_dir, *, registry: AdapterRegistry | None = None,
                 name: str = "worker-1", lease_seconds: int = 900) -> None:
        self.data_dir = Path(data_dir)
        self._registry = registry
        self.name = name
        self.lease_seconds = lease_seconds

    @property
    def registry(self) -> AdapterRegistry:
        if self._registry is None:
            from .task_adapters import default_registry
            self._registry = default_registry()
        return self._registry

    # --- one unit of work -------------------------------------------
    def tick(self, *, now: str | None = None) -> dict | None:
        q = load_tasks(self.data_dir)
        ev = load_events(self.data_dir)

        self._backfill_lifecycle_events(q, ev)

        for tid in q.reclaim_stale(now=now):
            t = q.get(tid)
            ev.emit("TASK_CREATED", task_id=tid, opportunity_id=t.opportunity_id,
                    task_type=t.task_type, actor=self.name,
                    note="requeued: worker lease expired")
        q.requeue_due(now=now)

        res = self._resolve(q, ev)

        ready = q.ready()
        if not ready:
            q.save()
            ev.save()
            return None

        task = ready[0]
        q.claim(task.task_id, self.name, lease_seconds=self.lease_seconds, now=now)
        t = q.get(task.task_id)
        q.save()                       # a crash now leaves a reclaimable RUNNING task
        ev.emit("TASK_STARTED", task_id=t.task_id, opportunity_id=t.opportunity_id,
                task_type=t.task_type, actor=self.name, attempt=t.attempt_count)
        ev.save()

        self._transition(t, _START_STATE.get(t.task_type), ev, when="start")

        result = self._execute(t, q)

        if result.ok:
            q.mark_succeeded(t.task_id, result.output,
                             actual_cost=result.actual_cost, now=now)
            ev.emit("TASK_SUCCEEDED", task_id=t.task_id,
                    opportunity_id=t.opportunity_id, task_type=t.task_type,
                    actor=self.name, actual_cost=result.actual_cost)
            self._on_success(t, result, ev, q)
        else:
            q.mark_failed(t.task_id, result.error, retryable=result.retryable,
                          now=now)
            nt = q.get(t.task_id)
            if nt.status == "FAILED_RETRYABLE":
                ev.emit("TASK_RETRY_SCHEDULED", task_id=t.task_id,
                        opportunity_id=t.opportunity_id, task_type=t.task_type,
                        actor=self.name, error=result.error,
                        attempt=nt.attempt_count, next_retry_at=nt.next_retry_at)
            else:
                ev.emit("TASK_FAILED", task_id=t.task_id,
                        opportunity_id=t.opportunity_id, task_type=t.task_type,
                        actor=self.name, error=result.error)
            # a failed task NEVER advances the opportunity

        self._resolve(q, ev)
        q.save()
        ev.save()
        return {"task_id": t.task_id, "task_type": t.task_type,
                "status": q.get(t.task_id).status, "ok": result.ok}

    def run(self, *, max_ticks: int = 100, now: str | None = None) -> dict:
        processed: list[dict] = []
        for _ in range(max(1, int(max_ticks))):
            r = self.tick(now=now)
            if r is None:
                break
            processed.append(r)
        return {"count": len(processed), "processed": processed,
                "bounded_at": max_ticks if len(processed) >= max_ticks else None}

    # --- internals -------------------------------------------------
    def _execute(self, task: ExecutionTask, q: TaskQueue) -> AdapterResult:
        adapter = self.registry.get(task.task_type)
        if adapter is None:
            return AdapterResult(
                ok=False, retryable=False,
                error=f"no adapter registered for task_type {task.task_type!r} "
                      f"(implemented in a later phase)")
        ctx = self._context(task, q)
        try:
            result = adapter.run(ctx)
        except ActionBlocked as exc:
            return AdapterResult(ok=False, retryable=False,
                                 error=f"firewall blocked the adapter: {exc}")
        except Exception as exc:        # noqa: BLE001 - worker must not die
            return AdapterResult(
                ok=False, retryable=True,
                error=f"adapter {type(adapter).__name__} raised "
                      f"{type(exc).__name__}: {exc}")
        if not isinstance(result, AdapterResult):
            return AdapterResult(ok=False, retryable=False,
                                 error="adapter did not return an AdapterResult")
        return result

    def _context(self, task: ExecutionTask, q: TaskQueue) -> AdapterContext:
        store = load_opportunities(self.data_dir)
        opp = store.get(task.opportunity_id) or {
            "id": task.opportunity_id,
            "title": (task.input or {}).get("title") or task.opportunity_id,
            "category": "other",
        }
        dep_outputs: dict = {}
        for dep_id in task.depends_on:
            d = q.get(dep_id)
            if d is not None and d.status == "SUCCEEDED":
                dep_outputs[d.task_type] = dict(d.output)
        return AdapterContext(self.data_dir, task, dict(opp), dep_outputs)

    def _transition(self, task: ExecutionTask, target: str | None,
                    ev: EventLog, *, when: str) -> None:
        if not target:
            return
        store = load_opportunities(self.data_dir)
        rec = store.get(task.opportunity_id)
        if rec is None:
            return
        frm = rec.get("state") or ostate.INITIAL
        if not ostate.can_transition(frm, target):
            return                     # not eligible - never forced by the worker
        try:
            tr = store.transition(
                task.opportunity_id, target,
                reason=f"{task.task_type} {when}", source="task",
                actor=self.name, task_id=task.task_id)
        except ostate.IllegalTransition:
            return
        store.save()
        ev.emit("OPPORTUNITY_TRANSITIONED", task_id=task.task_id,
                opportunity_id=task.opportunity_id, task_type=task.task_type,
                actor=self.name, **{"from": tr["previous_state"],
                                    "to": tr["next_state"],
                                    "reason": tr["reason"]})

    def _success_target(self, task: ExecutionTask, result: AdapterResult) -> str | None:
        """The opportunity state a SUCCEEDED task moves to - with the hard
        rules that DEPLOY only reaches LIVE on a confirmed live_url, and
        CHECK_REVENUE only reaches FIRST_SALE when it booked the first sale."""
        out = result.output or {}
        if task.task_type == "DEPLOY":
            from .deployment import valid_live_url
            return "LIVE" if valid_live_url(out.get("live_url", "")) else None
        if task.task_type == "CHECK_REVENUE":
            return "FIRST_SALE" if out.get("first_sale") else None
        if task.task_type == "DELIVER":
            return None                # DELIVERING/ACTIVE handled in _on_success
        return _SUCCESS_STATE.get(task.task_type)

    def _on_success(self, task: ExecutionTask, result: AdapterResult,
                    ev: EventLog, q: TaskQueue) -> None:
        store = load_opportunities(self.data_dir)
        rec = store.get(task.opportunity_id)
        if rec is None:
            return
        dirty = False

        if task.task_type == "DEPLOY":
            from .deployment import valid_live_url
            out = result.output or {}
            url = out.get("live_url", "")
            if valid_live_url(url):
                store.record_deployment(task.opportunity_id, dict(out))
                dirty = True
                ev.emit("DEPLOYMENT_COMPLETE", task_id=task.task_id,
                        opportunity_id=task.opportunity_id, task_type="DEPLOY",
                        actor=self.name, live_url=url,
                        deployment_id=out.get("deployment_id", ""),
                        provider=out.get("provider", ""),
                        commit_sha=out.get("commit_sha", ""),
                        idempotent=bool(out.get("idempotent")))

        if task.task_type == "CHECK_REVENUE":
            out = result.output or {}
            # events fire ONLY for rows newly booked this run -> a re-run over
            # an already-booked payment emits nothing (idempotent).
            for p in out.get("newly_booked", []):
                ev.emit("PAYMENT_DETECTED", task_id=task.task_id,
                        opportunity_id=task.opportunity_id,
                        task_type="CHECK_REVENUE", actor=self.name,
                        reference=p.get("reference", ""),
                        amount=p.get("amount", 0), currency=p.get("currency", ""),
                        provider=p.get("provider", ""),
                        customer_ref=p.get("customer_ref", ""))
                ev.emit("REVENUE_RECORDED", task_id=task.task_id,
                        opportunity_id=task.opportunity_id,
                        task_type="CHECK_REVENUE", actor=self.name,
                        reference=p.get("reference", ""),
                        ledger_ref=p.get("ledger_ref", ""),
                        amount=p.get("amount", 0), currency=p.get("currency", ""),
                        opportunity_total_eur=out.get("opportunity_total_eur", 0))
                # exactly ONE DELIVER task per confirmed payment (idempotency
                # key = opp + provider ref). Re-processing an already-booked
                # payment yields no newly_booked row -> no second DELIVER.
                self._spawn_deliver(task, p, ev, q)

        if task.task_type == "DELIVER" and (result.output or {}).get("success"):
            out = result.output or {}
            pref = str((task.input or {}).get("payment_ref", ""))
            store.record_delivery(task.opportunity_id, pref, dict(out))
            dirty = True
            ev.emit("DELIVERY_COMPLETE", task_id=task.task_id,
                    opportunity_id=task.opportunity_id, task_type="DELIVER",
                    actor=self.name, payment_ref=pref,
                    delivery_id=out.get("delivery_id", ""),
                    reference=out.get("reference", ""),
                    recipient=out.get("recipient", ""),
                    provider=out.get("provider", ""),
                    idempotent=bool(out.get("idempotent")))
            # DELIVERING and then ACTIVE - BOTH reached only from a confirmed
            # successful delivery (no speculative "start" transition).
            for tgt, why in (("DELIVERING", "delivery confirmed"),
                             ("ACTIVE", "delivery complete")):
                cur = (store.get(task.opportunity_id) or {}).get("state") \
                    or ostate.INITIAL
                if ostate.can_transition(cur, tgt):
                    try:
                        tr = store.transition(
                            task.opportunity_id, tgt, reason=f"DELIVER: {why}",
                            source="task", actor=self.name, task_id=task.task_id)
                        ev.emit("OPPORTUNITY_TRANSITIONED", task_id=task.task_id,
                                opportunity_id=task.opportunity_id,
                                task_type="DELIVER", actor=self.name,
                                **{"from": tr["previous_state"],
                                   "to": tr["next_state"], "reason": tr["reason"]})
                    except ostate.IllegalTransition:
                        pass

        target = self._success_target(task, result)
        if target:
            frm = (store.get(task.opportunity_id) or {}).get("state") or ostate.INITIAL
            if ostate.can_transition(frm, target):
                try:
                    tr = store.transition(
                        task.opportunity_id, target,
                        reason=f"{task.task_type} success", source="task",
                        actor=self.name, task_id=task.task_id)
                    dirty = True
                    ev.emit("OPPORTUNITY_TRANSITIONED", task_id=task.task_id,
                            opportunity_id=task.opportunity_id,
                            task_type=task.task_type, actor=self.name,
                            **{"from": tr["previous_state"],
                               "to": tr["next_state"], "reason": tr["reason"]})
                except ostate.IllegalTransition:
                    pass

        if dirty:
            store.save()

    def _spawn_deliver(self, check_task: ExecutionTask, payment: dict,
                       ev: EventLog, q: TaskQueue) -> None:
        oid = check_task.opportunity_id
        pref = str(payment.get("ledger_ref") or payment.get("reference") or "")
        if not pref:
            return
        before = {t.task_id for t in q.all()}
        t = q.create(
            oid, "DELIVER", priority=7,
            idempotency_key=f"deliver:{oid}:{pref}",
            input={"payment_ref": pref,
                   "amount": payment.get("amount", 0),
                   "currency": payment.get("currency", "EUR"),
                   "customer_ref": payment.get("customer_ref", ""),
                   "provider": payment.get("provider", "")})
        if t.task_id not in before:
            ev.emit("TASK_CREATED", task_id=t.task_id, opportunity_id=oid,
                    task_type="DELIVER", actor=self.name, depends_on=[],
                    priority=7, payment_ref=pref)

    def _resolve(self, q: TaskQueue, ev: EventLog) -> dict:
        res = q.resolve_dependencies()
        for tid in res.get("promoted", []):
            t = q.get(tid)
            ev.emit("TASK_READY", task_id=tid, opportunity_id=t.opportunity_id,
                    task_type=t.task_type, actor=self.name)
        for tid in res.get("blocked", []):
            t = q.get(tid)
            ev.emit("TASK_BLOCKED", task_id=tid, opportunity_id=t.opportunity_id,
                    task_type=t.task_type, actor=self.name,
                    approval_type=t.approval_type)
        for tid in res.get("failed", []):
            t = q.get(tid)
            ev.emit("TASK_FAILED", task_id=tid, opportunity_id=t.opportunity_id,
                    task_type=t.task_type, actor=self.name, error=t.error)
        return res

    def _backfill_lifecycle_events(self, q: TaskQueue, ev: EventLog) -> None:
        """Emit TASK_CREATED / TASK_CANCELLED for tasks that entered the
        queue (or were cancelled) outside `enqueue()` / `cancel_task()` -
        so the log is complete no matter how a task was added."""
        seen = {e.get("task_id") for e in ev.all() if e.get("task_id")}
        cancelled_evented = {e["task_id"] for e in ev.by_type("TASK_CANCELLED")}
        for t in q.all():
            if t.task_id not in seen:
                ev.emit("TASK_CREATED", task_id=t.task_id,
                        opportunity_id=t.opportunity_id, task_type=t.task_type,
                        actor="orchestrator", depends_on=list(t.depends_on),
                        priority=t.priority)
            if t.status == "CANCELLED" and t.task_id not in cancelled_evented:
                ev.emit("TASK_CANCELLED", task_id=t.task_id,
                        opportunity_id=t.opportunity_id, task_type=t.task_type,
                        actor="orchestrator", reason=t.error)


def run_worker(data_dir, *, max_ticks: int = 100, name: str = "worker-1",
               registry: AdapterRegistry | None = None) -> dict:
    """Drain the ready queue once. Returns a summary of what ran."""
    return Worker(data_dir, registry=registry, name=name).run(max_ticks=max_ticks)
